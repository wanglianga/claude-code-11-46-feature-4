"""Request/response schemas."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class FeatureSpecIn(BaseModel):
    name: str
    version: int | None = None
    description: str = ""
    owner: str | None = None
    entity_key: str = "account_id"
    source: str  # transactions | labels | account_profile
    aggregation: str  # COUNT | SUM | AVG | MIN | MAX | LAST
    field: str | None = None
    window_seconds: int | None = None
    filter_json: dict | None = None
    value_type: str = "number"


class PublishMemberIn(BaseModel):
    name: str
    version: int


class PublishIn(BaseModel):
    members: list[PublishMemberIn]
    note: str = ""


class TransactionEventIn(BaseModel):
    txn_id: str
    account_id: str
    event_time: datetime
    amount: float
    currency: str = "CNY"
    type: str = "purchase"
    status: str = "completed"
    merchant_id: str = ""


class LabelEventIn(BaseModel):
    label_id: str
    transaction_id: str
    account_id: str
    label_type: str  # chargeback | manual_review
    label_value: str  # fraud | legit | suspicious
    event_time: datetime


class AccountProfileIn(BaseModel):
    account_id: str
    snapshot_date: date
    kyc_level: str = "unknown"
    country: str = ""
    risk_segment: str = ""
    age_days: int = 0


class SampleIn(BaseModel):
    sample_key: str
    entity_id: str
    decision_time: datetime
    transaction_id: str | None = None


class DatasetIn(BaseModel):
    name: str
    feature_set: str = "fraud_scoring"
    set_version: int | None = None
    sample_ids: list[int] | None = None


class SimulateTransactionsIn(BaseModel):
    accounts: list[str] | None = None
    count: int = 60
    span_hours: int = 48
    duplicate_rate: float = 0.15
    late_rate: float = 0.15
    seed: int = 42


class SimulateAccountsIn(BaseModel):
    accounts: int = 8
    days: int = 7
    seed: int = 42


class SimulateLabelsIn(BaseModel):
    fraud_rate: float = 0.12
    min_delay_days: int = 1
    max_delay_days: int = 5
    seed: int = 42


class SourceOutageIn(BaseModel):
    source: str = Field(pattern="^(transactions|account_profiles|labels)$")
    status: str = Field(pattern="^(ok|degraded|unavailable)$")
    message: str = ""


# ---------------------------------------------------------------------------
# Replay & retention governance
# ---------------------------------------------------------------------------
class ModelIn(BaseModel):
    name: str = "fraud_blocker"
    description: str = ""
    weights: dict[str, float]
    bias: float = 0.0
    threshold: float = 0.5
    missing_value: float = 0.0


class BatchIn(BaseModel):
    batch_key: str
    model_name: str = "fraud_blocker"
    model_version: int | None = None
    feature_set: str = "fraud_scoring"
    set_version: int | None = None
    sample_ids: list[int] | None = None
    freeze: bool = True


class PackageIn(BaseModel):
    batch_id: int
    package_key: str | None = None
    pause_before_seal: bool = False


class PolicyIn(BaseModel):
    rules: dict
    digest_algorithm: str = "sha256"
    note: str = ""


class SweepIn(BaseModel):
    as_of: datetime | None = None


class ApprovalRequestIn(BaseModel):
    scope: str = Field(pattern="^(replay|plaintext)$")
    reason: str = ""


class ApprovalDecisionIn(BaseModel):
    approve: bool
    note: str = ""


class RevealIn(BaseModel):
    category: str | None = None


class KeyActionIn(BaseModel):
    reason: str = ""


class SourceCorrectionIn(BaseModel):
    record_id: str
    amount: float | None = None
    merchant_id: str | None = None
    type: str | None = None
    note: str = ""
