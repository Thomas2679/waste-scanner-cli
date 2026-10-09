"""Each check takes (session, region, config) and returns a list of Findings."""

import re
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from . import pricing
from .models import Finding


def _paginate(client, method, key, **kwargs):
    for page in client.get_paginator(method).paginate(**kwargs):
        yield from page.get(key, [])


def _name_tag(resource: dict) -> str:
    for tag in resource.get("Tags", []) or []:
        if tag["Key"] == "Name":
            return tag["Value"]
    return ""


def _label(resource_id: str, resource: dict) -> str:
    name = _name_tag(resource)
    return f"{resource_id} ({name})" if name else resource_id


def unattached_ebs_volumes(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    findings = []
    for vol in _paginate(ec2, "describe_volumes", "Volumes",
                         Filters=[{"Name": "status", "Values": ["available"]}]):
        cost = pricing.ebs_volume_monthly(vol["VolumeType"], vol["Size"],
                                          vol.get("Iops"), vol.get("Throughput"))
        findings.append(Finding(
            check="unattached_ebs_volume",
            region=region,
            resource_id=vol["VolumeId"],
            description=f"Unattached {vol['Size']} GB {vol['VolumeType']} volume {_label(vol['VolumeId'], vol)}",
            monthly_cost=cost,
            recommendation="Snapshot it if you might need the data, then delete the volume.",
            details={"size_gb": vol["Size"], "type": vol["VolumeType"],
                     "created": vol["CreateTime"].isoformat()},
        ))
    return findings


def unused_elastic_ips(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    findings = []
    for addr in ec2.describe_addresses().get("Addresses", []):
        if addr.get("AssociationId") or addr.get("NetworkInterfaceId"):
            continue
        findings.append(Finding(
            check="unused_elastic_ip",
            region=region,
            resource_id=addr.get("AllocationId", addr["PublicIp"]),
            description=f"Elastic IP {addr['PublicIp']} is not attached to anything",
            monthly_cost=round(pricing.PUBLIC_IPV4_MONTH, 2),
            recommendation="Release the Elastic IP if nothing depends on that address.",
            details={"public_ip": addr["PublicIp"]},
        ))
    return findings


def old_snapshots(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.snapshot_age_days)

    # Snapshots backing an AMI can't be deleted without deregistering the AMI; skip them.
    ami_snapshots = set()
    for image in ec2.describe_images(Owners=["self"]).get("Images", []):
        for bdm in image.get("BlockDeviceMappings", []):
            if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]:
                ami_snapshots.add(bdm["Ebs"]["SnapshotId"])

    findings = []
    for snap in _paginate(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]):
        if snap["StartTime"] > cutoff or snap["SnapshotId"] in ami_snapshots:
            continue
        if "FullSnapshotSizeInBytes" in snap:
            size_gb = snap["FullSnapshotSizeInBytes"] / 1024**3
            note = "full snapshot size"
        else:
            size_gb = snap["VolumeSize"]
            note = "upper bound; snapshots are incremental"
        age_days = (datetime.now(timezone.utc) - snap["StartTime"]).days
        findings.append(Finding(
            check="old_snapshot",
            region=region,
            resource_id=snap["SnapshotId"],
            description=f"Snapshot {_label(snap['SnapshotId'], snap)} is {age_days} days old "
                        f"(from {snap.get('VolumeId', 'unknown volume')}, {snap['VolumeSize']} GB)",
            monthly_cost=round(size_gb * pricing.SNAPSHOT_GB_MONTH, 2),
            recommendation="Delete it if it's outside your retention policy, or move it to the archive tier (75% cheaper).",
            details={"age_days": age_days, "volume_size_gb": snap["VolumeSize"], "cost_basis": note},
        ))
    return findings


_STOP_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")


def stopped_instances(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.stopped_days)
    findings = []
    for reservation in _paginate(ec2, "describe_instances", "Reservations",
                                 Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]):
        for inst in reservation["Instances"]:
            match = _STOP_TIME_RE.search(inst.get("StateTransitionReason", ""))
            stopped_at = None
            if match:
                stopped_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if stopped_at > cutoff:
                    continue

            volume_ids = [b["Ebs"]["VolumeId"] for b in inst.get("BlockDeviceMappings", []) if "Ebs" in b]
            cost = 0.0
            if volume_ids:
                for vol in ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]:
                    cost += pricing.ebs_volume_monthly(vol["VolumeType"], vol["Size"],
                                                       vol.get("Iops"), vol.get("Throughput"))
            since = f" since {stopped_at:%Y-%m-%d}" if stopped_at else ""
            findings.append(Finding(
                check="stopped_instance",
                region=region,
                resource_id=inst["InstanceId"],
                description=f"Instance {_label(inst['InstanceId'], inst)} ({inst['InstanceType']}) "
                            f"stopped{since}; its {len(volume_ids)} EBS volume(s) are still billed",
                monthly_cost=round(cost, 2),
                recommendation="Create an AMI and terminate the instance if it's no longer needed.",
                details={"instance_type": inst["InstanceType"], "volume_ids": volume_ids,
                         "stopped_at": stopped_at.isoformat() if stopped_at else None},
            ))
    return findings


