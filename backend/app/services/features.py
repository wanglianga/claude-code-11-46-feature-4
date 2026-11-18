"""Feature catalog management and atomic publishing."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Feature,
    FeatureSetMember,
    FeatureSetVersion,
    OnlinePointer,
)

VALID_SOURCES = {"transactions", "labels", "account_profile"}
VALID_AGGS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "LAST"}


def validate_feature_spec(spec: dict) -> list[str]:
    errors = []
    if spec.get("source") not in VALID_SOURCES:
        errors.append(f"source must be one of {sorted(VALID_SOURCES)}")
    agg = spec.get("aggregation")
    if agg not in VALID_AGGS:
        errors.append(f"aggregation must be one of {sorted(VALID_AGGS)}")
    if spec.get("source") == "account_profile":
        if agg != "LAST":
            errors.append("account_profile features must use LAST aggregation")
        if not spec.get("field"):
            errors.append("account_profile features require a profile attribute field")
    else:
        if not spec.get("window_seconds") or int(spec.get("window_seconds") or 0) <= 0:
            errors.append("streamed features require a positive window_seconds")
        if agg in ("SUM", "AVG", "MIN", "MAX") and spec.get("field") != "amount":
            errors.append(f"{agg} currently supports field='amount'")
    return errors


def create_feature(db: Session, spec: dict, created_by: str) -> Feature:
    errors = validate_feature_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))
    name = spec["name"]
    if spec.get("version") is None:
        max_v = db.execute(
            select(func.max(Feature.version)).where(Feature.name == name)
        ).scalar()
        version = (max_v or 0) + 1
    else:
        version = int(spec["version"])
    existing = db.execute(
        select(Feature).where(Feature.name == name, Feature.version == version)
    ).scalar_one_or_none()
    if existing:
        raise ValueError(f"feature {name} v{version} already exists")
    feature = Feature(
        name=name,
        version=version,
        description=spec.get("description", ""),
        owner=spec.get("owner", created_by),
        entity_key=spec.get("entity_key", "account_id"),
        source=spec["source"],
        aggregation=spec["aggregation"],
        field=spec.get("field"),
        window_seconds=spec.get("window_seconds"),
        filter_json=spec.get("filter_json"),
        value_type=spec.get("value_type", "number"),
        status="draft",
        created_by=created_by,
    )
    db.add(feature)
    db.commit()
    db.refresh(feature)
    return feature


def publish_feature_set(
    db: Session, set_name: str, members: list[dict], note: str, created_by: str
) -> FeatureSetVersion:
    """Publish a new complete version of a feature set and atomically move the
    online pointer. Concurrent readers keep reading whichever complete version
    they pinned at request start — never a mix of two releases."""
    if not members:
        raise ValueError("a feature set version must contain at least one feature")

    feature_ids = []
    for m in members:
        feature = db.execute(
            select(Feature).where(
                Feature.name == m["name"], Feature.version == int(m["version"])
            )
        ).scalar_one_or_none()
        if feature is None:
            raise ValueError(f"feature {m['name']} v{m['version']} not found")
        feature_ids.append(feature.id)

    max_v = db.execute(
        select(func.max(FeatureSetVersion.version)).where(FeatureSetVersion.name == set_name)
    ).scalar()
    new_version = (max_v or 0) + 1

    fsv = FeatureSetVersion(
        name=set_name, version=new_version, note=note, created_by=created_by
    )
    db.add(fsv)
    db.flush()
    for pos, fid in enumerate(feature_ids):
        db.add(FeatureSetMember(set_version_id=fsv.id, feature_id=fid, position=pos))

    # Member features become (or stay) published so the streaming engine
    # maintains their state.
    for feature in db.execute(select(Feature).where(Feature.id.in_(feature_ids))).scalars():
        if feature.status == "draft":
            feature.status = "published"

    # Atomic pointer swap: one row update, one commit.
    pointer = db.get(OnlinePointer, set_name)
    if pointer is None:
        pointer = OnlinePointer(name=set_name, set_version_id=fsv.id)
        db.add(pointer)
    else:
        pointer.set_version_id = fsv.id

    db.commit()
    db.refresh(fsv)
    return fsv
