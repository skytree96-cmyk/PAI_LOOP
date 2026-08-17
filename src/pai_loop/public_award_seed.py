from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AwardHistoryItem, Notice
from .public_notice_seed import PUBLIC_NOTICE_SOURCE_KEY


RESOURCE = "data/public_award_history_seed_v1.json"
SCHEMA_VERSION = "public-award-history-seed-1.0.0"
CLASSIFICATION = "PUBLIC_PROCUREMENT_DERIVED"
RECORD_FIELDS = {
    "record_key", "bid_notice_no", "revision_no", "title", "agency", "winner_name",
    "participant_count", "award_amount", "award_rate", "opened_at", "awarded_at",
    "similarity_score", "source",
}
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:0\d{1,2}|\+?82[ .-]?0?\d{1,2})[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)")
_IDENTIFIER = re.compile(r"(?<!\d)(?:\d{3}-\d{2}-\d{5}|\d{6}-[1-8]\d{6})(?!\d)")
_PERSON_ONLY = re.compile(r"^[가-힣]{2,4}$")
_LABELLED_PERSON = re.compile(r"(?:담당자|대표자|성명)\s*[:：]\s*[가-힣]{2,4}(?=\s|[,;/]|$)")


class PublicAwardSeedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicAwardImportResult:
    created: int
    existing: int
    records: int
    payload_sha256: str

    def aggregate_only(self) -> dict[str, Any]:
        return {
            "award_history_created": self.created,
            "award_history_existing": self.existing,
            "award_history_records": self.records,
            "award_history_payload_sha256": self.payload_sha256,
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _records_digest(records: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(records).encode("utf-8")).hexdigest()


def _safe_text(value: Any, *, maximum: int, required: bool = True) -> str:
    text = " ".join(str(value or "").replace("\ufeff", " ").split())
    if (required and not text) or len(text) > maximum:
        raise PublicAwardSeedError("award seed text length is invalid")
    if _EMAIL.search(text) or _PHONE.search(text) or _IDENTIFIER.search(text) or _LABELLED_PERSON.search(text):
        raise PublicAwardSeedError("award seed contains a private identifier")
    return text


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def validate_public_award_seed(payload: Mapping[str, Any]) -> None:
    if set(payload) != {"schema_version", "dataset_version", "classification", "target_notice_key", "allowlist", "provenance", "records"}:
        raise PublicAwardSeedError("award seed top-level allowlist is invalid")
    if payload["schema_version"] != SCHEMA_VERSION or payload["classification"] != CLASSIFICATION:
        raise PublicAwardSeedError("award seed version or classification is invalid")
    if payload["target_notice_key"] != PUBLIC_NOTICE_SOURCE_KEY:
        raise PublicAwardSeedError("award seed target is invalid")
    if set(payload["allowlist"]) != RECORD_FIELDS:
        raise PublicAwardSeedError("award seed record allowlist is invalid")
    records = payload["records"]
    if not isinstance(records, list) or not records:
        raise PublicAwardSeedError("award seed records are missing")
    keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise PublicAwardSeedError("award seed record fields are invalid")
        if not _DIGEST.fullmatch(str(record["record_key"])) or record["record_key"] in keys:
            raise PublicAwardSeedError("award seed record key is invalid")
        keys.add(record["record_key"])
        for field, maximum in (("bid_notice_no", 80), ("revision_no", 20), ("title", 500), ("agency", 255), ("winner_name", 255), ("source", 32)):
            _safe_text(record[field], maximum=maximum, required=field not in {"agency"})
        if _PERSON_ONLY.fullmatch(record["winner_name"]):
            raise PublicAwardSeedError("person-only winner name is not publishable")
        for field in ("participant_count", "award_amount", "award_rate", "similarity_score"):
            value = record[field]
            if value is not None and not isinstance(value, (int, float)):
                raise PublicAwardSeedError("award seed numeric field is invalid")
        _iso(record["opened_at"])
        _iso(record["awarded_at"])
    provenance = payload["provenance"]
    if set(provenance) != {"method", "source_kind", "record_count", "excluded_privacy_rows", "records_sha256"}:
        raise PublicAwardSeedError("award seed provenance is invalid")
    if provenance["record_count"] != len(records) or provenance["records_sha256"] != _records_digest(records):
        raise PublicAwardSeedError("award seed digest mismatch")


def build_public_award_seed_from_db(
    source_db: str | Path,
    *,
    dataset_version: str = "2026.08.17-v1",
) -> dict[str, Any]:
    connection = sqlite3.connect(str(source_db))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT a.external_identity, a.bid_notice_no, a.revision_no, a.title, a.agency,
                   a.winner_name, a.participant_count, a.award_amount, a.award_rate,
                   a.opened_at, a.awarded_at, a.similarity_score
            FROM award_history_items a
            JOIN notices n ON n.id = a.target_notice_id
            WHERE n.notice_key = ?
            ORDER BY COALESCE(a.awarded_at, a.opened_at) DESC, a.bid_notice_no
            """,
            (PUBLIC_NOTICE_SOURCE_KEY,),
        ).fetchall()
    finally:
        connection.close()
    records: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        try:
            winner = _safe_text(row["winner_name"], maximum=255)
            if _PERSON_ONLY.fullmatch(winner):
                raise PublicAwardSeedError("person-only winner")
            safe = {
                "bid_notice_no": _safe_text(row["bid_notice_no"], maximum=80),
                "revision_no": _safe_text(row["revision_no"], maximum=20),
                "title": _safe_text(row["title"], maximum=500),
                "agency": _safe_text(row["agency"], maximum=255, required=False),
                "winner_name": winner,
                "participant_count": int(row["participant_count"]) if row["participant_count"] is not None else None,
                "award_amount": float(row["award_amount"]) if row["award_amount"] is not None else None,
                "award_rate": float(row["award_rate"]) if row["award_rate"] is not None else None,
                "opened_at": _iso(row["opened_at"]),
                "awarded_at": _iso(row["awarded_at"]),
                "similarity_score": float(row["similarity_score"]) if row["similarity_score"] is not None else None,
                "source": "PPS_PUBLIC_DERIVED",
            }
            identity_material = f"{row['external_identity']}|{_canonical(safe)}"
            safe["record_key"] = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
            records.append({key: safe[key] for key in sorted(RECORD_FIELDS)})
        except (PublicAwardSeedError, TypeError, ValueError):
            excluded += 1
    records.sort(key=lambda item: ((item["awarded_at"] or item["opened_at"] or ""), item["record_key"]), reverse=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "classification": CLASSIFICATION,
        "target_notice_key": PUBLIC_NOTICE_SOURCE_KEY,
        "allowlist": sorted(RECORD_FIELDS),
        "provenance": {
            "method": "STRICT_ALLOWLIST_FROM_STORED_PPS_PUBLIC_FACTS",
            "source_kind": "PPS_NARA_AWARD_STORED",
            "record_count": len(records),
            "excluded_privacy_rows": excluded,
            "records_sha256": _records_digest(records),
        },
        "records": records,
    }
    validate_public_award_seed(payload)
    return payload


def write_public_award_seed(payload: Mapping[str, Any], output: str | Path) -> Path:
    validate_public_award_seed(payload)
    target = Path(output)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


@lru_cache(maxsize=1)
def _load_template() -> dict[str, Any]:
    payload = json.loads(resources.files("pai_loop").joinpath(RESOURCE).read_text(encoding="utf-8"))
    validate_public_award_seed(payload)
    return payload


def load_public_award_seed() -> dict[str, Any]:
    return copy.deepcopy(_load_template())


def import_public_award_seed(session: Session, payload: Mapping[str, Any] | None = None) -> PublicAwardImportResult:
    seed = copy.deepcopy(dict(payload)) if payload is not None else load_public_award_seed()
    validate_public_award_seed(seed)
    notice = session.scalar(select(Notice).where(Notice.notice_key == seed["target_notice_key"]))
    if notice is None:
        raise PublicAwardSeedError("target public notice must be imported first")
    created = 0
    existing = 0
    for record in seed["records"]:
        external_identity = f"PUBLIC_DERIVED:{record['record_key']}"
        current = session.scalar(select(AwardHistoryItem).where(
            AwardHistoryItem.target_notice_id == notice.id,
            AwardHistoryItem.external_identity == external_identity,
        ))
        if current is not None:
            existing += 1
            continue
        session.add(AwardHistoryItem(
            target_notice_id=notice.id,
            external_identity=external_identity,
            bid_notice_no=record["bid_notice_no"],
            revision_no=record["revision_no"],
            title=record["title"],
            agency=record["agency"],
            winner_name=record["winner_name"],
            participant_count=record["participant_count"],
            award_amount=record["award_amount"],
            award_rate=record["award_rate"],
            opened_at=datetime.fromisoformat(record["opened_at"]) if record["opened_at"] else None,
            awarded_at=datetime.fromisoformat(record["awarded_at"]) if record["awarded_at"] else None,
            similarity_score=record["similarity_score"] or 0.0,
            source=record["source"],
        ))
        created += 1
    session.commit()
    return PublicAwardImportResult(created, existing, len(seed["records"]), seed["provenance"]["records_sha256"])
