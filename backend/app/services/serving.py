"""Online feature serving.

Every request resolves the feature-set version ONCE (the atomic online
pointer, or an explicitly requested version) and then reads all features from
that single complete version — a scoring input is never assembled from fields
of two different releases, even while a publish is in flight.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Feature,
    FeatureSetMember,
    FeatureSetVersion,
    OnlinePointer,
    SourceStatus,
)
from .compute import SourceUnavailable, compute_feature_value


def resolve_set_version(
    db: Session, set_name: str, set_version: int | None
) -> FeatureSetVersion:
    """Pin the complete version for this request. Read exactly once."""
    if set_version is not None:
        row = db.execute(
            select(FeatureSetVersion).where(
                FeatureSetVersion.name == set_name,
                FeatureSetVersion.version == set_version,
            )
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"feature set {set_name} v{set_version} not found")
        return row
    pointer = db.get(OnlinePointer, set_name)
    if pointer is None:
        raise KeyError(f"feature set {set_name} has no published version")
    return db.get(FeatureSetVersion, pointer.set_version_id)


def _set_members(db: Session, set_version_id: int) -> list[Feature]:
    return list(
        db.execute(
            select(Feature)
            .join(FeatureSetMember, FeatureSetMember.feature_id == Feature.id)
            .where(FeatureSetMember.set_version_id == set_version_id)
            .order_by(FeatureSetMember.position)
        )
        .scalars()
        .all()
    )


def _source_snapshot(db: Session, source: str) -> dict:
    row = db.get(SourceStatus, source)
    if row is None:
        return {"source": source, "status": "unknown", "last_success_at": None}
    return {
        "source": source,
        "status": row.status,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "message": row.message,
    }


def read_online_features(
    db: Session,
    set_name: str,
    entity_id: str,
    as_of: datetime | None,
    set_version: int | None = None,
) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    version_row = resolve_set_version(db, set_name, set_version)
    features = _set_members(db, version_row.id)

    out_features = []
    sources_touched: dict[str, dict] = {}
    for feature in features:
        src_name = (
            "account_profiles" if feature.source == "account_profile" else feature.source
        )
        if src_name not in sources_touched:
            sources_touched[src_name] = _source_snapshot(db, src_name)
        item = {
            "name": feature.name,
            "version": feature.version,
            "as_of": as_of.isoformat(),
            "status": "ok",
            "value": None,
        }
        try:
            item["value"] = compute_feature_value(db, feature, entity_id, as_of)
        except SourceUnavailable as exc:
            # Missing upstream data is reported, never disguised as zero.
            item["status"] = "unavailable"
            item["value"] = None
            item["reason"] = exc.message
            item["source"] = {
                "source": exc.source,
                "status": exc.status,
                "last_success_at": exc.last_success_at.isoformat()
                if exc.last_success_at
                else None,
            }
        out_features.append(item)

    return {
        "feature_set": {"name": version_row.name, "version": version_row.version},
        "entity_id": entity_id,
        "as_of": as_of.isoformat(),
        "features": out_features,
        "sources": list(sources_touched.values()),
        "served_at": datetime.now(timezone.utc).isoformat(),
    }
