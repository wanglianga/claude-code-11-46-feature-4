"""Streaming window computation engine.

The same `compute_feature_value` function backs BOTH the online serving path
and the offline training replay, which is what keeps the two calibers
(口径) identical by construction.

Window semantics
----------------
* A windowed feature value at as_of T aggregates contributions with
  `event_time in (T - W, T]`  (left-open, right-closed).
* Events exactly at T are inside; events exactly at T - W are outside.
* Membership is a pure function of event_time, so arrival order never changes
  the result; events sharing the same event_time are processed in stable
  (event_time, event_id) order, making checkpoint creation deterministic.
* Empty windows: COUNT/SUM -> 0 (a real measurement, not a placeholder),
  AVG/MIN/MAX -> null.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AccountProfile,
    Checkpoint,
    Feature,
    Revision,
    SourceStatus,
    Watermark,
    WindowEntry,
)

# Sources whose events flow through the streaming window engine.
STREAMED_SOURCES = ("transactions", "labels")


class SourceUnavailable(Exception):
    """Raised when a feature's upstream source is missing or marked down."""

    def __init__(self, source: str, status: str, last_success_at, message: str):
        self.source = source
        self.status = status
        self.last_success_at = last_success_at
        self.message = message
        super().__init__(f"source {source} is {status}: {message}")


def get_source_status(db: Session, source: str) -> SourceStatus | None:
    return db.get(SourceStatus, source)


def mark_source_success(db: Session, source: str, at: datetime | None = None) -> None:
    row = db.get(SourceStatus, source)
    if row is None:
        row = SourceStatus(source=source, status="ok")
        db.add(row)
    row.status = "ok"
    row.last_success_at = at or datetime.now(timezone.utc)
    row.message = ""


