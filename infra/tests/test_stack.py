"""Phase 10.4: the AWS stack, checked by synthesizing it (no AWS account or Docker needed).

    cd infra && .venv/bin/pytest -q          (Node.js must be on PATH for the CDK)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INFRA))

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

from iflow_infra.api_stack import ApiStack  # noqa: E402
from iflow_infra.config import ConfigError, DeployConfig  # noqa: E402

CONTEXT = json.loads((INFRA / "cdk.json").read_text())["context"]["iflow"]


def synth(**overrides) -> Template:
    cfg = DeployConfig.from_context({**CONTEXT, "alert_email": "ops@example.org", **overrides})
    app = cdk.App()
    stack = ApiStack(app, "Test", cfg=cfg, env=cdk.Environment(account="123456789012", region=cfg.region))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def t() -> Template:
    return synth()


def resources(t: Template, kind: str) -> dict:
    return t.find_resources(kind)


# --- configuration ---------------------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self):
        cfg = DeployConfig.from_context(CONTEXT)
        assert cfg.api_url == "https://api.iflowcore.com" and cfg.region == "us-east-1"
        assert cfg.mail_from == "iFlow Core <no-reply@iflowcore.com>" and cfg.web_origins == ("https://iflowcore.com",)

    def test_placeholder_alert_email_blocks_a_deploy(self):
        with pytest.raises(ConfigError, match="alert_email"):
            DeployConfig.from_context(CONTEXT, for_deploy=True)

    @pytest.mark.parametrize("key,value,msg", [
        ("region", "mars-1", "region"), ("domain", "not a domain", "domain"), ("cpu", 256, None),
        ("memory_mib", 4096, "Fargate"), ("web_origins", ["http://iflowcore.com"], "https"),
        ("google_client_ids", ["nope"], "Google"), ("db_instance_class", "db.r7.huge", "db_instance_class"),
        ("min_tasks", 3, "max_tasks"), ("account", "12345", "12-digit"), ("log_retention_days", 31, "retention"),
    ])
    def test_rejected(self, key, value, msg):
        overrides = {key: value}
        if key == "min_tasks":
            overrides["max_tasks"] = 2
        if msg is None:
            DeployConfig.from_context({**CONTEXT, **overrides})
            return
        with pytest.raises(ConfigError, match=msg):
            DeployConfig.from_context({**CONTEXT, **overrides})


# --- the stack ---------------------------------------------------------------------------------------------


class TestNetwork:
    def test_no_nat_gateway(self, t):
        t.resource_count_is("AWS::EC2::NatGateway", 0)

    def test_database_accepts_only_the_api(self, t):
        rules = resources(t, "AWS::EC2::SecurityGroupIngress")
        db_rules = [r["Properties"] for r in rules.values() if r["Properties"].get("FromPort") == 5432]
        assert len(db_rules) == 1 and "SourceSecurityGroupId" in db_rules[0]
        groups = resources(t, "AWS::EC2::SecurityGroup")
        for group in groups.values():
            for rule in group["Properties"].get("SecurityGroupIngress", []):
                if rule.get("CidrIp") == "0.0.0.0/0":
                    assert rule["FromPort"] in (80, 443), rule           # only the load balancer is public

    def test_api_tasks_accept_only_the_load_balancer(self, t):
        rules = [r["Properties"] for r in resources(t, "AWS::EC2::SecurityGroupIngress").values()
                 if r["Properties"].get("FromPort") == 8080]
        assert rules and all("SourceSecurityGroupId" in r for r in rules)


class TestDatabase:
    def test_encrypted_private_protected(self, t):
        t.has_resource("AWS::RDS::DBInstance", {
            "Properties": Match.object_like({"StorageEncrypted": True, "PubliclyAccessible": False,
                                             "DeletionProtection": True, "Engine": "postgres", "EngineVersion": "16",
                                             "DBInstanceClass": "db.t4g.micro", "MultiAZ": False,
                                             "BackupRetentionPeriod": 7, "StorageType": "gp3"}),
            "DeletionPolicy": "Snapshot", "UpdateReplacePolicy": "Snapshot"})

    def test_tls_only(self, t):
        t.has_resource_properties("AWS::RDS::DBParameterGroup", {
            "Parameters": Match.object_like({"rds.force_ssl": "1"})})

    def test_password_is_generated_not_written(self, t):
        text = json.dumps(t.to_json())
        assert "MasterUserPassword" in text and "{{resolve:secretsmanager:" in text
        t.has_resource_properties("AWS::SecretsManager::Secret", {
            "Name": "iflow-core/api/db", "GenerateSecretString": Match.object_like({"GenerateStringKey": "password"})})

    def test_isolated_subnets(self, t):
        subnet_groups = resources(t, "AWS::RDS::DBSubnetGroup")
        assert len(subnet_groups) == 1
        routes = resources(t, "AWS::EC2::Route")
        # only public subnets have an internet route; the db subnets have none
        assert all("GatewayId" in r["Properties"] for r in routes.values())
        assert len(routes) == 2


class TestService:
    def container(self, t, name):
        for td in resources(t, "AWS::ECS::TaskDefinition").values():
            for c in td["Properties"]["ContainerDefinitions"]:
                if c["Name"] == name:
                    return c
        raise AssertionError(name)

    def test_secrets_are_references_not_values(self, t):
        api = self.container(t, "api")
        names = {s["Name"] for s in api["Secrets"]}
        assert names == {"IFLOW_API_DATABASE_SECRET", "IFLOW_API_JWT_PRIVATE_KEY", "IFLOW_API_CODE_PEPPER",
                         "IFLOW_API_INTEGRATION_CLIENTS"}
        env = {e["Name"]: e["Value"] for e in api["Environment"]}
        assert env["IFLOW_API_ENV"] == "production" and env["IFLOW_API_MAIL_BACKEND"] == "ses"
        assert env["IFLOW_API_TRUST_PROXY"] == "true" and env["IFLOW_API_CORS_ORIGINS"] == "https://iflowcore.com"
        assert not any(k in env for k in names)
        text = json.dumps(t.to_json())
        assert "PRIVATE KEY" not in text and "code_pepper\":\"" not in text

    def test_migrations_use_the_same_image_and_settings(self, t):
        api, migrate = self.container(t, "api"), self.container(t, "migrate")
        assert migrate["Command"] == ["alembic", "upgrade", "head"]
        assert migrate["Image"] == api["Image"] and migrate["Secrets"] == api["Secrets"]

    def test_fargate_public_ip_and_rollback(self, t):
        t.has_resource_properties("AWS::ECS::Service", {
            "LaunchType": "FARGATE", "DesiredCount": 1,
            "DeploymentConfiguration": Match.object_like({
                "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}, "MinimumHealthyPercent": 100}),
            "NetworkConfiguration": {"AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "ENABLED"})}})
        t.has_resource_properties("AWS::ApplicationAutoScaling::ScalableTarget", {"MinCapacity": 1, "MaxCapacity": 3})

    def test_email_sender_is_pinned(self, t):
        text = json.dumps(resources(t, "AWS::IAM::Policy"))
        assert "ses:SendEmail" in text and "no-reply@iflowcore.com" in text
        t.has_resource_properties("AWS::SES::EmailIdentity", {
            "EmailIdentity": "iflowcore.com", "MailFromAttributes": Match.object_like({"MailFromDomain": "mail.iflowcore.com"})})

    def test_migration_trigger_runs_before_the_service(self, t):
        triggers = resources(t, "Custom::Trigger")
        assert len(triggers) == 1
        (tid, trigger), = triggers.items()
        service_id = next(iter(resources(t, "AWS::ECS::Service")))
        assert tid in resources(t, "AWS::ECS::Service")[service_id].get("DependsOn", [])
        db_id = next(iter(resources(t, "AWS::RDS::DBInstance")))
        assert db_id in trigger.get("DependsOn", [])
        fn = [f for f in resources(t, "AWS::Lambda::Function").values()
              if "IMAGE" in (f["Properties"].get("Environment") or {}).get("Variables", {})]
        assert len(fn) == 1 and fn[0]["Properties"]["Timeout"] == 840

    def test_logs_are_retained_30_days(self, t):
        groups = [g["Properties"] for g in resources(t, "AWS::Logs::LogGroup").values()]
        assert {g["LogGroupName"] for g in groups} >= {"/iflow-core/api", "/iflow-core/api/migrate"}
        assert all(g["RetentionInDays"] == 30 for g in groups)


class TestEdge:
    def test_https_only(self, t):
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {
            "Port": 443, "Protocol": "HTTPS", "SslPolicy": Match.string_like_regexp("TLS")})
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {
            "Port": 80, "DefaultActions": [Match.object_like({"Type": "redirect",
                                                              "RedirectConfig": Match.object_like({"Protocol": "HTTPS"})})]})
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {
            "LoadBalancerAttributes": Match.array_with([{"Key": "routing.http.drop_invalid_header_fields.enabled",
                                                         "Value": "true"}])})
        t.has_resource_properties("AWS::CertificateManager::Certificate", {
            "DomainName": "api.iflowcore.com", "ValidationMethod": "DNS"})

    def test_health_check_is_shallow(self, t):
        t.has_resource_properties("AWS::ElasticLoadBalancingV2::TargetGroup", {
            "HealthCheckPath": "/healthz", "Port": 8080})

    def test_waf(self, t):
        (acl,) = resources(t, "AWS::WAFv2::WebACL").values()
        rules = {r["Name"]: r for r in acl["Properties"]["Rules"]}
        assert rules["rate-auth"]["Statement"]["RateBasedStatement"]["Limit"] == 100
        assert rules["rate-all"]["Statement"]["RateBasedStatement"]["Limit"] == 2000
        assert {"AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet",
                "AWSManagedRulesAmazonIpReputationList"} <= set(rules)
        # samples would store Authorization headers
        configs = [acl["Properties"]["VisibilityConfig"]] + [r["VisibilityConfig"] for r in rules.values()]
        assert all(c["SampledRequestsEnabled"] is False for c in configs)
        t.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)


class TestOperations:
    def test_alarms_email_and_budget(self, t):
        alarms = resources(t, "AWS::CloudWatch::Alarm")
        assert len(alarms) == 5 and all(a["Properties"]["AlarmActions"] for a in alarms.values())
        t.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.org"})
        t.has_resource_properties("AWS::Budgets::Budget", {
            "Budget": Match.object_like({"BudgetLimit": {"Amount": 80, "Unit": "USD"}, "TimeUnit": "MONTHLY"})})

    def test_outputs_for_dns(self, t):
        outputs = t.to_json()["Outputs"]
        assert outputs["ApiUrl"]["Value"] == "https://api.iflowcore.com"
        assert {"DnsApiCname", "DnsDkim1", "DnsDkim2", "DnsDkim3", "DnsMailFromMx", "DnsMailFromSpf"} <= set(outputs)

    def test_bigger_settings(self):
        t = synth(db_multi_az=True, db_instance_class="t4g.small", cpu=512, memory_mib=1024, min_tasks=2,
                  max_tasks=2)
        t.has_resource_properties("AWS::RDS::DBInstance", {"MultiAZ": True, "DBInstanceClass": "db.t4g.small"})
        t.resource_count_is("AWS::ApplicationAutoScaling::ScalableTarget", 0)        # fixed size: no scaling
        t.has_resource_properties("AWS::ECS::TaskDefinition", {"Cpu": "512", "Memory": "1024"})


# --- the migration trigger (Lambda) ------------------------------------------------------------------------


def load_trigger(monkeypatch, fake_ecs):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for k, v in {"CLUSTER": "c", "TASK_DEFINITION": "td", "SUBNETS": '["s-1","s-2"]', "SECURITY_GROUP": "sg",
                 "IMAGE": "img:1"}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("trigger", INFRA / "iflow_infra/migrate_trigger/index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ecs, mod.POLL_S = fake_ecs, 0
    return mod


class FakeEcs:
    def __init__(self, exit_code=0, failures=None):
        self.exit_code, self.failures, self.polls, self.run_args = exit_code, failures, 0, None

    def run_task(self, **kw):
        self.run_args = kw
        return {"failures": self.failures} if self.failures else {"tasks": [{"taskArn": "arn:task/abc"}], "failures": []}

    def describe_tasks(self, **kw):
        self.polls += 1
        status = "STOPPED" if self.polls >= 2 else "RUNNING"
        return {"tasks": [{"lastStatus": status, "containers": [{"name": "migrate", "exitCode": self.exit_code}]}]}


class TestMigrationTrigger:
    def test_success(self, monkeypatch):
        ecs = FakeEcs()
        assert load_trigger(monkeypatch, ecs).handler({}, None)["status"] == "migrated"
        net = ecs.run_args["networkConfiguration"]["awsvpcConfiguration"]
        assert net["subnets"] == ["s-1", "s-2"] and net["securityGroups"] == ["sg"]

    def test_failed_migration_fails_the_deploy(self, monkeypatch):
        with pytest.raises(RuntimeError, match="exit code 1"):
            load_trigger(monkeypatch, FakeEcs(exit_code=1)).handler({}, None)

    def test_task_that_cannot_start(self, monkeypatch):
        with pytest.raises(RuntimeError, match="Couldn't start"):
            load_trigger(monkeypatch, FakeEcs(failures=[{"reason": "RESOURCE:ENI"}])).handler({}, None)


# --- the statement-format CDN ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def formats() -> Template:
    from iflow_infra.formats_stack import FormatsStack

    cfg = DeployConfig.from_context({**CONTEXT, "alert_email": "ops@example.org"})
    app = cdk.App()
    return Template.from_stack(FormatsStack(app, "Formats", cfg=cfg,
                                            env=cdk.Environment(account="123456789012", region="us-east-1")))


class TestFormatsCdn:
    def test_bucket_is_private_encrypted_tls_only(self, formats):
        formats.has_resource("AWS::S3::Bucket", {
            "Properties": Match.object_like({
                "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                                   "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
                "VersioningConfiguration": {"Status": "Enabled"},
                "BucketEncryption": Match.any_value()}),
            "DeletionPolicy": "Retain"})
        policy = json.dumps(formats.find_resources("AWS::S3::BucketPolicy"))
        assert '"aws:SecureTransport": "false"' in policy                 # TLS-only
        assert "cloudfront.amazonaws.com" in policy and "s3:GetObject" in policy
        assert "s3:ListBucket" not in policy and "s3:PutObject" not in policy   # CDN reads, nobody lists/writes

    def test_cdn_is_read_only_https(self, formats):
        formats.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({
            "Aliases": ["formats.iflowcore.com"],
            "ViewerCertificate": Match.object_like({"MinimumProtocolVersion": "TLSv1.2_2021"}),
            "DefaultCacheBehavior": Match.object_like({"AllowedMethods": ["GET", "HEAD"],
                                                       "ViewerProtocolPolicy": "redirect-to-https"})})})
        formats.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
        formats.has_resource_properties("AWS::CertificateManager::Certificate", {
            "DomainName": "formats.iflowcore.com", "ValidationMethod": "DNS"})

    def test_cors_only_for_the_web_app(self, formats):
        (policy,) = formats.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()
        cors = policy["Properties"]["ResponseHeadersPolicyConfig"]["CorsConfig"]
        assert cors["AccessControlAllowOrigins"]["Items"] == ["https://iflowcore.com"]
        assert cors["AccessControlAllowMethods"]["Items"] == ["GET", "HEAD"]
        assert cors["AccessControlAllowCredentials"] is False

    def test_publisher_policy_is_least_privilege(self, formats):
        (policy,) = formats.find_resources("AWS::IAM::ManagedPolicy").values()
        actions = sorted(a for st in policy["Properties"]["PolicyDocument"]["Statement"]
                         for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"]))
        assert actions == ["cloudfront:CreateInvalidation", "s3:PutObject"]
        assert "/v1/*" in json.dumps(policy)

    def test_outputs(self, formats):
        outputs = formats.to_json()["Outputs"]
        assert outputs["FormatsUrl"]["Value"] == "https://formats.iflowcore.com/v1/bundle.ifpb"
        assert {"DnsFormatsCname", "ServiceEnv"} <= set(outputs)

    def test_the_app_default_matches(self, formats):
        sys.path.insert(0, str(INFRA.parent))
        from core.config import DEFAULT_FORMATS_URL

        assert formats.to_json()["Outputs"]["FormatsUrl"]["Value"] == DEFAULT_FORMATS_URL
