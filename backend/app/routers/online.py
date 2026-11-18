"""Online serving routes."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import serving

router = APIRouter(prefix="/api/online", tags=["online"])


@router.get("/features")
def online_features(
    entity_id: str,
    feature_set: str = "fraud_scoring",
    set_version: int | None = None,
    as_of: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        return serving.read_online_features(db, feature_set, entity_id, as_of, set_version)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