def window_bounds(as_of: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    """(start, end] with start = as_of - W (exclusive), end = as_of (inclusive)."""
    return as_of - timedelta(seconds=window_seconds), as_of


def aggregate_values(amounts: list, aggregation: str) -> float | int | None:
    """Aggregate pre-selected contribution amounts with the same semantics as
    the live window engine. Shared by serving and evidence replay so a
    reproduced value uses the identical arithmetic."""
    if aggregation == "COUNT":
        return len(amounts)
    values = [float(a) for a in amounts if a is not None]
    if not values:
        return 0 if aggregation == "SUM" else None
    if aggregation == "SUM":
        return sum(values)
    if aggregation == "AVG":
        return sum(values) / len(values)
    if aggregation == "MIN":
        return min(values)
    if aggregation == "MAX":
        return max(values)
    raise ValueError(f"unsupported aggregation {aggregation}")


def _aggregate(entries: list[WindowEntry], aggregation: str) -> float | int | None:
    # SUM of an empty window is honestly 0; AVG/MIN/MAX are undefined.
    return aggregate_values([e.amount for e in entries], aggregation)


def compute_feature_value(
    db: Session, feature: Feature, entity_id: str, as_of: datetime
) -> float | int | str | None:
    """Single source of truth for a feature value at an event-time point.

    Used by online serving, checkpoint materialization and training replay.
    Raises SourceUnavailable when the upstream feed cannot serve the read.
    """
    if feature.source == "account_profile":
        return _compute_profile_value(db, feature, entity_id, as_of)

    src = get_source_status(db, feature.source)
    if src is not None and src.status == "unavailable":
        raise SourceUnavailable(feature.source, src.status, src.last_success_at, src.message)

    start, end = window_bounds(as_of, feature.window_seconds or 0)
    entries = (
        db.execute(
            select(WindowEntry)
            .where(
                WindowEntry.feature_id == feature.id,
                WindowEntry.entity_id == entity_id,
                WindowEntry.event_time > start,
                WindowEntry.event_time <= end,
            )
            .order_by(WindowEntry.event_time, WindowEntry.event_id)
        )
        .scalars()
        .all()
    )
    return _aggregate(list(entries), feature.aggregation)


def _compute_profile_value(
    db: Session, feature: Feature, entity_id: str, as_of: datetime
) -> str | int | None:
    src = get_source_status(db, "account_profiles")
    if src is not None and src.status != "ok":
        raise SourceUnavailable("account_profiles", src.status, src.last_success_at, src.message)
    row = (
        db.execute(
            select(AccountProfile)
            .where(
                AccountProfile.account_id == entity_id,
                AccountProfile.snapshot_date <= as_of.date(),
            )
            .order_by(AccountProfile.snapshot_date.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        raise SourceUnavailable("account_profiles", "no_data", None, "no profile snapshot for entity")
    return getattr(row, feature.field)


def window_contributions(
    db: Session, feature: Feature, entity_id: str, as_of: datetime
) -> list[WindowEntry]:
    """The maintained-state entries contributing to the value at as_of.
    Used for training lineage (which input records produced the value)."""
    start, end = window_bounds(as_of, feature.window_seconds or 0)
    return list(
        db.execute(
            select(WindowEntry)
            .where(
                WindowEntry.feature_id == feature.id,
                WindowEntry.entity_id == entity_id,
                WindowEntry.event_time > start,
                WindowEntry.event_time <= end,
            )
            .order_by(WindowEntry.event_time, WindowEntry.event_id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Streaming state maintenance
# ---------------------------------------------------------------------------
def _get_watermark(db: Session, feature_id: int, entity_id: str) -> Watermark | None:
    return db.execute(
        select(Watermark).where(
            Watermark.feature_id == feature_id, Watermark.entity_id == entity_id
        )
    ).scalar_one_or_none()


def apply_event_to_feature(
    db: Session,
    feature: Feature,
    entity_id: str,
    event_id: str,
    event_time: datetime,
    amount,
) -> dict:
    """Apply one deduplicated event to one feature's maintained state.

    * Inserts the window contribution (idempotent via unique constraint).
    * Recomputes exactly the checkpoints whose windows contain the event:
      as_of in [event_time, event_time + W). On-time events only touch the
      checkpoint at their own boundary; late events additionally revise the
      historical checkpoints in (event_time, event_time + W) and each change
      is written to the revision log.
    * Advances the (feature, entity) watermark.

    Returns a small report dict used by the ingestion layer.
    """
    report = {"feature_id": feature.id, "late": False, "revisions": 0}

    wm = _get_watermark(db, feature.id, entity_id)
    is_late = wm is not None and event_time < wm.watermark
    report["late"] = is_late

    entry = WindowEntry(
        feature_id=feature.id,
        entity_id=entity_id,
        event_time=event_time,
        event_id=event_id,
        amount=amount,
    )
    # Idempotent insert via SAVEPOINT: a duplicate contribution must not abort
    # the surrounding ingestion transaction.
    try:
        with db.begin_nested():
            db.add(entry)
    except Exception:
        # Contribution already present -> nothing to do (defense in depth;
        # ingestion-level dedup normally catches duplicates first).
        return report

    window = timedelta(seconds=feature.window_seconds or 0)
    # Affected checkpoints: those whose window (as_of - W, as_of] contains the
    # event, i.e. as_of in [event_time, event_time + W).
    affected = list(
        db.execute(
            select(Checkpoint).where(
                Checkpoint.feature_id == feature.id,
                Checkpoint.entity_id == entity_id,
                Checkpoint.as_of >= event_time,
                Checkpoint.as_of < event_time + window,
            )
        )
        .scalars()
        .all()
    )
    affected_as_ofs = {c.as_of for c in affected}

    # Ensure a checkpoint exists at this event's own boundary.
    if event_time not in affected_as_ofs:
        cp = Checkpoint(
            feature_id=feature.id,
            entity_id=entity_id,
            as_of=event_time,
            value={"value": None},
        )
        db.add(cp)
        db.flush()
        affected.append(cp)

    for cp in affected:
        existed_before = cp.as_of in affected_as_ofs
        new_value = compute_feature_value(db, feature, entity_id, cp.as_of)
        new_payload = {"value": new_value}
        if cp.value != new_payload:
            if existed_before:
                # A previously materialized value changed -> audited revision.
                db.add(
                    Revision(
                        feature_id=feature.id,
                        entity_id=entity_id,
                        cause_event_id=event_id,
                        checkpoint_as_of=cp.as_of,
                        old_value=cp.value,
                        new_value=new_payload,
                    )
                )
                report["revisions"] += 1
            cp.value = new_payload
            cp.revision_seq += 1

    if wm is None:
        wm = Watermark(
            feature_id=feature.id,
            entity_id=entity_id,
            watermark=event_time,
            events_applied=0,
            late_events=0,
        )
        db.add(wm)
    wm.watermark = max(wm.watermark, event_time)
    wm.events_applied += 1
    if is_late:
        wm.late_events += 1
    # Flush so subsequent applies in the same batch read their own writes
    # (the session runs with autoflush disabled).
    db.flush()
    return report
