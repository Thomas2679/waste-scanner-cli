from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from .checks import ALL_CHECKS
from .models import ScanResult


@dataclass
class ScanConfig:
    snapshot_age_days: int = 90
    stopped_days: int = 14
    idle_days: int = 7
    checks: tuple[str, ...] = tuple(ALL_CHECKS)


def enabled_regions(session) -> list[str]:
    ec2 = session.client("ec2", region_name=session.region_name or "us-east-1")
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def _run_check(session, name, region, config):
    try:
        return ALL_CHECKS[name](session, region, config), None
    except (ClientError, BotoCoreError) as exc:
        return [], f"{region}/{name}: {exc}"


def scan(session, regions: list[str] | None = None, config: ScanConfig | None = None,
         max_workers: int = 16) -> ScanResult:
    config = config or ScanConfig()
    account_id = session.client("sts").get_caller_identity()["Account"]
    regions = regions or enabled_regions(session)
    result = ScanResult(account_id=account_id, regions=regions)

    jobs = [(name, region) for region in regions for name in config.checks]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for findings, error in pool.map(lambda job: _run_check(session, job[0], job[1], config), jobs):
            result.findings.extend(findings)
            if error:
                result.errors.append(error)

    # A gp2 volume on a stopped instance is already counted in full by the stopped-instance finding.
    stopped_volume_ids = {v for f in result.findings if f.check == "stopped_instance"
                          for v in f.details["volume_ids"]}
    result.findings = [f for f in result.findings
                       if not (f.check == "gp2_volume" and f.resource_id in stopped_volume_ids)]
    result.findings.sort(key=lambda f: f.monthly_cost, reverse=True)
    return result
