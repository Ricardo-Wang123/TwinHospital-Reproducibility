"""Read-only links between formal-study materials; never proof of hospital truth.

``study_material_problems(config, dataset)`` supplements CaseProvider.study_problems
and config.readiness. It does not execute actors, PPRS, SQL, HTTP, or write files.
Paths in config follow PROJECT_ROOT; raw calibration sources use the existing
dataset calibration_catalog.jsonl/calibration_events.csv and frozen artifact hashes.
CSV active_seconds is parsed as float, matching the calibration CLI; IDs/versions
stay strings. Clinical records use verification_id as cases.verification_ref and
hash the full input and reference objects using the existing canonical JSON rule.

All 100 independent anchors per task must support calibration, but rare activities
have no invented minimum sample size beyond nonempty measured support. The activity
list follows workflows.py branches, including revision and supplemental inquiry.
The caller must update it when clinical effort branches change. Clinical expertise,
consent, actual measurement, credential validity and source authenticity still need
independent review. An empty problem list would mean only local contract consistency.
"""
import csv
import hashlib
import io
from pathlib import Path

from sandbox.paths import PROJECT_ROOT
from .actors import ACTOR_IDS, ROLE_ALIASES
from .contracts import digest
from .data import TASKS, strict_json
from .research import calibrate
from .store import file_hash, safe_child


REQUIRED_ACTIVITIES = {
    "prescription": (("pharmacist", "prescription_review"),
        ("senior_pharmacist", "prescription_adjudication"),
        ("doctor", "prescription_revision_review"), ("doctor", "prescription_revision")),
    "preconsult": (("nurse", "information_collection"), ("nurse", "summary_documentation"),
        ("nurse", "summary_review"), ("nurse", "review_supplementation"),
        ("doctor", "summary_verification"), ("doctor", "redundant_inquiry"),
        ("doctor", "supplemental_inquiry")),
    "documentation": (("doctor", "documentation"), ("doctor", "note_review"),
        ("doctor", "note_rework"), ("doctor", "attestation")),
}


