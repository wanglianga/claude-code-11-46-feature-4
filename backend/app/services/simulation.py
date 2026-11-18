"""Local data simulation: transactions (with duplicates / late arrivals /
cross-day spread), daily account profiles, and delayed chargeback labels."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SourceStatus, Transaction
from . import ingestion


def simulate_accounts(db: Session, accounts: int = 8, days: int = 7, seed: int = 42) -> dict:
    rng = random.Random(seed)
    rows = []
    today = datetime.now(timezone.utc).date()
    for i in range(accounts):
        account_id = f"ACC{i + 1:03d}"
        opened_days_ago = rng.randint(30, 2000)
        for d in range(days):
            day = today - timedelta(days=d)
            rows.append(
                {
                    "account_id": account_id,
                    "snapshot_date": day,
                    "kyc_level": rng.choice(["basic", "standard", "enhanced"]),
                    "country": rng.choice(["CN", "CN", "CN", "SG", "US"]),
                    "risk_segment": rng.choice(["low", "medium", "high"]),
                    "age_days": opened_days_ago + (today - day).days,
                }
            )
    result = ingestion.ingest_account_profiles(db, rows)
    result["accounts"] = [f"ACC{i + 1:03d}" for i in range(accounts)]
    return result


def simulate_transactions(
    db: Session,
    accounts: list[str] | None = None,
    count: int = 60,
    span_hours: int = 48,
    duplicate_rate: float = 0.15,
    late_rate: float = 0.15,
    seed: int = 42,
) -> dict:
    """Generate transactions spread across day boundaries. A fraction of the
    callbacks is sent twice (duplicate), and a fraction is sent only after
    newer events for the same account (late arrival)."""
    rng = random.Random(seed)
    accounts = accounts or [f"ACC{i + 1:03d}" for i in range(8)]
    now = datetime.now(timezone.utc).replace(microsecond=0)

    base_events = []
    for i in range(count):
        account = rng.choice(accounts)
        event_time = now - timedelta(seconds=rng.randint(60, span_hours * 3600))
        base_events.append(
            {
                "txn_id": f"TXN-{seed}-{i:05d}",
                "account_id": account,
                "event_time": event_time,
                "amount": round(rng.uniform(1, 5000), 2),
                "currency": "CNY",
                "type": rng.choice(["purchase", "purchase", "transfer", "withdrawal"]),
                "status": "completed",
                "merchant_id": f"M{rng.randint(100, 120)}",
            }
        )

    # Order by event_time so most events are on-time, then shuffle in some
    # late arrivals: hold back old events and deliver them after newer ones.
    base_events.sort(key=lambda e: e["event_time"])
    stream: list[dict] = []
    held_back: list[dict] = []
    for ev in base_events:
        if rng.random() < late_rate:
            held_back.append(ev)
        else:
            stream.append(ev)
    stream.extend(held_back)  # late deliveries

    # Duplicate callbacks: re-send some events exactly as they were.
    duplicates = [dict(ev) for ev in stream if rng.random() < duplicate_rate]

    stats = ingestion.ingest_transactions(db, stream)
    dup_stats = ingestion.ingest_transactions(db, duplicates) if duplicates else {"duplicates": 0}
    stats["duplicate_callbacks_resent"] = len(duplicates)
    stats["duplicate_callbacks_detected"] = dup_stats.get("duplicates", 0)
    stats["held_back_late_events"] = len(held_back)
    return stats


def simulate_labels(
    db: Session,
    fraud_rate: float = 0.12,
    min_delay_days: int = 1,
    max_delay_days: int = 5,
    seed: int = 42,
) -> dict:
    """Confirm chargebacks / manual reviews for a subset of past transactions,
    effective days after the transaction (i.e. they arrive late)."""
    rng = random.Random(seed)
    txns = list(db.execute(select(Transaction)).scalars().all())
    if not txns:
        return {"applied": 0, "message": "no transactions to label"}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    events = []
    for txn in txns:
        if rng.random() >= fraud_rate:
            continue
        delay = timedelta(days=rng.randint(min_delay_days, max_delay_days))
        effective = min(txn.event_time + delay, now)
        events.append(
            {
                "label_id": f"LBL-{txn.txn_id}",
                "transaction_id": txn.txn_id,
                "account_id": txn.account_id,
                "label_type": rng.choice(["chargeback", "manual_review"]),
                "label_value": "fraud" if rng.random() < 0.8 else "legit",
                "event_time": effective,
            }
        )
    stats = ingestion.ingest_labels(db, events)
    stats["candidates"] = len(txns)
    return stats


def set_source_outage(db: Session, source: str, status: str, message: str = "") -> dict:
    row = db.get(SourceStatus, source)
    if row is None:
        row = SourceStatus(source=source)
        db.add(row)
    row.status = status
    row.message = message
    db.commit()
    return {
        "source": row.source,
        "status": row.status,
        "message": row.message,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
    }
