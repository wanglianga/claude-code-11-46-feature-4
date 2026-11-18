"""SQLAlchemy models for the anti-fraud feature platform.

Design notes
------------
* Canonical input records (Transaction / LabelEvent / AccountProfile) are stored
  once, deduplicated by their business id.
* The streaming compute engine maintains, per feature version and entity, a
  window-bounded contribution state (`WindowEntry`) plus materialized window
  results at event-time boundaries (`Checkpoint`).
* Late-arriving events only recompute the checkpoints of the affected
  (feature, entity, as_of range); every change is audited in `Revision`.
* Online serving pins one complete `FeatureSetVersion` per request via the
  atomically-updated `OnlinePointer`, so a scoring input never mixes fields
  from two releases.
"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="analyst")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Feature catalog
# ---------------------------------------------------------------------------
class Feature(Base):
    """One version of a feature definition (immutable once published)."""

    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_feature_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(64))
    entity_key: Mapped[str] = mapped_column(String(64), default="account_id")
    # transactions | labels | account_profile
    source: Mapped[str] = mapped_column(String(32))
    # COUNT | SUM | AVG | MIN | MAX | LAST
    aggregation: Mapped[str] = mapped_column(String(16))
    # numeric field for SUM/AVG/MIN/MAX (transactions); attribute name for LAST (account_profile)
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # window length in seconds; NULL for LAST (profile snapshot) features
    window_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # optional equality filter on transaction attributes, e.g. {"type": "purchase"}
    filter_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), default="number")  # number | string
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | deprecated
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureSetVersion(Base):
    """An immutable release of a named feature set (a complete scoring input)."""

    __tablename__ = "feature_set_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_set_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="published")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureSetMember(Base):
    __tablename__ = "feature_set_members"
    __table_args__ = (UniqueConstraint("set_version_id", "feature_id", name="uq_set_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    set_version_id: Mapped[int] = mapped_column(ForeignKey("feature_set_versions.id"), index=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class OnlinePointer(Base):
    """Current published version per feature-set name. Single atomic row update
    on publish -> a request either sees the old complete version or the new one."""

    __tablename__ = "online_pointers"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    set_version_id: Mapped[int] = mapped_column(ForeignKey("feature_set_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------
class SourceStatus(Base):
    __tablename__ = "source_status"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)  # transactions | account_profiles | labels
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | degraded | unavailable
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProcessedEvent(Base):
    """Ingestion log + idempotency store. Unique (source, event_id) makes
    duplicate callbacks a no-op."""

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("source", "event_id", name="uq_processed_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[str] = mapped_column(String(128), default="")
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # applied | late_applied
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_attempts: Mapped[int] = mapped_column(Integer, default=0)  # repeated callbacks seen
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"

    txn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    type: Mapped[str] = mapped_column(String(32), default="purchase")
    status: Mapped[str] = mapped_column(String(32), default="completed")
    merchant_id: Mapped[str] = mapped_column(String(64), default="")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LabelEvent(Base):
    """Chargeback / manual-review outcomes. They typically confirm days after
    the transaction, so they arrive late and trigger revisions of label-sourced
    window features; they also resolve training-sample labels."""

    __tablename__ = "label_events"

    label_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(128), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    label_type: Mapped[str] = mapped_column(String(32))  # chargeback | manual_review
    label_value: Mapped[str] = mapped_column(String(32))  # fraud | legit | suspicious
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # business effective time
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountProfile(Base):
    """Daily account snapshots fed by the upstream account system."""

    __tablename__ = "account_profiles"
    __table_args__ = (UniqueConstraint("account_id", "snapshot_date", name="uq_profile_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    kyc_level: Mapped[str] = mapped_column(String(16), default="unknown")
    country: Mapped[str] = mapped_column(String(8), default="")
    risk_segment: Mapped[str] = mapped_column(String(32), default="")
    age_days: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Streaming compute state
# ---------------------------------------------------------------------------
class WindowEntry(Base):
    """Maintained per-(feature, entity) window contribution state. Only events
    relevant to the feature (post-filter) are kept, deduplicated by event id.
    Online reads aggregate this indexed, window-bounded state instead of
    scanning the raw transaction table."""

    __tablename__ = "window_entries"
    __table_args__ = (
        UniqueConstraint("feature_id", "entity_id", "event_id", name="uq_window_entry"),
        Index("ix_window_entries_lookup", "feature_id", "entity_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)


class Checkpoint(Base):
    """Materialized window result for a (feature, entity) at an event-time
    boundary. Recomputed in-place when a late event affects it (audited via
    Revision)."""

    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("feature_id", "entity_id", "as_of", name="uq_checkpoint"),
        Index("ix_checkpoints_lookup", "feature_id", "entity_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[dict] = mapped_column(JSONB)  # {"value": <number|null>}
    revision_seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Revision(Base):
    """Audit record produced when a late event changes an already-materialized
    checkpoint. Scoped to exactly one (feature, entity, as_of)."""

    __tablename__ = "revisions"
    __table_args__ = (Index("ix_revisions_scope", "feature_id", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    cause_event_id: Mapped[str] = mapped_column(String(128))
    checkpoint_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Watermark(Base):
    """Compute progress per (feature, entity)."""

    __tablename__ = "watermarks"
    __table_args__ = (UniqueConstraint("feature_id", "entity_id", name="uq_watermark"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # max event_time applied
    events_applied: Mapped[int] = mapped_column(Integer, default=0)
    late_events: Mapped[int] = mapped_column(Integer, default=0)
    last_applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
class TrainingSample(Base):
    __tablename__ = "training_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_key: Mapped[str] = mapped_column(String(128), unique=True)
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_value: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | confirmed | none
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(Base):
    """A built training set: rows computed with the same definitions and the
    same maintained state that online serving reads."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    feature_set_name: Mapped[str] = mapped_column(String(128))
    feature_set_version: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Replay & retention governance
