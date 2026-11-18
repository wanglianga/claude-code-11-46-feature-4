"""Acceptance tests for replay packages and retention governance.

Scenarios from the task:
* retention expiry (原文不可用 but digest still provable)
* key revocation then restoration
* upstream re-upload correction (evidence-chain break, affected scores listed)
* policy update midway through manifest generation (package uses its start snapshot)
* multiple replays of the same model are identical, minimize disclosure, and
  never claim a non-replayable expired record was reproduced.
"""
from datetime import datetime, timedelta, timezone

from app.services import governance as gov

ACC = "ACC-RP-1"
D0 = "2026-11-10T08:00:00+00:00"
D1 = "2026-11-11T08:00:00+00:00"


def _hdr(client):
    r = client.post("/api/auth/login", json={"username": "analyst", "password": "analyst123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _admin_hdr(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _txn(i, account, event_time, amount):
    return {
        "txn_id": f"TXN-RP-{i}",
        "account_id": account,
        "event_time": event_time,
        "amount": amount,
        "currency": "CNY",
        "type": "purchase",
        "status": "completed",
        "merchant_id": "M-RP",
    }


def _setup_batch(client, auth, sample_key="S-RP-1", with_profile=True):
    if with_profile:
        client.post("/api/ingest/account-profiles", json=[{
            "account_id": ACC, "snapshot_date": "2026-11-11",
            "kyc_level": "enhanced", "country": "CN", "risk_segment": "low", "age_days": 500,
        }], headers=auth)
    client.post("/api/ingest/transactions", json=[
        _txn(1, ACC, D0, 100.0),
        _txn(2, ACC, "2026-11-11T07:30:00+00:00", 250.0),
    ], headers=auth)
    r = client.post("/api/training/samples", json={
        "sample_key": sample_key, "entity_id": ACC,
        "decision_time": D1, "transaction_id": "TXN-RP-1",
    }, headers=auth)
    assert r.status_code == 201, r.text
    sample_id = r.json()["id"]
    r = client.post("/api/model-batches", json={
        "batch_key": f"BATCH-{sample_key}", "model_name": "fraud_blocker",
        "sample_ids": [sample_id], "freeze": True,
    }, headers=auth)
    assert r.status_code == 201, r.text
    batch = r.json()
    assert batch["status"] == "frozen"
    assert batch["input_digest"]
    r = client.post("/api/replay-packages", json={"batch_id": batch["id"]}, headers=auth)
    assert r.status_code == 201, r.text
    return batch, r.json()


def _approve(client, auth, admin_auth, package_id, scope):
    r = client.post(f"/api/replay-packages/{package_id}/approvals",
                    json={"scope": scope, "reason": "审计复现"}, headers=auth)
    assert r.status_code == 201, r.text
    approval_id = r.json()["id"]
    r = client.post(f"/api/approvals/{approval_id}/decide",
                    json={"approve": True, "note": "ok"}, headers=admin_auth)
    assert r.status_code == 200, r.text


def test_package_contains_full_manifest_and_replays_consistently(client, auth):
    _, package = _setup_batch(client, auth)
    m = package["manifest"]
    # data scope / event order / field availability / feature values / label
    # maturity / environment fingerprint / integrity are all present.
    assert m["data_scope"]["record_counts"]
    assert m["data_scope"]["categories"][gov.CATEGORY_TRANSACTION]["rationale"]
    assert m["event_order"] and m["event_order"][0]["seq"] == 0
    assert m["samples"][0]["label"]["mature"] is False
    assert m["samples"][0]["features"]
    assert m["environment"]["fingerprint"]
    assert m["integrity"]["checksum"] and package["manifest_digest"]

    res = client.get(f"/api/replay-packages/{package['id']}/verify", headers=auth).json()
    assert res["manifest_checksum_ok"] and res["manifest_digest_ok"]
    assert res["overall_classification"] == gov.CLS_REPLAYABLE_PLAINTEXT
    assert all(r["state"] in ("plaintext", "encrypted_copy") for r in res["records"])

    # Replay requires an approval, then performs a controlled replay.
    r = client.post(f"/api/replay-packages/{package['id']}/replay", headers=auth)
    assert r.status_code == 403

    admin = _admin_hdr(client)
    _approve(client, auth, admin, package["id"], "replay")
    first = client.post(f"/api/replay-packages/{package['id']}/replay", headers=auth)
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["run"]["status"] == "consistent"
    second = client.post(f"/api/replay-packages/{package['id']}/replay", headers=auth).json()
    assert second["run"]["result_digest"] == first["run"]["result_digest"]
    assert second["run"]["run_number"] == first["run"]["run_number"] + 1

    # Dependency lookup spans all six holders.
    deps = client.get(f"/api/replay-packages/{package['id']}/dependencies", headers=auth).json()
    holders = deps["holders"]
    assert {"data_retention", "feature_compute", "model_batch",
            "evidence_packaging", "approval", "audit_query"} <= set(holders)
    assert holders["feature_compute"]["feature_set"]["version"] == 1
    assert holders["model_batch"]["model"]["version"] == 1


def test_retention_expiry_keeps_digest_but_blocks_field_replay(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-EXP")
    pid = package["id"]
    future = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    sweep = client.post("/api/retention/sweeps", json={"as_of": future}, headers=auth)
    assert sweep.status_code == 201, sweep.text
    body = sweep.json()
    assert body["details"][gov.CATEGORY_PROFILE]["expired"] >= 1

    res = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    states = {r["record_key"]: r["state"] for r in res["records"]}
    profile = [k for k in states if k.startswith("account_profile:")][0]
    # Profile is minimized: digest only, plaintext gone, never "reproduced".
    assert states[profile] == "digest_only"
    assert res["overall_classification"] == gov.CLS_PLAINTEXT_UNAVAILABLE
    assert res["replayable"] is False
    # The digest is still there to prove which input was used.
    rec = next(r for r in res["records"] if r["record_key"] == profile)
    assert rec["digest"] and rec["digest_algorithm"] == "sha256"

    admin = _admin_hdr(client)
    _approve(client, auth, admin, pid, "replay")
    r = client.post(f"/api/replay-packages/{pid}/replay", headers=auth)
    assert r.status_code == 409, r.text  # refused, not silently "reproduced"
    assert res["samples"][0]["classification"] == gov.CLS_PLAINTEXT_UNAVAILABLE


def test_key_revocation_then_restore(client, auth):
    # No account profile here: only transaction evidence is captured, and the
    # transaction category keeps an encrypted copy past plaintext expiry. (A
    # profile would be digest-only and thus genuinely unreplayable after expiry,
    # which the expiry test covers separately.)
    _, package = _setup_batch(client, auth, "S-RP-KEY", with_profile=False)
    pid = package["id"]
    admin = _admin_hdr(client)
    keys = client.get("/api/keys", headers=auth).json()
    active = next(k for k in keys if k["status"] == "active")

    # Plaintext still usable -> replay works before expiry.
    future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    client.post("/api/retention/sweeps", json={"as_of": future}, headers=auth)
    _approve(client, auth, admin, pid, "replay")
    _approve(client, auth, admin, pid, "plaintext")

    rev = client.post(f"/api/keys/{active['id']}/revoke", json={"reason": "应急撤销"},
                      headers=admin)
    assert rev.status_code == 200, rev.text
    res = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    # Transactions keep an encrypted copy: after plaintext expiry their copy is
    # locked by the revoked key -> plaintext unavailable, never fabricated.
    print("DBGFIND", res["findings"], res["manifest_checksum_ok"], res["manifest_digest_ok"])
    txn_states = [r["state"] for r in res["records"] if r["category"] == gov.CATEGORY_TRANSACTION]
    assert txn_states and all(s == "encrypted_locked" for s in txn_states)
    assert res["overall_classification"] == gov.CLS_PLAINTEXT_UNAVAILABLE
    revealed = client.post(f"/api/replay-packages/{pid}/reveal", json={}, headers=auth).json()
    assert all(not r["available"] for r in revealed["records"])

    # Restore the key: the encrypted copies become usable again -> replayable.
    rst = client.post(f"/api/keys/{active['id']}/restore", json={"reason": "撤销恢复"},
                      headers=admin)
    assert rst.status_code == 200, rst.text
    res2 = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    assert res2["overall_classification"] == gov.CLS_REPLAYABLE_ENCRYPTED
    replay = client.post(f"/api/replay-packages/{pid}/replay", headers=auth)
    assert replay.status_code == 200, replay.text
    assert replay.json()["run"]["status"] == "consistent"


def test_source_correction_breaks_chain_and_lists_affected_scores(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-CORR")
    pid = package["id"]
    admin = _admin_hdr(client)
    _approve(client, auth, admin, pid, "replay")

    r = client.post("/api/source-corrections", json={
        "record_id": "TXN-RP-1", "amount": 9999.0, "note": "上游补传修正金额",
    }, headers=auth)
    assert r.status_code == 201, r.text

    res = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    assert res["overall_classification"] == gov.CLS_EVIDENCE_BROKEN
    assert res["replayable"] is False
    assert res["affected_scores"], "affected scores must be listed"
    assert any(f["kind"] == "evidence_chain_broken" for f in res["findings"])

    r = client.post(f"/api/replay-packages/{pid}/replay", headers=auth)
    assert r.status_code == 409, r.text


def test_policy_change_midway_uses_start_snapshot(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-POL")
    batch_id = package["batch_id"]

    # Build with a pause, then publish a stricter policy mid-build.
    r = client.post("/api/replay-packages", json={
        "batch_id": batch_id, "package_key": "PKG-MIDWAY", "pause_before_seal": True,
    }, headers=auth)
    assert r.status_code == 201, r.text
    midway = r.json()
    assert midway["status"] == "building"
    original_policy_version = midway["policy_version"]

    stricter = {c: dict(v) for c, v in gov.DEFAULT_RULES.items()}
    stricter[gov.CATEGORY_TRANSACTION]["plaintext_days"] = 7
    r = client.post("/api/retention/policies", json={
        "rules": stricter, "digest_algorithm": "sha256", "note": "中途收紧保留期",
    }, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["version"] > original_policy_version

    r = client.post(f"/api/replay-packages/{midway['id']}/seal", headers=auth)
    assert r.status_code == 200, r.text
    sealed = client.get(f"/api/replay-packages/{midway['id']}/manifest", headers=auth).json()
    seal_note = sealed["manifest"]["seal"]["policy"]
    assert seal_note["policy_changed_during_build"] is True
    assert seal_note["used_snapshot"] is True
    assert sealed["manifest"]["policy"]["version"] == original_policy_version

    # The earlier package still verifies and replays with its frozen policy.
    admin = _admin_hdr(client)
    _approve(client, auth, admin, midway["id"], "replay")
    replay = client.post(f"/api/replay-packages/{midway['id']}/replay", headers=auth)
    assert replay.status_code == 200, replay.text
    assert replay.json()["run"]["status"] == "consistent"


def test_minimal_disclosure_and_reveal_approval(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-DISC")
    pid = package["id"]
    admin = _admin_hdr(client)

    # The manifest carries derived feature values (needed to reproduce a score)
    # but never raw personal detail: merchant id, transaction amount and KYC
    # level appear only as digests / evidence hashes.
    manifest_text = str(package["manifest"])
    assert "M-RP" not in manifest_text
    assert "merchant_id" in manifest_text and "100.0" not in manifest_text

    r = client.post(f"/api/replay-packages/{pid}/reveal", json={}, headers=auth)
    assert r.status_code == 403
    _approve(client, auth, admin, pid, "plaintext")
    revealed = client.post(f"/api/replay-packages/{pid}/reveal", json={}, headers=auth).json()
    txn = next(r for r in revealed["records"] if r["category"] == gov.CATEGORY_TRANSACTION)
    assert txn["available"] and txn["fields"]["merchant_id"] == "M-RP"

    # A requester can neither decide their own request nor act as an approver.
    r = client.post(f"/api/replay-packages/{pid}/approvals",
                    json={"scope": "replay", "reason": "同一审计员请求"}, headers=auth)
    assert r.status_code == 201, r.text
    self_decide = client.post(f"/api/approvals/{r.json()['id']}/decide",
                              json={"approve": True}, headers=auth)
    assert self_decide.status_code == 403
    # A second pending request for the same scope is rejected as a conflict.
    dup = client.post(f"/api/replay-packages/{pid}/approvals",
                      json={"scope": "replay", "reason": "重复申请"}, headers=auth)
    assert dup.status_code == 409


def test_key_rotation_keeps_old_packages_decryptable(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-ROT", with_profile=False)
    pid = package["id"]
    admin = _admin_hdr(client)
    old_keys = client.get("/api/keys", headers=auth).json()
    old_active = next(k for k in old_keys if k["status"] == "active")

    rot = client.post("/api/keys/rotate", json={"reason": "季度轮换"}, headers=admin)
    assert rot.status_code == 201, rot.text
    assert rot.json()["version"] == old_active["version"] + 1
    keys = client.get("/api/keys", headers=auth).json()
    retired = next(k for k in keys if k["id"] == old_active["id"])
    assert retired["status"] == "retired"

    # Old evidence still decrypts under its retired key; records report the key
    # version they were sealed with.
    future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    client.post("/api/retention/sweeps", json={"as_of": future}, headers=auth)
    res = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    assert res["overall_classification"] == gov.CLS_REPLAYABLE_ENCRYPTED
    txn = next(r for r in res["records"] if r["category"] == gov.CATEGORY_TRANSACTION)
    assert txn["key_version"] == old_active["version"]
    assert txn["key_status"] == "retired"  # still usable for decryption

    _approve(client, auth, admin, pid, "replay")
    assert client.post(f"/api/replay-packages/{pid}/replay", headers=auth).json()["run"]["status"] == "consistent"

    # A package built after rotation is sealed under the new key version.
    _, new_pkg = _setup_batch(client, auth, "S-RP-ROT2", with_profile=False)
    new_res = client.get(f"/api/replay-packages/{new_pkg['id']}/verify", headers=auth).json()
    new_txn = next(r for r in new_res["records"] if r["category"] == gov.CATEGORY_TRANSACTION)
    assert new_txn["key_version"] == old_active["version"] + 1


def test_digest_algorithm_migration_verifies_legacy_packages(client, auth):
    _, package = _setup_batch(client, auth, "S-RP-MIG", with_profile=False)
    pid = package["id"]
    assert package["digest_algorithm"] == "sha256"

    migrated = {c: dict(v) for c, v in gov.DEFAULT_RULES.items()}
    r = client.post("/api/retention/policies", json={
        "rules": migrated, "digest_algorithm": "sha3_256", "note": "摘要算法迁移 sha256→sha3_256",
    }, headers=auth)
    assert r.status_code == 201, r.text

    res = client.get(f"/api/replay-packages/{pid}/verify", headers=auth).json()
    mig = res["digest_migration"]
    assert mig["package_algorithm"] == "sha256"
    assert mig["current_policy_algorithm"] == "sha3_256"
    assert mig["migrated"] is True
    assert mig["legacy_verification_ok"] is True
    # Legacy evidence keeps verifying under its own frozen algorithm and does
    # not get re-labelled as "broken" just because the policy moved on.
    assert res["overall_classification"] in (gov.CLS_REPLAYABLE_PLAINTEXT,
                                              gov.CLS_REPLAYABLE_ENCRYPTED)
    assert res["replayable"] is True

    admin = _admin_hdr(client)
    _approve(client, auth, admin, pid, "replay")
    replay = client.post(f"/api/replay-packages/{pid}/replay", headers=auth)
    assert replay.status_code == 200, replay.text
    assert replay.json()["run"]["status"] == "consistent"

    # New packages use the new algorithm.
    _, new_pkg = _setup_batch(client, auth, "S-RP-MIG2", with_profile=False)
    assert new_pkg["digest_algorithm"] == "sha3_256"


def test_legal_hold_label_outlives_plaintext_and_audit_query_state(client, auth):
    # A label inside the decision window makes the legal-hold category appear.
    client.post("/api/ingest/labels", json=[{
        "label_id": "LBL-RP-1", "transaction_id": "TXN-RP-1", "account_id": ACC,
        "label_type": "chargeback", "label_value": "fraud",
        "event_time": "2026-11-11T07:00:00+00:00",
    }], headers=auth)
    _, package = _setup_batch(client, auth, "S-RP-HOLD", with_profile=False)
    assert package["manifest"]["data_scope"]["record_counts"][gov.CATEGORY_LABEL] >= 1

    # 200 days: label plaintext (180d) has expired, but its 730-day legal hold
    # keeps the encrypted copy; transactions are past their 30-day window.
    sweep = client.post("/api/retention/sweeps", json={
        "as_of": (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(),
    }, headers=auth).json()
    details = sweep["details"]
    assert details[gov.CATEGORY_LABEL]["expired"] >= 1
    assert details[gov.CATEGORY_LABEL]["archives_destroyed"] == 0
    assert details[gov.CATEGORY_LABEL]["legal_hold_records"] >= 1

    res = client.get(f"/api/replay-packages/{package['id']}/verify", headers=auth).json()
    label_rec = next(r for r in res["records"] if r["category"] == gov.CATEGORY_LABEL)
    assert label_rec["state"] == "encrypted_copy"

    # 800 days: legal hold over, encrypted copy destroyed, digest remains.
    client.post("/api/retention/sweeps", json={
        "as_of": (datetime.now(timezone.utc) + timedelta(days=800)).isoformat(),
    }, headers=auth)
    res2 = client.get(f"/api/replay-packages/{package['id']}/verify", headers=auth).json()
    label_rec2 = next(r for r in res2["records"] if r["category"] == gov.CATEGORY_LABEL)
    assert label_rec2["state"] == "destroyed"
    assert label_rec2["digest"]

    # Audit-query state is recorded per action and queryable per package.
    deps = client.get(f"/api/replay-packages/{package['id']}/dependencies", headers=auth).json()
    actions = {q["action"] for q in deps["holders"]["audit_query"]["queries"]}
    assert "verify" in actions  # each lookup is itself logged as an audit query
    deps2 = client.get(f"/api/replay-packages/{package['id']}/dependencies", headers=auth).json()
    assert "dependencies" in {q["action"] for q in deps2["holders"]["audit_query"]["queries"]}
    assert all(q["status"] == "completed" for q in deps2["holders"]["audit_query"]["queries"])