class _Materials:
    def __init__(self, config, dataset):
        self.config, self.dataset = config, dataset
        self.problems, self.validators = [], {}

    def need(self, field, reason):
        problem = {"field": field, "reason": reason}
        if problem not in self.problems:
            self.problems.append(problem)

    def valid(self, rows, schema_name, field):
        """Use bundled schemas only, and report no values from validation errors."""
        try:
            if schema_name not in self.validators:
                from jsonschema import Draft202012Validator, FormatChecker
                from referencing import Registry, Resource
                root = Path(__file__).parent / "schemas"
                def no_remote(uri):
                    raise ValueError("unbundled schema reference")
                schemas = [strict_json((root / name).read_text(encoding="utf-8"))
                           for name in ("common.json", schema_name)]
                registry = Registry(retrieve=no_remote).with_resources(
                    (schema["$id"], Resource.from_contents(schema)) for schema in schemas)
                self.validators[schema_name] = Draft202012Validator(
                    schemas[-1], registry=registry, format_checker=FormatChecker())
            ok = True
            for index, row in enumerate(rows, 1):
                error = next(self.validators[schema_name].iter_errors(row), None)
                if error is not None:
                    self.need(f"{field}[{index}]", f"schema {schema_name}: {error.validator} constraint failed")
                    ok = False
            return ok
        except (ImportError, OSError, ValueError, KeyError, TypeError):
            self.need(field + ".schema", "bundled schema or local validator unavailable; explicit adapter required")
            return False

    def read(self, path, field, schema=None, *, object_only=False, expected_hash=None):
        try:
            if not isinstance(path, (str, Path)) or not str(path).strip():
                raise ValueError("missing path")
            path = Path(path)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            raw = path.read_bytes()
            if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
                self.need(field, "actual artifact bytes differ from the frozen dataset hash")
                return None
            text = raw.decode("utf-8-sig")
            if path.suffix.lower() == ".json":
                value = strict_json(text)
                rows = value if isinstance(value, list) else [value]
            elif not object_only and path.suffix.lower() == ".jsonl":
                rows = [strict_json(line) for line in text.splitlines() if line.strip()]
            elif not object_only and path.suffix.lower() == ".csv":
                reader = csv.DictReader(io.StringIO(text))
                if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                    raise ValueError("missing or duplicate CSV header")
                rows = list(reader)
                for row in rows:
                    if None in row:
                        raise ValueError("CSV row has unexpected columns")
                    for key, value in row.items():
                        if value == "": row[key] = None
                    if schema == "calibration-event.json" and row.get("active_seconds") is not None:
                        row["active_seconds"] = float(row["active_seconds"])
            else:
                raise ValueError("unsupported material container")
            if object_only and (path.suffix.lower() != ".json" or not isinstance(value, dict)):
                raise ValueError("expected single JSON object")
            if not rows or not all(isinstance(row, dict) for row in rows):
                raise ValueError("expected nonempty object records")
            # jsonschema's numeric checks alone do not reject all nonfinite floats.
            digest(rows)
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
            self.need(field, "missing, unreadable or unsupported material format; explicit adapter required")
            return None
        if schema and not self.valid(rows, schema, field):
            return None
        return rows[0] if object_only else rows

    def configured(self, field, schema=None, *, object_only=False):
        return self.read(self.config.get(field), field, schema, object_only=object_only)

    def bound_source(self, name, schema):
        field = "dataset." + name
        artifacts = self.dataset.catalog.get("artifacts", [])
        matches = [row for row in artifacts if isinstance(row, dict) and row.get("path") == name]
        if len(matches) != 1 or not isinstance(matches[0].get("sha256"), str):
            self.need(field, "calibration source must have exactly one frozen artifact hash")
            return None
        try:
            path = safe_child(self.dataset.root, name)
        except (OSError, ValueError, TypeError):
            self.need(field, "dataset source path is unavailable or outside dataset root")
            return None
        return self.read(path, field, schema, expected_hash=matches[0]["sha256"])

    def calibration(self):
        report = self.configured("calibration_file", "calibration.json", object_only=True)
        catalog = self.bound_source("calibration_catalog.jsonl", "calibration-catalog.json")
        events = self.bound_source("calibration_events.csv", "calibration-event.json")
        if report is not None:
            for flag in ("research_eligible", "isolation_verified"):
                if report[flag] is not True:
                    self.need("calibration_file." + flag, "independent measured calibration declaration must be true")
            for key in ("unused_anchor_cases", "missing_source_groups"):
                if report[key] != 0:
                    self.need("calibration_file." + key, "all anchors must have measured support and an explicit source group")
            if report["anchor_cases"] != 300:
                self.need("calibration_file.anchor_cases", "requires three tasks with 100 anchors each")
            distributions = {}
            for row in report["distributions"]:
                key = (row["task"], row["role"], row["activity_type"])
                if key in distributions:
                    self.need("calibration_file.distributions", "duplicate task/role/activity distribution")
                distributions[key] = row
                if (row["timing_sources"] != {"measured": row["n_intervals"]}
                        or row["n_intervals"] != len(row["samples_seconds"])
                        or row["n_cases"] < 1 or row["n_source_groups"] < 1):
                    self.need("calibration_file.measured", "every distribution needs nonempty measured case/source support")
            required = {(task, role, activity) for task, pairs in REQUIRED_ACTIVITIES.items() for role, activity in pairs}
            if self.config.get("human_transcript_handling") is True:
                required.add(("documentation", "scribe", "transcript_handling"))
            for task, role, activity in sorted(required - distributions.keys()):
                self.need(f"calibration_file.distributions.{task}.{role}.{activity}", "missing required measured activity; no timing fallback allowed")
            for task in TASKS:
                if report["anchors_by_task"].get(task) != 100:
                    self.need("calibration_file.anchors." + task, "requires 100 supported independent anchors per task")
        if catalog is None or events is None:
            return
        def metadata(row):
            result = {key: row.get(key) for key in ("case_id", "task", "split", "source_group_id", "source_kind")}
            result["split"] = {"calibration": "anchor", "test": "validation"}.get(result["split"], result["split"])
            return result
        supplied = {row["case_id"]: metadata(row) for row in catalog}
        actual = {key: metadata(row) for key, row in self.dataset.cases.items()}
        if len(supplied) != len(catalog) or supplied != actual:
            self.need("calibration_file.catalog", "anchor and exclusion catalog must match this dataset's actual metadata")
        anchors = [row for row in supplied.values() if row["split"] == "anchor"]
        if any(row["source_kind"] != "real_deidentified" for row in anchors):
            self.need("calibration_file.source_kind", "formal anchors require real_deidentified source declarations")
        all_groups = set()
        for task in TASKS:
            rows = [row for row in anchors if row["task"] == task]
            groups = {row["source_group_id"] for row in rows}
            supported = {row["case_id"] for row in events if row["task"] == task}
            if len(rows) != 100 or len(groups) != 100 or len(supported) != 100 or all_groups.intersection(groups):
                self.need("calibration_file.anchors." + task, "requires 100 measured independent anchors, without source reuse across tasks")
            all_groups.update(groups)
        if any(row["timing_source"] != "measured" or row["source_kind"] != "real_deidentified" for row in events):
            self.need("calibration_file.measured", "raw activity rows must declare measured times from real deidentified anchors")
        try:
            computed = calibrate(events, catalog)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.need("calibration_file.recomputed", "raw anchor/activity/exclusion links are inconsistent")
            return
        if report is not None and digest({key: report.get(key) for key in computed}) != digest(computed):
            self.need("calibration_file.recomputed", "frozen calibration differs from recomputation of the linked raw materials")

    def clinical(self):
        rows = self.configured("clinical_verification_file", "clinical-verification.json")
        if rows is None:
            return
        records = {}
        for row in rows:
            if row["verification_id"] in records:
                self.need("clinical_verification_file.verification_id", "duplicate verification identifier")
            records[row["verification_id"]] = row
        validation = [row for row in self.dataset.cases.values() if row.get("split") == "validation"]
        for index, case in enumerate(validation, 1):
            field = f"clinical_verification_file.validation[{index}]"
            ref_id = case.get("verification_ref")
            row = records.get(ref_id) if isinstance(ref_id, str) else None
            if row is None:
                self.need(field + ".verification_ref", "validation case lacks a matching verification record")
                continue
            if row["case_id"] != case.get("case_id"):
                self.need(field + ".case_id", "verification record belongs to a different case")
            try:
                input_row = self.dataset.inputs[case["case_id"]]
                reference = self.dataset.references[case["case_id"]]
                if input_row.get("case_id") != case["case_id"] or reference.get("case_id") != case["case_id"]:
                    raise ValueError("dataset reference identity mismatch")
                if row["input_sha256"] != digest(input_row["input"]) or row["input_sha256"] != case.get("input_sha256"):
                    self.need(field + ".input_sha256", "verification hash does not match the actual full input")
                if row["reference_sha256"] != digest(reference):
                    self.need(field + ".reference_sha256", "verification hash does not match the actual reference object")
            except (ValueError, KeyError, TypeError, AttributeError):
                self.need(field, "missing or inconsistent dataset input/reference linkage")
                continue
            if row["verification_status"] != "accepted":
                self.need(field + ".verification_status", "linked clinical verification must be accepted")
            if len(set(row["reviewers"])) != len(row["reviewers"]):
                self.need(field + ".reviewers", "reviewer identifiers must be distinct; independence still requires external evidence")
            if row["source_kind"] != "protocol_synthetic" or case.get("source_kind") != "protocol_synthetic":
                self.need(field + ".source_kind", "formal validation inputs must declare protocol_synthetic provenance")
            if case.get("task") == "prescription" and reference.get("positive") is True and row["single_fault_confirmed"] is not True:
                self.need(field + ".single_fault_confirmed", "positive prescription case requires explicit single-fault confirmation")

    def pprs(self):
        spec = self.config.get("pprs", {})
        if not isinstance(spec, dict) or spec.get("provider") != "replay":
            self.need("pprs.provider", "formal rule materials require the existing frozen replay adapter")
            return
        rows = self.read(spec.get("replay_file"), "pprs.replay_file", "pprs-replay.json")
        if rows is None:
            return
        hashes = set()
        for row in rows:
            if row["rule_version"] != spec.get("version"):
                self.need("pprs.replay_file.rule_version", "replay rule version differs from the configured frozen version")
            if row["input_hash"] in hashes:
                self.need("pprs.replay_file.input_hash", "duplicate replay hash is not executable by ReplayPPRS")
            hashes.add(row["input_hash"])
        for index, case in enumerate(self.dataset.cases.values(), 1):
            if case.get("task") != "prescription" or case.get("split") != "validation":
                continue
            try:
                prescription = self.dataset.inputs[case["case_id"]]["input"]["prescription"]
                # workflows.py calls review(prescription) with no context argument.
                matched = isinstance(prescription, dict) and digest(prescription) in hashes
            except (KeyError, TypeError, ValueError):
                matched = False
            if not matched:
                self.need(f"pprs.replay_file.validation[{index}]", "no replay for the actual prescription-only input hash")

    def authorizations(self):
        rows = self.configured("authorization_file", "personnel-authorization.json")
        if rows is None:
            return
        mapping = self.config.get("actor_ids", {})
        personnel = self.config.get("personnel", {})
        if not isinstance(mapping, dict) or not isinstance(personnel, dict):
            self.need("authorization_file.actor_ids", "actor and personnel mappings must be objects")
            return
        actor_ids = {**ACTOR_IDS, **mapping}
        if "physician" in actor_ids:
            actor_ids["doctor"] = actor_ids["physician"]
        authorized, versions = set(), {}
        for index, row in enumerate(rows, 1):
            role = ROLE_ALIASES.get(row["role"], row["role"])
            actor = actor_ids.get(role)
            if row["practitioner_id"] != actor:
                self.need(f"authorization_file[{index}].practitioner_id", "authorization does not match configured role actor_id")
            if row["scope"] == "engineering_demo":
                self.need(f"authorization_file[{index}].scope", "engineering demo scope is not formal authorization")
            key = (row["practitioner_id"], role, row["operation"], row["scope"])
            if key in versions:
                self.need("authorization_file.version", "duplicate or conflicting authorization version for one permission scope")
            versions[key] = row["version"]
            authorized.add((row["practitioner_id"], role, row["operation"]))
        if (actor_ids.get("doctor"), "doctor", "note.attest") not in authorized:
            self.need("authorization_file.note.attest", "configured doctor lacks a matching note-attestation authorization row")
        for index, (actor, operations) in enumerate(personnel.items(), 1):
            if not isinstance(operations, list) or not all(isinstance(op, str) for op in operations):
                self.need(f"authorization_file.personnel[{index}]", "personnel permissions must be a string list")
                continue
            for operation in operations:
                roles = {role for role, configured in actor_ids.items() if configured == actor}
                if not any((actor, role, operation) in authorized for role in roles):
                    self.need(f"authorization_file.personnel[{index}]", "configured permission lacks a matching actor/role/operation authorization")
        # Scope interpretation/expiry is not defined in this schema. Do not turn a
        # nonempty scope or version string into proof of currently valid privileges.

    def institution(self, check_cases):
        """Verify a frozen master-data projection and per-case mapping subsets."""
        from .native_mapping import map_native_request, NativeMappingError
        catalog = self.configured("catalog_file", "institution_catalog.schema.json", object_only=True)
        if catalog is None:
            return
        if catalog["source_kind"] != "hospital_observed" or catalog["research_eligible"] is not True:
            self.need("catalog_file.research_eligible", "formal dictionaries require hospital_observed and research_eligible=true declarations; engineering dictionaries cannot qualify")
        if catalog["instance_id"] != self.config.get("instance_id"):
            self.need("catalog_file.instance_id", "dictionary belongs to a different configured instance")
        verification = catalog["verification"]
        if verification["verification_status"] != "accepted":
            self.need("catalog_file.verification.verification_status", "dictionary verification must be accepted")
        body = {key: value for key, value in catalog.items() if key != "verification"}
        if digest(body) != verification["content_sha256"]:
            self.need("catalog_file.verification.content_sha256", "dictionary contents/version differ from the reviewed content digest")
        path = Path(self.config["catalog_file"])
        root = (path if path.is_absolute() else PROJECT_ROOT / path).resolve().parent
        seen_sources = set()
        for index, artifact in enumerate(verification["source_artifacts"], 1):
            field = f"catalog_file.verification.source_artifacts[{index}]"
            try:
                source = safe_child(root, artifact["path"])
                if source in seen_sources or source == (root / path.name):
                    raise ValueError("duplicate or self-referential source")
                seen_sources.add(source)
                if file_hash(source) != artifact["sha256"]:
                    raise ValueError("source mismatch")
            except (OSError, ValueError):
                self.need(field, "source artifact is missing, changed, duplicated or outside catalog directory")
        dictionaries, mapping = catalog["dictionaries"], catalog["native_mapping"]
        # The projection's IDs refer to their own source tables. Pharmacy location
        # IDs are not assumed to be department IDs, and price-detail IDs are not
        # silently relabelled as inventory primary keys.
        for group in ("drugs", "diagnoses", "departments"):
            ids = [row["native_id"] for row in dictionaries[group].values()]
            if len(set(ids)) != len(ids) or any(int(value) > 9223372036854775807 for value in ids):
                self.need("catalog_file.dictionaries." + group, "duplicate or out-of-range native identifiers")
        for group in ("fees", "inventory"):
            if any(int(value) > 9223372036854775807 for value in dictionaries[group]):
                self.need("catalog_file.dictionaries." + group, "native identifiers exceed signed int64")
        for index, fee in enumerate(dictionaries["fees"].values(), 1):
            if fee["drug_code"] not in dictionaries["drugs"] or any(unit["code"] not in dictionaries["units"] for unit in fee["quantity_units"].values()):
                self.need(f"catalog_file.dictionaries.fees[{index}]", "fee references an undeclared drug or unit")
        for index, stock in enumerate(dictionaries["inventory"].values(), 1):
            fee = dictionaries["fees"].get(stock["fee_id"])
            if (stock["drug_code"] not in dictionaries["drugs"] or not fee
                    or fee["drug_code"] != stock["drug_code"] or int(stock["location_id"]) > 9223372036854775807):
                self.need(f"catalog_file.dictionaries.inventory[{index}]", "inventory drug/fee/location linkage is inconsistent")
        if set(mapping["drugs"]) != set(dictionaries["drugs"]):
            self.need("catalog_file.native_mapping.drugs", "frozen drug dictionary and mapping must cover the same declared drug codes")
        for index, (code, spec) in enumerate(mapping["drugs"].items(), 1):
            field = f"catalog_file.native_mapping.drugs[{index}]"
            native = spec["native"]
            drug = dictionaries["drugs"].get(code)
            fee = dictionaries["fees"].get(native["definitionId"])
            stock = dictionaries["inventory"].get(native["definitionDetailId"])
            consistent = bool(drug and fee and stock)
            if consistent:
                pairs = (("adviceDefinitionId", "native_id"), ("pharmacologyCategoryCode", "pharmacology_category_code"),
                    ("categoryCode", "category_code"), ("skinTestFlag", "skin_test_flag"), ("injectFlag", "inject_flag"))
                consistent = (all(native[left] == drug[right] for left, right in pairs)
                    and all(spec[key] == drug[key] for key in ("dose_units", "routes", "frequencies"))
                    and fee["drug_code"] == stock["drug_code"] == code
                    and fee["quantity_units"] == spec["quantity_units"]
                    and stock["fee_id"] == native["definitionId"] and stock["location_id"] == native["locationId"]
                    and stock["lot_number"] == native["lotNumber"]
                    and all(unit in dictionaries["units"] for unit in spec["dose_units"].values())
                    and all(unit["code"] in dictionaries["units"] for unit in spec["quantity_units"].values()))
            if not consistent:
                self.need(field, "native drug, unit, price or inventory projection disagrees with frozen dictionaries")
            # Pure DTO preparation exercises the current adapter's actual numeric
            # constraints. These unit probes are never clinical inputs or HIS calls.
            for quantity_unit in spec["quantity_units"]:
                probe = {"drug_code": code, "dose": "1", "dose_unit": next(iter(spec["dose_units"])),
                    "route": next(iter(spec["routes"])), "frequency": next(iter(spec["frequencies"])),
                    "quantity": "1", "quantity_unit": quantity_unit}
                try:
                    map_native_request("order.save", {"items": [probe]}, {
                        "mapping": mapping, "requests": {"order.save": {"adviceSaveList": [{}]}}})
                except (NativeMappingError, ArithmeticError, ValueError):
                    self.need(field, "frozen mapping is incompatible with the current native mapping numeric/DTO contract")
        if not check_cases:
            return
        cases = [row for row in self.dataset.cases.values() if row.get("split") == "validation"]
        for index, case in enumerate(cases, 1):
            field = f"catalog_file.validation[{index}]"
            try:
                value = self.dataset.inputs[case["case_id"]]["input"]
                refs = value.get("native_refs", {})
                supplied = refs.get("mapping") if isinstance(refs, dict) else None
                prescription = value.get("prescription")
                items = prescription.get("items", []) if isinstance(prescription, dict) else []
                required = {item["drug_code"] for item in items}
                if not required and supplied is None and case.get("task") != "prescription":
                    continue
                if not isinstance(supplied, dict) or supplied.get("schema_version") != mapping["schema_version"] or not isinstance(supplied.get("drugs"), dict):
                    raise ValueError("case mapping required")
                case_drugs = supplied["drugs"]
                if (not required.issubset(case_drugs) or (case.get("task") == "prescription" and not required)
                        or any(code not in mapping["drugs"] or digest(spec) != digest(mapping["drugs"][code]) for code, spec in case_drugs.items())):
                    raise ValueError("case dictionary differs")
            except (KeyError, TypeError, ValueError, AttributeError):
                self.need(field, "case prescription drug coverage or per-drug mapping differs from the frozen catalog; no repair or alias fallback")


def study_material_problems(config, dataset):
    """Return [{field, reason}] only; call from readiness when purpose == 'study'.

    ``dataset`` is an already loaded CaseProvider. Its normal study_problems checks
    (including 200 validation cases/task and the 60/140 prescription mix) remain
    required in the caller. This function does not assign research eligibility.
    Institution catalogs follow institution_catalog.schema.json. Complete matching
    materials can return []; declarations still do not prove hospital authenticity.
    """
    if not isinstance(config, dict):
        return [{"field": "config", "reason": "resolved configuration must be an object"}]
    materials = _Materials(config, dataset)
    usable = (dataset is not None and all(isinstance(getattr(dataset, key, None), dict)
              for key in ("cases", "inputs", "references", "catalog"))
              and isinstance(getattr(dataset, "root", None), (str, Path)))
    if usable and all(isinstance(row, dict) for row in dataset.cases.values()):
        materials.calibration()
        materials.clinical()
        materials.pprs()
    else:
        materials.need("dataset", "loaded CaseProvider with case/input/reference/catalog links is required")
    materials.authorizations()
    materials.institution(usable)
    return materials.problems
