#!/usr/bin/env python3
"""
End-to-end deployment validation: run before (and after) packaging a release.

    python scripts/validate_deployment.py                 # everything that works without Flutter
    python scripts/validate_deployment.py --binary build/pyinstaller/linux/dist/iFlowCore/iFlowCore
    python scripts/validate_deployment.py --dist          # also verify dist/ checksums + manifests
    python scripts/validate_deployment.py --json          # machine-readable summary (CI)

Stages (each reported pass / fail / skip):

  config        pyproject packaging config for all 5 targets: desktop ships SQLCipher, mobile ships
                flet-secure-storage and never SQLCipher, permissions, no signing secrets
  source        the app's self-test from source: assets, DB init, migrations v1…latest, SQLCipher
                handshake (required on desktop), mobile fallback, production defaults, UI startup
  mobile:<p>    staged app self-test as android / ios with SQLCipher made unimportable
  stage:<t>     allowlisted stage per target: no user data, keys or tokens; build info is metadata only
  binary        (--binary) the packaged app's own `--selftest`, which must report frozen + encrypted
  dist          (--dist) every artifact in dist/ matches the SHA-256 in its manifest, scans clean

Exit status 0 only if nothing failed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_common import (  # noqa: E402
    APP_ENV_PREFIXES,
    BUILD_DIR,
    DESKTOP_TARGETS,
    DIST_DIR,
    MOBILE_TARGETS,
    ROOT,
    BuildError,
    require_clean,
    sha256,
    stage,
    validate_pyproject,
)


@dataclass
class Result:
    stage: str
    status: str
    detail: str
    seconds: float


def _selftest(cmd: list[str], env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="iflowcore-validate-") as tmp:
        out = Path(tmp) / "report.json"
        env = {**env, "HOME": tmp, "USERPROFILE": tmp, "XDG_DATA_HOME": tmp, "XDG_CONFIG_HOME": tmp,
               "APPDATA": tmp, "LOCALAPPDATA": tmp}
        proc = subprocess.run([*cmd, "--selftest", "--json", f"--out={out}"], env=env, cwd=tmp,
                              capture_output=True, text=True, timeout=900)
        if not out.exists():
            raise BuildError(f"self-test crashed (exit {proc.returncode}): {proc.stderr[-1500:]}")
        return json.loads(out.read_text(encoding="utf-8"))


def _summarize(report: dict) -> str:
    counts: dict[str, int] = {}
    for c in report["checks"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    failed = [f"{c['name']}: {c['detail'][:200]}" for c in report["checks"] if c["status"] == "fail"]
    text = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    if failed:
        raise BuildError(text + " · " + "; ".join(failed))
    return text


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(APP_ENV_PREFIXES)}
    env.pop("PYTHONPATH", None)
    return env


# --- stages --------------------------------------------------------------------------------------


def v_config() -> str:
    problems = validate_pyproject()
    if problems:
        raise BuildError("; ".join(problems))
    return f"{len(DESKTOP_TARGETS)} desktop + {len(MOBILE_TARGETS)} mobile targets configured"


def v_source() -> str:
    report = _selftest([sys.executable, str(ROOT / "main.py")], clean_env())
    enc = next(c for c in report["checks"] if c["name"] == "encryption")
    if sys.platform in ("linux", "darwin", "win32") and enc["status"] != "pass":
        raise BuildError(f"SQLCipher handshake didn't pass on this desktop: {enc['detail']}")
    return _summarize(report) + f" · {enc['detail'][:60]}"


def v_mobile(platform: str) -> str:
    from package_mobile import FALLBACK_CHECKS, Plan, fallback_selftest

    st = stage(platform, channel="production", out=BUILD_DIR / "validate" / platform)
    plan = Plan(platform=platform, fmt="aab" if platform == "android" else "ipa", channel="production",
                version="", python="")
    report = fallback_selftest(plan, st.path)
    return f"{len(FALLBACK_CHECKS)} checks without SQLCipher · " + _summarize(report)


def v_stage(target: str) -> str:
    st = stage(target, channel="production", out=BUILD_DIR / "validate" / target)
    require_clean(st.path)
    info = json.loads((st.path / "_build_info.json").read_text(encoding="utf-8"))
    if set(info) - {"channel", "version", "target", "built_at", "backend", "app"}:
        raise BuildError(f"build info carries unexpected keys: {sorted(info)}")
    leaked = [f for f in st.files if f.split("/")[0] in ("tests", "services", "scripts", "venv", "api", "docs")]
    if leaked:
        raise BuildError(f"stage includes non-app files: {leaked[:5]}")
    return f"{len(st.files)} files, secret scan clean"


def v_binary(binary: Path) -> str:
    if not binary.exists():
        raise BuildError(f"{binary} not found")
    report = _selftest([str(binary)], clean_env())
    if not report.get("frozen"):
        raise BuildError("the binary's self-test didn't run frozen (is this the packaged app?)")
    status = {c["name"]: c["status"] for c in report["checks"]}
    runtime = next(c["detail"] for c in report["checks"] if c["name"] == "runtime")
    if status.get("encryption") != "pass":
        raise BuildError("SQLCipher isn't working inside the packaged app")
    return _summarize(report) + f" · {runtime}"


def v_dist() -> str:
    manifests = sorted(DIST_DIR.glob("*-manifest.json"))
    if not manifests:
        raise BuildError("no manifests in dist/ (build something first)")
    checked = 0
    for m in manifests:
        data = json.loads(m.read_text(encoding="utf-8"))
        for a in data["artifacts"]:
            path = DIST_DIR / a["file"]
            if not path.exists():
                raise BuildError(f"{a['file']} listed in {m.name} is missing")
            if sha256(path) != a["sha256"]:
                raise BuildError(f"{a['file']} doesn't match its manifest checksum")
            require_clean(path, binary=True)
            checked += 1
    return f"{checked} artifact(s) in {len(manifests)} manifest(s): checksums match, scans clean"


def run(args: argparse.Namespace) -> list[Result]:
    stages: list[tuple[str, object]] = [("config", v_config), ("source", v_source)]
    stages += [(f"mobile:{p}", lambda p=p: v_mobile(p)) for p in MOBILE_TARGETS]
    stages += [(f"stage:{t}", lambda t=t: v_stage(t)) for t in DESKTOP_TARGETS + MOBILE_TARGETS]
    if args.binary:
        stages.append(("binary", lambda: v_binary(args.binary.resolve())))
    if args.dist:
        stages.append(("dist", v_dist))
    results = []
    for name, fn in stages:
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()):                # build helpers log to stdout
                detail, status = fn(), "pass"                              # type: ignore[operator]
        except (BuildError, subprocess.TimeoutExpired, OSError, ValueError, StopIteration) as exc:
            detail, status = str(exc), "fail"
        results.append(Result(name, status, detail, round(time.monotonic() - started, 1)))
        if not args.json:
            print(f"  [{status.upper():4}] {name:<15} {detail}  ({results[-1].seconds}s)", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--binary", type=Path, help="packaged app executable to self-test")
    ap.add_argument("--dist", action="store_true", help="verify dist/ artifacts against their manifests")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.json:
        print("iFlow Core deployment validation")
    results = run(args)
    ok = all(r.status != "fail" for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "results": [asdict(r) for r in results]}, indent=2))
    else:
        print("RESULT: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
