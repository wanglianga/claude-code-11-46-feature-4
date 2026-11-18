"""Compute-status routes: watermarks, event counters, revision audit."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Checkpoint,
    Feature,
    ProcessedEvent,
    Revision,
    SourceStatus,
    User,
    Watermark,
)
from ..security import current_user

router = APIRouter(prefix="/api/compute", tags=["compute"])


@router.get("/status")
def compute_status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    features = db.execute(
        select(Feature).where(Feature.status.in_(("published", "deprecated")))
    ).scalars().all()
    per_feature = []
    for f in features:
        wms = db.execute(select(Watermark).where(Watermark.feature_id == f.id)).scalars().all()
        checkpoints = db.execute(
            select(func.count(Checkpoint.id)).where(Checkpoint.feature_id == f.id)
        ).scalar_one()
        revisions = db.execute(
            select(func.count(Revision.id)).where(Revision.feature_id == f.id)
        ).scalar_one()
        per_feature.append(
            {
                "feature_id": f.id,
                "name": f.name,
                "version": f.version,
                "source": f.source,
                "status": f.status,
                "entities": len(wms),
                "events_applied": sum(w.events_applied for w in wms),
                "late_events": sum(w.late_events for w in wms),
                "max_watermark": max((w.watermark for w in wms), default=None),
                "checkpoints": checkpoints,
                "revisions": revisions,
            }
        )
    for item in per_feature:
        if item["max_watermark"] is not None:
            item["max_watermark"] = item["max_watermark"].isoformat()

    event_counts = {}
    for source in ("transactions", "labels"):
        rows = db.execute(
            select(ProcessedEvent.status, func.count())
            .where(ProcessedEvent.source == source)
            .group_by(ProcessedEvent.status)
        ).all()
        counts = {status: count for status, count in rows}
        duplicates = db.execute(
            select(func.coalesce(func.sum(ProcessedEvent.duplicate_attempts), 0)).where(
                ProcessedEvent.source == source
            )
        ).scalar_one()
        counts["duplicate"] = duplicates
        event_counts[source] = counts

    sources = [
        {
            "source": s.source,
            "status": s.status,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "message": s.message,
        }
        for s in db.execute(select(SourceStatus)).scalars()
    ]
    return {"features": per_feature, "events": event_counts, "sources": sources}


@router.get("/watermarks")
def watermarks(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(
        select(Watermark, Feature)
        .join(Feature, Feature.id == Watermark.feature_id)
        .order_by(Feature.name, Watermark.entity_id)
    ).all()
    return [
        {
            "feature": f.name,
            "feature_version": f.version,
            "entity_id": w.entity_id,
            "watermark": w.watermark.isoformat(),
            "events_applied": w.events_applied,
            "late_events": w.late_events,
            "last_applied_at": w.last_applied_at.isoformat() if w.last_applied_at else None,
        }
        for w, f in rows
    ]


@router.get("/revisions")
def revisions(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    rows = db.execute(
        select(Revision, Feature)
        .join(Feature, Feature.id == Revision.feature_id)
        .order_by(Revision.id.desc())
        .limit(min(limit, 500))
    ).all()
    return [
        {
            "id": r.id,
            "feature": f.name,
            "feature_version": f.version,
            "entity_id": r.entity_id,
            "cause_event_id": r.cause_event_id,
            "checkpoint_as_of": r.checkpoint_as_of.isoformat(),
            "old_value": r.old_value,
            "new_value": r.new_value,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r, f in rows
    ]
