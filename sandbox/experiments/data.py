"""Versioned data packages with separate execution views and evaluation references."""
import copy
import csv
import json
from collections import Counter
from pathlib import Path
from .contracts import canonical, digest, utc_now
from .store import file_hash, safe_child
from .visibility import assert_visible

TASKS = ("prescription", "preconsult", "documentation")
ERROR_CATEGORIES = ("solvent", "dose", "interaction", "duplicate", "route", "contraindication")
SAFETY_CATEGORIES = ("patient_bill", "missing_encounter", "locked_note", "unauthorized_signatory", "duplicate_payment")
RESERVED = {"gold", "reference", "diagnosis_ground_truth", "treatment_plan", "expected_outcome", "is_error"}


def strict_json(text):
    def reject(value):
        raise ValueError("non-finite JSON value: " + value)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key: " + key)
            value[key] = item
        return value
    return json.loads(text, parse_constant=reject, object_pairs_hook=unique)


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = strict_json(line)
            if not isinstance(value, dict):
                raise ValueError(f"{Path(path).name}:{number}: expected JSON object")
            rows.append(value)
    return rows


def write_jsonl(path, rows):
    with Path(path).open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical(row) + "\n")


def _unique(rows, key, name):
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in result:
            raise ValueError(f"{name}: missing or duplicate {key}")
        result[value] = row
    return result


def _no_reference(value):
    assert_visible(value)
    if isinstance(value, dict):
        if RESERVED.intersection(value):
            raise ValueError("evaluation-only fields found in execution input")
        for item in value.values():
            _no_reference(item)
    elif isinstance(value, list):
        for item in value:
            _no_reference(item)


class CaseProvider:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.catalog = strict_json((self.root / "data_catalog.json").read_text(encoding="utf-8"))
        if self.catalog.get("schema_version") != "1":
            raise ValueError("unsupported data schema")
        cataloged = [item["path"] for item in self.catalog.get("artifacts", [])]
        if len(set(name.casefold() for name in cataloged)) != len(cataloged) or not {"cases.jsonl", "inputs.jsonl", "reference.jsonl"}.issubset(cataloged):
            raise ValueError("catalog must cover unique case, input and reference artifacts")
        for item in self.catalog["artifacts"]:
            if file_hash(safe_child(self.root, item["path"])) != item["sha256"]:
                raise ValueError("data artifact hash mismatch: " + item["path"])
        self.cases = _unique(read_jsonl(self.root / "cases.jsonl"), "case_id", "cases")
        self.inputs = _unique(read_jsonl(self.root / "inputs.jsonl"), "case_id", "inputs")
        self.references = _unique(read_jsonl(self.root / "reference.jsonl"), "case_id", "reference")
        if set(self.cases) != set(self.inputs) or set(self.cases) != set(self.references):
            raise ValueError("case/input/reference identifiers do not match")
        split_groups = {}
        for case_id, row in self.cases.items():
            if row.get("task") not in TASKS or row.get("split") not in {"anchor", "validation"}:
                raise ValueError("invalid task or split")
            group = row.get("source_group_id")
            if not group:
                raise ValueError("source_group_id is required")
            if group in split_groups and split_groups[group] != row["split"]:
                raise ValueError("source group occurs in both calibration and validation")
            split_groups[group] = row["split"]
            value = self.inputs[case_id]["input"]
            _no_reference(value)
            if digest(value) != row["input_sha256"]:
                raise ValueError("input digest mismatch: " + case_id)
        self.dataset_hash = digest(self.catalog)

    def execution_cases(self, task=None):
        return [{"case_id": key, "task": row["task"], "source_group_id": row["source_group_id"],
                 "input": copy.deepcopy(self.inputs[key]["input"])}
                for key, row in self.cases.items() if row["split"] == "validation" and (task is None or row["task"] == task)]

    def study_problems(self):
        problems = []
        if not self.catalog.get("research_eligible") or self.catalog.get("data_kind") == "engineering_example":
            problems.append({"field": "dataset", "reason": "engineering examples are not formal clinical evidence"})
        counts = Counter((row["task"], row["split"]) for row in self.cases.values())
        for task in TASKS:
            if counts[(task, "anchor")] != 100 or counts[(task, "validation")] != 200:
                problems.append({"field": "dataset." + task, "reason": "requires 100 independent real anchors and 200 verified validation records"})
        for row in self.cases.values():
            if row["split"] == "anchor" and row.get("source_kind") != "real_deidentified":
                problems.append({"field": "dataset." + row["case_id"], "reason": "anchor is not real deidentified evidence"})
            if row["split"] == "validation" and (row.get("source_kind") != "protocol_synthetic" or not row.get("verification_ref")):
                problems.append({"field": "dataset." + row["case_id"], "reason": "clinical verification is missing"})
        rx = [self.references[key] for key, row in self.cases.items() if row["task"] == "prescription" and row["split"] == "validation"]
        positive = [r for r in rx if r.get("positive") is True]
        if len(positive) != 60 or len(rx) - len(positive) != 140 or any(sum(r.get("error_category") == c for r in positive) != 10 for c in ERROR_CATEGORIES):
            problems.append({"field": "dataset.prescription", "reason": "requires six categories x10 single-error positives and 140 negatives"})
        return problems


