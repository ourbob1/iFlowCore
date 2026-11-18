#!/usr/bin/env python3
"""
Create the API's application secret in AWS Secrets Manager, once, before the first deploy:

    {"jwt_private_key": <Ed25519 PKCS#8 PEM>, "code_pepper": <64 hex chars>}

The values are generated in memory on this machine and sent straight to Secrets Manager (TLS). They
are never printed, written to disk or put in the CloudFormation template. Running it again leaves an
existing secret untouched, because replacing the signing key would sign every user out.

    AWS_PROFILE=iflow python scripts/create_app_secret.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from _common import load_config, session


def generate() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()).decode()
    return {"jwt_private_key": pem, "code_pepper": secrets.token_hex(32)}


CLIENTS_SECRET = "iflow-core/api/integration-clients"


def ensure_clients_secret(client, dry_run: bool) -> None:
    """The OAuth client secrets for platform connections (token broker). Starts as {}; add Uber's
    client id/secret in the AWS console once Uber approves iFlow (see infra/README.md)."""
    try:
        client.describe_secret(SecretId=CLIENTS_SECRET)
        print(f"✓ {CLIENTS_SECRET} already exists; left untouched.")
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    if dry_run:
        print(f"• {CLIENTS_SECRET} doesn't exist yet; run without --dry-run to create it (empty).")
        return
    client.create_secret(Name=CLIENTS_SECRET, SecretString="{}",
                         Description="iFlow Core API: OAuth client secrets for platform connections (token broker)",
                         Tags=[{"Key": "project", "Value": "iflow-core"}, {"Key": "component", "Value": "sync-api"}])
    print(f"✓ Created {CLIENTS_SECRET} (empty; fill it in once a platform issues iFlow a client).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="check access and whether the secret exists")
    args = parser.parse_args(argv)
    cfg = load_config()
    client = session(cfg).client("secretsmanager")
    ensure_clients_secret(client, args.dry_run)
    try:
        client.describe_secret(SecretId=cfg.app_secret_name)
        print(f"✓ {cfg.app_secret_name} already exists in {cfg.region}; left untouched.")
        return 0
    except client.exceptions.ResourceNotFoundException:
        pass
    if args.dry_run:
        print(f"• {cfg.app_secret_name} doesn't exist yet in {cfg.region}; run without --dry-run to create it.")
        return 0
    client.create_secret(
        Name=cfg.app_secret_name, SecretString=json.dumps(generate()),
        Description="iFlow Core API: JWT signing key (Ed25519) and email-code pepper. Generated once; "
                    "replacing the key signs every user out.",
        Tags=[{"Key": "project", "Value": "iflow-core"}, {"Key": "component", "Value": "sync-api"}])
    print(f"✓ Created {cfg.app_secret_name} in {cfg.region} (values generated here, never shown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
