"""Replay packages (复现清单): building, sealing, verification, controlled
replay, minimized disclosure and dependency lookup.

Guarantees
----------
* A package is built only from a FROZEN model batch and captures the retention
  policy snapshot at build start. A policy published midway never alters the
  in-flight package.
* Every captured record keeps a canonical digest forever; plaintext and
  encrypted copies follow the category rule. After expiry an auditor can still
  prove which inputs were used but cannot recover purged personal details.
* Verification distinguishes three outcomes: plaintext unavailable, evidence
  chain broken, and replayable in a controlled environment. A record that is
  no longer replayable is never reported as reproduced.
* Scoring is a pure function of captured feature values and the frozen model
  spec, so repeated replays are bit-identical.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AccountProfile,
    Approval,
    AuditQuery,
    EvidenceRecord,
    Feature,
    FeatureSetMember,
    FeatureSetVersion,
    KeyVersion,
    LabelEvent,
    ModelBatch,
    ModelVersion,
    ReplayPackage,
    RetentionPolicy,
    RetentionSweep,
    ReplayRun,
    SourceCorrection,
    TrainingSample,
    Transaction,
)
from . import governance as gov
from .compute import aggregate_values, window_bounds
from .serving import resolve_set_version
from .training import trace_sample

# Classification vocabulary is shared with the governance layer so the API,
# verification and the UI all speak one language.
CLS_PLAINTEXT_UNAVAILABLE = gov.CLS_PLAINTEXT_UNAVAILABLE
CLS_EVIDENCE_BROKEN = gov.CLS_EVIDENCE_BROKEN
CLS_REPLAYABLE_PLAINTEXT = gov.CLS_REPLAYABLE_PLAINTEXT
CLS_REPLAYABLE_ENCRYPTED = gov.CLS_REPLAYABLE_ENCRYPTED
CLASSIFICATION_LABELS = gov.CLASSIFICATION_LABELS


class ReplayError(Exception):
    """Domain error with an HTTP-ish code."""

    def __init__(self, message: str, code: str = "bad_request"):
        self.message = message
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Model batches
# ---------------------------------------------------------------------------
def model_out(model: ModelVersion) -> dict:
    return {
        "id": model.id,
        "name": model.name,
        "version": model.version,
        "digest": model.digest,
        "spec": model.spec,
        "status": model.status,
        "created_by": model.created_by,
        "created_at": gov.iso(model.created_at),
    }


def create_model(db: Session, spec: dict, created_by: str) -> ModelVersion:
    weights = spec.get("weights") or {}
    if not isinstance(weights, dict) or not weights:
        raise ReplayError("model spec requires a non-empty weights map")
    for v in weights.values():
        if not isinstance(v, (int, float)):
            raise ReplayError("model weights must be numeric")
    digest = gov.digest_obj("sha256", spec)
    name = spec.get("name") or "fraud_blocker"
    version = 1
    existing = db.execute(select(ModelVersion).where(ModelVersion.name == name)).scalars().all()
    if existing:
        version = max(m.version for m in existing) + 1
    model = ModelVersion(
        name=name,
        version=version,
        description=spec.get("description", ""),
        spec={
            "weights": weights,
            "bias": float(spec.get("bias", 0.0)),
            "threshold": float(spec.get("threshold", 0.5)),
            "missing_value": float(spec.get("missing_value", 0.0)),
        },
        digest=digest,
        status="active",
        created_by=created_by,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def _find_model(db: Session, name: str, version: int | None) -> ModelVersion:
    q = select(ModelVersion).where(ModelVersion.name == name)
    if version is not None:
        q = q.where(ModelVersion.version == version)
    row = db.execute(q.order_by(ModelVersion.version.desc()).limit(1)).scalars().first()
    if row is None:
        raise ReplayError(f"model {name} not found", "not_found")
    return row


def _capture_traces(db: Session, sample: TrainingSample, set_name: str, set_version: int) -> dict:
    """Trace a sample through the existing lineage service (same compute path
    online serving uses) and return the canonical captured rows."""
    return trace_sample(db, sample, set_name, set_version)


def _record_entries_from_trace(trace: dict) -> list[dict]:
    """Unique captured input records with all their fields' digests."""
    seen: dict[str, dict] = {}
    for feature in trace["features"]:
        for inp in feature.get("inputs") or []:
            if inp["kind"] == "transaction":
                rec_id = inp["txn_id"]
                payload = {
                    "kind": "transaction",
                    "txn_id": inp["txn_id"],
                    "account_id": inp["account_id"],
                    "event_time": inp["event_time"],
                    "amount": round(float(inp["amount"]), 2),
                    "currency": inp.get("currency", "CNY"),
                    "type": inp["type"],
                    "status": inp["status"],
                    "merchant_id": inp["merchant_id"],
                }
                event_time = inp["event_time"]
            elif inp["kind"] == "label":
                rec_id = inp["label_id"]
                payload = {
                    "kind": "label",
                    "label_id": inp["label_id"],
                    "transaction_id": inp["transaction_id"],
                    "account_id": inp.get("account_id", trace["entity_id"]),
                    "label_type": inp["label_type"],
                    "label_value": inp["label_value"],
                    "event_time": inp["event_time"],
                    "confirmed_at": inp.get("confirmed_at"),
                }
                event_time = inp["event_time"]
            else:
                rec_id = f"{inp['account_id']}:{inp['snapshot_date']}"
                payload = {
                    "kind": "account_profile",
                    "account_id": inp["account_id"],
                    "snapshot_date": inp["snapshot_date"],
                    "kyc_level": inp["kyc_level"],
                    "country": inp["country"],
                    "risk_segment": inp["risk_segment"],
                    "age_days": inp["age_days"],
                }
                event_time = f"{inp['snapshot_date']}T00:00:00+00:00"
            key = f"{payload['kind']}:{rec_id}"
            if key in seen:
                continue
            seen[key] = {
                "record_key": key,
                "record_id": rec_id,
                "category": gov.category_of_kind(payload["kind"]),
                "event_time": event_time,
                "payload": payload,
            }
    return list(seen.values())


