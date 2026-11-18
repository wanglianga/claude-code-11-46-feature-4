"""Pytest configuration: isolated test database, fresh schema per test."""
import os

# Must be set before any app module is imported (config reads env at import).
os.environ["DATABASE_URL"] = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev_test"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import Base, SessionLocal, engine
from app.main import app, seed

ADMIN_URL = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev"


def _ensure_test_db():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'ccdev_test'")
        ).scalar()
        if not exists:
            conn.execute(text("CREATE DATABASE ccdev_test"))
    admin.dispose()


@pytest.fixture()
def client():
    _ensure_test_db()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    r = client.post("/api/auth/login", json={"username": "analyst", "password": "analyst123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def make_txn(txn_id, account, event_time, amount, **kw):
    payload = {
        "txn_id": txn_id,
        "account_id": account,
        "event_time": event_time,
        "amount": amount,
        "currency": "CNY",
        "type": "purchase",
        "status": "completed",
        "merchant_id": "M100",
    }
    payload.update(kw)
    return payload


def online(client, auth, entity, as_of, **params):
    q = {"entity_id": entity, "as_of": as_of, **params}
    r = client.get("/api/online/features", params=q, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def values_by_name(resp):
    return {f["name"]: f["value"] for f in resp["features"]}
