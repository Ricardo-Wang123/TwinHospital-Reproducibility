"""Measure complete prepared role views without truncating any patient history."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics

from .mimic_cohort import save


def compact_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def describe(values, limit):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "median": None, "p95_nearest_rank": None, "max": None, "over_limit_count": 0}
    return {"count": len(ordered), "min": ordered[0], "median": statistics.median(ordered),
            "p95_nearest_rank": ordered[math.ceil(.95 * len(ordered)) - 1], "max": ordered[-1],
            "over_limit_count": sum(value > limit for value in ordered)}


def audit(folder, limit=120_000):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest_private.json").read_text())
    metrics = {name: [] for name in ("runtime_file_utf8_bytes", "runtime_compact_json_utf8_bytes",
                                    "his_initial_compact_json_utf8_bytes", "patient_initial_compact_json_utf8_bytes")}
    records = []
    for case in manifest["cases"]:
        path = folder / "cases" / case["case_id"] / "runtime_input.json"
        raw = path.read_bytes()
        obj = json.loads(raw)
        item = {"case_id": case["case_id"], "group": case["group"], "split": case["split"],
                "runtime_sha256": hashlib.sha256(raw).hexdigest(),
                "runtime_file_utf8_bytes": len(raw),
                "runtime_compact_json_utf8_bytes": compact_bytes(obj),
                "his_initial_compact_json_utf8_bytes": compact_bytes(obj["his_initial"]),
                "patient_initial_compact_json_utf8_bytes": compact_bytes(obj["patient_initial"])}
        records.append(item)
        for name in metrics:
            metrics[name].append(item[name])
    report = {"case_count": len(records), "comparison_limit_utf8_bytes": limit,
              "units": "UTF-8 bytes, not tokenizer tokens",
              "metrics": {name: describe(values, limit) for name, values in metrics.items()},
              "whole_prompt_limit_caveat": "System instructions, conversation history and tool messages add to these sizes; passing a case-only check does not certify a full API request.",
              "history_truncated": False,
              "required_followup": "A provenance-preserving chart retrieval/view-selection interface is required before using over-limit histories; preserve full local records and log requested/returned record IDs and time bounds.",
              "formulary_scope": "The five-item synthetic FORMULARY is an engineering catalogue only. MIMIC linked cases have an empty formulary pending normalization/native mapping. It neither covers all source medicines nor validates all six medication-error classes."}
    report["runtime_compact_by_group"] = {group: describe([r["runtime_compact_json_utf8_bytes"] for r in records if r["group"] == group], limit)
                                            for group in sorted({r["group"] for r in records})}
    report["runtime_compact_by_split"] = {split: describe([r["runtime_compact_json_utf8_bytes"] for r in records if r["split"] == split], limit)
                                            for split in sorted({r["split"] for r in records})}
    pilot = folder / "pilot50_private_manifest.json"
    if pilot.exists():
        pilot_ids = {r["case_id"] for r in json.loads(pilot.read_text())["cases"]}
        report["pilot50_runtime_compact"] = describe([r["runtime_compact_json_utf8_bytes"] for r in records if r["case_id"] in pilot_ids], limit)
    save(folder / "runtime_view_sizes_private.json", {"cases": records})
    save(folder / "runtime_view_size_summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--limit", type=int, default=120_000)
    args = parser.parse_args()
    print(json.dumps(audit(args.folder, args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
