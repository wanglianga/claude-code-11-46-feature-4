"""Auth routes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import LoginIn
from ..security import create_session, current_user, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid username or password")
    token = create_session(db, user)
    return {"token": token, "user": {"username": user.username, "role": user.role}}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role}