#
# Six independently stateful holders are modelled:
#   * retention policy versions + sweep jobs   (data retention)
#   * feature catalog compute snapshot         (feature computation, above)
#   * model batches                            (model batch)
#   * replay packages + evidence records       (evidence packaging)
#   * approvals                                (permission approval)
#   * audit queries                            (audit query)
# ---------------------------------------------------------------------------
class ModelVersion(Base):
    """An immutable model artefact: a deterministic scoring spec plus digest.

    Scores are a pure function of the feature values and this spec, so the same
    frozen model can be replayed many times and compared bit-for-bit.
    """

    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict] = mapped_column(JSONB)  # {"weights": {...}, "bias": x, "threshold": t}
    digest: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | retired
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelBatch(Base):
    """A frozen set of model + feature-set version + input samples.

    Once frozen it is immutable; a replay package can only be built from a
    frozen batch.
    """

    __tablename__ = "model_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(128), unique=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    feature_set_name: Mapped[str] = mapped_column(String(128))
    feature_set_version_id: Mapped[int] = mapped_column(ForeignKey("feature_set_versions.id"))
    sample_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | frozen | superseded
    input_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compute_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetentionPolicy(Base):
    """An immutable, versioned retention policy per data category.

    Packages snapshot the full policy at build start; later versions never
    retroactively change an in-flight or sealed package.
    """

    __tablename__ = "retention_policies"
    __table_args__ = (UniqueConstraint("version", name="uq_retention_policy_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded
    digest_algorithm: Mapped[str] = mapped_column(String(16), default="sha256")
    rules: Mapped[dict] = mapped_column(JSONB)  # category -> rule
    rules_digest: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetentionSweep(Base):
    """One execution of retention expiry enforcement (a stateful job)."""

    __tablename__ = "retention_sweeps"

    id: Mapped[int] = mapped_column(primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("retention_policies.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | completed
    records_expired: Mapped[int] = mapped_column(Integer, default=0)
    archives_destroyed: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    executed_by: Mapped[str] = mapped_column(String(64), default="system")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KeyVersion(Base):
    """A wrapping/encryption key version used for encrypted evidence copies.

    status: active (used for new encryption) | retired (decrypt only) |
    revoked (decryption blocked until restored). Rotation never invalidates
    old packages; revocation is a recoverable access-control state.
    """

    __tablename__ = "key_versions"
    __table_args__ = (UniqueConstraint("version", name="uq_key_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    algorithm: Mapped[str] = mapped_column(String(32), default="AES-256-GCM")
    secret: Mapped[str] = mapped_column(String(256))  # base64-encoded key material (demo KMS)
    status: Mapped[str] = mapped_column(String(16), default="active")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KeyEvent(Base):
    __tablename__ = "key_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_version_id: Mapped[int] = mapped_column(ForeignKey("key_versions.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # rotate | revoke | restore
    actor: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReplayPackage(Base):
    """An evidence package (复现清单) for one frozen model batch.

    status: building -> sealed. While building, the policy snapshot taken at
    start is authoritative even if a new policy version is published midway.
    """

    __tablename__ = "replay_packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    package_key: Mapped[str] = mapped_column(String(160), unique=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("model_batches.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="building")  # building | sealed
    policy_version_id: Mapped[int] = mapped_column(ForeignKey("retention_policies.id"))
    policy_snapshot: Mapped[dict] = mapped_column(JSONB)
    environment: Mapped[dict] = mapped_column(JSONB)
    digest_algorithm: Mapped[str] = mapped_column(String(16), default="sha256")
    manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceRecord(Base):
    """One captured input record inside a package.

    The payload digest is retained even after the plaintext (and any encrypted
    copy) is gone, so auditors can still prove which inputs were used.
    """

    __tablename__ = "evidence_records"
    __table_args__ = (
        UniqueConstraint("package_id", "record_key", name="uq_evidence_record"),
        Index("ix_evidence_package", "package_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("replay_packages.id"), index=True)
    record_key: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32))
    record_id: Mapped[str] = mapped_column(String(256))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_digest: Mapped[str] = mapped_column(String(128))
    digest_algorithm: Mapped[str] = mapped_column(String(16), default="sha256")
    strategy: Mapped[str] = mapped_column(String(32))  # encrypted_copy | digest_only
    plaintext_state: Mapped[str] = mapped_column(String(16), default="retained")  # retained|purged|destroyed
    plaintext_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archive_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Captured plaintext of the record while it is inside its plaintext window.
    # A retention sweep nulls it; the encrypted copy (if the category's policy
    # keeps one) survives until archive_until.
    plaintext_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    encrypted_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("key_versions.id"), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Approval(Base):
    """Access approval state machine: requested -> approved | rejected."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approval_package_scope", "package_id", "scope", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("replay_packages.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16))  # replay | plaintext
    status: Mapped[str] = mapped_column(String(16), default="requested")
    requested_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReplayRun(Base):
    """One controlled replay attempt of a sealed package."""

    __tablename__ = "replay_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("replay_packages.id"), index=True)
    run_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))  # consistent | mismatch | not_replayable
    classification: Mapped[str] = mapped_column(String(32))
    result_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    affected_scores: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    executed_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditQuery(Base):
    """Audit-side query log; each entry holds its own running/completed state."""

    __tablename__ = "audit_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("replay_packages.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(32))  # verify | replay | reveal | dependencies | sweep
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | completed | failed
    actor: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceCorrection(Base):
    """Audit trail for a corrected upstream re-upload (drill / real correction).

    A correction after a package was sealed makes the captured digest differ
    from the canonical record, which verification reports as an evidence-chain
    break rather than silently re-replaying changed data.
    """

    __tablename__ = "source_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(32))
    record_id: Mapped[str] = mapped_column(String(256))
    old_digest: Mapped[str] = mapped_column(String(128))
    new_digest: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text, default="")
    corrected_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
