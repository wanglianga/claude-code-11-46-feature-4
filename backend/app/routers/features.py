"""Feature catalog + publishing routes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Feature, FeatureSetMember, FeatureSetVersion, OnlinePointer, User
from ..schemas import FeatureSpecIn, PublishIn
from ..security import current_user
from ..services import features as feature_service

router = APIRouter(prefix="/api", tags=["features"])


def _feature_out(f: Feature) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "version": f.version,
        "description": f.description,
        "owner": f.owner,
        "entity_key": f.entity_key,
        "source": f.source,
        "aggregation": f.aggregation,
        "field": f.field,
        "window_seconds": f.window_seconds,
        "filter_json": f.filter_json,
        "value_type": f.value_type,
        "status": f.status,
        "created_by": f.created_by,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


@router.get("/features")
def list_features(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(Feature).order_by(Feature.name, Feature.version)).scalars().all()
    return [_feature_out(f) for f in rows]


@router.post("/features", status_code=201)
def create_feature(
    body: FeatureSpecIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        feature = feature_service.create_feature(
            db, body.model_dump(), created_by=user.username
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _feature_out(feature)


@router.get("/feature-sets")
def list_feature_sets(db: Session = Depends(get_db), user: User = Depends(current_user)):
    versions = db.execute(
        select(FeatureSetVersion).order_by(FeatureSetVersion.name, FeatureSetVersion.version)
    ).scalars().all()
    pointers = {p.name: p.set_version_id for p in db.execute(select(OnlinePointer)).scalars()}
    out = []
    for v in versions:
        members = db.execute(
            select(Feature)
            .join(FeatureSetMember, FeatureSetMember.feature_id == Feature.id)
            .where(FeatureSetMember.set_version_id == v.id)
            .order_by(FeatureSetMember.position)
        ).scalars().all()
        out.append(
            {
                "id": v.id,
                "name": v.name,
                "version": v.version,
                "note": v.note,
                "status": v.status,
                "created_by": v.created_by,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "is_current": pointers.get(v.name) == v.id,
                "members": [{"name": f.name, "version": f.version} for f in members],
            }
        )
    return out


@router.post("/feature-sets/{set_name}/publish", status_code=201)
def publish_feature_set(
    set_name: str,
    body: PublishIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        fsv = feature_service.publish_feature_set(
            db,
            set_name,
            [m.model_dump() for m in body.members],
            body.note,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": fsv.id, "name": fsv.name, "version": fsv.version, "status": fsv.status}