def _input_digest_from_traces(traces: list[dict], alg: str = "sha256") -> str:
    material = []
    for trace in sorted(traces, key=lambda t: t["sample_key"]):
        for rec in sorted(_record_entries_from_trace(trace), key=lambda r: r["record_key"]):
            material.append([rec["record_key"], gov.digest_obj(alg, rec["payload"])])
    return gov.digest_obj(alg, material)


def _compute_snapshot(db: Session) -> dict:
    """State of the compute side frozen with the batch."""
    from ..models import SourceStatus, Watermark

    watermarks = db.execute(select(Watermark)).scalars().all()
    sources = db.execute(select(SourceStatus)).scalars().all()
    return {
        "engine_version": gov.COMPUTE_ENGINE_VERSION,
        "watermarks": [
            {"feature_id": w.feature_id, "entity_id": w.entity_id, "watermark": gov.iso(w.watermark),
             "events_applied": w.events_applied, "late_events": w.late_events}
            for w in watermarks
        ],
        "sources": [
            {"source": s.source, "status": s.status, "last_success_at": gov.iso(s.last_success_at)}
            for s in sources
        ],
    }


def create_batch(
    db: Session,
    batch_key: str,
    model_name: str,
    model_version: int | None,
    set_name: str,
    set_version: int | None,
    sample_ids: list[int] | None,
    created_by: str,
    freeze: bool,
) -> ModelBatch:
    if db.execute(select(ModelBatch).where(ModelBatch.batch_key == batch_key)).scalar_one_or_none():
        raise ReplayError(f"batch {batch_key} already exists", "conflict")
    model = _find_model(db, model_name, model_version)
    version_row = resolve_set_version(db, set_name, set_version)
    query = select(TrainingSample).order_by(TrainingSample.id)
    if sample_ids:
        query = query.where(TrainingSample.id.in_(sample_ids))
    samples = list(db.execute(query).scalars().all())
    if not samples:
        raise ReplayError("batch requires at least one training sample")
    batch = ModelBatch(
        batch_key=batch_key,
        model_version_id=model.id,
        feature_set_name=version_row.name,
        feature_set_version_id=version_row.id,
        sample_ids=[s.id for s in samples],
        status="draft",
        created_by=created_by,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    if freeze:
        batch = freeze_batch(db, batch, created_by)
    return batch


def freeze_batch(db: Session, batch: ModelBatch, actor: str) -> ModelBatch:
    """Freeze the batch: pin model + feature set, capture the compute snapshot
    and a digest over every input record as it exists right now."""
    if batch.status == "frozen":
        return batch
    samples = [
        db.get(TrainingSample, sid) for sid in batch.sample_ids
    ]
    samples = [s for s in samples if s is not None]
    version_row = db.get(FeatureSetVersion, batch.feature_set_version_id)
    traces = [
        _capture_traces(db, s, version_row.name, version_row.version) for s in samples
    ]
    batch.input_digest = _input_digest_from_traces(traces)
    batch.compute_snapshot = _compute_snapshot(db)
    batch.status = "frozen"
    batch.frozen_at = gov.utcnow()
    db.commit()
    db.refresh(batch)
    return batch


def batch_out(db: Session, batch: ModelBatch) -> dict:
    model = db.get(ModelVersion, batch.model_version_id)
    fsv = db.get(FeatureSetVersion, batch.feature_set_version_id)
    return {
        "id": batch.id,
        "batch_key": batch.batch_key,
        "status": batch.status,
        "model": {"name": model.name, "version": model.version, "digest": model.digest},
        "feature_set": {"name": fsv.name, "version": fsv.version, "id": fsv.id},
        "sample_ids": batch.sample_ids,
        "input_digest": batch.input_digest,
        "compute_snapshot": batch.compute_snapshot,
        "created_by": batch.created_by,
        "created_at": gov.iso(batch.created_at),
        "frozen_at": gov.iso(batch.frozen_at),
    }


# ---------------------------------------------------------------------------
# Scoring (pure function of frozen feature values + model spec)
# ---------------------------------------------------------------------------
def score_row(model_spec: dict, feature_values: dict[str, float | int | None]) -> dict:
    weights = model_spec["weights"]
    bias = float(model_spec.get("bias", 0.0))
    missing_value = float(model_spec.get("missing_value", 0.0))
    total = bias
    contributions = {}
    missing = []
    for name, weight in weights.items():
        raw = feature_values.get(name)
        if raw is None or isinstance(raw, str):
            value = missing_value
            missing.append(name)
        else:
            value = float(raw)
        contributions[name] = round(value * float(weight), 6)
        total += value * float(weight)
    threshold = float(model_spec.get("threshold", 0.5))
    decision = "manual_review" if total >= threshold else "auto_pass"
    return {
        "value": round(total, 6),
        "decision": decision,
        "threshold": threshold,
        "missing_features": sorted(missing),
        "contributions": contributions,
    }


# ---------------------------------------------------------------------------
# Package building
# ---------------------------------------------------------------------------
def _windows(rule: dict, at: datetime) -> tuple[datetime, datetime]:
    plaintext_until = at + timedelta(days=int(rule["plaintext_days"]))
    archive_days = max(
        int(rule["protected_copy_days"]), int(rule["legal_hold_days"])
    )
    archive_until = at + timedelta(days=archive_days)
    return plaintext_until, archive_until


def _canonical_lookup(db: Session, category: str, record_id: str):
    if category == gov.CATEGORY_TRANSACTION:
        txn = db.get(Transaction, record_id)
        return gov.transaction_payload(txn) if txn else None
    if category == gov.CATEGORY_LABEL:
        label = db.get(LabelEvent, record_id)
        return gov.label_payload(label) if label else None
    account_id, day = record_id.split(":", 1)
    from datetime import date

    profile = db.execute(
        select(AccountProfile).where(
            AccountProfile.account_id == account_id,
            AccountProfile.snapshot_date == date.fromisoformat(day),
        )
    ).scalars().first()
    return gov.profile_payload(profile) if profile else None


def build_package(
    db: Session,
    batch: ModelBatch,
    package_key: str | None,
    created_by: str,
    pause_before_seal: bool = False,
) -> ReplayPackage:
    """Capture all evidence for a frozen batch. The retention policy snapshot
    is taken HERE, once; sealing later uses that snapshot only."""
    if batch.status != "frozen":
        raise ReplayError("a replay package can only be built from a frozen batch", "not_frozen")
    if db.execute(select(ReplayPackage).where(ReplayPackage.batch_id == batch.id,
                                              ReplayPackage.status == "building")).scalar_one_or_none():
        raise ReplayError("batch already has a package under construction", "conflict")
    policy = gov.active_policy(db)
    snapshot = gov.policy_snapshot_of(policy)
    alg = policy.digest_algorithm
    key = gov.active_key(db)
    if key is None:
        key = gov.rotate_key(db, "system", "initial evidence key")
    now = gov.utcnow()
    version_row = db.get(FeatureSetVersion, batch.feature_set_version_id)
    model = db.get(ModelVersion, batch.model_version_id)

    package_key = package_key or f"PKG-{batch.batch_key}-{now.strftime('%Y%m%d%H%M%S')}"
    if db.execute(select(ReplayPackage).where(ReplayPackage.package_key == package_key)).scalar_one_or_none():
        raise ReplayError(f"package {package_key} already exists", "conflict")

    package = ReplayPackage(
        package_key=package_key,
        batch_id=batch.id,
        status="building",
        policy_version_id=policy.id,
        policy_snapshot=snapshot,
        environment=gov.environment_fingerprint(db),
        digest_algorithm=alg,
        created_by=created_by,
    )
    db.add(package)
    db.flush()

    # ---- capture sample rows -------------------------------------------------
    samples = [db.get(TrainingSample, sid) for sid in batch.sample_ids]
    samples = [s for s in samples if s is not None]
    traces = [_capture_traces(db, s, version_row.name, version_row.version) for s in samples]

    sample_rows = []
    record_index: dict[str, dict] = {}
    for sample, trace in zip(samples, traces):
        feature_rows = []
        values_for_score: dict[str, float | int | None] = {}
        for feature in trace["features"]:
            if feature["status"] == "ok" and not isinstance(feature.get("value"), (dict, list)):
                values_for_score[feature["name"]] = feature.get("value")
            inputs = []
            for rec in _record_entries_from_trace({"features": [feature], "entity_id": trace["entity_id"],
                                                   "sample_key": trace["sample_key"]}):
                record_index.setdefault(rec["record_key"], rec)
                inputs.append(
                    {
                        "record_key": rec["record_key"],
                        "record_id": rec["record_id"],
                        "digest": gov.digest_obj(alg, rec["payload"]),
                        "fields": gov.field_digest_map(alg, rec["payload"]),
                    }
                )
            inputs.sort(key=lambda r: r["record_key"])
            feature_rows.append(
                {
                    "name": feature["name"],
                    "version": feature["version"],
                    "as_of": trace["decision_time"],
                    "status": feature["status"],
                    "value": feature.get("value"),
                    "reason": feature.get("reason"),
                    "inputs": inputs,
                }
            )

        label = trace.get("label_record")
        label_maturity = {
            "status": trace["label_status"],
            "value": trace.get("label_value"),
            "mature": trace["label_status"] == "confirmed",
            "label_id": label["label_id"] if label else None,
            "effective_at": label["event_time"] if label else None,
            "confirmed_at": label["confirmed_at"] if label else None,
            "acquired_after_decision": bool(
                label and label.get("event_time") and label["event_time"] > trace["decision_time"]
            ),
        }
        score = score_row(model.spec, values_for_score)
        row = {
            "sample_key": trace["sample_key"],
            "entity_id": trace["entity_id"],
            "decision_time": trace["decision_time"],
            "transaction_id": trace.get("transaction_id"),
            "label": label_maturity,
            "features": feature_rows,
            "feature_values_digest": gov.digest_obj(alg, {
                f["name"]: {"version": f["version"], "value": f["value"], "status": f["status"]}
                for f in feature_rows
            }),
            "score": score,
        }
        row["row_digest"] = gov.digest_obj(alg, {k: row[k] for k in (
            "sample_key", "entity_id", "decision_time", "transaction_id", "label",
            "features", "feature_values_digest", "score")})
        sample_rows.append(row)

    # ---- persist evidence records -------------------------------------------
    for rec_key in sorted(record_index):
        rec = record_index[rec_key]
        payload = rec["payload"]
        category = rec["category"]
        rule = snapshot["rules"][category]
        plaintext_until, archive_until = _windows(rule, now)
        digest = gov.digest_obj(alg, payload)
        blob = None
        key_id = None
        if rule["strategy"] == "encrypted_copy" and int(rule["protected_copy_days"]) > 0 \
                and archive_until > now:
            blob = gov.encrypt_payload(key.secret, payload, f"{package_key}|{rec_key}")
            key_id = key.id
        db.add(
            EvidenceRecord(
                package_id=package.id,
                record_key=rec_key,
                category=category,
                record_id=rec["record_id"],
                event_time=_parse_time(rec["event_time"]),
                payload_digest=digest,
                digest_algorithm=alg,
                strategy=rule["strategy"],
                plaintext_state="retained",
                plaintext_until=plaintext_until,
                archive_until=archive_until,
                plaintext_payload=payload,
                encrypted_blob=blob,
                key_version_id=key_id,
            )
        )

    # ---- event order across all inputs --------------------------------------
    ordered = sorted(
        record_index.values(), key=lambda r: (_parse_time(r["event_time"]), r["record_key"])
    )
    event_order = [
        {
            "seq": i,
            "record_key": r["record_key"],
            "category": r["category"],
            "event_time": gov.iso(_parse_time(r["event_time"])),
        }
        for i, r in enumerate(ordered)
    ]

    categories_summary = {}
    for cat in gov.CATEGORIES:
        rule = snapshot["rules"][cat]
        cats = [r for r in record_index.values() if r["category"] == cat]
        categories_summary[cat] = {
            "records": len(cats),
            "strategy": rule["strategy"],
            "plaintext_days": rule["plaintext_days"],
            "protected_copy_days": rule["protected_copy_days"],
            "legal_hold_days": rule["legal_hold_days"],
            "rationale": rule["rationale"],
            "legal_basis": rule["legal_basis"],
            "fields": sorted({field for r in cats for field in gov.payload_fields(r["payload"])}),
        }

    feature_defs = {}
    for member in db.execute(
        select(Feature)
        .join(FeatureSetMember, FeatureSetMember.feature_id == Feature.id)
        .where(FeatureSetMember.set_version_id == version_row.id)
        .order_by(FeatureSetMember.position)
    ).scalars():
        feature_defs[f"{member.name}@v{member.version}"] = {
            "source": member.source,
            "aggregation": member.aggregation,
            "field": member.field,
            "window_seconds": member.window_seconds,
            "filter_json": member.filter_json,
            "digest": _feature_digest(member, alg),
        }

    manifest = {
        "package_key": package_key,
        "created_at": gov.iso(now),
        "created_by": created_by,
        "digest_algorithm": alg,
        "batch": {
            "batch_key": batch.batch_key,
            "status": batch.status,
            "input_digest": batch.input_digest,
            "frozen_at": gov.iso(batch.frozen_at),
        },
        "model": {"name": model.name, "version": model.version, "digest": model.digest, "spec": model.spec},
        "feature_set": {
            "name": version_row.name,
            "version": version_row.version,
            "engine_version": gov.COMPUTE_ENGINE_VERSION,
            "features": feature_defs,
        },
        "data_scope": {
            "entities": sorted({r["entity_id"] for r in sample_rows}),
            "decision_time_min": min((r["decision_time"] for r in sample_rows), default=None),
            "decision_time_max": max((r["decision_time"] for r in sample_rows), default=None),
            "record_counts": {cat: v["records"] for cat, v in categories_summary.items()},
            "categories": categories_summary,
        },
        "event_order": event_order,
        "samples": sample_rows,
        "policy": snapshot,
        "environment": package.environment,
        "integrity": {
            "checksum_algorithm": alg,
            "record_count": len(record_index),
            "sample_count": len(sample_rows),
            "checksum": None,
        },
    }
    manifest = gov.normalize_numbers(manifest)
    manifest["integrity"]["checksum"] = gov.digest_obj(
        alg, {k: v for k, v in manifest.items() if k != "integrity"}
        | {"integrity": {k: v for k, v in manifest["integrity"].items() if k != "checksum"}}
    )
    package.manifest = manifest
    package.manifest_digest = gov.digest_obj(alg, manifest)
    package.status = "building" if pause_before_seal else "sealed"
    if not pause_before_seal:
        package.sealed_at = gov.utcnow()
    db.commit()
    db.refresh(package)
    return package


def _feature_digest(feature: Feature, alg: str) -> str:
    return gov.digest_obj(alg, {
        "name": feature.name,
        "version": feature.version,
        "source": feature.source,
        "aggregation": feature.aggregation,
        "field": feature.field,
        "window_seconds": feature.window_seconds,
        "filter_json": feature.filter_json,
        "value_type": feature.value_type,
    })


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return gov.ensure_utc(dt)


def seal_package(db: Session, package: ReplayPackage, actor: str) -> ReplayPackage:
    """Seal a package whose build was paused. Sealing re-checks that the
    manifest still matches the snapshot taken at build start, and that no
    policy change silently rewrote it."""
    if package.status != "building":
        raise ReplayError("only a package under construction can be sealed", "conflict")
    if package.manifest is None:
        raise ReplayError("package has no manifest", "conflict")

    # Policy may have moved on; the package must keep the frozen snapshot.
    current = gov.active_policy(db)
    snapshot = package.policy_snapshot
    policy_note = {
        "snapshot_version": snapshot["version"],
        "current_active_version": current.version,
        "policy_changed_during_build": current.version != snapshot["version"],
        "used_snapshot": True,
    }
    manifest = gov.normalize_numbers(dict(package.manifest))
    manifest["policy"] = snapshot
    manifest["seal"] = {"sealed_at": gov.iso(gov.utcnow()), "sealed_by": actor, "policy": policy_note}
    alg = package.digest_algorithm
    manifest["integrity"]["checksum"] = gov.digest_obj(
        alg,
        {k: v for k, v in manifest.items() if k != "integrity"}
        | {"integrity": {k: v for k, v in manifest["integrity"].items() if k != "checksum"}},
    )
    package.manifest = manifest
    package.manifest_digest = gov.digest_obj(alg, manifest)
    package.status = "sealed"
    package.sealed_at = gov.utcnow()
    db.commit()
    db.refresh(package)
    return package


# ---------------------------------------------------------------------------
# Availability / keys
# ---------------------------------------------------------------------------
def record_availability(db: Session, rec: EvidenceRecord, at: datetime | None = None) -> dict:
    """Current field availability for one evidence record.

    Never returns plaintext; only the state an auditor is allowed to know.
    """
    at = at or gov.utcnow()
    key = db.get(KeyVersion, rec.key_version_id) if rec.key_version_id else None
    if rec.plaintext_state == "retained" and rec.plaintext_payload is not None and at <= rec.plaintext_until:
        state = "plaintext"
    elif rec.encrypted_blob is not None:
        if key is not None and key.status == "revoked":
            state = "encrypted_locked"
        else:
            state = "encrypted_copy"
    elif rec.strategy == "digest_only":
        state = "digest_only"
    else:
        state = "destroyed"
    return {
        "record_key": rec.record_key,
        "category": rec.category,
        "record_id": rec.record_id,
        "strategy": rec.strategy,
        "state": state,
        "plaintext_until": gov.iso(rec.plaintext_until),
        "archive_until": gov.iso(rec.archive_until),
        "digest": rec.payload_digest,
        "digest_algorithm": rec.digest_algorithm,
        "key_version": key.version if key else None,
        "key_status": key.status if key else None,
        "event_time": gov.iso(rec.event_time),
    }


def _usable_plaintext(db: Session, rec: EvidenceRecord, at: datetime) -> tuple[dict | None, str]:
    """Return (payload, source) where source is plaintext|encrypted|none.
    Never fabricates data; a locked key yields (None, 'encrypted_locked')."""
    if rec.plaintext_state == "retained" and rec.plaintext_payload is not None and at <= rec.plaintext_until:
        return rec.plaintext_payload, "plaintext"
    if rec.encrypted_blob is None:
        return None, "none"
    key = db.get(KeyVersion, rec.key_version_id) if rec.key_version_id else None
    if key is None or key.status == "revoked":
        return None, "encrypted_locked"
    try:
        payload = gov.decrypt_payload(
            key.secret, rec.encrypted_blob, f"{_package_key_of(db, rec)}|{rec.record_key}"
        )
        return payload, "encrypted"
    except Exception:
        return None, "decrypt_failed"


def _package_key_of(db: Session, rec: EvidenceRecord) -> str:
    pkg = db.get(ReplayPackage, rec.package_id)
    return pkg.package_key


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def approval_state(db: Session, package_id: int, scope: str | None = None) -> list[dict]:
    q = select(Approval).where(Approval.package_id == package_id)
    if scope:
        q = q.where(Approval.scope == scope)
    rows = db.execute(q.order_by(Approval.id)).scalars().all()
    return [
        {
            "id": a.id,
            "scope": a.scope,
            "status": a.status,
            "requested_by": a.requested_by,
            "reason": a.reason,
            "decided_by": a.decided_by,
            "decided_at": gov.iso(a.decided_at),
            "note": a.note,
        }
        for a in rows
    ]


def has_approval(db: Session, package_id: int, scope: str) -> bool:
    return db.execute(
        select(Approval.id).where(
            Approval.package_id == package_id,
            Approval.scope == scope,
            Approval.status == "approved",
        )
    ).scalars().first() is not None


def _records_of(db: Session, package: ReplayPackage) -> list[EvidenceRecord]:
    return list(
        db.execute(
            select(EvidenceRecord)
            .where(EvidenceRecord.package_id == package.id)
            .order_by(EvidenceRecord.record_key)
        ).scalars().all()
    )


def verify_package(db: Session, package: ReplayPackage) -> dict:
    """Full integrity check. Returns classifications, per-record findings and
    the affected scores — never plaintext."""
    now = gov.utcnow()
    alg = package.digest_algorithm
    manifest = package.manifest or {}
    findings: list[dict] = []
    affected: dict[str, dict] = {}

    def affect(sample_key: str, reason: str) -> None:
        if sample_key not in affected:
            row = next((r for r in manifest.get("samples", []) if r["sample_key"] == sample_key), None)
            affected[sample_key] = {
                "sample_key": sample_key,
                "score": row["score"]["value"] if row else None,
                "decision": row["score"]["decision"] if row else None,
                "reasons": [],
            }
        if reason not in affected[sample_key]["reasons"]:
            affected[sample_key]["reasons"].append(reason)

    # 1. manifest checksum
    checksum_ok = False
    if manifest:
        expected = manifest.get("integrity", {}).get("checksum")
        recomputed = gov.digest_obj(
            alg,
            {k: v for k, v in manifest.items() if k != "integrity"}
            | {"integrity": {k: v for k, v in manifest.get("integrity", {}).items() if k != "checksum"}},
        )
        checksum_ok = expected == recomputed
        if not checksum_ok:
            findings.append({"scope": "manifest", "kind": "checksum_mismatch",
                             "detail": "manifest checksum does not match its content"})
    # JSONB round-trips number shapes (e.g. 1.0 -> 1), so the sealed digest is
    # normalised through canonical JSON before comparison.
    def _normalised(obj):
        return gov.json.loads(gov.canonical_json(obj))

    manifest_digest_ok = bool(manifest) and (
        gov.digest_obj(alg, _normalised(manifest)) == package.manifest_digest
    )
    if manifest and not manifest_digest_ok:
        findings.append({"scope": "manifest", "kind": "digest_mismatch",
                         "detail": "manifest digest does not match the sealed digest"})
    checksum_ok = checksum_ok and manifest_digest_ok

    # 2. feature definitions frozen in the manifest
    feature_defs = manifest.get("feature_set", {}).get("features", {})
    for key, spec in feature_defs.items():
        name, ver = key.rsplit("@v", 1)
        feature = db.execute(
            select(Feature).where(Feature.name == name, Feature.version == int(ver))
        ).scalar_one_or_none()
        if feature is None:
            findings.append({"scope": "feature", "kind": "definition_missing", "detail": key})
            continue
        if _feature_digest(feature, alg) != spec["digest"]:
            findings.append({"scope": "feature", "kind": "definition_changed", "detail": key})

    # 3. record-level checks: digest algorithm, source corrections, canonical copy
    current_policy = gov.active_policy(db)
    digest_migration = {
        "package_algorithm": alg,
        "current_policy_algorithm": current_policy.digest_algorithm,
        "migrated": current_policy.digest_algorithm != alg,
        "legacy_verification_ok": True,
    }
    corrections = db.execute(select(SourceCorrection)).scalars().all()
    corrections_by_key = {(c.category, c.record_id): c for c in corrections}
    records = _records_of(db, package)
    # Decrypt each usable copy once per verification pass.
    usable_cache: dict[str, tuple[dict | None, str]] = {}

    def usable(rec: EvidenceRecord):
        if rec.record_key not in usable_cache:
            usable_cache[rec.record_key] = _usable_plaintext(db, rec, now)
        return usable_cache[rec.record_key]

    record_rows = []
    for rec in records:
        avail = record_availability(db, rec, now)
        broken_reasons = []
        corr = corrections_by_key.get((rec.category, rec.record_id))
        if corr is not None:
            broken_reasons.append(f"来源补传修改：{corr.old_digest[:12]}→{corr.new_digest[:12]}")
        canonical = _canonical_lookup(db, rec.category, rec.record_id)
        canonical_digest = gov.digest_obj(alg, canonical) if canonical is not None else None
        source_matches = canonical is not None and canonical_digest == rec.payload_digest
        if canonical is not None and not source_matches and corr is None:
            broken_reasons.append("当前规范记录摘要与冻结证据不一致")
        # digest-algorithm migration: recompute the captured digest under the
        # new algorithm only when plaintext is still usable.
        migrated_digest = None
        payload, _src = usable(rec)
        if payload is not None and digest_migration["migrated"]:
            migrated_digest = gov.digest_obj(current_policy.digest_algorithm, payload)
        if payload is not None and gov.digest_obj(alg, payload) != rec.payload_digest:
            broken_reasons.append("证据副本摘要与冻结摘要不一致")
            digest_migration["legacy_verification_ok"] = False

        if broken_reasons:
            state = "evidence_chain_broken"
            findings.append({
                "scope": "record",
                "kind": "evidence_chain_broken",
                "detail": rec.record_key,
                "reasons": broken_reasons,
            })
            for sample in manifest.get("samples", []):
                for feature in sample["features"]:
                    if any(i["record_key"] == rec.record_key for i in feature["inputs"]):
                        affect(sample["sample_key"], f"{rec.record_key} 证据链损坏")
        else:
            state = avail["state"]

        record_rows.append({
            **avail,
            "state": state,
            "canonical_present": canonical is not None,
            "source_matches_captured": source_matches,
            "migrated_digest": migrated_digest,
        })

    # 4. sample-level replayability: every input record must be usable
    avail_by_key = {r["record_key"]: r for r in record_rows}
    sample_rows = []
    for sample in manifest.get("samples", []):
        input_states = {}
        blocked = set()
        for feature in sample["features"]:
            for inp in feature["inputs"]:
                st = avail_by_key[inp["record_key"]]["state"]
                input_states[inp["record_key"]] = st
                if st in ("evidence_chain_broken", "destroyed", "digest_only", "encrypted_locked"):
                    blocked.add(inp["record_key"])
                    affect(sample["sample_key"], f"{inp['record_key']} 不可用（{st}）")
        if any(s == "evidence_chain_broken" for s in input_states.values()):
            classification = CLS_EVIDENCE_BROKEN
            replayable = False
        elif blocked:
            classification = CLS_PLAINTEXT_UNAVAILABLE
            replayable = False
        elif any(s == "encrypted_copy" for s in input_states.values()):
            classification = CLS_REPLAYABLE_ENCRYPTED
            replayable = True
        else:
            classification = CLS_REPLAYABLE_PLAINTEXT
            replayable = True
        sample_rows.append({
            "sample_key": sample["sample_key"],
            "classification": classification,
            "classification_label": CLASSIFICATION_LABELS[classification],
            "replayable": replayable,
            "blocked_records": sorted(blocked),
            "score": sample["score"],
        })

    broken = any(f["kind"] in ("checksum_mismatch", "digest_mismatch", "definition_changed",
                               "definition_missing") for f in findings) or any(
        r["state"] == "evidence_chain_broken" for r in record_rows
    )
    plaintext_unavailable = any(s["classification"] == CLS_PLAINTEXT_UNAVAILABLE for s in sample_rows)
    replayable = any(s["replayable"] for s in sample_rows) and not broken
    if broken:
        overall = CLS_EVIDENCE_BROKEN
    elif plaintext_unavailable:
        overall = CLS_PLAINTEXT_UNAVAILABLE
    elif any(s["classification"] == CLS_REPLAYABLE_ENCRYPTED for s in sample_rows):
        overall = CLS_REPLAYABLE_ENCRYPTED
    else:
        overall = CLS_REPLAYABLE_PLAINTEXT

    availability_summary: dict[str, dict] = {}
    for rec in record_rows:
        cat = availability_summary.setdefault(
            rec["category"], {"records": 0, "plaintext": 0, "encrypted_copy": 0,
                              "encrypted_locked": 0, "digest_only": 0, "destroyed": 0,
                              "evidence_chain_broken": 0}
        )
        cat["records"] += 1
        cat[rec["state"]] = cat.get(rec["state"], 0) + 1

    # environment fingerprint comparison
    env_now = gov.environment_fingerprint(db)
    env_match = env_now["fingerprint"] == package.environment.get("fingerprint")
    if not env_match:
        findings.append({
            "scope": "environment",
            "kind": "environment_drift",
            "detail": "执行环境指纹与封存时不一致，重放需在受控环境完成",
        })

    return {
        "package_key": package.package_key,
        "status": package.status,
        "verified_at": gov.iso(now),
        "overall_classification": overall,
        "overall_label": CLASSIFICATION_LABELS[overall],
        "replayable": replayable,
        "manifest_checksum_ok": checksum_ok,
        "manifest_digest_ok": manifest_digest_ok,
        "manifest_digest": package.manifest_digest,
        "digest_migration": digest_migration,
        "environment": {"sealed": package.environment, "current_fingerprint": env_now["fingerprint"],
                        "match": env_match},
        "availability": availability_summary,
        "records": record_rows,
        "samples": sample_rows,
        "findings": findings,
        "affected_scores": sorted(affected.values(), key=lambda a: a["sample_key"]),
        "approvals": approval_state(db, package.id),
    }


# ---------------------------------------------------------------------------
# Controlled replay from captured evidence
# ---------------------------------------------------------------------------
def _replay_feature_value(feature: Feature, payloads: list[dict], entity_id: str,
                          decision_time: datetime):
    if feature.source == "account_profile":
        candidates = [
            p for p in payloads
            if p.get("kind") == "account_profile" and p.get("account_id") == entity_id
            and datetime.fromisoformat(p["snapshot_date"]).date() <= decision_time.date()
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p["snapshot_date"])
        return latest.get(feature.field)
    kind = "label" if feature.source == "labels" else "transaction"
    start, end = window_bounds(decision_time, feature.window_seconds or 0)
    selected = []
    for p in payloads:
        if p.get("kind") != kind or not p.get("event_time"):
            continue
        ts = _parse_time(p["event_time"])
        if not (ts > start and ts <= end):
            continue
        if feature.filter_json and any(p.get(k) != v for k, v in feature.filter_json.items()):
            continue
        selected.append(p)
    selected.sort(key=lambda p: (p["event_time"], p.get("txn_id") or p.get("label_id") or ""))
    return aggregate_values(
        [(float(p["amount"]) if p.get("amount") is not None else None) for p in selected],
        feature.aggregation,
    )


def _feature_from_definition(spec: dict, name: str, version: int) -> Feature:
    """Reconstruct the frozen feature definition without reading mutable
    catalog state (fall back to the DB record only for non-computational
    metadata such as status)."""
    feature = Feature(
        name=name,
        version=version,
        source=spec["source"],
        aggregation=spec["aggregation"],
        field=spec["field"],
        window_seconds=spec["window_seconds"],
        filter_json=spec["filter_json"],
        value_type=spec.get("value_type", "number"),
        owner="replay",
        status="published",
    )
    return feature


def replay_package(db: Session, package: ReplayPackage, actor: str,
                   require_approval: bool = True) -> dict:
    """Recompute every sample from captured evidence and compare with the sealed
    result. Requires an approved `replay` scope. Never reuses live compute
    state, so a changed source cannot leak into the reproduced value."""
    if package.status != "sealed":
        raise ReplayError("package is not sealed; nothing to replay", "not_sealed")
    if require_approval and not has_approval(db, package.id, "replay"):
        raise ReplayError("replay requires an approved replay approval", "approval_required")
    verify = verify_package(db, package)
    if verify["overall_classification"] == CLS_EVIDENCE_BROKEN:
        raise ReplayError("evidence chain is broken; controlled replay is refused", "evidence_broken")
    if not verify["replayable"]:
        raise ReplayError("package is not replayable: plaintext unavailable for every sample",
                          "not_replayable")

    now = gov.utcnow()
    alg = package.digest_algorithm
    manifest = package.manifest
    records = {r.record_key: r for r in _records_of(db, package)}
    payloads_by_key: dict[str, dict] = {}
    unresolved: list[str] = []
    for key, rec in records.items():
        payload, _src = _usable_plaintext(db, rec, now)
        if payload is None:
            unresolved.append(key)
        else:
            payloads_by_key[key] = payload

    rows = []
    all_consistent = True
    any_replayed = False
    for sample in manifest["samples"]:
        needed = {
            inp["record_key"]
            for f in sample["features"] for inp in f["inputs"]
        }
        missing = sorted(k for k in needed if k not in payloads_by_key)
        if missing:
            rows.append({
                "sample_key": sample["sample_key"],
                "replayed": False,
                "status": "not_replayable",
                "classification": CLS_PLAINTEXT_UNAVAILABLE,
                "reason": "原文不可用，无法复算",
                "missing_records": missing,
                "sealed_score": sample["score"],
            })
            all_consistent = False
            continue

        payloads = [payloads_by_key[k] for k in needed]
        values = {}
        for frozen in sample["features"]:
            definition = manifest["feature_set"]["features"][f"{frozen['name']}@v{frozen['version']}"]
            feature = _feature_from_definition(definition, frozen["name"], frozen["version"])
            value = _replay_feature_value(
                feature, payloads, sample["entity_id"], _parse_time(sample["decision_time"])
            )
            values[frozen["name"]] = {
                "version": frozen["version"],
                "value": value,
                "sealed_value": frozen["value"],
                "match": _values_equal(value, frozen["value"]),
            }
        score = score_row(manifest["model"]["spec"],
                          {k: v["value"] for k, v in values.items() if v["value"] is not None})
        digest_payload = {
            "sample_key": sample["sample_key"],
            "values": {k: v["value"] for k, v in values.items()},
            "score": score["value"],
            "decision": score["decision"],
        }
        result_digest = gov.digest_obj(alg, digest_payload)
        consistent = (
            all(v["match"] for v in values.values())
            and score["value"] == sample["score"]["value"]
            and score["decision"] == sample["score"]["decision"]
        )
        all_consistent = all_consistent and consistent
        any_replayed = True
        rows.append({
            "sample_key": sample["sample_key"],
            "replayed": True,
            "status": "consistent" if consistent else "mismatch",
            "classification": "reproduced",
            "features": values,
            "sealed_score": sample["score"],
            "replayed_score": score,
            "result_digest": result_digest,
            "environment_match": verify["environment"]["match"],
        })

    run_number = db.execute(
        select(func.count(ReplayRun.id)).where(ReplayRun.package_id == package.id)
    ).scalar_one() + 1
    status = "consistent" if all_consistent and any_replayed else (
        "mismatch" if any_replayed else "not_replayable"
    )
    classification = (
        CLS_EVIDENCE_BROKEN if status == "mismatch"
        else CLS_PLAINTEXT_UNAVAILABLE if not any_replayed
        else verify["overall_classification"]
    )
    run = ReplayRun(
        package_id=package.id,
        run_number=run_number,
        status=status,
        classification=classification,
        result_digest=gov.digest_obj(alg, [r.get("result_digest") for r in rows]),
        environment_match=verify["environment"]["match"],
        details={"rows": rows, "unresolved_records": unresolved},
        affected_scores=[{"sample_key": r["sample_key"], "sealed_score": r.get("sealed_score")}
                         for r in rows if r["status"] != "consistent"],
        executed_by=actor,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    gov.record_audit_query(
        db, action="replay", actor=actor, package_id=package.id,
        summary={"run_number": run.run_number, "status": status,
                 "classification": classification, "result_digest": run.result_digest},
    )
    return {
        "run": {
            "id": run.id,
            "run_number": run.run_number,
            "status": status,
            "classification": classification,
            "classification_label": CLASSIFICATION_LABELS.get(classification, classification),
            "result_digest": run.result_digest,
            "environment_match": run.environment_match,
            "executed_by": run.executed_by,
            "created_at": gov.iso(run.created_at),
        },
        "rows": rows,
        "environment_match": verify["environment"]["match"],
        "classification": classification,
        "classification_label": CLASSIFICATION_LABELS.get(classification, classification),
    }


def _values_equal(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(float(a), 6) == round(float(b), 6)
    return a == b


# ---------------------------------------------------------------------------
# Minimal disclosure
# ---------------------------------------------------------------------------
def reveal_records(db: Session, package: ReplayPackage, actor: str,
                   category: str | None = None) -> dict:
    """Return plaintext fields under an approved `plaintext` scope. Purged or
    locked records are reported as unavailable — never reconstructed."""
    if not has_approval(db, package.id, "plaintext"):
        raise ReplayError("plaintext access requires an approved plaintext approval",
                          "approval_required")
    now = gov.utcnow()
    out = []
    for rec in _records_of(db, package):
        if category and rec.category != category:
            continue
        payload, src = _usable_plaintext(db, rec, now)
        if payload is None:
            out.append({
                "record_key": rec.record_key,
                "category": rec.category,
                "available": False,
                "reason": "原件已按保留策略清除，仅保留可验证摘要"
                if src == "none" else "加密副本当前不可用（密钥撤销或解密失败）",
                "digest": rec.payload_digest,
                "plaintext_until": gov.iso(rec.plaintext_until),
            })
            continue
        out.append({
            "record_key": rec.record_key,
            "category": rec.category,
            "available": True,
            "source": src,
            "fields": gov.payload_fields(payload),
            "digest": rec.payload_digest,
            "plaintext_until": gov.iso(rec.plaintext_until),
        })
    gov.record_audit_query(
        db, action="reveal", actor=actor, package_id=package.id,
        summary={"category": category, "records": len(out),
                 "unavailable": sum(1 for r in out if not r["available"])},
    )
    return {"package_key": package.package_key, "records": out}


# ---------------------------------------------------------------------------
# Dependencies (six stateful holders)
# ---------------------------------------------------------------------------
def package_dependencies(db: Session, package: ReplayPackage) -> dict:
    manifest = package.manifest or {}
    batch = db.get(ModelBatch, package.batch_id)
    model = db.get(ModelVersion, batch.model_version_id)
    fsv = db.get(FeatureSetVersion, batch.feature_set_version_id)
    policy = db.get(RetentionPolicy, package.policy_version_id) if package.policy_version_id else None

    sweeps = db.execute(select(RetentionSweep)).scalars().all()
    affected_sweeps = [
        {
            "id": s.id, "as_of": gov.iso(s.as_of), "status": s.status,
            "records_expired": s.records_expired, "archives_destroyed": s.archives_destroyed,
        }
        for s in sweeps if (s.details or {}) and package.sealed_at and s.as_of >= package.sealed_at
    ]
    queries = db.execute(
        select(AuditQuery).where(AuditQuery.package_id == package.id).order_by(AuditQuery.id)
    ).scalars().all()
    keys = sorted({r.key_version_id for r in _records_of(db, package) if r.key_version_id})
    key_rows = [db.get(KeyVersion, kid) for kid in keys if db.get(KeyVersion, kid)]
    return {
        "package_key": package.package_key,
        "holders": {
            "data_retention": {
                "status": policy.status if policy else "unknown",
                "policy_version": manifest.get("policy", {}).get("version"),
                "policy_digest": manifest.get("policy", {}).get("rules_digest"),
                "digest_algorithm": package.digest_algorithm,
                "categories": manifest.get("data_scope", {}).get("categories"),
                "sweeps_after_seal": affected_sweeps,
            },
            "feature_compute": {
                "status": "frozen",
                "engine_version": manifest.get("feature_set", {}).get("engine_version"),
                "feature_set": {"name": fsv.name, "version": fsv.version, "id": fsv.id},
                "features": [
                    {"key": k, "digest": v["digest"]}
                    for k, v in manifest.get("feature_set", {}).get("features", {}).items()
                ],
                "compute_snapshot": batch.compute_snapshot,
            },
            "model_batch": {
                "status": batch.status,
                "batch_key": batch.batch_key,
                "input_digest": batch.input_digest,
                "frozen_at": gov.iso(batch.frozen_at),
                "model": {"name": model.name, "version": model.version, "digest": model.digest},
                "sample_count": len(batch.sample_ids),
            },
            "evidence_packaging": {
                "status": package.status,
                "sealed_at": gov.iso(package.sealed_at),
                "manifest_digest": package.manifest_digest,
                "checksum": (manifest.get("integrity") or {}).get("checksum"),
                "record_count": (manifest.get("integrity") or {}).get("record_count"),
                "keys": [
                    {"version": k.version, "status": k.status, "algorithm": k.algorithm}
                    for k in key_rows
                ],
            },
            "approval": {
                "status": "approved" if has_approval(db, package.id, "replay") else "pending",
                "items": approval_state(db, package.id),
            },
            "audit_query": {
                "status": "completed" if any(q.status == "completed" for q in queries) else "running",
                "queries": [
                    {"id": q.id, "action": q.action, "status": q.status, "actor": q.actor,
                     "created_at": gov.iso(q.created_at), "completed_at": gov.iso(q.completed_at)}
                    for q in queries
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
def request_approval(db: Session, package: ReplayPackage, scope: str, actor: str,
                     reason: str) -> Approval:
    if scope not in ("replay", "plaintext"):
        raise ReplayError("scope must be replay or plaintext", "bad_request")
    if package.status != "sealed":
        raise ReplayError("approvals are only for sealed packages", "not_sealed")
    pending = db.execute(
        select(Approval).where(Approval.package_id == package.id, Approval.scope == scope,
                               Approval.status == "requested")
    ).scalar_one_or_none()
    if pending:
        raise ReplayError("an approval for this scope is already pending", "conflict")
    approval = Approval(package_id=package.id, scope=scope, status="requested",
                        requested_by=actor, reason=reason)
    db.add(approval)
    db.commit()
    db.refresh(approval)
    gov.record_audit_query(db, action="approval_request", actor=actor, package_id=package.id,
                           summary={"approval_id": approval.id, "scope": scope})
    return approval


def decide_approval(db: Session, approval: Approval, actor: str, actor_role: str,
                    approve: bool, note: str = "") -> Approval:
    if approval.status != "requested":
        raise ReplayError("approval already decided", "conflict")
    if actor_role != "admin":
        raise ReplayError("only an admin can decide approvals", "forbidden")
    if approval.requested_by == actor:
        raise ReplayError("the requester cannot approve their own request", "forbidden")
    approval.status = "approved" if approve else "rejected"
    approval.decided_by = actor
    approval.decided_at = gov.utcnow()
    approval.note = note
    db.commit()
    db.refresh(approval)
    return approval
