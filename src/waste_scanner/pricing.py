"""On-demand list prices (USD, us-east-1). Other regions are usually within ~10-20%.

These are estimates for flagging waste, not billing-accurate numbers.
"""

HOURS_PER_MONTH = 730

# EBS storage, per GB-month
EBS_GB_MONTH = {
    "gp2": 0.10,
    "gp3": 0.08,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,
}
EBS_PIOPS_MONTH = 0.065          # io1/io2 provisioned IOPS
GP3_FREE_IOPS = 3000
GP3_IOPS_MONTH = 0.005           # per IOPS above 3000
GP3_FREE_THROUGHPUT = 125        # MB/s
GP3_THROUGHPUT_MONTH = 0.04      # per MB/s above 125

SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IPV4_MONTH = 0.005 * HOURS_PER_MONTH
ALB_MONTH = 0.0225 * HOURS_PER_MONTH
NLB_MONTH = 0.0225 * HOURS_PER_MONTH
GWLB_MONTH = 0.0125 * HOURS_PER_MONTH
CLB_MONTH = 0.025 * HOURS_PER_MONTH
NAT_GATEWAY_MONTH = 0.045 * HOURS_PER_MONTH


def ebs_volume_monthly(volume_type: str, size_gb: int, iops: int | None = None,
                       throughput: int | None = None) -> float:
    cost = size_gb * EBS_GB_MONTH.get(volume_type, EBS_GB_MONTH["gp2"])
    if volume_type in ("io1", "io2") and iops:
        cost += iops * EBS_PIOPS_MONTH
    if volume_type == "gp3":
        cost += max(0, (iops or GP3_FREE_IOPS) - GP3_FREE_IOPS) * GP3_IOPS_MONTH
        cost += max(0, (throughput or GP3_FREE_THROUGHPUT) - GP3_FREE_THROUGHPUT) * GP3_THROUGHPUT_MONTH
    return round(cost, 2)


def gp2_to_gp3_savings(size_gb: int) -> float:
    """Monthly savings from migrating gp2 to gp3 while matching gp2's baseline IOPS."""
    gp2_iops = min(max(100, 3 * size_gb), 16000)
    gp3_cost = ebs_volume_monthly("gp3", size_gb, iops=max(gp2_iops, GP3_FREE_IOPS))
    return round(ebs_volume_monthly("gp2", size_gb) - gp3_cost, 2)
