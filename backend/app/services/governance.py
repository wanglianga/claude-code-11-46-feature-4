"""Retention governance primitives: canonical digests, evidence encryption,
versioned retention policies, key lifecycle and expiry sweeps.

These are deliberately separate from the compute engine: retention state does
not live in the feature tables, and a policy version never mutates an already
sealed replay package (the package keeps the snapshot taken when its build
started).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
from datetime import datetime, timedelta, timezone

import fastapi
import sqlalchemy
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AccountProfile,
    AuditQuery,
    EvidenceRecord,
    KeyEvent,
    KeyVersion,
    LabelEvent,
    RetentionPolicy,
    RetentionSweep,
    ReplayPackage,
    Transaction,
)

APP_VERSION = "1.1.0"
COMPUTE_ENGINE_VERSION = "window-engine-1"
SERVICE_NAME = "anti-fraud-feature-platform"

SUPPORTED_DIGEST_ALGORITHMS = ("sha256", "sha3_256", "sha512")

# Evidence classification vocabulary: the three states an auditor must never
# conflate — plaintext unavailable, evidence chain broken, replayable.
CLS_PLAINTEXT_UNAVAILABLE = "plaintext_unavailable"
CLS_EVIDENCE_BROKEN = "evidence_chain_broken"
CLS_REPLAYABLE_PLAINTEXT = "replayable_plaintext"
CLS_REPLAYABLE_ENCRYPTED = "replayable_encrypted"

CLASSIFICATION_LABELS = {
    CLS_PLAINTEXT_UNAVAILABLE: "原文不可用",
    CLS_EVIDENCE_BROKEN: "证据链损坏",
    CLS_REPLAYABLE_PLAINTEXT: "可在受控环境重放（原文）",
    CLS_REPLAYABLE_ENCRYPTED: "可在受控环境重放（加密副本）",
}

CATEGORY_TRANSACTION = "transaction_detail"
CATEGORY_LABEL = "label_outcome"
CATEGORY_PROFILE = "account_profile"
CATEGORIES = (CATEGORY_TRANSACTION, CATEGORY_LABEL, CATEGORY_PROFILE)

# Default, explainable retention policy. The choice per category is deliberate:
# transactions keep an encrypted copy so a chargeback dispute can still be
# replayed after the plaintext window; labels are a legal-hold category with a
# long archive; account profiles are minimized to digests + derived features
# only, which means field-level replay is impossible once plaintext expires.
DEFAULT_RULES = {
    CATEGORY_TRANSACTION: {
        "plaintext_days": 30,
        "protected_copy_days": 365,
        "legal_hold_days": 0,
        "strategy": "encrypted_copy",
        "rationale": (
            "交易明细含商户与账户标识。30 天内审计可直接按清单重放原文；"
            "超出后只保留加密副本，需二次明文审批在受控环境解密复算，"
            "以支持拒付争议与模型复核。"
        ),
        "legal_basis": "内部风控数据保留政策 R-01",
    },
    CATEGORY_LABEL: {
        "plaintext_days": 180,
        "protected_copy_days": 730,
        "legal_hold_days": 730,
        "strategy": "encrypted_copy",
        "rationale": (
            "拒付与人工核查结果是支付争议的法定留档对象，730 天内不得销毁；"
            "保留期届满前以加密副本支持复算，法定期间内不因普通保留到期而清除。"
        ),
        "legal_basis": "支付清算争议留档要求（730 天）",
    },
    CATEGORY_PROFILE: {
        "plaintext_days": 30,
        "protected_copy_days": 30,
        "legal_hold_days": 0,
        "strategy": "digest_only",
        "rationale": (
            "账户资料属个人明细，按数据最小化原则只保留可验证摘要与派生特征，"
            "不额外保存加密副本；过期后仍能证明当时使用了哪些输入及其摘要，"
            "但无法恢复个人明细，逐字段复算被有意限制。"
        ),
        "legal_basis": "个人信息最小化原则",
    },
}


# ---------------------------------------------------------------------------
# Time / canonical serialization / digests
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return ensure_utc(dt).isoformat() if dt else None


def canonical_json(obj) -> str:
    """Deterministic JSON used for every digest and manifest (stable ordering,
    no whitespace, UTF-8 preserved)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_obj(alg: str, obj) -> str:
    if alg not in SUPPORTED_DIGEST_ALGORITHMS:
        raise ValueError(f"unsupported digest algorithm: {alg}")
    return hashlib.new(alg, canonical_json(obj).encode("utf-8")).hexdigest()


