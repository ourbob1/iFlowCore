#!/usr/bin/env python3
"""CDK entry point: `cdk synth` / `cdk deploy` (run from infra/, see README.md)."""

import os

import aws_cdk as cdk

from iflow_infra.api_stack import ApiStack
from iflow_infra.config import DeployConfig
from iflow_infra.formats_stack import FormatsStack

app = cdk.App()
cfg = DeployConfig.from_context(app.node.try_get_context("iflow"), for_deploy=os.environ.get("IFLOW_DEPLOY") == "1")
account = cfg.account or os.environ.get("CDK_DEFAULT_ACCOUNT")
ApiStack(app, "IflowCoreApi", cfg=cfg, env=cdk.Environment(account=account, region=cfg.region))
# CloudFront certificates must live in us-east-1, whatever region the API uses
FormatsStack(app, "IflowCoreFormats", cfg=cfg, env=cdk.Environment(account=account, region="us-east-1"))
app.synth()
