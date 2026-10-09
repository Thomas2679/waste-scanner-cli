import argparse
import sys

import boto3

from .checks import ALL_CHECKS
from .report import FORMATTERS
from .scanner import ScanConfig, scan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find forgotten AWS resources that are costing you money.")
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument("--regions", help="Comma-separated regions (default: all enabled regions)")
    parser.add_argument("--checks", help=f"Comma-separated checks (default: all). Options: {', '.join(ALL_CHECKS)}")
    parser.add_argument("--snapshot-age-days", type=int, default=90, help="Flag snapshots older than this (default 90)")
    parser.add_argument("--stopped-days", type=int, default=14, help="Flag instances stopped longer than this (default 14)")
    parser.add_argument("--idle-days", type=int, default=7, help="Look-back window for idle NAT gateways (default 7)")
    parser.add_argument("--format", choices=FORMATTERS, default="text")
    parser.add_argument("-o", "--output", help="Write report to this file instead of stdout")
    args = parser.parse_args(argv)

    checks = tuple(ALL_CHECKS)
    if args.checks:
        checks = tuple(c.strip() for c in args.checks.split(","))
        unknown = set(checks) - set(ALL_CHECKS)
        if unknown:
            parser.error(f"unknown checks: {', '.join(sorted(unknown))}")

    session = boto3.Session(profile_name=args.profile)
    regions = [r.strip() for r in args.regions.split(",")] if args.regions else None
    config = ScanConfig(snapshot_age_days=args.snapshot_age_days, stopped_days=args.stopped_days,
                        idle_days=args.idle_days, checks=checks)

    result = scan(session, regions=regions, config=config)
    report = FORMATTERS[args.format](result)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(report)
        print(f"Wrote {args.format} report to {args.output}: ${result.total_monthly_cost:,.2f}/month in waste",
              file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
