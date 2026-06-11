#!/usr/bin/env python3
"""
One-time (per environment) setup of the SNS APNs platform application used by
Soul Pings + all iOS push, and storage of its ARN in SSM so `serverless deploy`
auto-wires `SNS_IOS_APP_ARN` for that stage.

WHY THIS SCRIPT (and not CloudFormation): SNS platform applications hold your
APNs signing key and have no native CloudFormation resource. We create them
once here, store the ARN in SSM Parameter Store, and serverless.yml resolves
`${ssm:/mirror-collective/<stage>/sns/ios-app-arn}` at deploy time.

ENVIRONMENT MATCHING (critical — mismatch = silent non-delivery):
    stage 'staging'  → APNS_SANDBOX  ↔ Debug builds (aps-environment=development)
    stage 'prod*'    → APNS          ↔ TestFlight/App Store (aps-environment=production)

Uses token-based APNs auth (.p8 signing key) — the modern, non-expiring option.

USAGE:
    export APNS_KEY_ID=ABC123DEFG
    export APNS_TEAM_ID=TEAMID1234
    export APNS_BUNDLE_ID=com.themirrorcollective.mirror
    python scripts/setup_apns_platform_app.py --stage staging --p8 ./AuthKey_ABC123DEFG.p8
    python scripts/setup_apns_platform_app.py --stage prod    --p8 ./AuthKey_ABC123DEFG.p8

Requires AWS credentials with sns:CreatePlatformApplication + ssm:PutParameter.
Re-running updates the platform app's credentials and the SSM value in place.
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

SSM_PREFIX = "/mirror-collective"


def _platform_for_stage(stage: str, override: str | None) -> str:
    """Map a deploy stage to its APNs SNS platform type.

    Anything starting with 'prod' uses production APNs; everything else uses
    the sandbox, so dev/Debug builds (which register sandbox tokens) match.
    """
    if override:
        return override
    return "APNS" if stage.lower().startswith("prod") else "APNS_SANDBOX"


def _require(value: str | None, name: str) -> str:
    if not value:
        sys.exit(f"ERROR: {name} is required (pass the flag or set the env var).")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        required=True,
        help="Deploy stage (e.g. staging, prod, production-v2).",
    )
    parser.add_argument(
        "--p8",
        help="Path to the APNs .p8 signing key. "
        "Alternatively set APNS_SIGNING_KEY with the key contents.",
    )
    parser.add_argument("--key-id", default=os.getenv("APNS_KEY_ID"))
    parser.add_argument("--team-id", default=os.getenv("APNS_TEAM_ID"))
    parser.add_argument("--bundle-id", default=os.getenv("APNS_BUNDLE_ID"))
    parser.add_argument(
        "--platform",
        choices=["APNS", "APNS_SANDBOX"],
        help="Override the stage→platform mapping.",
    )
    parser.add_argument("--region", default=os.getenv("AWS_SNS_REGION", "us-east-1"))
    args = parser.parse_args()

    key_id = _require(args.key_id, "APNS key id (--key-id / APNS_KEY_ID)")
    team_id = _require(args.team_id, "APNS team id (--team-id / APNS_TEAM_ID)")
    bundle_id = _require(
        args.bundle_id, "APNS bundle id (--bundle-id / APNS_BUNDLE_ID)"
    )

    signing_key = os.getenv("APNS_SIGNING_KEY")
    if not signing_key:
        if not args.p8:
            sys.exit("ERROR: provide --p8 <path> or set APNS_SIGNING_KEY.")
        with open(args.p8, "r", encoding="utf-8") as fh:
            signing_key = fh.read()

    platform = _platform_for_stage(args.stage, args.platform)
    name = f"mirror-collective-ios-{args.stage}"

    print(f"Stage:    {args.stage}")
    print(f"Platform: {platform}")
    print(f"Name:     {name}")
    print(f"Region:   {args.region}")

    sns = boto3.client("sns", region_name=args.region)
    response = sns.create_platform_application(
        Name=name,
        Platform=platform,
        Attributes={
            "PlatformCredential": signing_key,  # .p8 contents
            "PlatformPrincipal": key_id,  # signing Key ID
            "ApplePlatformTeamID": team_id,
            "ApplePlatformBundleID": bundle_id,
        },
    )
    arn = response["PlatformApplicationArn"]
    print(f"\nPlatformApplicationArn: {arn}")

    param_name = f"{SSM_PREFIX}/{args.stage}/sns/ios-app-arn"
    ssm = boto3.client("ssm", region_name=args.region)
    ssm.put_parameter(
        Name=param_name,
        Value=arn,
        Type="String",
        Overwrite=True,
        Description=f"SNS APNs platform application ARN ({platform}) for {args.stage}",
    )
    print(f"Stored in SSM: {param_name}")
    print(
        "\nDone. Next `serverless deploy --stage "
        f"{args.stage}` will wire SNS_IOS_APP_ARN automatically."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