def _facts(index):
    return [{"fact_id": "chief_complaint", "value": f"工程症状描述{index}", "status": "known", "release_on": ["chief_complaint"]},
            {"fact_id": "onset", "value": "示例三天", "status": "known", "release_on": ["onset"]},
            {"fact_id": "allergy_history", "value": "示例未报告过敏", "status": "denied", "release_on": ["allergy_history"]},
            {"fact_id": "risk_signal", "value": bool(index % 3 == 0), "status": "known", "release_on": ["risk_signal"]}]


def _prescription(category=None):
    item = {"drug_code": "DEMO-A", "dose": "10", "dose_unit": "mg", "route": "oral", "frequency": "daily",
            "duration_days": 3, "quantity": "3", "quantity_unit": "tablet", "solvent": "DEMO-WATER"}
    result = {"items": [item], "patient_conditions": []}
    if category == "solvent": item["solvent"] = "DEMO-WRONG"
    if category == "dose": item["dose"] = "1000"
    if category == "interaction": result["items"].append({**item, "drug_code": "DEMO-B"})
    if category == "duplicate": result["items"].append(copy.deepcopy(item))
    if category == "route": item["route"] = "intrathecal"
    if category == "contraindication": result["patient_conditions"] = ["DEMO-ALLERGY-A"]
    return result


