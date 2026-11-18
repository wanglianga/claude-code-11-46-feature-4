"""Simulation routes (local mock data) and source-status inspection."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SourceStatus, User
from ..schemas import (
    SimulateAccountsIn,
    SimulateLabelsIn,
    SimulateTransactionsIn,
    SourceOutageIn,
)
from ..security import current_user
from ..services import simulation

router = APIRouter(prefix="/api", tags=["simulation"])


@router.post("/simulate/accounts")
def simulate_accounts(
    body: SimulateAccountsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return simulation.simulate_accounts(db, body.accounts, body.days, body.seed)


@router.post("/simulate/transactions")
def simulate_transactions(
    body: SimulateTransactionsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return simulation.simulate_transactions(
        db,
        accounts=body.accounts,
        count=body.count,
        span_hours=body.span_hours,
        duplicate_rate=body.duplicate_rate,
        late_rate=body.late_rate,
        seed=body.seed,
    )


@router.post("/simulate/labels")
def simulate_labels(
    body: SimulateLabelsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return simulation.simulate_labels(
        db, body.fraud_rate, body.min_delay_days, body.max_delay_days, body.seed
    )


@router.post("/sources/outage")
def source_outage(
    body: SourceOutageIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return simulation.set_source_outage(db, body.source, body.status, body.message)


@router.get("/sources")
def list_sources(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(SourceStatus)).scalars().all()
    return [
        {
            "source": s.source,
            "status": s.status,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "message": s.message,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in rows
    ]
