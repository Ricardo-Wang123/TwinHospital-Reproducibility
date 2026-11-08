#!/usr/bin/env python3
"""Read-only, pre-run source availability count under the frozen v4 policy."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.case_preparation.current_episode_v4 import candidate_plan, POLICY, scalar
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash, write


def audit(manifest_path, output):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists(): raise FileExistsError("Keep previous audits; use a new output version")
    m = json.loads(manifest_path.read_text()); protected = {str(manifest_path):file_hash(manifest_path)}
    rows, stats = [], defaultdict(Counter)
    for i,e in enumerate(m["cases"],1):
        if file_hash(e["path"]) != e["sha256"]: raise ValueError("Frozen source hash mismatch")
        protected[e["path"]] = e["sha256"]
        c = json.loads(Path(e["path"]).read_text())
        plans, _ = candidate_plan(c); usable = [x for x in plans if not x["reasons"]]
        primary = c["provenance"]["selected_historical_order"]
        labs = [o for o in c["his_initial"]["historical_observations"]
            if o.get("source_encounter_ref") == primary["source_encounter_ref"]
            and (o.get("fluid") or "").casefold() == "blood"
            and type(o.get("time",{}).get("event_offset_hours")) in (int,float)
            and -168 <= o["time"]["event_offset_hours"] <= 0
            and scalar(o.get("numeric_value_raw")) is not None]
        booleans = {"scheduled_candidates_present":bool(usable),
            "previous_primary_pattern_present":any(x["source_order"]["record_id"] == primary["record_id"] for x in usable),
            "blood_creatinine_numeric_in_source_last168h":any((o.get("display") or "").casefold()=="creatinine" for o in labs),
            "blood_potassium_numeric_in_source_last168h":any((o.get("display") or "").casefold()=="potassium" for o in labs)}
        rows.append({"candidate_index":f"C{i:03d}", "case_id":e["case_id"], "case_path":e["path"],
            "case_sha256":e["sha256"], "group":e["group"], "patient_key":e["patient_key"],
            "scheduled_candidate_count":len(usable), "unresolved_candidate_count":len(plans)-len(usable),
            **booleans, "clinical_qualification":"pending; availability predicates are not clinical eligibility"})
        stats[e["group"]].update(cases=1, scheduled_candidate_items=len(usable),
            unresolved_candidate_items=len(plans)-len(usable), **{k:int(v) for k,v in booleans.items()})
    if any(file_hash(p)!=h for p,h in protected.items()): raise RuntimeError("Source changed during audit")
    summary={"version":"current-episode-v4-source-pool-audit-1", "frozen_source_cases":len(rows), "by_group":dict(stats),
        "source_hashes_unchanged":True, "clinical_approved_cases":0, "model_calls":0, "model_outputs_read":False,
        "candidate_filter":"predefined source prescription window and parseable dose/frequency; not a clinical gold or model-outcome filter",
        "lab_window":"numeric historical blood observation within168h before source discharge; new review168h after discharge; these are7–14day-old source tests, not current tests or automatic safety clearance",
        "source_manifest_sha256":protected[str(manifest_path)], "source_predicate_not_clinical_eligibility":True}
    output.mkdir(parents=True, mode=0o700)
    write(output/"summary.json", summary)
    write(output/"candidate_pool_private.json", {"policy":POLICY,"cases":rows,
        "source_exclusion_means":"retrieval criterion only; never clinically stopped/contraindicated/ineligible disease"})
    write(output/"source_hash_audit_private.json", {"protected":protected,"all_unchanged":True})
    write(output/"audit_freeze.json", {"files":[{"path":str(p),"sha256":file_hash(p)} for p in sorted(output.iterdir())],
        "tool_sha256":file_hash(__file__)})
    return summary


if __name__ == "__main__":
    os.umask(0o077); p=argparse.ArgumentParser();p.add_argument("--manifest",required=True);p.add_argument("--output",required=True)
    a=p.parse_args();print(json.dumps(audit(a.manifest,a.output),ensure_ascii=False))
