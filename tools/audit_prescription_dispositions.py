"""Inspect historical review-state routing without rewriting any run outcome.

Counts are affected review events, not independent patients or counterfactual
clinical results. No model call or native mutation is performed.
"""
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_event(event):
    if event.get("event") != "prescription_reviewed":
        return []
    rule = event.get("rule_review") or {}
    ai = event.get("ai_review") or {}
    pharmacist = event.get("pharmacist_review") or {}
    if rule.get("decision") == "reject":
        return []
    issues = []
    if pharmacist.get("decision") == "flag" and event.get("decision") == "reject" and event.get("senior_review") is None:
        issues.append("unresolved_pharmacist_flag_recorded_as_reject")
    automatic = ("reject" if ai.get("decision") == "reject" else "flag"
        if rule.get("decision") == "flag" or ai.get("decision") == "flag" else "approve")
    if (event.get("condition", {}).get("workflow") == "parallel" and pharmacist
            and automatic != pharmacist.get("decision") and event.get("senior_review") is None):
        issues.append("distinct_review_states_without_adjudication")
    return issues


def audit(batches_root, public_path, private_path):
    public_path, private_path = Path(public_path), Path(private_path)
    if public_path.exists() or private_path.exists():
        raise FileExistsError("Keep earlier impact audits")
    rows, evidence = [], []
    for batch in sorted(Path(batches_root).iterdir()):
        if not (batch / "batch_summary.json").exists():
            continue
        counts, runs = Counter(), 0
        for path in sorted(batch.glob("*/**/run.json")):
            before = sha(path)
            run = json.loads(path.read_text())
            runs += 1
            for event in run.get("events", []):
                issues = inspect_event(event)
                if issues:
                    counts.update(issues)
                    evidence.append({"run_path": str(path), "run_sha256": before,
                        "event_sequence": event.get("sequence"), "issues": issues})
            if sha(path) != before:
                raise RuntimeError("Source run changed during inspection")
        rows.append({"batch": batch.name, "observed_runs": runs,
                     "review_event_issue_counts": dict(counts)})
    report = {"version": "prescription-disposition-impact-audit-2",
        "created_at": datetime.now(timezone.utc).isoformat(), "script_sha256": sha(__file__),
        "method": "Static recorded-event inspection; no counterfactual clinical result inferred. Counts are review events, not independent patients.",
        "batches": rows, "affected_review_events": len(evidence),
        "formal_results_rewritten": False, "model_calls": 0}
    for path, value, mode in ((private_path, {"public": report, "evidence": evidence}, 0o600),
                              (public_path, report, 0o644)):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if mode == 0o600 else 0o755)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return {"affected_review_events": len(evidence), "public_report": str(public_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches-root", required=True, type=Path)
    parser.add_argument("--public-report", required=True, type=Path)
    parser.add_argument("--private-report", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.batches_root, args.public_report, args.private_report)))
