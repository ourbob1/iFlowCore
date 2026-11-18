"""
Statement-format distribution (Phase 11): write the signed bundle as static files that apps download
from a CDN. No API route serves bundles to apps, so the service's attack surface doesn't grow; the
files are public and the apps trust only the vendor signature inside them.

Layout (under a prefix, default "v1/"):
    v1/bundle.ifpb                         the current bundle (short cache: 5 minutes)
    v1/latest.json                         {"issued_at", "sha256", "schemas", "bytes"} (short cache)
    v1/archive/<issued_at>-<sha12>.ifpb    every bundle ever published (immutable)

Targets:
    RS_AI_FORMATS_BUCKET (+ RS_AI_FORMATS_DISTRIBUTION_ID)   S3 behind CloudFront (infra/ FormatsStack).
                                                             Credentials come from the environment
                                                             (AWS_PROFILE / SSO), never from settings.
    RS_AI_FORMATS_DIR                                        a local folder (self-hosting, tests)
    RS_AI_FORMATS_PUBLIC_URL                                 where apps fetch it (shown in the console)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SHORT_CACHE = "public, max-age=300"
IMMUTABLE = "public, max-age=31536000, immutable"
CONTENT_TYPE = "text/plain; charset=utf-8"


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class Published:
    target: str
    public_url: str | None
    issued_at: str
    sha256: str
    schemas: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "public_url": self.public_url, "issued_at": self.issued_at,
                "sha256": self.sha256, "schemas": self.schemas, "bytes": self.bytes}


class Publisher(Protocol):
    target: str
    public_url: str | None

    def publish(self, bundle: str, issued_at: str, schemas: int) -> Published: ...


def _files(bundle: str, issued_at: str, schemas: int, prefix: str) -> tuple[list[tuple[str, bytes, str]], str]:
    data = bundle.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    stamp = issued_at.replace(":", "").replace("-", "")
    latest = json.dumps({"issued_at": issued_at, "sha256": digest, "schemas": schemas, "bytes": len(data)},
                        sort_keys=True).encode()
    return [(f"{prefix}archive/{stamp}-{digest[:12]}.ifpb", data, IMMUTABLE),   # archive first:
            (f"{prefix}bundle.ifpb", data, SHORT_CACHE),                       # never point at a
            (f"{prefix}latest.json", latest, SHORT_CACHE)], digest             # file that isn't there


class DirectoryPublisher:
    def __init__(self, root: str | Path, public_url: str | None = None, prefix: str = "v1/") -> None:
        self.root, self.prefix = Path(root), prefix
        self.target = f"folder {self.root}"
        self.public_url = public_url

    def publish(self, bundle: str, issued_at: str, schemas: int) -> Published:
        files, digest = _files(bundle, issued_at, schemas, self.prefix)
        try:
            for key, data, _ in files:
                path = self.root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)                       # atomic: readers never see half a file
        except OSError as exc:
            raise PublishError(f"Couldn't write to {self.root}: {exc.strerror or exc}") from None
        return Published(self.target, self.public_url, issued_at, digest, schemas, len(bundle.encode()))


class S3Publisher:
    def __init__(self, bucket: str, *, distribution_id: str | None = None, public_url: str | None = None,
                 prefix: str = "v1/", client: Any = None, cloudfront: Any = None, profile: str | None = None) -> None:
        self.bucket, self.prefix, self.distribution_id, self.profile = bucket, prefix, distribution_id, profile
        self.target = f"s3://{bucket}/{prefix}"
        self.public_url = public_url
        self._s3, self._cf = client, cloudfront

    def _clients(self) -> tuple[Any, Any]:
        if self._s3 is None:
            import boto3                                     # only needed with a bucket configured

            session = boto3.Session(profile_name=self.profile) if self.profile else boto3.Session()
            self._s3 = session.client("s3")
            self._cf = session.client("cloudfront") if self.distribution_id else None
        return self._s3, self._cf

    def publish(self, bundle: str, issued_at: str, schemas: int) -> Published:
        files, digest = _files(bundle, issued_at, schemas, self.prefix)
        s3, cf = self._clients()
        try:
            for key, data, cache in files:
                s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=CONTENT_TYPE, CacheControl=cache,
                              ChecksumAlgorithm="SHA256")
            if cf is not None and self.distribution_id:
                cf.create_invalidation(DistributionId=self.distribution_id, InvalidationBatch={
                    "Paths": {"Quantity": 2, "Items": [f"/{self.prefix}bundle.ifpb", f"/{self.prefix}latest.json"]},
                    "CallerReference": f"iflow-{digest[:16]}-{issued_at}"})
        except Exception as exc:                            # botocore errors: expired SSO session, denied…
            name = type(exc).__name__
            hint = (" Sign in again with `aws sso login`." if any(w in name for w in ("Token", "Credential", "SSO",
                                                                                   "ProfileNotFound")) else "")
            raise PublishError(f"Upload to the CDN bucket failed ({name}).{hint}") from None
        return Published(self.target, self.public_url, issued_at, digest, schemas, len(bundle.encode()))


def from_env(env: dict[str, str] | None = None) -> Publisher | None:
    env = dict(os.environ if env is None else env)
    public_url = env.get("RS_AI_FORMATS_PUBLIC_URL", "").strip() or None
    bucket = env.get("RS_AI_FORMATS_BUCKET", "").strip()
    if bucket:
        return S3Publisher(bucket, distribution_id=env.get("RS_AI_FORMATS_DISTRIBUTION_ID", "").strip() or None,
                           public_url=public_url)
    folder = env.get("RS_AI_FORMATS_DIR", "").strip()
    if folder:
        return DirectoryPublisher(folder, public_url)
    return None
