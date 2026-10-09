import html
import json
from collections import defaultdict

from .models import ScanResult

CHECK_TITLES = {
    "unattached_ebs_volume": "Unattached EBS volumes",
    "unused_elastic_ip": "Unused Elastic IPs",
    "old_snapshot": "Old EBS snapshots",
    "stopped_instance": "Long-stopped EC2 instances",
    "idle_load_balancer": "Load balancers with no targets",
    "idle_nat_gateway": "Idle NAT gateways",
    "gp2_volume": "gp2 volumes that could be gp3",
}


def _grouped(result: ScanResult):
    groups = defaultdict(list)
    for f in result.findings:
        groups[f.check].append(f)
    return sorted(groups.items(), key=lambda kv: sum(f.monthly_cost for f in kv[1]), reverse=True)


def to_json(result: ScanResult) -> str:
    return json.dumps(result.to_dict(), indent=2, default=str)


def to_text(result: ScanResult) -> str:
    lines = [
        f"AWS waste report for account {result.account_id}",
        f"Regions scanned: {len(result.regions)}",
        f"Estimated waste: ${result.total_monthly_cost:,.2f}/month "
        f"(${result.total_monthly_cost * 12:,.2f}/year)",
        "",
    ]
    if not result.findings:
        lines.append("No waste found. Nice and tidy.")
    for check, findings in _grouped(result):
        subtotal = sum(f.monthly_cost for f in findings)
        lines.append(f"== {CHECK_TITLES.get(check, check)}: {len(findings)} found, ${subtotal:,.2f}/month ==")
        for f in findings:
            lines.append(f"  ${f.monthly_cost:>9,.2f}  [{f.region}] {f.description}")
        lines.append(f"  -> {findings[0].recommendation}")
        lines.append("")
    if result.errors:
        lines.append(f"{len(result.errors)} check(s) could not run:")
        lines.extend(f"  ! {e}" for e in result.errors)
    lines.append("Costs are estimates based on us-east-1 on-demand list prices.")
    return "\n".join(lines)


def to_html(result: ScanResult, manage_url: str | None = None) -> str:
    e = html.escape
    sections = []
    for check, findings in _grouped(result):
        subtotal = sum(f.monthly_cost for f in findings)
        rows = "".join(
            f"<tr><td style='padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap'>{e(f.region)}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee'>{e(f.description)}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap'>"
            f"${f.monthly_cost:,.2f}</td></tr>"
            for f in findings
        )
        sections.append(
            f"<h2 style='font-size:16px;margin:28px 0 4px'>{e(CHECK_TITLES.get(check, check))} "
            f"<span style='color:#b45309'>${subtotal:,.2f}/mo</span></h2>"
            f"<p style='margin:0 0 8px;color:#555;font-size:13px'>{e(findings[0].recommendation)}</p>"
            f"<table style='width:100%;border-collapse:collapse;font-size:13px'>{rows}</table>"
        )
    body = "".join(sections) or "<p>No waste found. Nice and tidy.</p>"
    errors = ""
    if result.errors:
        items = "".join(f"<li>{e(err)}</li>" for err in result.errors)
        errors = f"<p style='color:#991b1b;font-size:12px'>Some checks could not run:</p><ul style='font-size:12px'>{items}</ul>"
    manage = ""
    if manage_url:
        manage = f" <a href='{e(manage_url)}' style='color:#888'>Manage or cancel your subscription</a>."
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AWS waste report</title></head>
<body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#111;max-width:720px;margin:0 auto;padding:24px">
<p style="color:#555;margin:0">Account {e(result.account_id)} &middot; {len(result.regions)} regions</p>
<h1 style="font-size:28px;margin:4px 0">${result.total_monthly_cost:,.2f}/month in waste</h1>
<p style="color:#555;margin:0">That's ${result.total_monthly_cost * 12:,.2f} a year across {len(result.findings)} resources.</p>
{body}
{errors}
<p style="color:#888;font-size:12px;margin-top:32px">Estimates use us-east-1 on-demand list prices.{manage}</p>
</body></html>"""


FORMATTERS = {"text": to_text, "json": to_json, "html": to_html}
