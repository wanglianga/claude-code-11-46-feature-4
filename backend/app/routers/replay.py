"""Replay & retention governance API: models, batches, packages, verification,
controlled replay, approvals, keys, retention policies and sweeps."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Approval,
    KeyVersion,
    ModelBatch,
    ModelVersion,
    ReplayPackage,
    ReplayRun,
    RetentionPolicy,
    RetentionSweep,
    SourceCorrection,
    Transaction,
    User,
)
from ..schemas import (
    ApprovalDecisionIn,
    ApprovalRequestIn,
    BatchIn,
    KeyActionIn,
    ModelIn,
    PackageIn,
    PolicyIn,
    RevealIn,
    SourceCorrectionIn,
    SweepIn,
)
from ..security import current_user, require_role
from ..services import governance as gov
from ..services import replay as svc

router = APIRouter(prefix="/api", tags=["replay"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.ReplayError):
        status = {
            "not_found": 404,
            "conflict": 409,
            "forbidden": 403,
            "approval_required": 403,
            "not_frozen": 409,
            "not_sealed": 409,
            "evidence_broken": 409,
            "not_replayable": 409,
        }.get(exc.code, 400)
        return HTTPException(status, exc.message)
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# Models & batches
# ---------------------------------------------------------------------------
@router.get("/models")
def list_models(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(ModelVersion).order_by(ModelVersion.name, ModelVersion.version)).scalars()
    return [svc.model_out(m) for m in rows]


@router.post("/models", status_code=201)
def create_model(body: ModelIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        model = svc.create_model(db, body.model_dump(), user.username)
    except svc.ReplayError as exc:
        raise _http(exc)
    return svc.model_out(model)


@router.get("/model-batches")
def list_batches(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(ModelBatch).order_by(ModelBatch.id.desc())).scalars()
    return [svc.batch_out(db, b) for b in rows]


@router.post("/model-batches", status_code=201)
def create_batch(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        batch = svc.create_batch(
            db,
            batch_key=body.batch_key,
            model_name=body.model_name,
            model_version=body.model_version,
            set_name=body.feature_set,
            set_version=body.set_version,
            sample_ids=body.sample_ids,
            created_by=user.username,
            freeze=body.freeze,
        )
    except (svc.ReplayError, KeyError) as exc:
        raise _http(exc)
    return svc.batch_out(db, batch)


@router.post("/model-batches/{batch_id}/freeze")
def freeze_batch(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.get(ModelBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    if batch.status != "draft":
        raise HTTPException(409, f"batch is {batch.status}; only a draft can be frozen")
    batch = svc.freeze_batch(db, batch, user.username)
    return svc.batch_out(db, batch)


@router.get("/model-batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.get(ModelBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    return svc.batch_out(db, batch)


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
@router.get("/replay-packages")
def list_packages(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(ReplayPackage).order_by(ReplayPackage.id.desc())).scalars()
    out = []
    for p in rows:
        manifest = p.manifest or {}
        out.append(
            {
                "id": p.id,
                "package_key": p.package_key,
                "batch_id": p.batch_id,
                "batch_key": (db.get(ModelBatch, p.batch_id).batch_key if db.get(ModelBatch, p.batch_id) else None),
                "status": p.status,
                "policy_version": manifest.get("policy", {}).get("version"),
                "digest_algorithm": p.digest_algorithm,
                "sample_count": (manifest.get("integrity") or {}).get("sample_count"),
                "record_count": (manifest.get("integrity") or {}).get("record_count"),
                "manifest_digest": p.manifest_digest,
                "created_by": p.created_by,
                "created_at": gov.iso(p.created_at),
                "sealed_at": gov.iso(p.sealed_at),
            }
        )
    return out


@router.post("/replay-packages", status_code=201)
def create_package(body: PackageIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.get(ModelBatch, body.batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    try:
        package = svc.build_package(
            db, batch, body.package_key, user.username, pause_before_seal=body.pause_before_seal
        )
    except svc.ReplayError as exc:
        raise _http(exc)
    return _package_out(package, include_manifest=True)


def _package_out(p: ReplayPackage, include_manifest: bool = True) -> dict:
    return {
        "id": p.id,
        "package_key": p.package_key,
        "batch_id": p.batch_id,
        "status": p.status,
        "policy_version": p.policy_snapshot.get("version"),
        "policy_snapshot": p.policy_snapshot,
        "environment": p.environment,
        "digest_algorithm": p.digest_algorithm,
        "manifest_digest": p.manifest_digest,
        "created_by": p.created_by,
        "created_at": gov.iso(p.created_at),
        "sealed_at": gov.iso(p.sealed_at),
        "manifest": p.manifest if include_manifest else None,
    }


@router.get("/replay-packages/{package_id}")
def get_package(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    return _package_out(p, include_manifest=True)


@router.get("/replay-packages/{package_id}/manifest")
def get_manifest(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    return {"package_key": p.package_key, "manifest_digest": p.manifest_digest,
            "manifest": p.manifest}


@router.post("/replay-packages/{package_id}/seal")
def seal_package(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    try:
        p = svc.seal_package(db, p, user.username)
    except svc.ReplayError as exc:
        raise _http(exc)
    return _package_out(p, include_manifest=False)


@router.get("/replay-packages/{package_id}/verify")
def verify_package(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    result = svc.verify_package(db, p)
    gov.record_audit_query(
        db, action="verify", actor=user.username, package_id=p.id,
        summary={"overall_classification": result["overall_classification"],
                 "replayable": result["replayable"]},
    )
    return result


@router.get("/replay-packages/{package_id}/dependencies")
def package_dependencies(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    result = svc.package_dependencies(db, p)
    gov.record_audit_query(db, action="dependencies", actor=user.username, package_id=p.id,
                           summary={"holders": list(result["holders"].keys())})
    return result


@router.post("/replay-packages/{package_id}/replay")
def replay_package(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    try:
        return svc.replay_package(db, p, user.username)
    except svc.ReplayError as exc:
        raise _http(exc)


@router.get("/replay-packages/{package_id}/runs")
def list_runs(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(
        select(ReplayRun).where(ReplayRun.package_id == package_id).order_by(ReplayRun.run_number)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "run_number": r.run_number,
            "status": r.status,
            "classification": r.classification,
            "result_digest": r.result_digest,
            "environment_match": r.environment_match,
            "affected_scores": r.affected_scores,
            "executed_by": r.executed_by,
            "created_at": gov.iso(r.created_at),
        }
        for r in rows
    ]


@router.post("/replay-packages/{package_id}/reveal")
def reveal(package_id: int, body: RevealIn, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    try:
        return svc.reveal_records(db, p, user.username, body.category)
    except svc.ReplayError as exc:
        raise _http(exc)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
@router.get("/replay-packages/{package_id}/approvals")
def list_approvals(package_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    return svc.approval_state(db, p.id)


@router.post("/replay-packages/{package_id}/approvals", status_code=201)
def request_approval(package_id: int, body: ApprovalRequestIn, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    p = db.get(ReplayPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    try:
        a = svc.request_approval(db, p, body.scope, user.username, body.reason)
    except svc.ReplayError as exc:
        raise _http(exc)
    return {"id": a.id, "scope": a.scope, "status": a.status,
            "requested_by": a.requested_by, "reason": a.reason}


@router.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: int, body: ApprovalDecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    a = db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(404, "approval not found")
    try:
        a = svc.decide_approval(db, a, user.username, user.role, body.approve, body.note)
    except svc.ReplayError as exc:
        raise _http(exc)
    return {"id": a.id, "scope": a.scope, "status": a.status,
            "decided_by": a.decided_by, "note": a.note}


# ---------------------------------------------------------------------------
# Retention policies & sweeps
# ---------------------------------------------------------------------------
def _policy_out(p: RetentionPolicy) -> dict:
    return {
        "id": p.id,
        "version": p.version,
        "status": p.status,
        "digest_algorithm": p.digest_algorithm,
        "rules": p.rules,
        "rules_digest": p.rules_digest,
        "note": p.note,
        "created_by": p.created_by,
        "created_at": gov.iso(p.created_at),
    }


@router.get("/retention/policies")
def list_policies(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(RetentionPolicy).order_by(RetentionPolicy.version.desc())).scalars()
    return [_policy_out(p) for p in rows]


@router.post("/retention/policies", status_code=201)
def create_policy(body: PolicyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        policy = gov.create_policy(db, body.rules, body.digest_algorithm, body.note, user.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _policy_out(policy)


@router.get("/retention/sweeps")
def list_sweeps(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(RetentionSweep).order_by(RetentionSweep.id.desc())).scalars()
    return [
        {
            "id": s.id,
            "as_of": gov.iso(s.as_of),
            "policy_version": (db.get(RetentionPolicy, s.policy_version_id).version
                               if s.policy_version_id else None),
            "status": s.status,
            "records_expired": s.records_expired,
            "archives_destroyed": s.archives_destroyed,
            "details": s.details,
            "executed_by": s.executed_by,
            "started_at": gov.iso(s.started_at),
            "completed_at": gov.iso(s.completed_at),
        }
        for s in rows
    ]


@router.post("/retention/sweeps", status_code=201)
def run_sweep(body: SweepIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    as_of = body.as_of or gov.utcnow()
    sweep = gov.run_sweep(db, as_of, user.username)
    return {
        "id": sweep.id,
        "as_of": gov.iso(sweep.as_of),
        "status": sweep.status,
        "records_expired": sweep.records_expired,
        "archives_destroyed": sweep.archives_destroyed,
        "details": sweep.details,
    }


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------
def _key_out(k: KeyVersion) -> dict:
    return {
        "id": k.id,
        "version": k.version,
        "algorithm": k.algorithm,
        "status": k.status,
        "reason": k.reason,
        "created_at": gov.iso(k.created_at),
        "retired_at": gov.iso(k.retired_at),
        "revoked_at": gov.iso(k.revoked_at),
        "restored_at": gov.iso(k.restored_at),
    }


@router.get("/keys")
def list_keys(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(KeyVersion).order_by(KeyVersion.version.desc())).scalars()
    return [_key_out(k) for k in rows]


@router.post("/keys/rotate", status_code=201)
def rotate_key(body: KeyActionIn, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    return _key_out(gov.rotate_key(db, user.username, body.reason))


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: int, body: KeyActionIn, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    key = db.get(KeyVersion, key_id)
    if key is None:
        raise HTTPException(404, "key not found")
    try:
        return _key_out(gov.revoke_key(db, key, user.username, body.reason))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/keys/{key_id}/restore")
def restore_key(key_id: int, body: KeyActionIn, db: Session = Depends(get_db),
                user: User = Depends(require_role("admin"))):
    key = db.get(KeyVersion, key_id)
    if key is None:
        raise HTTPException(404, "key not found")
    try:
        return _key_out(gov.restore_key(db, key, user.username, body.reason))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# ---------------------------------------------------------------------------
# Source corrections (drill: re-upload changes a canonical record)
# ---------------------------------------------------------------------------
@router.get("/source-corrections")
def list_corrections(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(SourceCorrection).order_by(SourceCorrection.id.desc())).scalars()
    return [
        {
            "id": c.id, "category": c.category, "record_id": c.record_id,
            "old_digest": c.old_digest, "new_digest": c.new_digest,
            "note": c.note, "corrected_by": c.corrected_by,
            "created_at": gov.iso(c.created_at),
        }
        for c in rows
    ]


@router.post("/source-corrections", status_code=201)
def apply_correction(body: SourceCorrectionIn, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Simulate a legitimate upstream re-upload that modifies an already
    ingested transaction. The canonical record changes; sealed evidence keeps
    its captured digest, so verification reports an evidence-chain break."""
    txn = db.get(Transaction, body.record_id)
    if txn is None:
        raise HTTPException(404, "transaction not found")
    old_payload = gov.transaction_payload(txn)
    if body.amount is not None:
        txn.amount = body.amount
    if body.merchant_id is not None:
        txn.merchant_id = body.merchant_id
    if body.type is not None:
        txn.type = body.type
    db.flush()
    new_payload = gov.transaction_payload(txn)
    alg = "sha256"
    correction = SourceCorrection(
        category=gov.CATEGORY_TRANSACTION,
        record_id=txn.txn_id,
        old_digest=gov.digest_obj(alg, old_payload),
        new_digest=gov.digest_obj(alg, new_payload),
        note=body.note,
        corrected_by=user.username,
    )
    db.add(correction)
    db.commit()
    db.refresh(correction)
    return {
        "id": correction.id, "record_id": correction.record_id,
        "old_digest": correction.old_digest, "new_digest": correction.new_digest,
    }
