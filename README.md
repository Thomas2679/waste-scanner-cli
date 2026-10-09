# Waste Scanner

Finds forgotten AWS resources that are quietly costing money, and puts a dollar figure on each one.

```
AWS waste report for account 123456789012
Regions scanned: 1
Estimated waste: $398.23/month ($4,778.76/year)

== Unattached EBS volumes: 3 found, $278.00/month ==
  $   220.00  [us-east-1] Unattached 200 GB io1 volume vol-4aa5223742e4b35c4 (db-migration-tmp)
  $    50.00  [us-east-1] Unattached 500 GB gp2 volume vol-28568610bbd2161e7 (old-jenkins-data)
  $     8.00  [us-east-1] Unattached 100 GB gp3 volume vol-fc1c665f6c6f4f287
  -> Snapshot it if you might need the data, then delete the volume.

== gp2 volumes that could be gp3: 2 found, $40.00/month ==
  $    20.00  [us-east-1] 1000 GB gp2 volume vol-6b8e0949835d7c069 could be gp3
  $    20.00  [us-east-1] 1000 GB gp2 volume vol-a224b01a8e681fb3c could be gp3
  -> Modify the volume type to gp3 (no downtime). gp3 is ~20% cheaper with equal or better baseline performance.

== Idle NAT gateways: 1 found, $32.85/month ==
  $    32.85  [us-east-1] NAT gateway nat-37fbc45c8d6473ce9 sent no traffic in 7 days
  -> Delete the NAT gateway (and release its Elastic IP) if the subnets don't need outbound internet.

== Long-stopped EC2 instances: 1 found, $20.00/month ==
...
```

Read-only, no agents, no infrastructure. Run it from your laptop or CI with any credentials that have the [permissions below](#permissions).

## Install

```bash
pip install git+https://github.com/Thomas2679/waste-scanner-cli
```

## Usage

```bash
waste-scanner                                      # all enabled regions, text report
waste-scanner --regions us-east-1,eu-west-1
waste-scanner --profile prod
waste-scanner --format html -o report.html         # shareable HTML report
waste-scanner --format json                        # machine-readable, for scripts and CI
waste-scanner --checks idle_nat_gateway,unused_elastic_ip
waste-scanner --snapshot-age-days 180 --stopped-days 30 --idle-days 14
```

## What it checks

| Check | Flags | Typical cost |
|---|---|---|
| `unattached_ebs_volume` | EBS volumes not attached to any instance | $0.08–0.125/GB-mo + IOPS |
| `old_snapshot` | Snapshots older than 90 days (skips AMI-backed ones) | $0.05/GB-mo |
| `stopped_instance` | Instances stopped >14 days whose volumes still bill | volume cost |
| `idle_load_balancer` | ALB/NLB/GWLB with no registered targets, CLBs with no instances | ~$16–18/mo each |
| `idle_nat_gateway` | NAT gateways with zero outbound bytes in 7 days | ~$33/mo each |
| `unused_elastic_ip` | Elastic IPs not associated with anything | $3.65/mo each |
| `gp2_volume` | In-use gp2 volumes that would be cheaper as gp3 | ~20% of volume cost |

Costs are estimates from us-east-1 on-demand list prices (`src/waste_scanner/pricing.py`). Other regions are usually within 10–20%. Snapshot costs are an upper bound, because snapshots are incremental.

## Permissions

The scanner never modifies anything. It needs only the read-only actions in [`iam-policy.json`](iam-policy.json):

```
ec2:DescribeRegions            ec2:DescribeInstances
ec2:DescribeVolumes            ec2:DescribeNatGateways
ec2:DescribeAddresses          elasticloadbalancing:DescribeLoadBalancers
ec2:DescribeSnapshots          elasticloadbalancing:DescribeTargetGroups
ec2:DescribeImages             elasticloadbalancing:DescribeTargetHealth
cloudwatch:GetMetricStatistics
```

The AWS-managed `ReadOnlyAccess` policy also works. If a permission is missing, that check is skipped and reported at the end; the rest still run.

## Development

```bash
pip install -e '.[dev]'
pytest                                   # runs against moto, no AWS account needed
PYTHONPATH=src python examples/demo.py   # report from a simulated account full of waste
```

Adding a check: write a function in `src/waste_scanner/checks.py` that takes `(session, region, config)` and returns a list of `Finding`s, register it in `ALL_CHECKS`, give it a title in `report.py`, and add a moto test. Pull requests welcome.

## License

MIT
