import boto3
import pytest
from moto import mock_aws

from waste_scanner import pricing
from waste_scanner.report import to_html, to_json, to_text
from waste_scanner.scanner import ScanConfig, scan

REGION = "us-east-1"
CONFIG = ScanConfig(snapshot_age_days=0, stopped_days=0)


@pytest.fixture
def aws(monkeypatch):
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def by_check(result, check):
    return [f for f in result.findings if f.check == check]


def test_clean_account_has_no_findings(aws):
    result = scan(aws, regions=[REGION], config=ScanConfig(stopped_days=0))
    assert result.findings == []
    assert result.errors == []


def test_unattached_volume(aws):
    ec2 = aws.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    result = scan(aws, regions=[REGION], config=CONFIG)
    [f] = by_check(result, "unattached_ebs_volume")
    assert f.resource_id == vol["VolumeId"]
    assert f.monthly_cost == 8.0


def test_unused_eip_but_not_attached_eip(aws):
    ec2 = aws.client("ec2")
    free = ec2.allocate_address(Domain="vpc")
    used = ec2.allocate_address(Domain="vpc")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    inst = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]
    ec2.associate_address(AllocationId=used["AllocationId"], InstanceId=inst["InstanceId"])

    result = scan(aws, regions=[REGION], config=CONFIG)
    assert [f.resource_id for f in by_check(result, "unused_elastic_ip")] == [free["AllocationId"]]


def test_old_snapshot_skips_ami_backed(aws):
    ec2 = aws.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50)
    loose = ec2.create_snapshot(VolumeId=vol["VolumeId"])["SnapshotId"]
    image_id = ec2.register_image(Name="mine", RootDeviceName="/dev/sda1",
                                  BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": loose}}])["ImageId"]
    # moto ignores the requested snapshot and attaches its own, so read back what the AMI really uses.
    [image] = ec2.describe_images(ImageIds=[image_id])["Images"]
    ami_backed = image["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]

    flagged = {f.resource_id for f in by_check(scan(aws, regions=[REGION], config=CONFIG), "old_snapshot")}
    assert loose in flagged
    assert ami_backed not in flagged

    fresh = scan(aws, regions=[REGION], config=ScanConfig(snapshot_age_days=30))
    assert loose not in {f.resource_id for f in by_check(fresh, "old_snapshot")}


def test_stopped_instance_counts_volumes_and_dedupes_gp2(aws):
    ec2 = aws.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    inst = ec2.run_instances(
        ImageId=image, MinCount=1, MaxCount=1, InstanceType="t3.micro",
        BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 20, "VolumeType": "gp2"}}],
    )["Instances"][0]
    ec2.stop_instances(InstanceIds=[inst["InstanceId"]])

    result = scan(aws, regions=[REGION], config=CONFIG)
    [f] = by_check(result, "stopped_instance")
    assert f.resource_id == inst["InstanceId"]
    assert f.monthly_cost > 0
    assert by_check(result, "gp2_volume") == []


def test_gp2_volume_in_use(aws):
    ec2 = aws.client("ec2")
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ec2.run_instances(
        ImageId=image, MinCount=1, MaxCount=1,
        BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 500, "VolumeType": "gp2"}}],
    )
    result = scan(aws, regions=[REGION], config=CONFIG)
    [f] = by_check(result, "gp2_volume")
    assert f.monthly_cost == pricing.gp2_to_gp3_savings(500) == 10.0


def test_idle_load_balancer(aws):
    ec2 = aws.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=az)["Subnet"]["SubnetId"]
               for i, az in enumerate(["us-east-1a", "us-east-1b"])]
    elbv2 = aws.client("elbv2")
    elbv2.create_load_balancer(Name="orphan", Subnets=subnets)
    aws.client("elb").create_load_balancer(
        LoadBalancerName="old-clb", AvailabilityZones=["us-east-1a"],
        Listeners=[{"Protocol": "HTTP", "LoadBalancerPort": 80, "InstancePort": 80}],
    )
    result = scan(aws, regions=[REGION], config=CONFIG)
    assert sorted(f.resource_id for f in by_check(result, "idle_load_balancer")) == ["old-clb", "orphan"]


def test_idle_nat_gateway(aws):
    ec2 = aws.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24")["Subnet"]["SubnetId"]
    eip = ec2.allocate_address(Domain="vpc")
    nat = ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip["AllocationId"])["NatGateway"]
    result = scan(aws, regions=[REGION], config=CONFIG)
    assert [f.resource_id for f in by_check(result, "idle_nat_gateway")] == [nat["NatGatewayId"]]


def test_reports_render(aws):
    aws.client("ec2").create_volume(AvailabilityZone="us-east-1a", Size=10)
    result = scan(aws, regions=[REGION], config=ScanConfig(checks=("unattached_ebs_volume",)))
    assert "Unattached EBS volumes" in to_text(result)
    assert '"total_monthly_cost": 1.0' in to_json(result)
    assert "$1.00/month in waste" in to_html(result)
    assert "Manage or cancel" not in to_html(result)
    assert "href='https://billing.stripe.com/p/login/x'" in to_html(result, manage_url="https://billing.stripe.com/p/login/x")


def test_gp2_savings_math():
    assert pricing.gp2_to_gp3_savings(100) == 2.0          # 300 IOPS -> gp3 baseline covers it
    assert pricing.gp2_to_gp3_savings(2000) == 25.0        # 6000 IOPS -> pay for 3000 extra on gp3
