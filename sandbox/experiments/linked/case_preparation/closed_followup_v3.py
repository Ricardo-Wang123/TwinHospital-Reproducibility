"""Closed, explicitly constructed follow-up views over immutable MIMIC history.

This generator defines ordinary observable facts before any model runs. It does
not validate a treatment, invent missing source tests, or require a prescription.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from ..cases import runtime_visible_case, write_case
from ..medication_catalogue import get_formulary_v3
from .case_contract import build_case, validate_package
from .cohort_eligibility import DRUG_TERMS, number
from .development_pilot50_v3 import VERSION, SYSTEMS, REFERENCES
from .mimic_cohort import save, timestamp

SCENARIO_VERSION = "closed-observable-followup-v3.0-20261107"
NAMES = {"type2_diabetes": "2型糖尿病", "heart_failure": "心力衰竭", "copd": "慢阻肺", "pneumonia": "肺炎", "uti": "尿路感染"}
GENERATION_POLICY = {
    "version": SCENARIO_VERSION, "selection_version": VERSION,
    "freeze_before_new_model_calls": True,
    "current_drug_selection": "latest-starting anchor MAIN order with group keyword, explicit PO/IH/INH/NEB, scalar dose and integral daily count1..6; then stable record ref",
    "no_route_indication_inference": "oral vancomycin excluded as a pneumonia current-treatment candidate; retained in historical chart",
    "unknown_route_product": "do not guess product or catalogue code; stage readiness remains pending",
    "candidate_regimen_status": "a historical pattern reused as a constructed patient report, not sourced discharge instructions or a clinically approved regimen",
    "adherence": "construct all scheduled doses taken in first24h; no injected medication fault or deliberate omission",
    "supply": "constructed initial supply3 daily dose-equivalents; after first24h remaining2 daily dose-equivalents; not a claimed source dispensing quantity",
    "infection_course": "postdischarge doses and current supply known, pre-discharge actual administrations and intended stop date unknown; available source order span is not total treatment duration",
    "current_measurements": "new explicitly constructed stable vital signs only; no constructed laboratory or imaging result",
    "observable_symptoms": "fixed development templates relative to discharge baseline, mild residual symptoms retained; not read from outcomes",
    "source_unknowns": "allergies/comorbidities/current-medication reconciliation not converted to negative findings",
    "source_conflict_handling": "source codes and lab results unchanged; code-based risk cues flagged for independent review, not erased",
    "reference_status": "pending_independent_review; no model self-gold",
    "references": REFERENCES + ["https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=ae6efaaf-37eb-4510-ba71-5f3dc3abdbd3"],
}


def medication_candidates(package, group):
    source = package["case"]
    anchor = source["source_anchor_encounter_ref"]
    accepted, rejected = [], Counter()
    for order in source["clinical"]["medications"]["orders"]:
        if order["source_encounter_ref"] != anchor or order["prescription_component_type_raw"] != "MAIN":
            continue
        name = (order["drug_name_raw"] or "").casefold()
        if not any(term in name for term in DRUG_TERMS[group]):
            continue
        r = order["regimen_raw"]
        if r.get("route") not in {"PO", "IH", "INH", "NEB"}:
            rejected["route_not_explicit_oral_or_inhaled"] += 1; continue
        if group == "pneumonia" and "vancomycin" in name and r["route"] == "PO":
            rejected["oral_vancomycin_not_pneumonia_treatment"] += 1; continue
        dose, daily = number(r.get("dose_val_rx")), number(r.get("doses_per_24_hrs"))
        if dose is None or dose <= 0 or daily is None or daily != int(daily) or not 1 <= daily <= 6 or not r.get("prod_strength"):
            rejected["regimen_not_supported_by_closed_daily_inventory_parser"] += 1; continue
        accepted.append(order)
    def order_key(order):
        offset = order.get("time", {}).get("event_offset_hours")
        return (-(offset if isinstance(offset, (int, float)) else -1e20), order["record_id"])
    return sorted(accepted, key=order_key), dict(rejected)


def source_risk_cues(package):
    cues = []
    for encounter in package["case"]["clinical"]["history"]["encounters"]:
        for d in encounter["diagnoses"]:
            code = d["coding"]["code"].replace(".", "").upper()
            label = (d.get("display") or "").casefold()
            reasons = []
            if "dementia" in label or "encephalopathy" in label or "delirium" in label:
                reasons.append("historical_cognitive_condition_current_communication_requires_review")
            if "amput" in label or "parapleg" in label or "quadripleg" in label:
                reasons.append("historical_functional_limitation_do_not_assume_independent_walking")
            if (code.startswith(("N17", "N18", "N19", "585", "584")) or "dialysis" in label):
                reasons.append("historical_renal_condition_regimen_and_current_function_require_review")
            if "pregnan" in label or "transplant" in label or "neutropenia" in label:
                reasons.append("historical_special_population_requires_review")
            if "catheter" in label or "prostat" in label or "neurogenic bladder" in label:
                reasons.append("urinary_complexity_not_erased_by_unspecified_uti_group")
            if "allergy" in label or "allergic" in label:
                reasons.append("historical_allergy_related_code_requires_reconciliation")
            if reasons:
                cues.append({"diagnosis_ref": d["record_id"], "source_encounter_ref": encounter["encounter_ref"],
                             "reasons": reasons, "not_a_current_state_inference": True})
    return cues


def facts_for(group, index, candidate):
    facts = []
    def add(topic, text, *, section="common", knowledge="known_self_experience", state="reported", refs=()):
        unknown = state == "unknown"
        facts.append({"fact_id": f"followup3-{len(facts)+1:03d}", "section": section, "topic": topic, "field": topic,
            "value": None if unknown else text, "reported_expression": text if unknown else None,
            "status": state, "source_kind": "constructed", "source_refs": list(refs),
            "knowledge_state": knowledge, "time_interval": "source_discharge_to_constructed_visit_24h",
            "construction_version": SCENARIO_VERSION,
            "unknown_reason": "not available from patient-held evidence; do not infer a negative" if unknown else None})
    add("chief_complaint", "昨天出院，今天来复诊核对最近的身体情况和后续用药，想请医生判断还要怎样处理。")
    add("onset", "我说的是昨天回家后到今天这24小时的情况，不是住院最重时的情况。")
    add("symptom_trajectory", "总体和昨天出院时接近，没有突然明显变差；还有下面这些具体感受。")
    add("past_medical_history", f"住院记录里有{NAMES[group]}这个病名；其他既往问题我不能逐一说全，可以请医生查记录。", knowledge="known_from_brought_record")
    add("daily_function", "昨天能完成的日常活动今天也大致能完成，没有新出现的昏倒或突然不能交流；是否能走多远、要不要辅助器具需要结合我原来的情况。")
    add("oral_intake", "这一天能吃东西喝水，能把口服药咽下去，没有反复呕吐。喝水量没有精确计算。")
    add("diarrhea", "回家后这一天没有新出现的频繁水样腹泻。以前住院期间是否发生过，要查记录。")
    add("medication_list_completeness", "我能读出这次带来的这一种药和最近一天怎么用，其他药的完整清单需要核对；没有列出不代表没有用。", state="unknown", knowledge="not_available")
    add("allergy_history", "我现在不能确认过去有没有医生认定的药物过敏，不能把没想起来说成没有，请核对医院记录。", state="unknown", knowledge="uncertain_recall")
    add("recent_adverse_experience", "最近这一天没有新发现全身风团、嘴唇舌头肿起或突然喘不过气；这不能说明以前没有药物过敏。")
    add("additional_products", "回家后这一天没有新加自己买的药或保健品；更早用过什么、其他医生开的药是否都记全，需要进一步核对。")
    add("measurement_resources", "这一天没有在家量体重、血压、血氧或血糖，也没有连续读数记录；不能说这些数值正常。", knowledge="not_measured")
    add("followup_questions", "我能听懂日常问法，不熟悉的医学名词会请解释；想知道药该继续、改掉还是先检查，以及什么情况需要尽快再就医。")
    if candidate:
        raw = candidate["regimen_raw"]; daily = int(number(raw["doses_per_24_hrs"]))
        med = f"{candidate['drug_name_raw']}，药品规格写着{raw['prod_strength']}；每次{raw['dose_val_rx']} {raw['dose_unit_rx']}，一天{daily}次，途径记录为{raw['route']}"
        add("medications", "带来的药盒和用法小条上写的是：" + med + "。这是我这次实际报告的用法，请医生核对是否合适。", knowledge="known_from_brought_record", refs=[candidate["record_id"]])
        add("recent_administration", f"从昨天出院后到现在已经按上面的每次量用过{daily}次，没有临时加量，也没有漏掉这一天计划的次数。最后一次在来诊前约{round(12/daily, 1)}小时。")
        add("supply_inventory", f"昨天回家时带的量按这张用法小条够用{3*daily}次；已经用了{daily}次，还够{2*daily}次，即照原频次约两天。我不是说医生已经决定一定再用两天。", knowledge="known_from_brought_record")
        if raw["route"] in {"IH", "INH", "NEB"}:
            add("inhaler_device", "这次药对应的吸入器具在身边，我愿意演示实际操作；我不能自己保证吸得标准，需要医护核对。药是用于吸入的，没有把吸入胶囊或雾化液吞下去。", section="medication")
    else:
        add("medications", "这次带来的材料还不能可靠确认一个适合这个复诊情境的当前用药方案，请医生先核对历史药单。", state="unknown", knowledge="not_available")
        add("recent_administration", "仅凭当前已整理资料不能确认回家后实际服过哪种药和几次。", state="unknown", knowledge="not_available")
        add("supply_inventory", "手里各药的剩余量还没有可靠清单。", state="unknown", knowledge="not_available")
    grouped = {
        "type2_diabetes": [
            ("low_glucose_experiences", "这一天没有明显发抖、冷汗、心慌、突然饥饿或意识含糊；没有实测血糖，不能据此排除血糖异常。"),
            ("thirst_urination", "有一点口渴，喝水能缓解；排尿次数和昨天接近，没有新出现的明显增加。"),
            ("gi_intake", "这一天没有持续腹痛或反复恶心呕吐，吃饭时间大体规律。"),
            ("foot_skin_changes", "我没有注意到这一天新出现的脚部伤口或皮肤明显变化；原来有没有伤口、截肢、感觉减退，要结合既往记录和查体，不能说以前没有。"),
        ],
        "heart_failure": [
            ("dyspnea", "安静坐着时没有新加重的气短，做和昨天相近的活动时有一点累，程度没明显变重。"),
            ("orthopnea_night", "昨晚没有因为突然憋醒坐起来，也没有比昨天需要垫更多枕头；原本的睡姿习惯和昨天相同。"),
            ("swelling", "脚踝看上去和昨天差不多，没有这一天突然明显肿起来；原先有没有水肿需要结合记录和查体。"),
            ("weight_change", "这一天没称体重，没有可靠的涨跌公斤数。"),
            ("dizziness_syncope", "这一天没有新出现的站起就明显头晕，也没有晕倒。"),
            ("chest_palpitations", "这一天没有新出现的胸痛或持续明显心慌。"),
            ("urination", "自己感觉排尿和昨天差不多，没有完全尿不出来；没有量过总毫升数。"),
            ("fluid_plan", "喝水量没有精确记录；医生是否给过具体限水数值我手边没有，不能自己编一个。"),
        ],
        "copd": [
            ("breathlessness", "活动时仍会有一点气短，和昨天相近；安静休息时没有突然比昨天更喘。"),
            ("cough_sputum", "仍有间断咳嗽，有少量痰，和昨天相比痰量和颜色没有明显变化。"),
            ("wheeze_sleep", "偶尔会有喘鸣，但昨晚没有因呼吸困难反复醒来，和昨天接近。"),
            ("fever_hemoptysis_chest", "这一天没有自觉发热寒战，没有咳血或新出现胸痛。"),
            ("rescue_inhaler", "除了刚才能读出的药，这一天有没有另一个专门急救的吸入药，需要核对完整药单；我不能把不认识药名说成没有。"),
            ("oxygen_plan", "是否有长期家庭氧疗和具体流量，当前手边清单没写清，要查既往记录，不能推断从来不需要氧。"),
            ("recent_exposure", "回家后这一天没有吸烟或接触明显烟尘；过去的吸烟经历要另问或核对，不能因此说从不吸烟。"),
        ],
        "pneumonia": [
            ("cough_sputum", "还会咳嗽，少量有痰，比出院时没有明显增加，没有咯血。"),
            ("fever_chills", "回家后这一天没有觉得发热或打寒战；家里没有量体温，不能用感觉代替测量。"),
            ("dyspnea_chest", "安静坐着没有新加重气促，咳嗽时胸部有一点不适，没有持续胸痛。"),
            ("oral_intake_confusion", "可以吃饭喝水，没有突然意识含糊或很难叫醒的情况。"),
            ("antibiotic_total_course", "回家后最近一天怎么吃可以按小条说清，但住院之前和住院期间究竟实际给过多少天、最后应吃到哪天，我手边没有完整说明，不能把剩下多少药当成总疗程。"),
            ("imaging_microbiology", "是否拍过什么片子、细菌是哪种、药敏如何，当前带来的资料没有结果，不能仅凭病名说已确认。"),
            ("pneumonia_onset_setting", "这次肺炎原先是在入院前还是住院期间开始，当前材料没说清，不能自行认定为社区获得性。"),
        ],
        "uti": [
            ("urinary_symptoms", "排尿时还有一点灼热不适，次数略多；回家后这一天没有明显加重。"),
            ("lower_abdominal_pain", "小腹偶尔有轻微不舒服，没有持续明显疼痛。"),
            ("fever_flank_pain", "这一天没有自觉发热、寒战或新出现的腰背肋下痛。"),
            ("urine_retention_hematuria", "还能排出尿，没有看见新出现的明显血尿；尿量没有用容器测过。"),
            ("nausea_vomiting", "这一天没有呕吐，可以喝水和服药。"),
            ("antibiotic_total_course", "回家后这一天的服药次数可以核对，但住院前后实际抗菌药给了几天、总疗程计划和停药日期，手边没有完整说明，不能凭剩药量决定。"),
            ("culture_susceptibility", "尿培养是不是查到了细菌、对什么药敏感，当前带来的资料没有结果；尿常规不能代替药敏。"),
            ("urinary_complexity", "有没有导尿、结石、前列腺或其他泌尿问题，需要逐项核对既往记录；不能因为只记得尿路感染就说没有这些情况。"),
        ],
    }
    unknown_topics = {"fluid_plan", "rescue_inhaler", "oxygen_plan", "antibiotic_total_course", "imaging_microbiology", "pneumonia_onset_setting", "culture_susceptibility", "urinary_complexity"}
    for topic, text in grouped[group]:
        add(topic, text, section=group, state="unknown" if topic in unknown_topics else "reported",
            knowledge="not_available" if topic in unknown_topics else "not_measured" if topic == "weight_change" else "known_self_experience")
    return facts


def source_seed(db, selection, dictionaries, cache_meta):
    rows = defaultdict(list)
    for table, payload in db.execute("SELECT table_name,payload FROM rows WHERE subject=?", (selection["subject_id"],)):
        rows[table].append(json.loads(payload))
    anchor = next(r for r in rows["admissions"] if r["hadm_id"] == selection["hadm_id"])
    used_codes = {(r["icd_code"], r["icd_version"]) for r in rows["diagnoses_icd"]}
    used_items = {r["itemid"] for r in rows["labevents"]}
    coverage = {table: {"status": "loaded", "rows": len(values)} for table, values in rows.items()}
    coverage.update({t: {"status": "omitted", "rows": None, "reason": "not_extracted"} for t in cache_meta["omitted_tables"]})
    return {"patient": rows["patients"][0], "anchor_admission": anchor,
        "admissions": rows["admissions"], "diagnoses": rows["diagnoses_icd"], "prescriptions": rows["prescriptions"],
        "labs": rows["labevents"], "diagnosis_dictionary": [dictionaries[0][k] for k in used_codes if k in dictionaries[0]],
        "lab_dictionary": [dictionaries[1][k] for k in used_items if k in dictionaries[1]],
        "coverage": {"mode": "historical_seed", "tables": coverage}}, rows["pharmacy"]


def build_linked(package, seed, selection, index, candidate):
    partition = selection.get('split', 'development')
    if partition not in {'development','test'}:raise ValueError('Unknown patient partition')
    src = package["case"]; clinical = src["clinical"]; ageinfo = clinical["demographics"]["age_at_source_admission"]
    visit = timestamp(seed["anchor_admission"]["dischtime"]) + timedelta(days=1)
    age = int(seed["patient"]["anchor_age"]) + visit.year - int(seed["patient"]["anchor_year"]) if ageinfo["status"] == "approximate" else None
    demographics = {"age": age, "age_status": ageinfo["status"], "source_admission_age": ageinfo,
        "sex": clinical["demographics"]["sex_recorded"], "age_time_basis": "approximate at constructed discharge+24h visit; source90+ remains nonnumeric"}
    group = selection["group"]; facts = facts_for(group, index, candidate)
    # Fixed deterministic development variation, not adjusted to source results.
    vital_values = [("blood_pressure", ["118/72", "126/78", "132/80"][index % 3], "mmHg"),
        ("pulse", [72, 80, 86][index % 3], "/min"), ("respiratory_rate", [16, 18, 20][index % 3], "/min"),
        ("temperature", [36.6, 36.8, 37.0][index % 3], "degC"), ("SpO2", 95 if group == "copd" else 97, "%")]
    vitals = [{"name": n, "value": v, "unit": u, "source_kind": "constructed", "available_at": "physician_consultation",
        "status": "simulated_clinic_measurement_oxygen_support_unspecified_do_not_assume_room_air" if n == "SpO2" else "simulated_clinic_measurement",
        "time": {"basis": "source_relative_to_anchor_discharge", "event_offset_hours": 24,
                 "recorded_offset_hours": 24, "event_time_known": True,
                 "available_by_source_cutoff": False, "availability_basis": "constructed_new_visit"}} for n,v,u in vital_values]
    cues = source_risk_cues(package)
    case = {"schema_version": "twinhospital.linked.case/1", "case_id": src["case_id"] + "_closed_v3", "patient_key": src["patient_key"],
        "group": group, "data_kind": "mimic_derived", "split": partition,
        "patient_initial": {"demographics": deepcopy(demographics), "patient_facts": facts,
            "communication_profile": {"style": "自然口语，能读手边简明记录，陌生医学术语会要求日常表达",
                "intentional_misreport": False, "automatic_age_memory_penalty": False,
                "unlisted_fact_policy": "Do not invent facts. Distinguish not measured/not known from forgetting; ordinary symptoms use the defined facts."}},
        "his_initial": {"demographics": deepcopy(demographics),
            "time_basis": {"history_offset_origin": "source_anchor_discharge", "new_visit_offset_hours": 24,
                "source_mode": "historical_seed", "meaning": "All source labs/orders precede constructed visit; no old lab relabeled current."},
            "historical_encounters": clinical["history"]["encounters"], "historical_orders": clinical["medications"]["orders"],
            "historical_observations": clinical["observations"], "allergy_record": clinical["allergies"],
            "current_use_record": {"status": "not_reconciled", "items": [], "meaning": "Patient-reported current use available only through interview; source inpatient orders are not current medication list."},
            "current_observations": vitals},
        "formulary": get_formulary_v3(),
        "evaluation": {"clinical_reference_status": "pending_independent_review", "required_facts": [],
            "reference_facts": deepcopy(facts), "required_fact_selection_status": "pending_existing_paper_rubric_alignment",
            "prescription_labels": None, "risk_labels": None, "prescribing_required": False,
            "acceptable_process_outcomes": ["prescribe", "revise", "stop", "defer", "refer"],
            "source_risk_cues": cues, "clinical_current_suitability": "pending_independent_review",
            "source_medication_candidate_ref": candidate["record_id"] if candidate else None,
            "source_medication_indication": "unverified; group-keyword does not establish individual indication"},
        "provenance": {"source_dataset": "MIMIC-IV 2.2", "uses_patient_records": True,
            "source_case_id": src["case_id"], "construction_version": SCENARIO_VERSION,
            "constructed_fields": ["patient_initial.patient_facts", "patient_initial.communication_profile", "his_initial.current_observations"],
            "current_medication_relation": "Explicitly constructed continuity from one historical product/regimen; NOT sourced discharge prescription, observed administration or physician recommendation.",
            "selected_historical_order": deepcopy(candidate), "system": SYSTEMS[group], "model_outputs_read": False,
            "lab_policy": "Retain source values and times; no invented/currently remeasured laboratory values.",
            "clinical_plausibility": "pending_independent_review"}}
    return case


def generate(folder, identity_key):
    folder = Path(folder)
    if (folder / "manifest.json").exists():
        raise FileExistsError("case manifest frozen; use another version")
    save(folder / "generation_policy_frozen_before_cases.json", {"policy": GENERATION_POLICY,
        "new_case_model_calls": 0, "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    selected = json.loads((folder / "selection_private.json").read_text())
    db = sqlite3.connect(folder / "source_cache.sqlite")
    dictionaries = ({(c,v):json.loads(p) for c,v,p in db.execute("SELECT code,version,payload FROM dictionary WHERE table_name='d_icd_diagnoses'")},
                    {c:json.loads(p) for c,p in db.execute("SELECT code,payload FROM dictionary WHERE table_name='d_labitems'")})
    cache_meta = json.loads((folder / "source_cache_complete.json").read_text())
    manifest, audit, products = [], [], Counter()
    for index, selection in enumerate(selected):
        seed, pharmacy = source_seed(db, selection, dictionaries, cache_meta)
        package = build_case(seed, selection["group"], identity_key)
        # Source package remains a historical record, with no presentation fact.
        issues = validate_package(package)
        if issues:
            raise ValueError("source package contract failed: " + ";".join(issues))
        source_folder = folder / "source_packages" / package["case"]["case_id"]
        for name, value in package.items(): save(source_folder / (name + ".json"), value)
        save(source_folder / "pharmacy_private.json", pharmacy)
        candidates, rejected = medication_candidates(package, selection["group"])
        candidate = candidates[0] if candidates else None
        linked = build_linked(package, seed, selection, index, candidate)
        path = folder / "cases" / (linked["case_id"] + ".json")
        write_case(path, linked)
        problems = [] if candidate else ["no_route_and_regimen_candidate_for_constructed_current_use"]
        entry = {"case_id": linked["case_id"], "patient_key": linked["patient_key"], "group": selection["group"],
            "system": SYSTEMS[selection["group"]], "split": linked['split'], "data_kind": "mimic_derived", "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source_material_ready": True,
            "constructed_view_complete": not problems, "clinical_reference_status": "pending_independent_review",
            "clinical_suitability": "pending", "prescribing_required": False, "not_ready_reasons": problems}
        manifest.append(entry)
        audit.append({"case_id": linked["case_id"], "candidate_order_count": len(candidates), "rejected_order_reasons": rejected,
            "source_encounters": len(linked["his_initial"]["historical_encounters"]), "source_orders": len(linked["his_initial"]["historical_orders"]),
            "source_labs": len(linked["his_initial"]["historical_observations"]), "fact_count": len(linked["patient_initial"]["patient_facts"]),
            "source_risk_cues": linked["evaluation"]["source_risk_cues"], "source_package": str(source_folder),
            "source_history_sha256": hashlib.sha256(json.dumps(package["case"]["clinical"], sort_keys=True).encode()).hexdigest()})
        if candidate:
            r = candidate["regimen_raw"]
            products[(selection["group"], candidate["drug_name_raw"], r["prod_strength"], r["route"], candidate["codes_raw"].get("ndc"),
                      r["dose_unit_rx"], r.get("form_rx"), r.get("form_unit_disp"), r.get("dose_val_rx"), r.get("doses_per_24_hrs"))]+=1
    db.close()
    payload = {"version": SCENARIO_VERSION, "selection_version": VERSION, "cases": manifest, "case_count": len(manifest),
        "purpose": "development candidate views; clinical review and metric-rubric alignment pending", "formal_eligible": False,
        "clinical_reference_status": "pending_independent_review", "prescribing_required": False}
    save(folder / "manifest.json", payload)
    view_ready = [x for x in manifest if x["constructed_view_complete"]]
    save(folder / "current_view_ready_manifest.json", {**payload, "cases": view_ready, "case_count": len(view_ready),
        "scope": "complete constructed view only; not clinical approval, full formulary coverage or formal evaluation readiness"})
    save(folder / "case_audit_private.json", {"cases": audit})
    save(folder / "selected_products_aggregate.json", {"products": [{"group":k[0], "name_raw":k[1], "strength_raw":k[2],
        "route_raw":k[3], "ndc_raw":k[4], "dose_unit_raw":k[5], "form_raw":k[6], "dispense_quantity_unit_raw":k[7],
        "dose_value_raw":k[8], "daily_dose_count_raw":k[9], "ingredient_salt_verified":None,
        "release_and_device_verified":None, "strength_basis_verified":None, "case_count":v}
        for k,v in sorted(products.items(), key=lambda x: tuple(str(v) for v in x[0]))],
        "mapping_status": "awaiting exact identity/form/strength verification; no near-product substitutions"})
    summary = {"cases": len(manifest), "by_group": dict(Counter(x["group"] for x in manifest)),
        "constructed_view_complete": sum(x["constructed_view_complete"] for x in manifest),
        "not_ready": dict(Counter(reason for x in manifest for reason in x["not_ready_reasons"])),
        "source_encounters": sum(x["source_encounters"] for x in audit), "source_orders":sum(x["source_orders"] for x in audit),
        "source_labs":sum(x["source_labs"] for x in audit),
        "clinical_review_pending": len(manifest), "clinical_gold_labels": 0,
        "metric_required_facts_pending_rubric_alignment": len(manifest),
        "antibiotic_total_course_unknown_preserved": sum(x["group"] in {"pneumonia", "uti"} for x in manifest),
        "source_risk_cue_cases":sum(bool(x["source_risk_cues"]) for x in audit),
        "historical_labs_made_current":0, "unknown_allergies_made_negative":0, "model_calls":0}
    save(folder / "generation_summary.json", summary)
    freeze_paths = [Path(x["path"]) for x in manifest] + [folder/n for n in ("manifest.json", "current_view_ready_manifest.json", "protocol_frozen_before_selection.json", "generation_policy_frozen_before_cases.json", "selection_private.json")]
    save(folder / "input_freeze.json", {"version": SCENARIO_VERSION, "files":[{"path":str(p),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in freeze_paths]})
    return summary


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--folder",required=True);p.add_argument("--identity-key",required=True)
    args=p.parse_args();print(json.dumps(generate(args.folder,Path(args.identity_key).read_bytes()),ensure_ascii=False))
