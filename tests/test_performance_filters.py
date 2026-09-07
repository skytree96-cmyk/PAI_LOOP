from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from pai_loop import public_performance


@pytest.fixture
def performance_rows(monkeypatch):
    records = [
        dict(record_key=f"SYN-{index}", project_name="SYN 교육", overview="",
             agency="SYN 기관", keywords=[], division=division, contract_year=2025,
             contract_date=day, contract_amount_krw=amount)
        for index, (day, amount, division) in enumerate([
            ("2025-01-01", "100", "SYN A"), ("2025-06-30", "200", "SYN A"),
            ("2025-07-01", "201", "SYN A"), (None, "150", "SYN A"),
            ("2025-05-01", None, "SYN A"), ("2025-05-01", "0", "SYN A"),
            ("2025-04-01", "150", "SYN B"),
        ])
    ]
    monkeypatch.setattr(public_performance, "load_public_performance_seed", lambda: {
        "records": records, "dataset_version": "SYN-1", "provenance": {"records_sha256": "0" * 64},
    })
    return records


def test_combined_ranges_are_inclusive_and_count_before_pagination(client, performance_rows):
    response = client.get("/api/v1/performance", params={
        "date_from": "2025-01-01", "date_to": "2025-06-30", "min_amount": 100,
        "max_amount": 200, "year": 2025, "division": "SYN A", "q": "교육",
        "limit": 1, "offset": 1,
    })
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["records"] == [performance_rows[1]]


def test_zero_amount_is_filterable_and_missing_amount_is_not_zero(client, performance_rows):
    response = client.get("/api/v1/performance", params={"min_amount": 0, "max_amount": 0})
    assert response.status_code == 200
    assert response.json()["records"] == [performance_rows[5]]
    assert client.get("/api/v1/performance").json()["total"] == len(performance_rows)


@pytest.mark.parametrize("params", [
    {"date_from": "2025-07-01", "date_to": "2025-01-01"},
    {"min_amount": 200, "max_amount": 100}, {"min_amount": -1},
    {"max_amount": "1.5"}, {"date_from": "2025-02-30"},
])
def test_invalid_ranges_are_rejected(client, performance_rows, params):
    assert client.get("/api/v1/performance", params=params).status_code == 422


def test_frontend_ranges_preserve_zero_validate_order_and_reset():
    source = (Path(__file__).parents[1] / "src/pai_loop/static/app.js").read_text(encoding="utf-8")
    names = ["buildPerformanceRequestPath", "performanceRangeControls", "validatePerformanceRanges", "resetPerformanceFilters"]
    functions = []
    for name in names:
        match = re.search(rf"  function {name}\(.*?(?=\n  (?:async )?function )", source, re.S)
        assert match
        functions.append(match.group())
    script = """
const assert = require('node:assert/strict');
const control = (value='') => ({value, validity:'', setCustomValidity(message){this.validity=message;}});
const els = Object.fromEntries(['performanceSearchInput','performanceYearFilter','performanceDivisionFilter',
 'performanceDateFrom','performanceDateTo','performanceMinAmount','performanceMaxAmount'].map(id=>[id,control()]));
els.performanceFilterForm = {reportValidity:()=> !Object.values(els).some(c=>c.validity)};
const state = {performance:{limit:24,offset:24}};
let loaded=0; function loadPerformance(){loaded++;}
""" + "\n".join(functions) + """
els.performanceMinAmount.value='0'; els.performanceMaxAmount.value='200';
els.performanceDateFrom.value='2025-01-01'; els.performanceDateTo.value='2025-06-30';
let params=new URL('https://example.invalid'+buildPerformanceRequestPath()).searchParams;
assert.equal(params.get('min_amount'),'0'); assert.equal(params.get('date_to'),'2025-06-30');
assert.equal(params.get('offset'),'24'); assert.ok(validatePerformanceRanges());
els.performanceDateTo.value='2024-12-31'; assert.equal(validatePerformanceRanges(),false);
els.performanceMinAmount.value='201'; assert.equal(validatePerformanceRanges(),false);
resetPerformanceFilters(); assert.ok(validatePerformanceRanges()); assert.equal(loaded,1);
params=new URL('https://example.invalid'+buildPerformanceRequestPath()).searchParams;
for(const key of ['min_amount','max_amount','date_from','date_to']) assert.equal(params.has(key),false);
assert.equal(params.get('offset'),'0');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