def normalize_numbers(obj):
    """Mirror PostgreSQL JSONB number handling so a digest computed before
    persisting equals one recomputed after reading back (1.0 -> 1)."""
    if isinstance(obj, float):
        return int(obj) if obj.is_integer() else obj
    if isinstance(obj, dict):
        return {k: normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize_numbers(v) for v in obj]
    return obj


def digest_bytes(alg: str, data: bytes) -> str:
    return hashlib.new(alg, data).hexdigest()


# ---------------------------------------------------------------------------
# Canonical payloads (single source of truth for what "the input" is)
# ---------------------------------------------------------------------------
def transaction_payload(txn: Transaction) -> dict:
    return {
        "kind": "transaction",
        "txn_id": txn.txn_id,
        "account_id": txn.account_id,
        "event_time": iso(txn.event_time),
        "amount": round(float(txn.amount), 2),
        "currency": txn.currency,
        "type": txn.type,
        "status": txn.status,
        "merchant_id": txn.merchant_id,
    }


def label_payload(label: LabelEvent) -> dict:
    return {
        "kind": "label",
        "label_id": label.label_id,
        "transaction_id": label.transaction_id,
        "account_id": label.account_id,
        "label_type": label.label_type,
        "label_value": label.label_value,
        "event_time": iso(label.event_time),
        "confirmed_at": iso(label.confirmed_at),
    }


def profile_payload(profile: AccountProfile) -> dict:
    return {
        "kind": "account_profile",
        "account_id": profile.account_id,
        "snapshot_date": profile.snapshot_date.isoformat(),
        "kyc_level": profile.kyc_level,
        "country": profile.country,
        "risk_segment": profile.risk_segment,
        "age_days": profile.age_days,
    }


def profile_record_id(profile: AccountProfile) -> str:
    return f"{profile.account_id}:{profile.snapshot_date.isoformat()}"


def category_of_kind(kind: str) -> str:
    return {
        "transaction": CATEGORY_TRANSACTION,
        "label": CATEGORY_LABEL,
        "account_profile": CATEGORY_PROFILE,
    }[kind]


def payload_fields(payload: dict) -> dict:
    """Business fields whose per-field digests are kept for minimized records."""
    return {k: v for k, v in payload.items() if k != "kind"}


def field_digest_map(alg: str, payload: dict) -> dict:
    return {k: digest_obj(alg, v) for k, v in payload_fields(payload).items()}


# ---------------------------------------------------------------------------
# Evidence encryption (AES-256-GCM, per-record key version)
# ---------------------------------------------------------------------------
def encrypt_payload(secret_b64: str, payload: dict, aad: str) -> str:
    key = base64.b64decode(secret_b64)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, canonical_json(payload).encode("utf-8"), aad.encode("utf-8"))
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_payload(secret_b64: str, blob: str, aad: str) -> dict:
    key = base64.b64decode(secret_b64)
    raw = base64.b64decode(blob)
    nonce, ct = raw[:12], raw[12:]
    plain = AESGCM(key).decrypt(nonce, ct, aad.encode("utf-8"))
    return json.loads(plain.decode("utf-8"))


