"""Training sample management, point-in-time replay and lineage tracing.

Replay uses the very same `compute_feature_value` over the same maintained
state that online serving reads, so a training row and an online read at the
same as_of are consistent by construction. Late-arriving events revise the
maintained state once, and both readers observe the corrected values.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AccountProfile,
    Dataset,
    Feature,
    LabelEvent,
    Transaction,
    TrainingSample,
)
from .compute import SourceUnavailable, compute_feature_value, window_contributions
from .serving import _set_members, resolve_set_version


def create_sample(
    db: Session,
    sample_key: str,
    entity_id: str,
    decision_time: datetime,
    transaction_id: str | None,
    created_by: str = "system",
) -> TrainingSample:
    if decision_time.tzinfo is None:
        decision_time = decision_time.replace(tzinfo=timezone.utc)
    sample = TrainingSample(
        sample_key=sample_key,
        entity_id=entity_id,
        decision_time=decision_time,
        transaction_id=transaction_id,
        created_by=created_by,
    )
    # Attach the label if the outcome is already known; otherwise the sample
    # stays pending until the chargeback / review result arrives.
    if transaction_id:
        label = db.execute(
            select(LabelEvent)
            .where(LabelEvent.transaction_id == transaction_id)
            .order_by(LabelEvent.confirmed_at.desc())
            .limit(1)
        ).scalars().first()
        if label:
            sample.label_value = label.label_value
            sample.label_status = "confirmed"
        else:
            sample.label_status = "pending"
    else:
        sample.label_status = "none"
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def replay_features(
    db: Session, set_name: str, set_version: int | None, entity_id: str, decision_time: datetime
) -> list[dict]:
    """Point-in-time feature computation for one entity at decision_time."""
    version_row = resolve_set_version(db, set_name, set_version)
    features = _set_members(db, version_row.id)
    out = []
    for feature in features:
        item = {"name": feature.name, "version": feature.version, "status": "ok", "value": None}
        try:
            item["value"] = compute_feature_value(db, feature, entity_id, decision_time)
        except SourceUnavailable as exc:
            item["status"] = "unavailable"
            item["reason"] = exc.message
        out.append(item)
    return out


def trace_sample(
    db: Session, sample: TrainingSample, set_name: str, set_version: int | None
) -> dict:
    """Full lineage of one training sample: decision time, replayed feature
    values, and the exact input records each value was computed from."""
    version_row = resolve_set_version(db, set_name, set_version)
    features = _set_members(db, version_row.id)
    dt = sample.decision_time

    feature_traces = []
    for feature in features:
        trace = {
            "name": feature.name,
            "version": feature.version,
            "status": "ok",
            "value": None,
            "inputs": [],
        }
        try:
            trace["value"] = compute_feature_value(db, feature, sample.entity_id, dt)
        except SourceUnavailable as exc:
            trace["status"] = "unavailable"
            trace["reason"] = exc.message
            feature_traces.append(trace)
            continue

        if feature.source == "account_profile":
            profile = db.execute(
                select(AccountProfile)
                .where(
                    AccountProfile.account_id == sample.entity_id,
                    AccountProfile.snapshot_date <= dt.date(),
                )
                .order_by(AccountProfile.snapshot_date.desc())
                .limit(1)
            ).scalars().first()
            if profile:
                trace["inputs"] = [
                    {
                        "kind": "account_profile",
                        "account_id": profile.account_id,
                        "snapshot_date": profile.snapshot_date.isoformat(),
                        "kyc_level": profile.kyc_level,
                        "country": profile.country,
                        "risk_segment": profile.risk_segment,
                        "age_days": profile.age_days,
                    }
                ]
        else:
            for entry in window_contributions(db, feature, sample.entity_id, dt):
                if feature.source == "transactions":
                    txn = db.get(Transaction, entry.event_id)
                    if txn:
                        trace["inputs"].append(
                            {
                                "kind": "transaction",
                                "txn_id": txn.txn_id,
                                "account_id": txn.account_id,
                                "event_time": txn.event_time.isoformat(),
                                "amount": float(txn.amount),
                                "currency": txn.currency,
                                "type": txn.type,
                                "status": txn.status,
                                "merchant_id": txn.merchant_id,
                            }
                        )
                else:
                    label = db.get(LabelEvent, entry.event_id)
                    if label:
                        trace["inputs"].append(
                            {
                                "kind": "label",
                                "label_id": label.label_id,
                                "transaction_id": label.transaction_id,
                                "label_type": label.label_type,
                                "label_value": label.label_value,
                                "event_time": label.event_time.isoformat(),
                                "confirmed_at": label.confirmed_at.isoformat()
                                if label.confirmed_at
                                else None,
                            }
                        )
        feature_traces.append(trace)

    label_record = None
    if sample.transaction_id:
        label = db.execute(
            select(LabelEvent)
            .where(LabelEvent.transaction_id == sample.transaction_id)
            .order_by(LabelEvent.confirmed_at.desc())
            .limit(1)
        ).scalars().first()
        if label:
            label_record = {
                "label_id": label.label_id,
                "label_type": label.label_type,
                "label_value": label.label_value,
                "event_time": label.event_time.isoformat(),
                "confirmed_at": label.confirmed_at.isoformat() if label.confirmed_at else None,
            }

    return {
        "sample_id": sample.id,
        "sample_key": sample.sample_key,
        "entity_id": sample.entity_id,
        "decision_time": sample.decision_time.isoformat(),
        "transaction_id": sample.transaction_id,
        "label_status": sample.label_status,
        "label_value": sample.label_value,
        "label_record": label_record,
        "feature_set": {"name": version_row.name, "version": version_row.version},
        "features": feature_traces,
    }


def build_dataset(
    db: Session,
    name: str,
    set_name: str,
    set_version: int | None,
    sample_ids: list[int] | None,
    created_by: str = "system",
) -> Dataset:
    version_row = resolve_set_version(db, set_name, set_version)
    query = select(TrainingSample).order_by(TrainingSample.id)
    if sample_ids:
        query = query.where(TrainingSample.id.in_(sample_ids))
    samples = list(db.execute(query).scalars().all())

    rows = []
    for sample in samples:
        values = replay_features(db, set_name, version_row.version, sample.entity_id, sample.decision_time)
        rows.append(
            {
                "sample_id": sample.id,
                "sample_key": sample.sample_key,
                "entity_id": sample.entity_id,
                "decision_time": sample.decision_time.isoformat(),
                "label_value": sample.label_value,
                "label_status": sample.label_status,
                "features": {v["name"]: v["value"] for v in values},
                "feature_status": {v["name"]: v["status"] for v in values},
            }
        )

    dataset = Dataset(
        name=name,
        feature_set_name=version_row.name,
        feature_set_version=version_row.version,
        rows=rows,
        created_by=created_by,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset
