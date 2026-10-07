"""Three-year award sweep on the plain PPS award operation's own daily quota.

W14 searches one notice at a time on ``getScsbidListSttusServcPPSSrch``: about
36 calls per notice (28-day windows since 2024-01-01), so its 1,000-call daily
quota covers roughly 27 notices. PPS meters every operation separately, and the
plain ``getScsbidListSttusServc`` operation lists every service award by opening
date with its demand-agency code (probed 2026-09-29: 999 rows per page accepted,
about 4,400-11,500 awards per 28 days, 2024 history present). One pass over
2024-01-01..today therefore costs roughly 250-350 calls and serves every stored
notice at once: each page is matched against all biddable notices with the same
demand-agency and search-term rule W14 uses, and matches are written to
``award_history_items`` exactly as W14 writes them.

The sweep advances in bounded steps from the W14 planner so no workflow change
is needed. It never touches the PPSSrch ledger (its jobs use their own source),
never starts when the same W14 cycle is about to call ``/run``, and stops for the
KST day at ``DAILY_CALL_CAP`` or on a provider quota response.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .api import _award_similarity, _comparable_utc
from .award_automation import _active_notice_ids, _classification
from .award_intelligence import build_annual_award_table
from .award_scope import (OTHER_AGENCY_MIN_SIMILARITY, OTHER_AGENCY_SOURCE, award_core_matches, award_core_terms,
                          award_same_agency_title_matches, derive_award_keyword, filter_notice_awards,
                          filter_other_agency_awards, normalize_award_agency, resolve_notice_award_scope)
from .integrations.awards import OpeningResultsIncomplete, PpsAwardClient, is_pps_rate_limit_error, normalise_award
from .integrations.pps import PpsApiError, PpsClient, parse_paged_response, split_date_range
from .models import AwardHistoryItem, IngestionJob, Notice

BULK_SOURCE = "PPS_AWARD_BULK"
BULK_OPERATION = "as/ScsbidInfoService/getScsbidListSttusServc"
# inqryDiv 3 = opening time; an award exists only after its opening.
BULK_INQUIRY_DIVISION = "3"
SWEEP_START = date(2024, 1, 1)
# PPS rejects calls above 1,000 per operation per day; keep a margin for
# manual checks against the same operation.
DAILY_CALL_CAP = 990
DAILY_FAILURE_CAP = 5
# A slow provider page (older windows take longer) is not a broken request;
# it is retried from the same cursor next cycle and has its own, looser cap.
DAILY_NETWORK_FAILURE_CAP = 20
PAGE_ROWS = 999
WINDOW_DAYS = 28  # PPS enforces a calendar-month range; 28 days is always safe.
# Planner-only cycles have the whole W14 execution limit (570 s) to themselves:
# 240 s here plus the 90 s opening step leaves room for planning and HTTP.
STEP_WALL_SECONDS = 240
# A request is started only when its full client timeout still fits in the step.
# Squeezing the last request into the remaining seconds turned slow-but-healthy
# pages into NETWORK_ERROR failures, and five failures stop the sweep for the day.
REQUEST_TIMEOUT_SECONDS = 60
STALE_RUNNING = timedelta(minutes=15)
_KST = timezone(timedelta(hours=9))


def _client_factory(settings: Any) -> PpsClient:
    return PpsClient(service_key=settings.pps_api_key, base_url=settings.pps_base_url,
                     timeout_seconds=REQUEST_TIMEOUT_SECONDS, max_retries=0)


def _kst_midnight(now: datetime) -> datetime:
    return now.astimezone(_KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _windows(sweep_day: date) -> list:
    # Newest first: the current and previous year matter most on the screen.
    return list(reversed(split_date_range(SWEEP_START, sweep_day, max_days=WINDOW_DAYS)))


def _targets(session: Session, now: datetime) -> list[tuple[Notice, Any, str]]:
    notices = list(session.scalars(select(Notice).options(
        selectinload(Notice.award_scope_versions), selectinload(Notice.award_agency_metadata))))
    active = _active_notice_ids(session, notices, now)
    targets = []
    for notice in notices:
        # The same eligibility W14 applies: real PPS, biddable, service, agency and keyword known.
        if _classification(notice, active)[0] != "PENDING":
            continue
        targets.append((notice, resolve_notice_award_scope(notice), derive_award_keyword(notice.title)))
    return targets


class _TargetIndex:
    """Find candidate notices by agency first, then apply the exact shared rule."""

    def __init__(self, targets: list[tuple[Notice, Any, str]]) -> None:
        self.by_code: dict[str, list] = defaultdict(list)
        self.coded_by_name: dict[str, list] = defaultdict(list)
        self.uncoded_by_name: dict[str, list] = defaultdict(list)
        for target in targets:
            scope = target[1]
            code = normalize_award_agency(scope.demand_agency_code)
            name = normalize_award_agency(scope.demand_agency_name)
            if code:
                self.by_code[code].append(target)
                if name:
                    self.coded_by_name[name].append(target)
            elif name:
                self.uncoded_by_name[name].append(target)

    def matches(self, award: dict[str, Any]) -> list[tuple[Notice, Any, str]]:
        code = normalize_award_agency(award.get("demand_agency_code"))
        name = normalize_award_agency(award.get("agency"))
        # matches_award_agency compares codes when both sides have one and
        # names otherwise; pick candidates the same way, then re-check exactly.
        candidates = list(self.by_code.get(code, ())) if code else list(self.coded_by_name.get(name, ()))
        candidates += self.uncoded_by_name.get(name, ())
        return [target for target in candidates
                if target[1].matches_award(award) and award_same_agency_title_matches(
                    target[0].title, (target[1].demand_agency_name, target[1].announcing_agency_name),
                    award.get("title"))]


def _history_row(notice: Notice, award: dict[str, Any], *, source: str = "PPS",
                 similarity: float | None = None) -> AwardHistoryItem:
    return AwardHistoryItem(
        target_notice_id=notice.id, external_identity=award["identity"],
        bid_notice_no=award["bid_notice_no"], revision_no=award.get("revision_no") or "000",
        title=award["title"], agency=award.get("agency") or "",
        demand_agency_code=award.get("demand_agency_code"), winner_name=award["winner_name"],
        participant_count=award.get("participant_count"), award_amount=award.get("award_amount"),
        award_rate=award.get("award_rate"),
        opened_at=_comparable_utc(award["opened_at"]) if award.get("opened_at") else None,
        awarded_at=_comparable_utc(award["awarded_at"]) if award.get("awarded_at") else None,
        similarity_score=_award_similarity(notice.title, award["title"]) if similarity is None else similarity,
        source=source,
    )


def _cursor_state(session: Session, now: datetime) -> tuple[str, dict, IngestionJob | None] | None:
    """Return (sweep id, next cursor, stale job to close), or None when nothing is due."""
    latest = session.scalar(select(IngestionJob).where(IngestionJob.source == BULK_SOURCE)
                            .order_by(IngestionJob.created_at.desc()).limit(1))
    today = now.astimezone(_KST).date().isoformat()
    fresh = {"window": 0, "page": 1, "rows": PAGE_ROWS}
    if latest is None:
        return today, fresh, None
    stale = None
    if latest.status == "RUNNING":
        if _comparable_utc(latest.created_at) > now - STALE_RUNNING:
            return None  # another step is in flight
        stale = latest
    request = latest.request_json or {}
    sweep = request.get("sweep") or today
    cursor = request.get("next")
    if cursor is None:
        # One full sweep per KST day; the next day re-reads for newly stored notices.
        return None if sweep >= today else (today, fresh, stale)
    return sweep, dict(cursor), stale


def _today_usage(session: Session, now: datetime) -> tuple[int, int, bool]:
    jobs = list(session.scalars(select(IngestionJob).where(
        IngestionJob.source == BULK_SOURCE, IngestionJob.created_at >= _kst_midnight(now))))
    calls = sum(max(0, job.api_calls or 0) for job in jobs)
    failed = [job for job in jobs if job.status == "FAILED"]
    network = sum(job.error_code == "BULK_NETWORK_ERROR" for job in failed)
    failures = len(failed) - network
    if network >= DAILY_NETWORK_FAILURE_CAP:
        failures = max(failures, DAILY_FAILURE_CAP)
    limited = any("BULK_PROVIDER_RATE_LIMIT" in (job.warnings or []) for job in jobs)
    return calls, failures, limited


def advance_bulk_sweep(session: Session, settings: Any, now: datetime, *,
                       monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Advance the sweep by at most STEP_WALL_SECONDS. Provider errors are recorded, not raised."""
    if not getattr(settings, "pps_api_key", None):
        return {"status": "NO_KEY"}
    # Local, test and demo instances carry placeholder keys; only production
    # spends the provider quota from the planner.
    if str(getattr(settings, "environment", "")).casefold() != "production":
        return {"status": "DISABLED"}
    calls_today, failures_today, limited = _today_usage(session, now)
    if limited or calls_today >= DAILY_CALL_CAP or failures_today >= DAILY_FAILURE_CAP:
        return {"status": "DAILY_LIMIT", "calls_today": calls_today}
    state = _cursor_state(session, now)
    if state is None:
        return {"status": "IDLE"}
    sweep, cursor, stale = state
    if stale is not None:
        stale.status, stale.completed_at = "PARTIAL", now
        stale.warnings = [*(stale.warnings or []), "BULK_STEP_ABANDONED"]
    windows = _windows(date.fromisoformat(sweep))
    targets = _targets(session, now)
    index = _TargetIndex(targets)
    # Other-agency candidates for every notice with at least two core terms.
    cores = [(notice, scope, core) for notice, scope, _keyword in targets
             if len(core := award_core_terms(notice.title, (scope.demand_agency_name,
                                                            scope.announcing_agency_name))) >= 2]
    target_ids = [notice.id for notice, _scope, _keyword in targets]
    existing = set(session.execute(
        select(AwardHistoryItem.target_notice_id, AwardHistoryItem.external_identity)
        .where(AwardHistoryItem.target_notice_id.in_(target_ids))).all()) if target_ids else set()
    job = IngestionJob(source=BULK_SOURCE, mode="LIVE", status="RUNNING",
                       window_json={"from": SWEEP_START.isoformat(), "to": sweep}, keyword=None,
                       request_json={"sweep": sweep, "start": dict(cursor), "next": dict(cursor),
                                     "operation": BULK_OPERATION, "targets": len(targets)},
                       notice_keys=[], warnings=[], created_at=now)
    session.add(job)
    session.commit()
    job_id = job.id
    deadline = monotonic() + STEP_WALL_SECONDS
    fetched = matched = created = other_created = 0
    client = _client_factory(settings)
    try:
        while cursor is not None and calls_today + client.request_count < DAILY_CALL_CAP and monotonic() + REQUEST_TIMEOUT_SECONDS <= deadline:
            window = windows[cursor["window"]]
            payload = client._request(BULK_OPERATION, {
                "inqryDiv": BULK_INQUIRY_DIVISION,
                "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                "pageNo": cursor["page"], "numOfRows": cursor["rows"],
            })
            items, total = parse_paged_response(payload)
            fetched += len(items)
            for raw in items:
                award = normalise_award(raw)
                if not (award["bid_notice_no"] and award["title"] and award["winner_name"]):
                    continue
                for notice, _scope, _keyword in index.matches(award):
                    matched += 1
                    key = (notice.id, award["identity"])
                    if key in existing:
                        continue
                    session.add(_history_row(notice, award))
                    existing.add(key)
                    created += 1
                for notice, scope, core in cores:
                    if scope.matches_award(award) or not award_core_matches(core, award["title"]):
                        continue
                    key = (notice.id, award["identity"])
                    if key in existing:
                        continue
                    similarity = _award_similarity(notice.title, award["title"])
                    if similarity < OTHER_AGENCY_MIN_SIMILARITY:
                        continue
                    session.add(_history_row(notice, award, source=OTHER_AGENCY_SOURCE, similarity=similarity))
                    existing.add(key)
                    other_created += 1
            if cursor["page"] * cursor["rows"] >= total or not items:
                cursor = {"window": cursor["window"] + 1, "page": 1, "rows": cursor["rows"]}
                if cursor["window"] >= len(windows):
                    cursor = None
            else:
                cursor = {**cursor, "page": cursor["page"] + 1}
            # Rows and progress commit together, page by page, so a killed
            # step resumes exactly where its last durable page ended.
            job.request_json = {**job.request_json, "next": cursor, "other_agency_created": other_created}
            job.api_calls, job.fetched, job.matched, job.created_count = (
                client.request_count, fetched, matched, created)
            session.commit()
        job.status = "COMPLETED"
    except PpsApiError as exc:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        metadata = exc.safe_metadata()
        if is_pps_rate_limit_error(exc):
            job.status, job.warnings = "PARTIAL", ["BULK_PROVIDER_RATE_LIMIT"]
        else:
            job.status, job.error_code = "FAILED", f"BULK_{metadata['error_type']}"[:64]
            job.warnings = [f"{metadata['error_type']}:{metadata['http_status']}:{metadata['provider_code']}"]
    except Exception:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        job.status, job.error_code = "FAILED", "BULK_CLIENT_ERROR"
    finally:
        client.close()
    # The attempted call is charged even when its page did not commit.
    job.api_calls = client.request_count
    job.completed_at = datetime.now(timezone.utc)
    session.commit()
    return {"status": job.status, "calls": client.request_count, "fetched": fetched,
            "matched": matched, "created": created, "other_agency_created": other_created,
            "next": (job.request_json or {}).get("next")}


# --- Opening results for the rows the screen actually shows ---------------
#
# The award list names the winner only. Participants, their bids, ranks and
# evaluation scores come from getOpengResultListInfoOpengCompt, read by exact
# notice number (one call per award) on its own 1,000/day quota. Only rows the
# annual table displays are read: per year the same project first, similar
# candidates only in years without one. An award linked to several notices is
# read once and written to all of them.
OPENING_SOURCE = "PPS_OPENING_BULK"
OPENING_DAILY_CALL_CAP = 990
OPENING_STEP_WALL_SECONDS = 90
OPENING_REQUEST_SECONDS = 20


def _opening_client_factory(settings: Any) -> PpsAwardClient:
    return PpsAwardClient(service_key=settings.pps_api_key, base_url=settings.pps_base_url,
                          timeout_seconds=OPENING_REQUEST_SECONDS, max_retries=0)


def _displayed_unread(session: Session, targets: list[tuple[Notice, Any, str]], now: datetime) -> list[str]:
    """Award identities shown on some notice's table whose openings were never read."""
    ids = [notice.id for notice, _scope, _keyword in targets]
    if not ids:
        return []
    by_notice: dict[str, list[AwardHistoryItem]] = defaultdict(list)
    for row in session.scalars(select(AwardHistoryItem).where(AwardHistoryItem.target_notice_id.in_(ids))):
        by_notice[row.target_notice_id].append(row)
    wanted: list[str] = []
    seen: set[str] = set()
    # Longest-lived notices first: they stay on screen the longest.
    for notice, scope, _keyword in sorted(targets, key=lambda target: _comparable_utc(target[0].deadline),
                                          reverse=True):
        stored = by_notice.get(notice.id, [])
        same, others = filter_notice_awards(notice, stored), filter_other_agency_awards(notice, stored)
        rows = [*same, *others]
        if not rows:
            continue
        table = build_annual_award_table(same, target_title=notice.title,
                                         target_agency=scope.demand_agency_name, as_of=now,
                                         other_agency_records=others)
        shown = {(item["bid_notice_no"], item["revision_no"]) for item in table["rows"]}
        for row in rows:
            # A recorded status means a read was already attempted; do not spend again.
            if (row.opening_results is None and row.opening_results_status is None
                    and (row.bid_notice_no, row.revision_no) in shown
                    and row.external_identity not in seen):
                seen.add(row.external_identity)
                wanted.append(row.external_identity)
    return wanted


