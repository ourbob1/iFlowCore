#!/usr/bin/env bash
# One-command deploy of the iFlow Core sync API (see infra/README.md for first-time setup).
#
#   AWS_PROFILE=iflow scripts/deploy.sh            # preflight → secret → cdk deploy → DNS → smoke test
#   AWS_PROFILE=iflow scripts/deploy.sh --diff     # show what would change, deploy nothing
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/node/bin:$PATH" CDK_DISABLE_VERSION_CHECK=1
PY=.venv/bin/python

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

say "Preflight"
[ -n "${AWS_PROFILE:-}" ] || fail "Set AWS_PROFILE to your SSO profile (aws configure sso), e.g. AWS_PROFILE=iflow"
command -v aws >/dev/null || fail "Install the AWS CLI v2 (see README)"
command -v cdk >/dev/null || fail "Install the CDK CLI: npm install -g aws-cdk"
docker info >/dev/null 2>&1 || fail "Docker isn't running (the API image is built locally)"
IDENTITY=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) \
  || fail "Not signed in: aws sso login --profile $AWS_PROFILE"
echo "✓ AWS identity: $IDENTITY"
IFLOW_DEPLOY=1 $PY -c "import sys; sys.path.insert(0, 'scripts'); from _common import load_config; \
c = load_config(for_deploy=True); print(f'✓ Config: {c.api_url} in {c.region}, alerts to {c.alert_email}')"

if [ "${1:-}" = "--diff" ]; then
  IFLOW_DEPLOY=1 cdk diff IflowCoreApi IflowCoreFormats
  exit 0
fi

say "Application secret"
$PY scripts/create_app_secret.py

say "Deploy (the first run waits until the certificate's DNS record exists: run scripts/dns_records.py in another terminal)"
IFLOW_DEPLOY=1 cdk deploy IflowCoreApi IflowCoreFormats --require-approval broadening --outputs-file cdk-outputs.json

say "Admin service settings for publishing statement formats (add to its environment)"
$PY -c "import json; o = json.load(open('cdk-outputs.json')).get('IflowCoreFormats', {}); print(o.get('ServiceEnv', '(see the IflowCoreFormats outputs)'))"

say "DNS records"
$PY scripts/dns_records.py --check

say "Smoke test"
$PY scripts/smoke_test.py || echo "(If api.<domain> doesn't resolve yet, add the CNAME above and re-run scripts/smoke_test.py.)"
