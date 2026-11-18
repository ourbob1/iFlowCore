"""Shared helpers for the infra scripts: settings from cdk.json, the AWS session."""

from __future__ import annotations

import json
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INFRA))

from iflow_infra.config import DeployConfig  # noqa: E402

STACK = "IflowCoreApi"
FORMATS_STACK = "IflowCoreFormats"


def load_config(*, for_deploy: bool = False) -> DeployConfig:
    context = json.loads((INFRA / "cdk.json").read_text())["context"]
    return DeployConfig.from_context(context.get("iflow"), for_deploy=for_deploy)


def session(cfg: DeployConfig):
    import boto3

    return boto3.Session(region_name=cfg.region)       # credentials: AWS_PROFILE (SSO), never keys in files
