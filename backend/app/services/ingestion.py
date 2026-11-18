"""Event ingestion: idempotent intake of transactions, labels and account
profiles, and dispatch to the streaming compute engine.

Processing order equals arrival order (stream semantics). Within one request
batch, events sharing the same event_time are applied in stable
(event_time, event_id) order so checkpoint materialization is deterministic.
"""
from __future__ import annotations

from datetime import date, datetime, timezone


def _json_safe(payload: dict) -> dict:
    """Make an event payload JSONB-serializable (datetimes -> ISO strings)."""
    out = {}
    for k, v in payload.items():
        out[k] = v.isoformat() if isinstance(v, (datetime, date)) else v
    return out

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import (
    AccountProfile,
    Feature,
    LabelEvent,
    ProcessedEvent,
    Transaction,
)
from . import compute


def _record_duplicate(db: Session, source: str, event_id: str) -> None:
    """Count a repeated callback on its already-processed event."""
    db.execute(
        update(ProcessedEvent)
        .where(ProcessedEvent.source == source, ProcessedEvent.event_id == event_id)
        .values(duplicate_attempts=ProcessedEvent.duplicate_attempts + 1)
    )


def _active_streamed_features(db: Session, source: str) -> list[Feature]:
    """Features whose state is maintained for a given streamed source.
    Draft features are defined in the catalog but not yet computed."""
    return list(
        db.execute(
            select(Feature).where(
                Feature.source == source,
                Feature.status.in_(("published",)),
                Feature.window_seconds.isnot(None),
            )
        )
        .scalars()
        .all()
    )


def _passes_filter(feature: Feature, payload: dict) -> bool:
    if not feature.filter_json:
        return True
    return all(payload.get(k) == v for k, v in feature.filter_json.items())


def ingest_transactions(db: Session, events: list[dict]) -> dict:
    """Consume a batch of transaction callbacks.

    Duplicate transaction ids (repeated callbacks) are acknowledged but never
    applied twice, so window counts are unaffected.
    """
    stats = {"received": len(events), "applied": 0, "duplicates": 0, "late": 0, "details": []}
    features = _active_streamed_features(db, "transactions")

    for ev in events:
        txn_id = ev["txn_id"]
        event_time = ev["event_time"]
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        # --- idempotency gate -------------------------------------------------
        stmt = (
            pg_insert(ProcessedEvent)
            .values(
                source="transactions",
                event_id=txn_id,
                entity_id=ev["account_id"],
                event_time=event_time,
                payload=_json_safe(ev),
                status="applied",
                is_late=False,
            )
            .on_conflict_do_nothing(constraint="uq_processed_event")
            .returning(ProcessedEvent.id)
        )
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is None:
            _record_duplicate(db, "transactions", txn_id)
            stats["duplicates"] += 1
            stats["details"].append({"txn_id": txn_id, "status": "duplicate"})
            continue

        # --- store canonical record ------------------------------------------
        db.add(
            Transaction(
                txn_id=txn_id,
                account_id=ev["account_id"],
                event_time=event_time,
                amount=ev["amount"],
                currency=ev.get("currency", "CNY"),
                type=ev.get("type", "purchase"),
                status=ev.get("status", "completed"),
                merchant_id=ev.get("merchant_id", ""),
            )
        )

        # --- apply to maintained feature state --------------------------------
        late = False
        for feature in features:
            if not _passes_filter(feature, ev):
                continue
            amount = ev.get("amount") if feature.field == "amount" else None
            report = compute.apply_event_to_feature(
                db, feature, ev["account_id"], txn_id, event_time, amount
            )
            late = late or report["late"]
        if late:
            pe = db.execute(
                select(ProcessedEvent).where(
                    ProcessedEvent.source == "transactions",
                    ProcessedEvent.event_id == txn_id,
                )
            ).scalar_one()
            pe.status = "late_applied"
            pe.is_late = True
            stats["late"] += 1
        stats["applied"] += 1
        stats["details"].append(
            {"txn_id": txn_id, "status": "late_applied" if late else "applied"}
        )

    compute.mark_source_success(db, "transactions")
    db.commit()
    return stats


def ingest_labels(db: Session, events: list[dict]) -> dict:
    """Consume chargeback / manual-review outcomes. These typically confirm
    days after the transaction, so they are usually late and revise the
    label-sourced window features of exactly the affected account."""
    stats = {"received": len(events), "applied": 0, "duplicates": 0, "late": 0, "details": []}
    features = _active_streamed_features(db, "labels")

    for ev in events:
        label_id = ev["label_id"]
        event_time = ev["event_time"]
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        stmt = (
            pg_insert(ProcessedEvent)
            .values(
                source="labels",
                event_id=label_id,
                entity_id=ev["account_id"],
                event_time=event_time,
                payload=_json_safe(ev),
                status="applied",
                is_late=False,
            )
            .on_conflict_do_nothing(constraint="uq_processed_event")
            .returning(ProcessedEvent.id)
        )
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is None:
            _record_duplicate(db, "labels", label_id)
            stats["duplicates"] += 1
            stats["details"].append({"label_id": label_id, "status": "duplicate"})
            continue

        db.add(
            LabelEvent(
                label_id=label_id,
                transaction_id=ev["transaction_id"],
                account_id=ev["account_id"],
                label_type=ev["label_type"],
                label_value=ev["label_value"],
                event_time=event_time,
            )
        )

        late = False
        for feature in features:
            if feature.filter_json and feature.filter_json.get("label_type") not in (
                None,
                ev["label_type"],
            ):
                continue
            report = compute.apply_event_to_feature(
                db, feature, ev["account_id"], label_id, event_time, None
            )
            late = late or report["late"]
        if late:
            pe = db.execute(
                select(ProcessedEvent).where(
                    ProcessedEvent.source == "labels", ProcessedEvent.event_id == label_id
                )
            ).scalar_one()
            pe.status = "late_applied"
            pe.is_late = True
            stats["late"] += 1
        stats["applied"] += 1
        stats["details"].append(
            {"label_id": label_id, "status": "late_applied" if late else "applied"}
        )

        # Resolve any training sample waiting on this transaction's outcome.
        from ..models import TrainingSample

        samples = db.execute(
            select(TrainingSample).where(
                TrainingSample.transaction_id == ev["transaction_id"]
            )
        ).scalars().all()
        for sample in samples:
            sample.label_value = ev["label_value"]
            sample.label_status = "confirmed"

    compute.mark_source_success(db, "labels")
    db.commit()
    return stats


def ingest_account_profiles(db: Session, rows: list[dict]) -> dict:
    """Daily account snapshot upsert (idempotent per account+date)."""
    applied = 0
    for row in rows:
        stmt = (
            pg_insert(AccountProfile)
            .values(
                account_id=row["account_id"],
                snapshot_date=row["snapshot_date"],
                kyc_level=row.get("kyc_level", "unknown"),
                country=row.get("country", ""),
                risk_segment=row.get("risk_segment", ""),
                age_days=row.get("age_days", 0),
            )
            .on_conflict_do_update(
                constraint="uq_profile_day",
                set_={
                    "kyc_level": row.get("kyc_level", "unknown"),
                    "country": row.get("country", ""),
                    "risk_segment": row.get("risk_segment", ""),
                    "age_days": row.get("age_days", 0),
                },
            )
        )
        db.execute(stmt)
        applied += 1
    compute.mark_source_success(db, "account_profiles")
    db.commit()
    return {"received": len(rows), "applied": applied}