# ---------------------------------------------------------------------------
# Environment fingerprint
# ---------------------------------------------------------------------------
def environment_fingerprint(db: Session | None = None) -> dict:
    info = {
        "service": SERVICE_NAME,
        "app_version": APP_VERSION,
        "compute_engine": COMPUTE_ENGINE_VERSION,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sqlalchemy_version": sqlalchemy.__version__,
        "fastapi_version": fastapi.__version__,
        "timezone": "UTC",
        "digest_algorithms": list(SUPPORTED_DIGEST_ALGORITHMS),
    }
    if db is not None:
        info["db_dialect"] = db.get_bind().dialect.name
    info["fingerprint"] = digest_obj("sha256", info)
    return info


# ---------------------------------------------------------------------------
# Retention policies
# ---------------------------------------------------------------------------
def _validate_rules(rules: dict, digest_algorithm: str) -> None:
    if digest_algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
        raise ValueError(f"digest_algorithm must be one of {list(SUPPORTED_DIGEST_ALGORITHMS)}")
    missing = [c for c in CATEGORIES if c not in rules]
    if missing:
        raise ValueError(f"policy is missing categories: {missing}")
    for cat in CATEGORIES:
        rule = rules[cat]
        for key in ("plaintext_days", "protected_copy_days", "legal_hold_days", "strategy", "rationale"):
            if key not in rule:
                raise ValueError(f"rule {cat} missing {key}")
        if rule["strategy"] not in ("encrypted_copy", "digest_only"):
            raise ValueError(f"rule {cat} strategy must be encrypted_copy or digest_only")
        for key in ("plaintext_days", "protected_copy_days", "legal_hold_days"):
            if int(rule[key]) < 0:
                raise ValueError(f"rule {cat} {key} must be >= 0")
        # A digest-only category minimizes data by design: no encrypted copy is
        # kept past the plaintext window. Strategies are otherwise free to set
        # any protected-copy period >= the plaintext period.
        if int(rule["protected_copy_days"]) < int(rule["plaintext_days"]):
            raise ValueError(f"rule {cat}: protected_copy_days must be >= plaintext_days")


def active_policy(db: Session) -> RetentionPolicy:
    row = db.execute(
        select(RetentionPolicy)
        .where(RetentionPolicy.status == "active")
        .order_by(RetentionPolicy.version.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        raise LookupError("no active retention policy")
    return row


def policy_snapshot_of(policy: RetentionPolicy) -> dict:
    return {
        "id": policy.id,
        "version": policy.version,
        "digest_algorithm": policy.digest_algorithm,
        "rules": policy.rules,
        "rules_digest": policy.rules_digest,
        "note": policy.note,
        "created_by": policy.created_by,
        "created_at": iso(policy.created_at),
    }


def create_policy(db: Session, rules: dict, digest_algorithm: str, note: str, actor: str) -> RetentionPolicy:
    _validate_rules(rules, digest_algorithm)
    # Any new policy is a re-statement of every category: atomically supersede
    # the previous version. Existing packages keep their own snapshot.
    current = db.execute(
        select(RetentionPolicy).where(RetentionPolicy.status == "active")
    ).scalars().all()
    for row in current:
        row.status = "superseded"
    max_v = db.execute(select(func.max(RetentionPolicy.version))).scalar()
    policy = RetentionPolicy(
        version=(max_v or 0) + 1,
        status="active",
        digest_algorithm=digest_algorithm,
        rules=rules,
        rules_digest=digest_obj(digest_algorithm, rules),
        note=note,
        created_by=actor,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------
def active_key(db: Session) -> KeyVersion | None:
    return db.execute(
        select(KeyVersion)
        .where(KeyVersion.status == "active")
        .order_by(KeyVersion.version.desc())
        .limit(1)
    ).scalars().first()


def rotate_key(db: Session, actor: str, reason: str = "") -> KeyVersion:
    current = active_key(db)
    if current is not None:
        current.status = "retired"
        current.retired_at = utcnow()
        db.add(KeyEvent(key_version_id=current.id, action="rotate", actor=actor,
                        detail=f"retired in favour of next version; {reason}"))
        db.flush()
    max_v = db.execute(select(func.max(KeyVersion.version))).scalar()
    key = KeyVersion(
        version=(max_v or 0) + 1,
        secret=base64.b64encode(os.urandom(32)).decode("ascii"),
        status="active",
        created_by=actor,
    )
    db.add(key)
    db.flush()
    db.add(KeyEvent(key_version_id=key.id, action="rotate", actor=actor,
                    detail=f"activated; {reason}"))
    db.commit()
    db.refresh(key)
    return key


def revoke_key(db: Session, key: KeyVersion, actor: str, reason: str) -> KeyVersion:
    if key.status == "revoked":
        raise ValueError("key already revoked")
    key.status = "revoked"
    key.revoked_at = utcnow()
    key.reason = reason
    db.add(KeyEvent(key_version_id=key.id, action="revoke", actor=actor, detail=reason))
    db.commit()
    db.refresh(key)
    return key


def restore_key(db: Session, key: KeyVersion, actor: str, reason: str = "") -> KeyVersion:
    if key.status != "revoked":
        raise ValueError("only a revoked key can be restored")
    # A restored key does not automatically become the active encryption key;
    # it becomes usable for decryption again.
    key.status = "retired"
    key.restored_at = utcnow()
    db.add(KeyEvent(key_version_id=key.id, action="restore", actor=actor, detail=reason))
    db.commit()
    db.refresh(key)
    return key


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------
def record_audit_query(
    db: Session,
    action: str,
    actor: str,
    package_id: int | None = None,
    summary: dict | None = None,
    status: str = "completed",
) -> AuditQuery:
    q = AuditQuery(
        package_id=package_id,
        action=action,
        status=status,
        actor=actor,
        summary=summary,
        completed_at=utcnow() if status == "completed" else None,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def run_sweep(db: Session, as_of: datetime, actor: str) -> RetentionSweep:
    """Enforce plaintext expiry / archive destruction up to ``as_of``.

    Each package's own policy snapshot is authoritative for its records, so a
    policy published after a package was built never changes that package.
    Legal-hold categories encode their minimum retention in ``archive_until``,
    so they are not destroyed before their legal period ends.
    """
    as_of = ensure_utc(as_of)
    policy = active_policy(db)
    sweep = RetentionSweep(
        as_of=as_of,
        policy_version_id=policy.id,
        status="running",
        executed_by=actor,
    )
    db.add(sweep)
    db.flush()

    expired = 0
    destroyed = 0
    per_category: dict[str, dict] = {}
    records = db.execute(select(EvidenceRecord)).scalars().all()
    for rec in records:
        stats = per_category.setdefault(
            rec.category, {"records": 0, "expired": 0, "archives_destroyed": 0, "legal_hold_records": 0}
        )
        stats["records"] += 1
        if as_of > rec.archive_until and rec.encrypted_blob is not None:
            rec.encrypted_blob = None
            rec.plaintext_state = "destroyed"
            rec.purged_at = rec.purged_at or as_of
            destroyed += 1
            stats["archives_destroyed"] += 1
        elif as_of > rec.plaintext_until and rec.plaintext_state == "retained":
            rec.plaintext_state = "purged"
            rec.purged_at = rec.purged_at or as_of
            expired += 1
            stats["expired"] += 1
        if rec.archive_until > rec.plaintext_until:
            stats["legal_hold_records"] += 1

    sweep.status = "completed"
    sweep.records_expired = expired
    sweep.archives_destroyed = destroyed
    sweep.details = per_category
    sweep.completed_at = utcnow()
    db.commit()
    db.refresh(sweep)

    record_audit_query(
        db,
        action="sweep",
        actor=actor,
        summary={"sweep_id": sweep.id, "as_of": iso(as_of), "records_expired": expired,
                 "archives_destroyed": destroyed, "categories": per_category},
    )
    return sweep
