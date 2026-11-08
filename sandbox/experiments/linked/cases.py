"""Role-separated cases. Independent fixtures never read MIMIC source records.

These cases test engineering and workflow behavior. Programmatic checks cover
declared fields, source links and business states; unrestricted clinical
appropriateness is not assessed.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path


GROUPS = ("type2_diabetes", "heart_failure", "copd", "pneumonia", "uti")
VERSION = "twinhospital.linked.case/1"
RUNTIME_KEYS = ("schema_version", "case_id", "patient_key", "group", "data_kind",
                "split", "patient_initial", "his_initial", "formulary")

# This formulary is a permitted simulated catalogue, not a treatment key. All
# entries require native-dictionary mapping before a real HIS order is written.
FORMULARY = [
    {"drug_code": "SIM-METFORMIN-500", "name": "metformin", "display_name_zh": "二甲双胍片",
     "strength": "500 mg/tablet", "dose": 500, "dose_unit": "mg", "route": "oral",
     "frequency": "BID", "quantity_unit": "tablet", "ingredient": "metformin"},
    {"drug_code": "SIM-FUROSEMIDE-20", "name": "furosemide", "display_name_zh": "呋塞米片",
     "strength": "20 mg/tablet", "dose": 20, "dose_unit": "mg", "route": "oral",
     "frequency": "QD", "quantity_unit": "tablet", "ingredient": "furosemide"},
    {"drug_code": "SIM-SALBUTAMOL-100", "name": "salbutamol inhaler", "display_name_zh": "硫酸沙丁胺醇吸入气雾剂",
     "strength": "100 microgram/actuation", "dose": 2, "dose_unit": "puff", "route": "inhaled",
     "frequency": "QID", "quantity_unit": "inhaler", "ingredient": "salbutamol"},
    {"drug_code": "SIM-AMOXICILLIN-500", "name": "amoxicillin", "display_name_zh": "阿莫西林胶囊",
     "strength": "500 mg/capsule", "dose": 500, "dose_unit": "mg", "route": "oral",
     "frequency": "TID", "quantity_unit": "capsule", "ingredient": "amoxicillin"},
    {"drug_code": "SIM-NITROFURANTOIN-100", "name": "nitrofurantoin", "display_name_zh": "呋喃妥因胶囊",
     "strength": "100 mg/capsule", "dose": 100, "dose_unit": "mg", "route": "oral",
     "frequency": "BID", "quantity_unit": "capsule", "ingredient": "nitrofurantoin"},
]

PROFILES = {
    "type2_diabetes": {"history": "既往被医生诊断为2型糖尿病，曾因血糖控制不佳住院。",
        "complaint": "最近比平时口渴，想复查血糖和目前的用药。",
        "symptoms": ["口渴较平时明显", "没有呕吐或意识不清"], "med": 0,
        "observations": [{"name": "capillary_glucose", "value": 10.5, "unit": "mmol/L"}]},
    "heart_failure": {"history": "既往被医生诊断为心力衰竭，曾因气促住院。",
        "complaint": "最近走路比平时容易喘，脚踝有一点肿，来复诊。",
        "symptoms": ["步行后气促", "双侧脚踝轻度水肿", "没有静息时胸痛"], "med": 1,
        "observations": [{"name": "potassium", "value": 4.1, "unit": "mmol/L"},
                         {"name": "creatinine", "value": 0.9, "unit": "mg/dL"}]},
    "copd": {"history": "既往被医生诊断为慢阻肺，曾因呼吸症状加重住院。",
        "complaint": "这几天咳嗽比平时多，活动后喘，想检查吸入药用得对不对。",
        "symptoms": ["咳嗽比平时增加", "活动后气促", "没有咯血"], "med": 2,
        "observations": [{"name": "SpO2", "value": 95, "unit": "%"}]},
    "pneumonia": {"history": "过去曾因肺炎住院，出院后已恢复。",
        "complaint": "最近咳嗽有痰，感觉发热，来检查。",
        "symptoms": ["咳嗽有痰", "自觉发热", "没有呼吸困难或意识改变"], "med": None,
        "observations": [{"name": "temperature", "value": 37.8, "unit": "degC"},
                         {"name": "SpO2", "value": 97, "unit": "%"}]},
    "uti": {"history": "过去有过尿路感染，曾接受住院治疗。",
        "complaint": "最近尿频、排尿时不舒服，来检查。",
        "symptoms": ["尿频", "排尿灼热", "没有发热、腰痛或呕吐"], "med": None,
        "observations": [{"name": "temperature", "value": 36.8, "unit": "degC"},
                         {"name": "creatinine", "value": 0.8, "unit": "mg/dL"}]},
}


def _fact(number, topic, value, *, status="reported"):
    return {"fact_id": f"fact-{number:03d}", "topic": topic, "field": topic,
            "value": None if status == "unknown" else value,
            "reported_expression": value if status == "unknown" else None,
            "status": status, "source_kind": "constructed",
            "source_refs": []}


def build_synthetic_case(group: str, index: int, *, split="development") -> dict:
    """Build one wholly fictional case without consulting any patient data.

    Reusing (group,index) gives the same underlying patient even if split changes;
    split validation therefore catches accidental cross-split reuse.
    """
    if group not in GROUPS or not isinstance(index, int) or index < 0:
        raise ValueError("expected one declared group and non-negative integer index")
    if split not in {"development", "engineering", "test", "safety"}:
        raise ValueError("invalid split")
    profile = deepcopy(PROFILES[group])
    key = hashlib.sha256(f"independent-synthetic-v1:{group}:{index}".encode()).hexdigest()[:20]
    cid, pid = "syn_" + key, "synpt_" + key
    age = 35 + ((index * 7 + GROUPS.index(group) * 11) % 46)
    sex = "F" if group == "uti" or index % 2 else "M"
    duration = [2, 3, 5, 7, 10][index % 5]
    demographics = {"age": age, "sex": sex, "source_kind": "constructed"}
    allergy = "未发现明确药物过敏史，但愿意配合核对" if index % 4 else "不确定自己有没有药物过敏，过去没有明确记录"
    allergy_status = "reported" if index % 4 else "unknown"
    med = FORMULARY[profile["med"]] if profile["med"] is not None else None
    medication = (f"自述目前服用{med['display_name_zh']}，每次{med['dose']} {med['dose_unit']}，"
                  f"频次{med['frequency']}；药名和用法记在药盒上。") if med else "自述目前没有规律使用处方药，愿意核对近期自行用药情况"
    facts = [_fact(1, "chief_complaint", profile["complaint"]),
             _fact(2, "onset", f"大约{duration}天前开始，与平时相比有变化"),
             _fact(3, "past_medical_history", profile["history"]),
             _fact(4, "medications", medication),
             _fact(5, "allergy_history", allergy, status=allergy_status)]
    facts.extend(_fact(6 + n, "symptom_progression", s) for n, s in enumerate(profile["symptoms"]))
    # No age threshold creates cognitive impairment or deliberate omissions.
    communication = {"style": ["简洁", "自然口语", "稍详细"][index % 3],
                     "understanding": "能描述基本情况，不熟悉的医学术语会要求解释",
                     "intentional_misreport": False, "automatic_age_memory_penalty": False}
    historic = {"encounter_ref": "synhist_" + key, "setting": "inpatient",
                "status": "completed", "relative_discharge_days": -(90 + index % 180),
                "diagnoses": [{"display": profile["history"], "source_kind": "constructed"}],
                "source_kind": "constructed"}
    current_observations = [dict(item, source_kind="constructed", available_at="physician_consultation")
                            for item in profile["observations"]]
    historical_orders = []
    if med:
        historical_orders.append({"record_id": "synorder_" + key, "drug_name": med["name"],
            "drug_code": med["drug_code"], "dose": med["dose"], "dose_unit": med["dose_unit"],
            "route": med["route"], "frequency": med["frequency"], "evidence_type": "historical_order",
            "source_kind": "constructed", "current_use_inferred": False})
    case = {"schema_version": VERSION, "case_id": cid, "patient_key": pid,
        "group": group, "data_kind": "synthetic_independent", "split": split,
        "patient_initial": {"demographics": deepcopy(demographics), "patient_facts": facts,
                            "communication_profile": communication},
        "his_initial": {"demographics": deepcopy(demographics), "historical_encounters": [historic],
            "historical_orders": historical_orders, "historical_observations": [],
            "allergy_record": {"status": "unknown", "items": [], "source_kind": "constructed"},
            "current_use_record": {"status": "not_reconciled", "items": [], "source_kind": "constructed"},
            "current_observations": current_observations},
        "formulary": deepcopy(FORMULARY),
        "evaluation": {"clinical_reference_status": "not_assessed_outside_programmatic_scope",
            "engineering_reference_status": "explicit_constructed_facts",
            "required_fact_ids": [f["fact_id"] for f in facts], "required_facts": deepcopy(facts),
            "prescription_labels": None, "risk_labels": None,
            "not_a_treatment_recommendation": True},
        "provenance": {"source_kind": "independently_constructed", "uses_patient_records": False,
            "construction_version": "independent-synthetic-v1", "source_dataset": None,
            "purpose": "engineering_and_development; not validated clinical treatment"}}
    validate_case(case)
    return case


def synthetic_cases(n_per_group=10, *, split="development", start_index=0):
    if not isinstance(n_per_group, int) or n_per_group < 1:
        raise ValueError("n_per_group must be positive")
    return [build_synthetic_case(group, n, split=split)
            for group in GROUPS for n in range(start_index, start_index + n_per_group)]


def runtime_visible_case(case):
    """Separate actor inputs from assessment/source provenance at the boundary."""
    validate_case(case)
    return {key: deepcopy(case[key]) for key in RUNTIME_KEYS if key in case}


def validate_case(case):
    if not isinstance(case, dict) or not case.get("case_id") or not case.get("patient_key"):
        raise ValueError("case identity is required")
    if case.get("group") not in GROUPS:
        raise ValueError("undeclared disease group")
    if case.get("data_kind") not in {"synthetic_independent", "mimic_derived"}:
        raise ValueError("data origin must be explicit")
    for field in ("patient_initial", "his_initial"):
        if not isinstance(case.get(field), dict):
            raise ValueError(f"{field} must be an object")
    facts = case["patient_initial"].get("patient_facts", [])
    if not isinstance(facts, list) or len({x.get("fact_id") for x in facts}) != len(facts):
        raise ValueError("patient facts must have unique IDs")
    for fact in facts:
        if fact.get("status") not in {"reported", "unknown", "denied"}:
            raise ValueError("unknown/denied/reported patient facts must remain distinct")
    if case["data_kind"] == "synthetic_independent":
        prov = case.get("provenance", {})
        if prov.get("uses_patient_records") is not False or prov.get("source_dataset") is not None:
            raise ValueError("independent synthetic provenance is required; renamed patient data is not independent")
    return True


def validate_patient_splits(cases):
    owners = {}
    seen = set()
    for case in cases:
        validate_case(case)
        if case["case_id"] in seen:
            raise ValueError("duplicate case")
        seen.add(case["case_id"])
        patient = case["patient_key"]
        split = case.get("split")
        if patient in owners:
            raise ValueError("source patient repeated across cases or splits")
        owners[patient] = split
    return True


def write_case(path, case):
    validate_case(case)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def load_case(path):
    case = json.loads(Path(path).read_text())
    validate_case(case)
    return case
