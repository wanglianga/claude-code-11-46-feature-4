"""Training routes: samples, lineage tracing, dataset building."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Dataset, TrainingSample, User
from ..schemas import DatasetIn, SampleIn
from ..security import current_user
from ..services import training

router = APIRouter(prefix="/api/training", tags=["training"])


def _sample_out(s: TrainingSample) -> dict:
    return {
        "id": s.id,
        "sample_key": s.sample_key,
        "entity_id": s.entity_id,
        "decision_time": s.decision_time.isoformat(),
        "transaction_id": s.transaction_id,
        "label_value": s.label_value,
        "label_status": s.label_status,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


@router.get("/samples")
def list_samples(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(
        select(TrainingSample).order_by(TrainingSample.id.desc())
    ).scalars().all()
    return [_sample_out(s) for s in rows]


@router.post("/samples", status_code=201)
def create_sample(
    body: SampleIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    existing = db.execute(
        select(TrainingSample).where(TrainingSample.sample_key == body.sample_key)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"sample_key {body.sample_key} already exists")
    sample = training.create_sample(
        db,
        sample_key=body.sample_key,
        entity_id=body.entity_id,
        decision_time=body.decision_time,
        transaction_id=body.transaction_id,
        created_by=user.username,
    )
    return _sample_out(sample)


@router.get("/samples/{sample_id}/trace")
def trace_sample(
    sample_id: int,
    feature_set: str = "fraud_scoring",
    set_version: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    sample = db.get(TrainingSample, sample_id)
    if sample is None:
        raise HTTPException(404, "sample not found")
    try:
        return training.trace_sample(db, sample, feature_set, set_version)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.get("/datasets")
def list_datasets(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(Dataset).order_by(Dataset.id.desc())).scalars().all()
    return [
        {
            "id": d.id,
            "name": d.name,
            "feature_set_name": d.feature_set_name,
            "feature_set_version": d.feature_set_version,
            "row_count": len(d.rows),
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


@router.post("/datasets", status_code=201)
def build_dataset(
    body: DatasetIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        dataset = training.build_dataset(
            db,
            name=body.name,
            set_name=body.feature_set,
            set_version=body.set_version,
            sample_ids=body.sample_ids,
            created_by=user.username,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {
        "id": dataset.id,
        "name": dataset.name,
        "feature_set_name": dataset.feature_set_name,
        "feature_set_version": dataset.feature_set_version,
        "row_count": len(dataset.rows),
    }


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(404, "dataset not found")
    return {
        "id": dataset.id,
        "name": dataset.name,
        "feature_set_name": dataset.feature_set_name,
        "feature_set_version": dataset.feature_set_version,
        "rows": dataset.rows,
        "created_by": dataset.created_by,
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
    }
