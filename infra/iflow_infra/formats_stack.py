"""
Statement-format CDN (Phase 11): signed parser bundles as static files, https://formats.<domain>/v1/.

    Admin Console "Publish" ──(service, your AWS SSO credentials)──▶ S3 (private) ◀── CloudFront (OAC) ◀── apps

  • The bucket is private (all public access blocked, TLS-only, encrypted, versioned); only CloudFront
    reads it, through Origin Access Control. Nothing can list it.
  • Read-only for the world, GET/HEAD only. Apps verify the Ed25519 signature in every bundle, so even
    a compromised bucket can't make an app accept a tampered or older format.
  • Browser builds (the web app) may fetch it cross-origin from the web origins in config.
  • `FormatsPublisher` is a least-privilege managed policy (upload under v1/ + invalidate) for whoever
    publishes; attach it to a permission set instead of full admin if you like.

CloudFront needs its certificate in us-east-1, so this stack is pinned there.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct

from .config import DeployConfig

PREFIX = "v1/"


class FormatsStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: DeployConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, termination_protection=True,
                         description="iFlow Core statement-format CDN (signed static bundles)", **kwargs)
        cdk.Tags.of(self).add("project", "iflow-core")
        cdk.Tags.of(self).add("component", "formats-cdn")
        host = f"formats.{cfg.domain}"

        self.bucket = s3.Bucket(
            self, "Bundles", block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED, versioned=True, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(365))])

        self.certificate = acm.Certificate(self, "Cert", domain_name=host,
                                           validation=acm.CertificateValidation.from_dns())
        headers = cloudfront.ResponseHeadersPolicy(
            self, "Headers", comment="iFlow formats: security headers + read-only CORS for the web app",
            cors_behavior=cloudfront.ResponseHeadersCorsBehavior(
                access_control_allow_credentials=False, access_control_allow_headers=["If-None-Match"],
                access_control_allow_methods=["GET", "HEAD"], access_control_allow_origins=list(cfg.web_origins),
                access_control_expose_headers=["ETag"], access_control_max_age=Duration.hours(1), origin_override=True),
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=False, override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True)))
        self.distribution = cloudfront.Distribution(
            self, "Cdn", comment="iFlow Core statement formats", domain_names=[host], certificate=self.certificate,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100, http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,          # honours the 5-minute max-age
                response_headers_policy=headers, compress=True))

        self.publisher_policy = iam.ManagedPolicy(
            self, "FormatsPublisher", managed_policy_name="IflowFormatsPublisher",
            description="Publish iFlow statement-format bundles (upload under v1/, invalidate the CDN)",
            statements=[
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[self.bucket.arn_for_objects(f"{PREFIX}*")]),
                iam.PolicyStatement(actions=["cloudfront:CreateInvalidation"], resources=[
                    self.format_arn(service="cloudfront", region="", resource="distribution",
                                    resource_name=self.distribution.distribution_id)]),
            ])

        url = f"https://{host}/{PREFIX}bundle.ifpb"
        cdk.CfnOutput(self, "FormatsUrl", value=url, description="Apps fetch this (core.config.DEFAULT_FORMATS_URL)")
        cdk.CfnOutput(self, "DnsFormatsCname", value=f"{host} CNAME {self.distribution.distribution_domain_name}",
                      description="Add at your DNS host")
        cdk.CfnOutput(self, "ServiceEnv", description="Environment for the admin service (Admin Console publishing)",
                      value=cdk.Fn.join("", ["RS_AI_FORMATS_BUCKET=", self.bucket.bucket_name,
                                             " RS_AI_FORMATS_DISTRIBUTION_ID=", self.distribution.distribution_id,
                                             f" RS_AI_FORMATS_PUBLIC_URL={url}"]))
