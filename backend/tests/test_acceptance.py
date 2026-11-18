"""Acceptance tests for the anti-fraud feature platform.

Covers the acceptance criteria from the task:
* duplicate transaction callbacks must not inflate window counts
* transactions with identical event_time get deterministic window membership
  (stable-key ordering, half-open windows)
* cross-day-boundary windows
* late events revise only the affected account and windows
* delayed chargeback results resolve training labels and revise features
* publishing a feature release mid-traffic never mixes two versions
* offline training values and online reads at the same as_of are consistent
* unavailable upstream account data is reported, never disguised as zero
* training samples trace back to decision time, values and input records
"""
import threading
from datetime import datetime, timezone

from app.db import SessionLocal
from app.services import serving
from app.services.features import publish_feature_set

from .conftest import make_txn, online, values_by_name

T0 = "2026-11-17T10:00:00+00:00"
ACC = "ACC901"


def _ingest(client, auth, events):
    r = client.post("/api/ingest/transactions", json=events, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Duplicate callbacks
# ---------------------------------------------------------------------------
def test_duplicate_callback_does_not_inflate_window(client, auth):
    ev = make_txn("TXN-DUP-1", ACC, T0, 100.0)
    _ingest(client, auth, [ev])
    stats = _ingest(client, auth, [ev, ev])  # repeated callback, twice in one batch
    assert stats["duplicates"] == 2
    assert stats["applied"] == 0

    resp = online(client, auth, ACC, "2026-11-17T10:30:00+00:00")
    vals = values_by_name(resp)
    assert vals["txn_count_1h"] == 1
    assert vals["txn_amount_sum_1h"] == 100.0

    status = client.get("/api/compute/status", headers=auth).json()
    assert status["events"]["transactions"].get("duplicate", 0) == 2
    assert status["events"]["transactions"].get("applied", 0) == 1


# ---------------------------------------------------------------------------
# 2. Same event_time -> deterministic membership regardless of arrival order
# ---------------------------------------------------------------------------
def test_same_event_time_order_independent_and_boundary_rules(client, auth):
    a = make_txn("TXN-SAME-A", ACC, T0, 10.0)
    b = make_txn("TXN-SAME-B", ACC, T0, 20.0)

    _ingest(client, auth, [a, b])
    first = values_by_name(online(client, auth, ACC, "2026-11-17T10:30:00+00:00"))

    # Reset state by using a different account for the reversed arrival order.
    acc2 = "ACC902"
    a2 = make_txn("TXN-SAME-A2", acc2, T0, 10.0)
    b2 = make_txn("TXN-SAME-B2", acc2, T0, 20.0)
    _ingest(client, auth, [b2, a2])  # reversed arrival order
    second = values_by_name(online(client, auth, acc2, "2026-11-17T10:30:00+00:00"))

    assert first["txn_count_1h"] == second["txn_count_1h"] == 2
    assert first["txn_amount_sum_1h"] == second["txn_amount_sum_1h"] == 30.0

    # Half-open window (as_of - 1h, as_of]: events exactly at the left edge are
    # excluded, events exactly at as_of are included — pure event_time semantics.
    assert values_by_name(online(client, auth, ACC, T0))["txn_count_1h"] == 2
    assert values_by_name(online(client, auth, ACC, "2026-11-17T11:00:00+00:00"))["txn_count_1h"] == 0


# ---------------------------------------------------------------------------
# 3. Cross-day boundary
# ---------------------------------------------------------------------------
def test_cross_day_boundary_window(client, auth):
    _ingest(client, auth, [
        make_txn("TXN-DAY-1", ACC, "2026-11-17T23:45:00+00:00", 50.0),
        make_txn("TXN-DAY-2", ACC, "2026-11-18T00:15:00+00:00", 75.0),
    ])
    vals = values_by_name(online(client, auth, ACC, "2026-11-18T00:30:00+00:00"))
    # Window (23:30, 00:30] spans midnight; no calendar-day truncation anywhere.
    assert vals["txn_count_1h"] == 2
    assert vals["txn_amount_sum_1h"] == 125.0


# ---------------------------------------------------------------------------
# 4. Late events revise only the affected account and windows
# ---------------------------------------------------------------------------
def test_late_event_revises_only_affected_account_and_windows(client, auth):
    _ingest(client, auth, [
        make_txn("TXN-L-1", ACC, "2026-11-17T10:00:00+00:00", 100.0),
        make_txn("TXN-L-2", ACC, "2026-11-17T10:10:00+00:00", 200.0),
        make_txn("TXN-L-3", ACC, "2026-11-17T10:20:00+00:00", 300.0),
        make_txn("TXN-L-B", "ACC902", "2026-11-17T10:05:00+00:00", 999.0),
    ])
    before_other = values_by_name(online(client, auth, "ACC902", "2026-11-17T10:05:00+00:00"))

    # Late arrival for ACC901: event_time 09:50 is behind its watermark (10:20).
    stats = _ingest(client, auth, [make_txn("TXN-L-LATE", ACC, "2026-11-17T09:50:00+00:00", 50.0)])
    assert stats["late"] == 1

    # Online value for the affected account now includes the late event.
    vals = values_by_name(online(client, auth, ACC, "2026-11-17T10:20:00+00:00"))
    assert vals["txn_count_1h"] == 4
    assert vals["txn_amount_sum_1h"] == 650.0

    # The other account is untouched.
    after_other = values_by_name(online(client, auth, "ACC902", "2026-11-17T10:05:00+00:00"))
    assert after_other == before_other
    assert after_other["txn_count_1h"] == 1

    # Revision audit: scoped to ACC901 only, caused by the late event, and only
    # for checkpoints whose windows contain 09:50 (as_of in [09:50, 10:50)).
    revisions = client.get("/api/compute/revisions", headers=auth).json()
    assert revisions, "expected revision records"
    assert {r["entity_id"] for r in revisions} == {ACC}
    assert {r["cause_event_id"] for r in revisions} == {"TXN-L-LATE"}
    count_revisions = [r for r in revisions if r["feature"] == "txn_count_1h"]
    assert len(count_revisions) == 3  # checkpoints at 10:00, 10:10, 10:20
    assert sorted(r["checkpoint_as_of"] for r in count_revisions) == [
        "2026-11-17T10:00:00+00:00",
        "2026-11-17T10:10:00+00:00",
        "2026-11-17T10:20:00+00:00",
    ]
    assert [r["old_value"]["value"] for r in
            sorted(count_revisions, key=lambda r: r["checkpoint_as_of"])] == [1, 2, 3]
    assert [r["new_value"]["value"] for r in
            sorted(count_revisions, key=lambda r: r["checkpoint_as_of"])] == [2, 3, 4]


# ---------------------------------------------------------------------------
# 5. Delayed chargeback results
# ---------------------------------------------------------------------------
def test_delayed_chargeback_resolves_label_and_revises_feature(client, auth):
    _ingest(client, auth, [
        make_txn("TXN-CB-1", ACC, "2026-11-15T08:00:00+00:00", 800.0),
        make_txn("TXN-CB-2", ACC, "2026-11-17T09:00:00+00:00", 120.0),
    ])
    # Training sample created while the outcome is still unknown.
    r = client.post("/api/training/samples", json={
        "sample_key": "S-CB-1",
        "entity_id": ACC,
        "decision_time": "2026-11-15T08:00:01+00:00",
        "transaction_id": "TXN-CB-1",
    }, headers=auth)
    assert r.status_code == 201, r.text
    sample = r.json()
    assert sample["label_status"] == "pending"

    # The chargeback confirms days later, effective 2026-11-17 (late arrival).
    r = client.post("/api/ingest/labels", json=[{
        "label_id": "LBL-CB-1",
        "transaction_id": "TXN-CB-1",
        "account_id": ACC,
        "label_type": "chargeback",
        "label_value": "fraud",
        "event_time": "2026-11-17T12:00:00+00:00",
    }], headers=auth)
    assert r.status_code == 200, r.text

    # Sample label resolved.
    samples = client.get("/api/training/samples", headers=auth).json()
    s = next(x for x in samples if x["sample_key"] == "S-CB-1")
    assert s["label_status"] == "confirmed"
    assert s["label_value"] == "fraud"

    # The label-sourced window feature reflects the late confirmation.
    vals = values_by_name(online(client, auth, ACC, "2026-11-18T00:00:00+00:00"))
    assert vals["chargeback_count_30d"] == 1

    # Trace shows the label record lineage.
    trace = client.get(f"/api/training/samples/{s['id']}/trace", headers=auth).json()
    assert trace["label_record"]["label_id"] == "LBL-CB-1"
    assert trace["label_record"]["label_value"] == "fraud"


# ---------------------------------------------------------------------------
# 6. Publishing mid-traffic never mixes versions
# ---------------------------------------------------------------------------
def _member_pairs(resp):
    return {(f["name"], f["version"]) for f in resp["features"]}


def test_publish_mid_traffic_never_mixes_versions(client, auth):
    # v1 is the seeded release (9 features).
    sets = client.get("/api/feature-sets", headers=auth).json()
    v1 = next(s for s in sets if s["name"] == "fraud_scoring" and s["version"] == 1)
    assert v1["is_current"]
    v1_members = {(m["name"], m["version"]) for m in v1["members"]}

    # Define txn_count_1h v2 (2h window) and publish a smaller v2 release.
    r = client.post("/api/features", json={
        "name": "txn_count_1h", "version": 2, "source": "transactions",
        "aggregation": "COUNT", "window_seconds": 7200,
        "description": "近2小时交易笔数", "owner": "risk-ml",
    }, headers=auth)
    assert r.status_code == 201, r.text
    v2_member_list = [
        {"name": "txn_count_1h", "version": 2},
        {"name": "txn_amount_sum_1h", "version": 1},
    ]
    v2_members = {("txn_count_1h", 2), ("txn_amount_sum_1h", 1)}

    expected = {1: v1_members, 2: v2_members}
    results, errors = [], []

    def reader():
        try:
            for _ in range(40):
                with SessionLocal() as db:
                    resp = serving.read_online_features(db, "fraud_scoring", "ACC-X", None, None)
                results.append(resp)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def publisher():
        try:
            with SessionLocal() as db:
                publish_feature_set(db, "fraud_scoring", v2_member_list, "v2", "tester")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    pub = threading.Thread(target=publisher)
    for t in threads:
        t.start()
    pub.start()
    for t in threads + [pub]:
        t.join()

    assert not errors
    assert results
    for resp in results:
        version = resp["feature_set"]["version"]
        assert version in (1, 2)
        # The response must be exactly one complete release, never a mix.
        assert _member_pairs(resp) == expected[version]

    # After the publish, the default read is v2-complete; explicit pin reads v1.
    now = client.get("/api/online/features", params={"entity_id": "ACC-X"}, headers=auth).json()
    assert now["feature_set"]["version"] == 2
    assert _member_pairs(now) == v2_members
    pinned = client.get("/api/online/features",
                        params={"entity_id": "ACC-X", "set_version": 1}, headers=auth).json()
    assert pinned["feature_set"]["version"] == 1
    assert _member_pairs(pinned) == v1_members


# ---------------------------------------------------------------------------
# 7. Offline/online consistency (incl. after late revisions)
# ---------------------------------------------------------------------------
def test_offline_online_consistency_with_late_events(client, auth):
    # Account profile so profile features also carry real values.
    client.post("/api/ingest/account-profiles", json=[{
        "account_id": ACC, "snapshot_date": "2026-11-18",
        "kyc_level": "enhanced", "country": "CN", "risk_segment": "low", "age_days": 400,
    }], headers=auth)

    _ingest(client, auth, [
        make_txn("TXN-O-1", ACC, "2026-11-17T23:45:00+00:00", 100.0),
        make_txn("TXN-O-2", ACC, "2026-11-18T00:15:00+00:00", 200.0),
        make_txn("TXN-O-3", ACC, "2026-11-18T00:20:00+00:00", 300.0),
    ])
    _ingest(client, auth, [make_txn("TXN-O-2", ACC, "2026-11-18T00:15:00+00:00", 200.0)])  # dup
    _ingest(client, auth, [make_txn("TXN-O-4", ACC, "2026-11-17T23:50:00+00:00", 50.0)])  # late

    decision_time = "2026-11-18T00:30:00+00:00"
    r = client.post("/api/training/samples", json={
        "sample_key": "S-CONS-1", "entity_id": ACC, "decision_time": decision_time,
    }, headers=auth)
    assert r.status_code == 201, r.text
    sample_id = r.json()["id"]

    def offline_values():
        trace = client.get(f"/api/training/samples/{sample_id}/trace", headers=auth).json()
        return {f["name"]: f["value"] for f in trace["features"]}

    def online_values():
        return values_by_name(online(client, auth, ACC, decision_time))

    offline_v1, online_v1 = offline_values(), online_values()
    assert offline_v1 == online_v1
    assert online_v1["txn_count_1h"] == 4
    assert online_v1["txn_amount_sum_1h"] == 650.0
    assert online_v1["txn_amount_max_24h"] == 300.0
    assert online_v1["account_kyc_level"] == "enhanced"
    assert online_v1["account_age_days"] == 400

    # A dataset built now must carry the same values as the online read.
    r = client.post("/api/training/datasets", json={
        "name": "ds-consistency", "feature_set": "fraud_scoring",
    }, headers=auth)
    assert r.status_code == 201, r.text
    ds = client.get(f"/api/training/datasets/{r.json()['id']}", headers=auth).json()
    row = next(x for x in ds["rows"] if x["sample_key"] == "S-CONS-1")
    assert row["features"] == online_v1

    # Another late event arrives and revises history; both readers move together.
    _ingest(client, auth, [make_txn("TXN-O-5", ACC, "2026-11-17T23:55:00+00:00", 500.0)])
    offline_v2, online_v2 = offline_values(), online_values()
    assert offline_v2 == online_v2
    assert online_v2["txn_count_1h"] == 5
    assert online_v2["txn_amount_sum_1h"] == 1150.0
    assert online_v2["txn_amount_max_24h"] == 500.0


# ---------------------------------------------------------------------------
# 8. Upstream account data unavailable -> status, never fake zeros
# ---------------------------------------------------------------------------
def test_account_source_unavailable_returns_status_not_zero(client, auth):
    _ingest(client, auth, [make_txn("TXN-U-1", ACC, T0, 42.0)])
    r = client.post("/api/sources/outage", json={
        "source": "account_profiles", "status": "unavailable",
        "message": "upstream account feed delayed",
    }, headers=auth)
    assert r.status_code == 200

    resp = online(client, auth, ACC, "2026-11-17T10:30:00+00:00")
    by_name = {f["name"]: f for f in resp["features"]}

    profile = by_name["account_kyc_level"]
    assert profile["status"] == "unavailable"
    assert profile["value"] is None  # never disguised as zero
    assert profile["source"]["source"] == "account_profiles"
    assert profile["source"]["status"] == "unavailable"
    assert "last_success_at" in profile["source"]

    # Transaction-sourced features are unaffected and keep real values.
    assert by_name["txn_count_1h"]["status"] == "ok"
    assert by_name["txn_count_1h"]["value"] == 1

    # Source overview exposes the outage and last successful update.
    sources = client.get("/api/sources", headers=auth).json()
    ap = next(s for s in sources if s["source"] == "account_profiles")
    assert ap["status"] == "unavailable"
    assert ap["message"] == "upstream account feed delayed"


# ---------------------------------------------------------------------------
# 9. Training sample lineage
# ---------------------------------------------------------------------------
def test_training_sample_traces_inputs(client, auth):
    _ingest(client, auth, [
        make_txn("TXN-T-1", ACC, "2026-11-17T10:00:00+00:00", 10.0),
        make_txn("TXN-T-2", ACC, "2026-11-17T10:10:00+00:00", 20.0),
        make_txn("TXN-T-3", ACC, "2026-11-17T10:20:00+00:00", 30.0),
        make_txn("TXN-T-OLD", ACC, "2026-11-17T08:00:00+00:00", 999.0),  # outside 1h window
    ])
    r = client.post("/api/training/samples", json={
        "sample_key": "S-TRACE-1", "entity_id": ACC,
        "decision_time": "2026-11-17T10:30:00+00:00",
    }, headers=auth)
    assert r.status_code == 201, r.text

    trace = client.get(f"/api/training/samples/{r.json()['id']}/trace", headers=auth).json()
    assert trace["decision_time"] == "2026-11-17T10:30:00+00:00"
    count_feature = next(f for f in trace["features"] if f["name"] == "txn_count_1h")
    assert count_feature["value"] == 3
    input_ids = {i["txn_id"] for i in count_feature["inputs"]}
    assert input_ids == {"TXN-T-1", "TXN-T-2", "TXN-T-3"}  # not TXN-T-OLD


# ---------------------------------------------------------------------------
# 10. Auth guard
# ---------------------------------------------------------------------------
def test_auth_required(client):
    assert client.get("/api/features").status_code == 401
    assert client.get("/api/online/features", params={"entity_id": "X"}).status_code == 401


# ---------------------------------------------------------------------------
# 11. Regression: same-timestamp events in one batch keep watermark state sane
# ---------------------------------------------------------------------------
def test_same_timestamp_labels_in_one_batch(client, auth):
    _ingest(client, auth, [
        make_txn("TXN-R-1", ACC, "2026-11-10T08:00:00+00:00", 10.0),
        make_txn("TXN-R-2", ACC, "2026-11-11T08:00:00+00:00", 20.0),
    ])
    # Two delayed chargebacks confirmed with the identical effective time.
    r = client.post("/api/ingest/labels", json=[
        {"label_id": "LBL-R-1", "transaction_id": "TXN-R-1", "account_id": ACC,
         "label_type": "chargeback", "label_value": "fraud",
         "event_time": "2026-11-15T00:00:00+00:00"},
        {"label_id": "LBL-R-2", "transaction_id": "TXN-R-2", "account_id": ACC,
         "label_type": "chargeback", "label_value": "fraud",
         "event_time": "2026-11-15T00:00:00+00:00"},
    ], headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2
    vals = values_by_name(online(client, auth, ACC, "2026-11-16T00:00:00+00:00"))
    assert vals["chargeback_count_30d"] == 2
