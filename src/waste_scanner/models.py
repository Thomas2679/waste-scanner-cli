from dataclasses import dataclass, field, asdict


@dataclass
class Finding:
    check: str
    region: str
    resource_id: str
    description: str
    monthly_cost: float
    recommendation: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    account_id: str
    regions: list[str]
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_monthly_cost(self) -> float:
        return round(sum(f.monthly_cost for f in self.findings), 2)

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "regions": self.regions,
            "total_monthly_cost": self.total_monthly_cost,
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
        }
