"""Seed a fake AWS account with typical waste (via moto) and print a report.

    python examples/demo.py --format html -o sample-report.html
"""
import os
import sys

os.environ.update(AWS_ACCESS_KEY_ID="demo", AWS_SECRET_ACCESS_KEY="demo", AWS_DEFAULT_REGION="us-east-1")
os.environ.pop("AWS_PROFILE", None)

import boto3
from moto import mock_aws

from waste_scanner.report import FORMATTERS
from waste_scanner.scanner import ScanConfig, scan


def tag(name):
    return [{"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": name}]}]


def seed(session):
    ec2 = session.client("ec2")
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=500, VolumeType="gp2", TagSpecifications=tag("old-jenkins-data"))
    ec2.create_volume(AvailabilityZone="us-east-1b", Size=200, VolumeType="io1", Iops=3000, TagSpecifications=tag("db-migration-tmp"))
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    for _ in range(3):
        ec2.allocate_address(Domain="vpc")

    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=image, MinCount=2, MaxCount=2, InstanceType="m5.large",
                      BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 1000, "VolumeType": "gp2"}}])
    stopped = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="c5.xlarge",
                                BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 250, "VolumeType": "gp3"}}],
                                TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "staging-worker"}]}])
    ec2.stop_instances(InstanceIds=[stopped["Instances"][0]["InstanceId"]])

    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=az)["Subnet"]["SubnetId"]
               for i, az in enumerate(["us-east-1a", "us-east-1b"])]
    session.client("elbv2").create_load_balancer(Name="legacy-api-alb", Subnets=subnets)
    nat_eip = ec2.allocate_address(Domain="vpc")
    ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=nat_eip["AllocationId"])


def main():
    fmt = sys.argv[sys.argv.index("--format") + 1] if "--format" in sys.argv else "text"
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else None
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        seed(session)
        # moto pre-seeds hundreds of snapshots for its built-in AMIs; skip that check in the demo.
        checks = tuple(c for c in ScanConfig().checks if c != "old_snapshot")
        result = scan(session, regions=["us-east-1"], config=ScanConfig(stopped_days=0, checks=checks))
    report = FORMATTERS[fmt](result)
    if out:
        with open(out, "w") as fh:
            fh.write(report)
    else:
        print(report)


if __name__ == "__main__":
    main()
