#!/usr/bin/env python3
"""
Print the DNS records to add at your DNS host (DNS for the domain stays where it is).

Run it while `cdk deploy` is waiting on the certificate (the TLS certificate is only issued once its
validation record exists), and again afterwards for the API and email records:

    AWS_PROFILE=iflow python scripts/dns_records.py [--check]

--check looks each record up (needs `dig`) and marks it ✓ once it's visible.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass

from _common import FORMATS_STACK, STACK, load_config, session


@dataclass
class Record:
    purpose: str
    name: str
    type: str
    value: str
    required: bool = True


def gather(cfg, sess) -> tuple[list[Record], list[str]]:
    records: list[Record] = []
    notes: list[str] = []

    import boto3

    formats_host = f"formats.{cfg.domain}"
    for host, region in ((cfg.api_host, cfg.region), (formats_host, "us-east-1")):
        acm = boto3.Session(region_name=region).client("acm")
        cert_arn = None
        for page in acm.get_paginator("list_certificates").paginate(
                CertificateStatuses=["PENDING_VALIDATION", "ISSUED"]):
            for c in page["CertificateSummaryList"]:
                if c["DomainName"] == host:
                    cert_arn = c["CertificateArn"]
        if not cert_arn:
            notes.append(f"No certificate for {host} yet: start the deploy, then run this again.")
            continue
        cert = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]
        for opt in cert.get("DomainValidationOptions", []):
            rr = opt.get("ResourceRecord")
            if rr:
                records.append(Record(f"TLS certificate for {host} ({cert['Status'].lower().replace('_', ' ')})",
                                      rr["Name"].rstrip("."), rr["Type"], rr["Value"].rstrip(".")))

    try:
        cf_east = boto3.Session(region_name="us-east-1").client("cloudformation")
        outputs = cf_east.describe_stacks(StackName=FORMATS_STACK)["Stacks"][0].get("Outputs", [])
        cname = next((o["OutputValue"] for o in outputs if o["OutputKey"] == "DnsFormatsCname"), None)
        if cname:
            name, _, target = cname.split(" ", 2)          # "host CNAME target"
            records.append(Record("Statement-format CDN", name, "CNAME", target))
    except Exception:
        notes.append("The format CDN isn't created yet; its CNAME appears here once it is.")

    elb = sess.client("elbv2")
    alb_dns = None
    try:
        cf = sess.client("cloudformation")
        res = cf.describe_stack_resource(StackName=STACK, LogicalResourceId=_logical_id(cf, "AWS::ElasticLoadBalancingV2::LoadBalancer"))
        arn = res["StackResourceDetail"]["PhysicalResourceId"]
        alb_dns = elb.describe_load_balancers(LoadBalancerArns=[arn])["LoadBalancers"][0]["DNSName"]
    except Exception:                                   # stack not created yet
        pass
    if alb_dns:
        records.append(Record("API address", cfg.api_host, "CNAME", alb_dns))
    else:
        notes.append("The load balancer doesn't exist yet; its CNAME appears here once it does.")

    sesv2 = sess.client("sesv2")
    try:
        ident = sesv2.get_email_identity(EmailIdentity=cfg.domain)
        for token in ident.get("DkimAttributes", {}).get("Tokens", []):
            records.append(Record("Email signing (DKIM)", f"{token}._domainkey.{cfg.domain}", "CNAME",
                                  f"{token}.dkim.amazonses.com"))
        if not ident.get("VerifiedForSendingStatus"):
            notes.append("SES verifies the domain automatically once the DKIM records are visible (minutes to hours).")
    except sesv2.exceptions.NotFoundException:
        notes.append("The SES identity doesn't exist yet; DKIM records appear here once it does.")
    records.append(Record("Email bounce handling (MAIL FROM)", cfg.mail_from_domain, "MX",
                          f"10 feedback-smtp.{cfg.region}.amazonses.com"))
    records.append(Record("Email SPF for the MAIL FROM domain", cfg.mail_from_domain, "TXT",
                          '"v=spf1 include:amazonses.com ~all"'))
    records.append(Record("DMARC policy (merge with any existing _dmarc record)", f"_dmarc.{cfg.domain}", "TXT",
                          '"v=DMARC1; p=none; adkim=s; aspf=r"', required=False))
    return records, notes


def _logical_id(cf, resource_type: str) -> str:
    for page in cf.get_paginator("list_stack_resources").paginate(StackName=STACK):
        for r in page["StackResourceSummaries"]:
            if r["ResourceType"] == resource_type:
                return r["LogicalResourceId"]
    raise LookupError(resource_type)


def visible(record: Record) -> bool | None:
    if not shutil.which("dig"):
        return None
    out = subprocess.run(["dig", "+short", record.type, record.name], capture_output=True, text=True, timeout=10).stdout
    want = record.value.split()[-1].strip('"').rstrip(".").lower()
    return any(want in line.lower().rstrip(".") for line in out.splitlines())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="look each record up with dig")
    args = parser.parse_args(argv)
    cfg = load_config()
    records, notes = gather(cfg, session(cfg))
    print(f"DNS records for {cfg.domain} (add them at your DNS host; names are fully qualified):\n")
    for r in records:
        mark = ""
        if args.check:
            state = visible(r)
            mark = {True: "✓ ", False: "✗ ", None: "? "}[state]
        print(f"{mark}{r.purpose}{'' if r.required else ' (recommended)'}")
        print(f"    {r.name}  {r.type}  {r.value}\n")
    for n in notes:
        print(f"• {n}")
    print("\nIf the domain already has an SPF or DMARC record, merge rather than add a second one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
