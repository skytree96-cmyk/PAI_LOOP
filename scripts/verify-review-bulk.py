"""Validate a private review export and restore its standalone HTML controls.

This is an offline tool. It never connects to a service or changes decisions.
Keep input/output artifacts outside the public repository.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def restore(decisions_path: Path, html_path: Path, output_dir: Path) -> dict:
    raw = decisions_path.read_bytes()
    decisions = json.loads(raw.decode("utf-8-sig"))
    html = html_path.read_text(encoding="utf-8-sig")
    match = re.search(r'(<script\b[^>]*\bid="data"[^>]*>)(.*?)(</script>)', html, re.S)
    if not match:
        raise ValueError("review HTML has no data block")
    data = json.loads(match[2])
    base = {r["id"]: r for r in data["base"]["decisions"]}
    rows = {r["id"]: r for r in decisions["decisions"]}
    if len(rows) != len(decisions["decisions"]) or rows.keys() != base.keys():
        raise ValueError("duplicate or missing condition IDs")
    if any(decisions[k] != data["base"][k] for k in ("report_at", "runtime_commit")):
        raise ValueError("the review and HTML refer to different snapshots")
    for qid, row in rows.items():
        for key in ("key", "condition", "fact_key", "notice_keys"):
            if row[key] != base[qid][key]:
                raise ValueError(f"source identity changed: {qid}/{key}")
        if row["decision"] not in {"", "P", "F", "R", "EXCLUDE"}:
            raise ValueError(f"unknown decision: {qid}")

    groups = {g["id"]: g for g in decisions["bulk_groups"]}
    if len(groups) != len(decisions["bulk_groups"]):
        raise ValueError("duplicate group IDs")
    expected_groups = {g["id"]: {r["id"] for r in g["items"]} for g in data["groups"]}
    if groups.keys() != expected_groups.keys():
        raise ValueError("group IDs do not match the source HTML")
    state = {"groups": {}, "items": {}, "doc": {}}
    assigned = set()
    for gid, group in groups.items():
        ids = group["ids"]
        if len(set(ids)) != len(ids) or set(ids) != expected_groups[gid] or assigned.intersection(ids):
            raise ValueError(f"duplicate, missing or misplaced group member: {gid}")
        assigned.update(ids)
        state["groups"][gid] = {"decision": group["decision"], "note": group.get("note", "")}
        for qid in ids:
            row = rows[qid]
            if row.get("group") != gid or not row["decision"]:
                raise ValueError(f"unresolved or mismatched condition: {qid}")
            if row.get("applied_via") == "individual":
                state["items"][qid] = {"decision": row["decision"], "note": row.get("note", "")}
            elif row.get("applied_via") != "bulk" or (
                row["decision"], row.get("note", "")
            ) != (group["decision"], group.get("note", "")):
                raise ValueError(f"bulk value does not match condition: {qid}")
        overrides = group.get("overrides", [])
        expected_overrides = {q: state["items"][q] for q in ids if q in state["items"]}
        actual_overrides = {o["id"]: {"decision": o["decision"], "note": o.get("note", "")} for o in overrides}
        if len(actual_overrides) != len(overrides) or actual_overrides != expected_overrides:
            raise ValueError(f"group override metadata disagrees with decisions: {gid}")
    untouched = set(rows) - assigned
    if any(rows[q]["decision"] or rows[q].get("note") for q in untouched):
        raise ValueError("out-of-scope condition was changed")
    for note in decisions.get("document_notes", []):
        state["doc"][note["id"]] = note.get("note", "")

    summary = {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_report_at": decisions["report_at"],
        "total_conditions": len(rows), "reviewed_conditions": len(assigned),
        "untouched_conditions": len(untouched), "groups": len(groups),
        "decisions": dict(Counter(rows[q]["decision"] for q in sorted(assigned))),
        "individual_overrides": sorted(state["items"]),
        "notices": len(data["notices"]),
        "notices_with_document_gaps": sum(bool(n.get("d01")) for n in data["notices"]),
        "production_applied": False,
        "document_notes_are_not_automatic_clearance": True,
    }
    data["base"] = decisions
    data["restored_state"] = state
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = html[:match.start(2)] + serialized + html[match.end(2):]
    old = "let S={groups:{},items:{},doc:{}};"
    if html.count(old) != 1:
        raise ValueError("unsupported HTML state initializer")
    html = html.replace(old, "let S=JSON.parse(JSON.stringify(D.restored_state));")
    html = html.replace("if(e.via)d.applied_via=e.via", "if(e.via)d.applied_via=e.via;else delete d.applied_via")
    html = html.replace("const KEY='pai-review-bulk-'+D.base.report_at;",
                        "const KEY='pai-review-restored-" + summary["source_sha256"][:16] + "-'+D.base.report_at;")
    if html_path.resolve() == (output_dir / "review-resumed.html").resolve():
        raise ValueError("output must not overwrite the input HTML")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review-resumed.html").write_text(html, encoding="utf-8")
    (output_dir / "resume-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("html", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(restore(args.decisions, args.html, args.output), ensure_ascii=False, indent=2))
