"""Durable one-shot reservation for automatic partial scoring recovery."""
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.exc import IntegrityError

from .models import IngestionJob

POLICY = "QUANTITATIVE_RECOVERY_ONCE"


def claim_id(version):
    payload = version.source_payload
    # Retry/version IDs cannot mint another allowance for unchanged source.
    identity = [version.notice_id, payload["attachment_id"],
                payload["current_manifest_sha256"], payload["manifest_sha256"],
                payload["document_sha256"]]
    return str(uuid5(NAMESPACE_URL, POLICY + json.dumps(identity, separators=(",", ":"))))


def consumed(session, version):
    return session.get(IngestionJob, claim_id(version)) is not None


def consume(session, version, validate_current):
    """Commit before provider I/O; concurrent/uncertain reservations fail closed."""
    if session.in_transaction():
        raise RuntimeError("QUANTITATIVE_RECOVERY_TRANSACTION_NOT_CLEAN")
    try:
        with session.begin():
            if not validate_current():
                return False
            session.add(IngestionJob(
                id=claim_id(version), source=POLICY, mode="LIVE", status="RESERVED",
                window_json={}, notice_keys=[], completed_at=datetime.now(timezone.utc),
                request_json={"policy": POLICY, "original_failure_id": version.id,
                              "state": "CONSUMED_OUTCOME_UNCONFIRMED", "max_calls": 2},
                warnings=["QUANTITATIVE_RECOVERY_RESERVED"],
            ))
    except IntegrityError:
        return False
    return True