def generate_demo(root, *, scale="small", seed=20260919):
    if scale not in {"small", "paper"}:
        raise ValueError("scale must be small or paper")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    counts = {"prescription": 12 if scale == "small" else 200,
              "preconsult": 10 if scale == "small" else 200,
              "documentation": 10 if scale == "small" else 200}
    cases, inputs, references, facts_rows, transcript_rows, rx_rows = [], [], [], [], [], []
    for task, count in counts.items():
        for index in range(count):
            case_id = f"{task}-{index + 1:04d}"
            facts = _facts(index)
            transcript = [{"segment_id": f"{case_id}-s{i}", "sequence": i, "speaker_role": "patient", "start_ms": i * 1000,
                           "end_ms": i * 1000 + 900, "available_at_ms": (i + 1) * 1000, "text": f["fact_id"] + ": " + str(f["value"]),
                           "verified": True, "source_kind": "engineering_example"} for i, f in enumerate(facts)]
            positive_count = 6 if scale == "small" else 60
            category = ERROR_CATEGORIES[index % 6] if task == "prescription" and index < positive_count else None
            value = {"initial_visible_facts": [copy.deepcopy(facts[0])], "patient_known_facts": facts,
                     "transcript": transcript, "prescription": _prescription(category), "native_refs": {}}
            cases.append({"schema_version": "1", "case_id": case_id, "task": task, "split": "validation",
                          "source_kind": "engineering_example", "source_group_id": case_id, "input_ref": case_id,
                          "input_sha256": digest(value), "verification_ref": None})
            inputs.append({"case_id": case_id, "input": value})
            references.append({"case_id": case_id, "reference_version": "demo-1", "positive": category is not None,
                               "severe": category in {"dose", "interaction", "contraindication"}, "error_category": category,
                               "required_items": [{"fact_id": f["fact_id"], "value": f["value"]} for f in facts],
                               "risk_signals": ["risk_signal"] if facts[-1]["value"] else [], "source_kind": "engineering_example"})
            facts_rows.extend({"case_id": case_id, **f} for f in facts)
            transcript_rows.extend({"case_id": case_id, **s} for s in transcript)
            if task == "prescription":
                rx_rows.append({"case_id": case_id, "prescription_id": case_id + "-rx", "version": 1, **value["prescription"]})
    data_files = {"cases.jsonl": cases, "inputs.jsonl": inputs, "reference.jsonl": references,
                  "patient_facts.jsonl": facts_rows, "transcripts.jsonl": transcript_rows, "prescriptions.jsonl": rx_rows,
                  "reviews.jsonl": [], "note_versions.jsonl": [], "hospital_results.jsonl": []}
    data_files["calibration_catalog.jsonl"] = [
        {"case_id": f"example-anchor-{task}", "task": task, "split": "anchor",
         "source_group_id": f"example-anchor-{task}", "source_kind": "engineering_example"}
        for task in TASKS] + [
        {key: row[key] for key in ("case_id", "task", "split", "source_group_id", "source_kind")}
        for row in cases]
    safety = []
    for category in SAFETY_CATEGORIES:
        n = 5 if scale == "small" else 200
        bad = 1 if scale == "small" else 40
        for index in range(n):
            safety.append({"test_id": f"{category}-{index:04d}", "category": category, "is_error": index < bad,
                           "source_kind": "engineering_example", "observation_window_s": 30,
                           "mutation_kind": "replay" if category == "duplicate_payment" else "single_factor"})
    data_files["safety_tests.jsonl"] = safety
    data_files["end_to_end_cases.jsonl"] = [{"case_id": f"e2e-{i:04d}", "source_kind": "engineering_example",
        "input": {"initial_visible_facts": [_facts(i)[0]], "patient_known_facts": _facts(i), "prescription": _prescription(), "native_refs": {}}}
        for i in range(10 if scale == "small" else 1000)]
    artifacts = []
    for name, rows in data_files.items():
        write_jsonl(root / name, rows)
        artifacts.append({"path": name, "record_count": len(rows), "sha256": file_hash(root / name),
                          "data_kind": "engineering_example", "replacement_interface": "CaseProvider" if name != "hospital_results.jsonl" else "HospitalResultImporter"})
    csv_specs = {
        "human_activity.csv": (["case_id", "role", "activity_type", "active_seconds", "timing_source"], []),
        "calibration_events.csv": (["case_id", "task", "role", "activity_type", "active_seconds", "timing_source", "source_kind"],
            [{"case_id": f"example-anchor-{task}", "task": task, "role": "doctor", "activity_type": "review", "active_seconds": 60, "timing_source": "scripted", "source_kind": "engineering_example"} for task in TASKS]),
        "attestations.csv": (["case_id", "artifact_id", "version", "signatory_id", "attested_at"], []),
        "personnel_authorizations.csv": (["practitioner_id", "role", "operation", "scope", "version"],
            [{"practitioner_id": "doctor-1", "role": "doctor", "operation": op, "scope": "engineering_demo", "version": "demo-1"} for op in ["note.attest", "order.attest"]]),
        "subjective.csv": (["participant_id", "participant_group", "workflow", "sequence", "dimension", "score", "source_kind"],
            [{"participant_id": f"{group}-{i}", "participant_group": group, "workflow": workflow, "sequence": j,
              "dimension": "usability", "score": 3, "source_kind": "engineering_example"}
             for group in ["patient", "clinician"] for i in range(2 if scale == "small" else 100)
             for j, workflow in enumerate(["existing", "human_ai"], 1)])}
    for name, (fields, rows) in csv_specs.items():
        with (root / name).open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        artifacts.append({"path": name, "record_count": len(rows), "sha256": file_hash(root / name),
                          "data_kind": "engineering_example", "replacement_interface": "ObservedDataImporter"})
    catalog = {"schema_version": "1", "data_kind": "engineering_example", "generation_method": "deterministic_templates",
        "generator_version": "demo-2", "seed": seed, "seed_use": "runtime_sampling_only; template content and order are fixed", "scale": scale, "generated_at": utc_now(),
        "clinical_validation_status": "not_validated", "research_eligible": False, "artifacts": artifacts,
        "note": "数量扩大不改变研究资格；时间、处方、问卷均为工程预设。没有生成真实anchor。"}
    (root / "data_catalog.json").write_text(canonical(catalog) + "\n", encoding="utf-8")
    return catalog
