from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .evaluator import evaluate_notice
from .models import AtomicRequirement, CompanyFact, Evaluation, Evidence, Notice, NoticeVersion

FIXTURE_VERSION = "synthetic-v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seed_synthetic_replay(session: Session) -> tuple[int, int, list[str]]:
    """Load PII-free deterministic fixtures and their first evaluation.

    The fixture models the three visible states without copying tender documents,
    company names, people, phone numbers, emails, credentials, or source answers.
    Calling it repeatedly is idempotent.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    evidence = session.scalar(select(Evidence).where(Evidence.evidence_key == "SYN-E-LEGAL-001"))
    if evidence is None:
        evidence = Evidence(
            evidence_key="SYN-E-LEGAL-001",
            name="가상 법인 상태 증빙",
            evidence_type="SYNTHETIC_CERTIFICATE",
            status="VERIFIED",
            issued_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_until=datetime(2028, 12, 31, tzinfo=timezone.utc),
            sha256=_digest("synthetic-evidence"),
            metadata_json={"synthetic": True},
        )
        session.add(evidence)
        session.flush()

    facts = {
        "legal_status": ("active", evidence),
        "direct_production": ("not_held", evidence),
        "delivery_capacity": (1000, evidence),
    }
    for key, (value, linked_evidence) in facts.items():
        exists = session.scalar(
            select(CompanyFact).where(
                CompanyFact.fact_key == f"synthetic.{key}",
                CompanyFact.source == "SYNTHETIC",
            )
        )
        if exists is None:
            session.add(
                CompanyFact(
                    fact_key=f"synthetic.{key}",
                    value=value,
                    value_label=str(value),
                    effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    effective_to=datetime(2028, 12, 31, tzinfo=timezone.utc),
                    evidence_id=linked_evidence.id,
                    verified=True,
                    source="SYNTHETIC",
                )
            )
    session.flush()

    fixture_specs = [
        {
            "key": "SYN-PASS-001",
            "title": "[가상] 지역정책 연구용역",
            "fact_key": "synthetic.legal_status",
            "operator": "eq",
            "required": "active",
            "pass_rule": "P-ENTITY",
            "review": None,
            "trigger": None,
            "risk": {"qualification": 10, "execution": 20, "competition": 25, "profitability": 15, "operation": 20, "document": 10},
        },
        {
            "key": "SYN-REVIEW-001",
            "title": "[가상] 비영리 참여 가능 콘텐츠 용역",
            "fact_key": "synthetic.direct_production",
            "operator": "eq",
            "required": "held",
            "pass_rule": "P-CERT-DIRECT",
            "review": "R01",
            "trigger": "not_held",
            "risk": {"qualification": 60, "execution": 25, "competition": 45, "profitability": 35, "operation": 30, "document": 35},
        },
        {
            "key": "SYN-FAIL-001",
            "title": "[가상] 대규모 전국 교육 운영용역",
            "fact_key": "synthetic.delivery_capacity",
            "operator": "gte",
            "required": 10000,
            "pass_rule": "P-RESOURCE",
            "review": None,
            "trigger": None,
            "risk": {"qualification": 80, "execution": 85, "competition": 70, "profitability": 75, "operation": 90, "document": 50},
        },
    ]
    created = 0
    existing = 0
    keys: list[str] = []
    for index, spec in enumerate(fixture_specs, start=1):
        keys.append(spec["key"])
        notice = session.scalar(select(Notice).where(Notice.notice_key == spec["key"]))
        if notice is not None:
            existing += 1
            continue
        notice = Notice(
            notice_key=spec["key"],
            bid_notice_no=f"SYN-2026-{index:04d}",
            revision_no="00",
            title=spec["title"],
            agency="가상 발주기관",
            published_at=now,
            deadline=now + timedelta(days=7 + index),
            status="OPEN",
            category="SYNTHETIC",
            estimated_amount=float(index * 100_000_000),
            source_url=None,
            risk_dimensions=spec["risk"],
        )
        version = NoticeVersion(
            version_no=1,
            file_sha256=_digest(spec["key"]),
            document_complete=True,
            extraction_status="COMPLETE",
            extraction_confidence=0.98,
            source_payload={"synthetic": True, "fixture_version": FIXTURE_VERSION},
        )
        requirement = AtomicRequirement(
            requirement_key=f"{spec['key']}-REQ-01",
            group_key="G-SYNTHETIC",
            path_key="PATH-PRIMARY",
            sequence=1,
            label="가상 핵심 참가자격",
            fact_key=spec["fact_key"],
            operator=spec["operator"],
            required_value=spec["required"],
            evidence_required=True,
            mandatory=True,
            pass_rule_id=spec["pass_rule"],
            linked_review_code=spec["review"],
            review_trigger_value=spec["trigger"],
            parse_confidence=0.98,
            source_excerpt="가상 회귀 테스트용 조건이며 실제 공고 문구가 아닙니다.",
            source_location="synthetic://fixture/requirement/1",
            active=True,
        )
        version.requirements.append(requirement)
        notice.versions.append(version)
        session.add(notice)
        session.flush()

        all_facts = list(session.scalars(select(CompanyFact)).all())
        result = evaluate_notice(notice, version, version.requirements, all_facts)
        notice.evaluations.append(
            Evaluation(
                notice_version_id=version.id,
                deadline_snapshot_at=notice.deadline,
                eligibility=result.eligibility.value,
                reason_code=result.reason_code,
                readiness_score=result.readiness_score,
                readiness_status=result.readiness_status.value,
                evidence_coverage=result.evidence_coverage,
                risk_score=result.risk_score,
                risk_band=result.risk_band.value,
                ruleset_version="2026.08-v2.1",
                atomic_results=result.atomic_results,
                explanation=result.explanation,
            )
        )
        created += 1
    session.commit()
    return created, existing, keys

