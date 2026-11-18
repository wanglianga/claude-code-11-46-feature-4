"""Ingestion routes (transaction callbacks, label confirmations, account feeds)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import AccountProfileIn, LabelEventIn, TransactionEventIn
from ..security import current_user
from ..services import ingestion

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.post("/transactions")
def ingest_transactions(
    body: list[TransactionEventIn],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return ingestion.ingest_transactions(db, [e.model_dump() for e in body])


@router.post("/labels")
def ingest_labels(
    body: list[LabelEventIn],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return ingestion.ingest_labels(db, [e.model_dump() for e in body])


@router.post("/account-profiles")
def ingest_account_profiles(
    body: list[AccountProfileIn],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return ingestion.ingest_account_profiles(db, [e.model_dump() for e in body])
