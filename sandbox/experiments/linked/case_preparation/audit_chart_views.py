"""Local-only byte and pagination audit of an explicit private case manifest."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics

from ..chart import HistoryChart, INITIAL_BYTE_LIMIT, PAGE_BYTE_LIMIT, json_bytes
from .mimic_cohort import save


def audit(manifest_path, output):
    manifest_path = Path(manifest_path).resolve()
    records = json.loads(manifest_path.read_text())["cases"]
    initial_sizes, page_sizes, expansion_counts = [], [], Counter()
    per_case, excluded = [], 0
    for item in records:
        path = Path(item["path"])
        if not path.is_absolute():
            path = manifest_path.parent / path
        raw = json.loads(path.read_text())["his_initial"]
        raw_hash = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        chart = HistoryChart(raw)
        view = chart.initial_view()
        initial_sizes.append(json_bytes(view))
        excluded += view["excluded_non_whitelisted_field_count"]
        n_pages, n_returned, n_expansion = 0, 0, 0
        for section in view["sections"]:
            cursor, seen, expanded = 0, set(), 0
            while cursor is not None:
                page = chart.query(section, offset=cursor)
                assert json_bytes(page) <= PAGE_BYTE_LIMIT
                page_sizes.append(json_bytes(page))
                n_pages += 1
                for row in page["records"]:
                    assert row["record_ref"] not in seen
                    seen.add(row["record_ref"])
                expanded += len(page["requires_expansion"])
                expansion_counts[section] += len(page["requires_expansion"])
                next_cursor = page["next_offset"]
                assert next_cursor is None or next_cursor > cursor
                cursor = next_cursor
            assert len(seen) + expanded == view["sections"][section]["total_count"]
            n_returned += len(seen)
            n_expansion += expanded
        assert json_bytes(view) <= INITIAL_BYTE_LIMIT
        assert hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == raw_hash
        per_case.append({"case_id": item["case_id"], "his_initial_sha256": raw_hash,
                         "initial_view_utf8_bytes": initial_sizes[-1], "pages": n_pages,
                         "returned_records_across_sections": n_returned, "requires_expansion_records": n_expansion})
    result = {"case_count": len(records), "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "initial_view_utf8_bytes": {"median": statistics.median(initial_sizes), "max": max(initial_sizes), "limit": INITIAL_BYTE_LIMIT},
              "query_pages_utf8_bytes": {"count": len(page_sizes), "median": statistics.median(page_sizes), "max": max(page_sizes), "limit": PAGE_BYTE_LIMIT},
              "requires_expansion_by_section": dict(expansion_counts), "non_whitelisted_fields_excluded": excluded,
              "pagination_all_records_accounted_for": True, "full_history_unchanged": True,
              "bytes_representation": "json.dumps(ensure_ascii=False,separators=(',',':')).encode('utf-8')",
              "scope": "all unfiltered pages in every declared section for this manifest; clinical accuracy not evaluated"}
    output = Path(output)
    save(output / "chart_view_audit_private.json", {"cases": per_case})
    save(output / "chart_view_audit_summary.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.manifest, args.output), ensure_ascii=False))