def idle_load_balancers(session, region, config):
    findings = []
    elbv2 = session.client("elbv2", region_name=region)
    monthly = {"application": pricing.ALB_MONTH, "network": pricing.NLB_MONTH,
               "gateway": pricing.GWLB_MONTH}
    for lb in _paginate(elbv2, "describe_load_balancers", "LoadBalancers"):
        try:
            target_groups = elbv2.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TargetGroupNotFound":
                raise
            target_groups = []
        registered = 0
        for tg in target_groups:
            registered += len(elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                              ["TargetHealthDescriptions"])
        if registered:
            continue
        findings.append(Finding(
            check="idle_load_balancer",
            region=region,
            resource_id=lb["LoadBalancerName"],
            description=f"{lb['Type'].title()} load balancer {lb['LoadBalancerName']} has no registered targets",
            monthly_cost=round(monthly.get(lb["Type"], pricing.ALB_MONTH), 2),
            recommendation="Delete the load balancer if the service behind it is gone.",
            details={"arn": lb["LoadBalancerArn"], "type": lb["Type"], "target_groups": len(target_groups)},
        ))

    elb = session.client("elb", region_name=region)
    for lb in _paginate(elb, "describe_load_balancers", "LoadBalancerDescriptions"):
        if lb.get("Instances"):
            continue
        findings.append(Finding(
            check="idle_load_balancer",
            region=region,
            resource_id=lb["LoadBalancerName"],
            description=f"Classic load balancer {lb['LoadBalancerName']} has no instances",
            monthly_cost=round(pricing.CLB_MONTH, 2),
            recommendation="Delete the load balancer if the service behind it is gone.",
            details={"type": "classic"},
        ))
    return findings


def idle_nat_gateways(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.idle_days)
    findings = []
    for nat in _paginate(ec2, "describe_nat_gateways", "NatGateways",
                         Filters=[{"Name": "state", "Values": ["available"]}]):
        stats = cw.get_metric_statistics(
            Namespace="AWS/NATGateway", MetricName="BytesOutToDestination",
            Dimensions=[{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}],
            StartTime=start, EndTime=end, Period=86400, Statistics=["Sum"],
        )
        total_bytes = sum(dp["Sum"] for dp in stats.get("Datapoints", []))
        if total_bytes > 0:
            continue
        findings.append(Finding(
            check="idle_nat_gateway",
            region=region,
            resource_id=nat["NatGatewayId"],
            description=f"NAT gateway {_label(nat['NatGatewayId'], nat)} sent no traffic in {config.idle_days} days",
            monthly_cost=round(pricing.NAT_GATEWAY_MONTH, 2),
            recommendation="Delete the NAT gateway (and release its Elastic IP) if the subnets don't need outbound internet.",
            details={"vpc_id": nat.get("VpcId"), "subnet_id": nat.get("SubnetId")},
        ))
    return findings


def gp2_volumes(session, region, config):
    ec2 = session.client("ec2", region_name=region)
    findings = []
    for vol in _paginate(ec2, "describe_volumes", "Volumes",
                         Filters=[{"Name": "volume-type", "Values": ["gp2"]},
                                  {"Name": "status", "Values": ["in-use"]}]):
        savings = pricing.gp2_to_gp3_savings(vol["Size"])
        if savings <= 0:
            continue
        findings.append(Finding(
            check="gp2_volume",
            region=region,
            resource_id=vol["VolumeId"],
            description=f"{vol['Size']} GB gp2 volume {_label(vol['VolumeId'], vol)} could be gp3",
            monthly_cost=savings,
            recommendation="Modify the volume type to gp3 (no downtime). gp3 is ~20% cheaper with equal or better baseline performance.",
            details={"size_gb": vol["Size"]},
        ))
    return findings


ALL_CHECKS = {
    "unattached_ebs_volume": unattached_ebs_volumes,
    "unused_elastic_ip": unused_elastic_ips,
    "old_snapshot": old_snapshots,
    "stopped_instance": stopped_instances,
    "idle_load_balancer": idle_load_balancers,
    "idle_nat_gateway": idle_nat_gateways,
    "gp2_volume": gp2_volumes,
}