def advance_opening_backfill(session: Session, settings: Any, now: datetime, *,
                             monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read openings for displayed rows for at most OPENING_STEP_WALL_SECONDS."""
    if not getattr(settings, "pps_api_key", None):
        return {"status": "NO_KEY"}
    if str(getattr(settings, "environment", "")).casefold() != "production":
        return {"status": "DISABLED"}
    jobs = list(session.scalars(select(IngestionJob).where(
        IngestionJob.source == OPENING_SOURCE, IngestionJob.created_at >= _kst_midnight(now))))
    calls_today = sum(max(0, job.api_calls or 0) for job in jobs)
    if (calls_today >= OPENING_DAILY_CALL_CAP
            or sum(job.status == "FAILED" for job in jobs) >= DAILY_FAILURE_CAP
            or any("BULK_PROVIDER_RATE_LIMIT" in (job.warnings or []) for job in jobs)
            or any(job.status == "RUNNING" and _comparable_utc(job.created_at) > now - STALE_RUNNING
                   for job in jobs)):
        return {"status": "DAILY_LIMIT", "calls_today": calls_today}
    wanted = _displayed_unread(session, _targets(session, now), now)
    if not wanted:
        return {"status": "IDLE"}
    job = IngestionJob(source=OPENING_SOURCE, mode="LIVE", status="RUNNING", window_json={}, keyword=None,
                       request_json={"operation": "as/ScsbidInfoService/getOpengResultListInfoOpengCompt",
                                     "pending": len(wanted)},
                       notice_keys=[], warnings=[], created_at=now)
    session.add(job)
    session.commit()
    job_id = job.id
    deadline = monotonic() + OPENING_STEP_WALL_SECONDS
    read = collected = 0
    client = _opening_client_factory(settings)
    try:
        for identity in wanted:
            if calls_today + client.request_count >= OPENING_DAILY_CALL_CAP or monotonic() + OPENING_REQUEST_SECONDS * 3 > deadline:
                break
            rows = list(session.scalars(select(AwardHistoryItem).where(
                AwardHistoryItem.external_identity == identity, AwardHistoryItem.opening_results.is_(None))))
            if not rows:
                continue
            notice_no, revision, classification, rebid = (identity.split("|") + ["", "", "", ""])[:4]
            try:
                companies = client.fetch_opening_results(
                    bid_notice_no=notice_no, revision_no=revision or "000",
                    classification_no=classification or "0", rebid_no=rebid or "000",
                    # No step deadline here: a read cut short would be recorded as
                    # PARTIAL and never retried. Up to three pages fit by the guard above.
                    rows=100, max_pages=3)
                status = "COLLECTED" if companies else "UNAVAILABLE"
            except OpeningResultsIncomplete:
                companies, status = None, "PARTIAL"
            except PpsApiError as exc:
                if is_pps_rate_limit_error(exc):
                    raise
                companies, status = None, "ERROR"
            read += 1
            stamp = datetime.now(timezone.utc)
            for row in rows:
                # NULL means "never read". A failed read stays NULL and records
                # why in the status column, so the table never shows a false
                # empty competitor set.
                if companies is not None:
                    row.opening_results, row.opening_results_read_at = companies, stamp
                    collected += 1
                row.opening_results_status = status
            job.api_calls, job.fetched, job.matched = client.request_count, read, collected
            session.commit()
        job.status = "COMPLETED"
    except PpsApiError:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        job.status, job.warnings = "PARTIAL", ["BULK_PROVIDER_RATE_LIMIT"]
    except Exception:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        job.status, job.error_code = "FAILED", "OPENING_CLIENT_ERROR"
    finally:
        client.close()
    job.api_calls = client.request_count
    job.completed_at = datetime.now(timezone.utc)
    session.commit()
    return {"status": job.status, "calls": client.request_count, "read": read, "collected": collected,
            "pending": len(wanted)}
