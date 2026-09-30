import copy
import importlib.util
import json
from pathlib import Path
import re

import pytest


spec = importlib.util.spec_from_file_location(
    "review_bulk_restore", Path(__file__).parents[1] / "scripts" / "verify-review-bulk.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def inputs(tmp_path):
    rows = [dict(id=f"SYN-Q{i}", key=f"syn-key-{i}", condition=f"SYN condition {i}",
                 fact_key="SYN-FACT", notice_keys=["SYN-NOTICE"], decision="", note="")
            for i in range(3)]
    base = dict(report_at="2026-09-30", runtime_commit="SYN-COMMIT", decisions=rows,
                document_notes=[])
    data = dict(base=copy.deepcopy(base), groups=[dict(id="SYN-G1", items=rows[:2])],
                notices=[dict(key="SYN-NOTICE", d01=None)])
    html = '<script id="data" type="application/json">' + json.dumps(data) + '</script>'
    html += "<script>const KEY='pai-review-bulk-'+D.base.report_at;let S={groups:{},items:{},doc:{}};</script>"
    source = tmp_path / "source.html"
    source.write_text(html, encoding="utf-8")
    review = copy.deepcopy(base)
    review["decisions"][0].update(decision="P", group="SYN-G1", applied_via="bulk")
    review["decisions"][1].update(decision="R", note="</script> SYN", group="SYN-G1", applied_via="individual")
    review["bulk_groups"] = [dict(id="SYN-G1", ids=["SYN-Q0", "SYN-Q1"], decision="P",
                                 overrides=[dict(id="SYN-Q1", decision="R", note="</script> SYN")])]
    return review, source


def test_restore_preserves_decisions_and_escapes_embedded_script_text(tmp_path):
    review, source = inputs(tmp_path)
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    original = path.read_bytes()
    summary = module.restore(path, source, tmp_path / "out")
    assert summary["decisions"] == {"P": 1, "R": 1}
    assert summary["untouched_conditions"] == 1
    assert summary["production_applied"] is False
    html = (tmp_path / "out" / "review-resumed.html").read_text(encoding="utf-8")
    payload = json.loads(re.search(r'<script[^>]*>(.*?)</script>', html)[1])
    assert payload["base"] == review
    assert payload["restored_state"]["items"]["SYN-Q1"]["note"] == "</script> SYN"
    assert len(re.findall("</script>", html)) == 2
    assert "pai-review-restored-" in html
    assert path.read_bytes() == original


@pytest.mark.parametrize("corruption", ["source", "duplicate", "untouched", "override", "bulk", "snapshot"])
def test_restore_rejects_inconsistent_inputs(tmp_path, corruption):
    review, source = inputs(tmp_path)
    if corruption == "source":
        review["decisions"][0]["condition"] = "SYN changed source"
    elif corruption == "duplicate":
        review["decisions"].append(copy.deepcopy(review["decisions"][0]))
    elif corruption == "untouched":
        review["decisions"][2]["decision"] = "P"
    elif corruption == "override":
        review["bulk_groups"][0]["overrides"] = []
    elif corruption == "bulk":
        review["decisions"][0]["decision"] = "F"
    else:
        review["runtime_commit"] = "SYN-OTHER-SNAPSHOT"
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValueError):
        module.restore(path, source, tmp_path / "out")
    assert not (tmp_path / "out").exists()
