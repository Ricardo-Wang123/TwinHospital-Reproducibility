"""Construct a new current episode over immutable historical MIMIC material.

This is development input authoring, not a reconstruction of real outpatient
use or a clinician-approved prescription. No model outputs are read. Original
cases remain unchanged; the source and constructed reporting layers are kept.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

from ..cases import validate_case, runtime_visible_case
from ..chart import HistoryChart, json_bytes
from ..chart_projection import project_history_page, verify_projection
from .formulary_overlay import digest, file_hash, write

VERSION = "constructed-current-episode-v4-preview-2.2"
HISTORY_FIELDS = ("historical_encounters", "historical_orders", "historical_observations")
VISIT_HOURS = 168
PLAN_HOURS = 24
LOOKBACK_HOURS = 24
ROUTES = {"PO": "口服", "IH": "吸入", "INH": "吸入", "NEB": "雾化吸入"}
POLICY = {
    "version": VERSION, "status": "development_input_not_clinically_validated",
    "model_outputs_read": False, "model_calls": 0,
    "selection": "Episode construction preserves the supplied source patient, disease group and partition. The invoking manifest records the exact source-selection policy; no model output is used by this constructor.",
    "historical_material": "All historical encounters, diagnoses, orders and observations remain byte-equivalent as JSON values. Source allergy/current-use records are retained separately.",
    "episode_time": {"origin": "source_anchor_discharge", "prior_simulated_clinic_plan_hours": PLAN_HOURS,
                     "current_visit_hours": VISIT_HOURS, "source_relabelled_current": False},
    "candidate_window": "Same anchor admission; MAIN oral/inhaled orders started by source discharge and with a known prescribed stop no earlier than discharge minus24h. This is an explicit candidate retrieval window, not a clinical definition of discharge medication.",
    "candidate_regular_use": "One unambiguous latest source regimen per exact raw name/strength/route; positive scalar dose, named unit, nonempty strength and integer1..6 doses/day that agrees with an explicit scheduled frequency. Pharmacy sidecar frequency can supplement a missing prescription field without changing the historical order. Unknown/PRN/one-time/interval-conflicting frequency, ambiguous competing regimens and incomplete identity remain reconciliation items.",
    "constructed_transition": "Eligible candidate patterns are explicitly adopted into a fictional prior clinic plan starting at +24h for review at +168h. Actual use and dose-equivalent supply in this new episode are constructed, not inferred from source orders. This adoption is unreviewed clinical input, not an approved treatment or normal-control label.",
    "not_current_by_default": "Historical drugs outside the candidate window are not silently active, stopped, unnecessary or absent. They stay in full history. The new episode reports a defined current-use window and explicitly excludes a claim of complete real-world longitudinal reconciliation.",
    "reconciliation_scope": "Known reported scheduled use plus unresolved candidate entries; prescription/OTC/PRN/recent-change reporting states are defined separately. A complete simulation ledger is not a claim that the source database supplied a complete current medication list.",
    "allergy": "Retain original source status/items. If source allergy items or an allergy-related diagnosis cue exists, do not construct a contradictory none-known report; otherwise create a patient-reported none-known statement, never confirmed allergy absence.",
    "symptoms": "Natural current experience, with mild residual disease-specific symptoms. No deliberate omission, no automatic age-memory penalty and no research caveats in the patient's spoken wording.",
    "memory": "A fixed common-disease ICD9/10-to-lay-name mapping creates explicit constructed historical memory. Source codes support only historical diagnosis material, not proof that the real patient received this explanation or that the present episode has that diagnosis.",
    "fact_time_scope": "episode_window groups facts in a scenario; it is never a claim that every symptom/negative persisted across the whole episode. Each fact retains statement_as_of and narrative_time_scope; imprecise or historical onset is not invented.",
    "tests": "No laboratory or imaging values are constructed or normalised. Existing constructed vital values are retained with new explicit current-episode times and their original measurement limitations.",
    "qualification": "Mechanical structural checks are separate from input clinical sufficiency, indication, regimen appropriateness, necessary safety tests and required-fact applicability: all clinical decisions remain independent_review_pending.",
    "outcomes": "Prescribe, revise, stop, defer or refer remain valid decisions; a no-prescription case is not a completed medication-payment chain.",
    "references": ["https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html",
                   "https://mimic.mit.edu/docs/iv/modules/hosp/emar.html",
                   "https://www.nice.org.uk/guidance/ng5/chapter/Recommendations",
                   "https://www.nice.org.uk/guidance/cg183/chapter/Recommendations"],
}


def scalar(value):
    if isinstance(value, bool) or value is None: return None
    try:
        text = str(value).strip()
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", text): return None
        x = float(text)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError): return None


def _ref(seed):
    return "rx_" + hashlib.sha256(seed.encode()).hexdigest()[:24]


def scheduled_daily_count(frequency):
    text = re.sub(r"\s+", "", str(frequency or "").upper())
    if not text: return None, "full_frequency_missing"
    if "PRN" in text or "ASNEEDED" in text: return None, "explicit_prn_frequency"
    aliases = {"DAILY":1,"QDAY":1,"QD":1,"QAM":1,"QPM":1,"QHS":1,"NIGHTLY":1,
        "BID":2,"TID":3,"TIDAC":3,"QID":4,"QIDACHS":4,"BIDAC":2,"Q24H":1,"Q12H":2,"Q8H":3,"Q6H":4,"Q4H":6}
    if text in aliases: return aliases[text], None
    return None, "frequency_interval_or_one_time_not_supported"


def frequency_spoken(frequency, daily):
    raw = re.sub(r"\s+", "", str(frequency or "").upper())
    m = re.fullmatch(r"Q(\d+)H", raw)
    if m: return f"每{m.group(1)}小时一次"
    return {"QAM":"每天早上一次", "QPM":"每天晚上一次", "QHS":"每晚睡前一次", "NIGHTLY":"每晚一次",
            "BIDAC":"每天两次，饭前用", "TIDAC":"每天三次，饭前用", "QIDACHS":"每天三餐前及睡前用"}.get(raw, f"一天{daily}次")


def historical_patient_memory(case):
    """A small fixed lay vocabulary, not access to the complete chart/evaluator."""
    names = defaultdict(list)
    for enc in case["his_initial"]["historical_encounters"]:
        for dx in enc.get("diagnoses", []):
            coding = dx.get("coding", {}); code = str(coding.get("code", "")).upper().replace(".", "")
            version = str(coding.get("version", "")); labels = []
            if version == "10":
                if code.startswith("E11"): labels.append("糖尿病")
                if code.startswith("I50"): labels.append("心力衰竭")
                if code.startswith("J44"): labels.append("慢阻肺")
                if code[:3] in {"I10","I11","I12","I13","I15"}: labels.append("高血压")
                if code.startswith("E78"): labels.append("血脂偏高")
                if code.startswith("N18"): labels.append("慢性肾病")
                if code.startswith("I48"): labels.append("房颤")
            elif version == "9":
                if code.startswith("250") and len(code)>=5 and code[4] in "02": labels.append("糖尿病")
                if code.startswith("428"): labels.append("心力衰竭")
                if code.startswith(("491","492","496")): labels.append("慢阻肺")
                if code[:3] in {"401","402","403","404","405"}: labels.append("高血压")
                if code.startswith("272"): labels.append("血脂偏高")
                if code.startswith("585"): labels.append("慢性肾病")
                if code == "42731": labels.append("房颤")
            for label in labels:
                if dx.get("record_id"): names[label].append(dx["record_id"])
    return [{"lay_name": name, "source_refs": sorted(set(refs))} for name,refs in sorted(names.items())]


def candidate_plan(case, frequency_context=None):
    """Return source-linked candidate states without interpreting an indication."""
    anchor = case["provenance"]["selected_historical_order"]["source_encounter_ref"]
    grouped, exclusions = defaultdict(list), Counter()
    for order in case["his_initial"]["historical_orders"]:
        r, t = order.get("regimen_raw", {}), order.get("time", {})
        start, stop = t.get("event_offset_hours"), order.get("planned_stop_offset_hours")
        if order.get("source_encounter_ref") != anchor: exclusions["other_admission"] += 1; continue
        if order.get("prescription_component_type_raw") != "MAIN": exclusions["non_main"] += 1; continue
        if r.get("route") not in ROUTES: exclusions["other_or_unknown_route"] += 1; continue
        if type(start) not in (int, float) or type(stop) not in (int, float): exclusions["unknown_order_time"] += 1; continue
        if start > 0 or stop < -LOOKBACK_HOURS: exclusions["outside_explicit_window"] += 1; continue
        key = ((order.get("drug_name_raw") or "").strip().casefold(), r.get("prod_strength"), r.get("route"))
        grouped[key].append(order)
    entries = []
    for key in sorted(grouped, key=lambda x: tuple(str(v) for v in x)):
        rows = sorted(grouped[key], key=lambda o: (o["time"]["event_offset_hours"], o["record_id"]), reverse=True)
        entries.append(regimen_candidate(rows, frequency_context))
    return entries, dict(exclusions)


def regimen_candidate(rows, frequency_context=None):
    """Parse explicit source patterns; no timing, indication or current-use inference."""
    chosen, r = rows[0], rows[0]["regimen_raw"]
    dose, daily = scalar(r.get("dose_val_rx")), scalar(r.get("doses_per_24_hrs"))
    annotation = deepcopy((frequency_context or {}).get(chosen["record_id"], {}))
    frequency = r.get("frequency") or annotation.get("frequency")
    expected_daily, frequency_reason = scheduled_daily_count(frequency)
    reasons = []
    if frequency_reason: reasons.append(frequency_reason)
    if dose is None or dose <= 0: reasons.append("dose_not_positive_scalar")
    if daily is None or daily != int(daily) or not 1 <= daily <= 6:
        reasons.append("scheduled_frequency_missing_or_not_supported; PRN_cannot_be_inferred")
    elif expected_daily is not None and daily != expected_daily:
        reasons.append("explicit_frequency_conflicts_with_daily_count")
    if annotation.get("join_status") not in (None, "unique_agreement"):
        reasons.append("pharmacy_frequency_link_not_unique")
    if r.get("frequency") and annotation.get("frequency") and str(r["frequency"]).strip().upper() != str(annotation["frequency"]).strip().upper():
        reasons.append("prescription_and_pharmacy_frequency_disagree")
    if not r.get("dose_unit_rx") or not r.get("prod_strength"):
        reasons.append("source_product_or_dose_unit_incomplete")
    signatures = {(str(o["regimen_raw"].get("dose_val_rx")), o["regimen_raw"].get("dose_unit_rx"),
                   str(o["regimen_raw"].get("doses_per_24_hrs"))) for o in rows}
    if len(signatures) > 1: reasons.append("competing_near_cutoff_regimens_require_reconciliation")
    # A package volume alone does not establish the strength of its contents.
    strength = str(r.get("prod_strength") or "")
    if "ml" in strength.casefold() and not re.search(r"(?:mg|mcg|ug|%)", strength, re.I):
        reasons.append("source_liquid_concentration_missing")
    return {"source_order": deepcopy(chosen), "other_candidate_refs": [o["record_id"] for o in rows[1:]],
        "candidate_state": "scheduled_pattern_adopted_as_constructed_report" if not reasons else "unresolved_not_assumed_taken",
        "reasons": reasons, "dose": dose, "daily": int(daily) if daily is not None and daily == int(daily) else None,
        "frequency_raw": frequency, "frequency_source_annotation": annotation}


def medication_item(case, entry):
    order, eligible = entry["source_order"], not entry["reasons"]
    r = order["regimen_raw"]
    row = {"record_id": _ref(case["case_id"] + VERSION + order["record_id"]),
        "source_ref": order["record_id"], "name": order["drug_name_raw"], "drug_name": order["drug_name_raw"],
        "strength": r.get("prod_strength"), "dosage_form": r.get("form_rx"),
        "dose": r.get("dose_val_rx"), "dose_unit": r.get("dose_unit_rx"), "route": r.get("route"),
        "frequency": entry.get("frequency_raw"), "planned_daily_dose_count": entry["daily"] if eligible else None,
        "source_kind": "constructed", "status": "patient_report_with_prior_plan" if eligible else "reconciliation_item",
        "current_use_status": "reported_taken_in_defined_episode" if eligible else "reported_not_taken_in_defined_episode; continuation_intent_unresolved",
        "product_identity_status": "raw_historical_product_pattern; manufacturer_salt_device_and_source_NDC_exact_match_not_verified",
        "indication_status": "source_order_does_not_establish_current_indication",
        "historical_order_stop_offset_hours": order.get("planned_stop_offset_hours"),
        "uncertainty": "; ".join(entry["reasons"]) if not eligible else "prior plan appropriateness and complete longitudinal reconciliation require current clinician review",
        "time": {"basis": "source_relative_to_anchor_discharge", "event_offset_hours": VISIT_HOURS,
                 "recorded_offset_hours": VISIT_HOURS, "event_time_known": True, "available_by_source_cutoff": False,
                 "availability_basis": "constructed_current_report_and_prior_simulated_plan"},
    }
    if eligible:
        daily, days = entry["daily"], (VISIT_HOURS-PLAN_HOURS)//24
        row["plan"] = {"document_kind": "constructed_prior_outpatient_medication_plan", "issued_offset_hours": PLAN_HOURS,
            "start_offset_hours": PLAN_HOURS, "review_due_offset_hours": VISIT_HOURS, "stop_offset_hours": None,
            "purpose": "review use, symptoms and the need to continue/change/stop; no new-visit decision is preassigned",
            "source_kind": "constructed"}
        row["actual_use"] = {"window_start_offset_hours": PLAN_HOURS, "window_end_offset_hours": VISIT_HOURS,
            "planned_doses": daily*days, "reported_taken_doses": daily*days, "missed_doses": 0,
            "additional_doses": 0, "last_taken_offset_hours": VISIT_HOURS-12/daily, "source_kind": "constructed"}
        row["supply"] = {"initial_dose_equivalents": daily*(days+1), "used_dose_equivalents": daily*days,
            "remaining_dose_equivalents": daily, "meaning": "one dose-equivalent is the exact stated dose+unit, not a guessed tablet/device count; supply is not permission to continue", "source_kind": "constructed"}
    return row


SYMPTOMS = {
    "type2_diabetes": [("thirst_urination", "最近一天有一点口渴，喝水能缓解；排尿次数和前几天差不多。"),
        ("low_glucose_experiences", "最近一天没有发抖、冷汗或突然意识含糊。"),
        ("meal_intake", "这几天吃饭时间大致规律，没有反复恶心呕吐。")],
    "heart_failure": [("dyspnea", "做和平常相近的活动时有一点累，近几天没有明显加重；坐着休息时不气短。"),
        ("orthopnea_night", "昨晚没有憋醒，睡觉用的枕头数量和前几天一样。"),
        ("swelling", "脚踝看起来和前几天差不多。"),
        ("weight_change", "最近没称体重。"), ("dizziness_syncope", "近一天没有站起就明显头晕，也没晕倒。"),
        ("chest_palpitations", "近一天没有新出现的胸痛或持续心慌。"),
        ("urination", "排尿和平时差不多，没有量过具体毫升数。")],
    "copd": [("breathlessness", "活动时有一点气短，近几天差不多，休息后会好一些。"),
        ("cough_sputum", "有时咳嗽，有少量痰，最近痰量和颜色没有明显变化。"),
        ("wheeze_sleep", "偶尔会喘，昨晚没有因为喘反复醒来。"),
        ("fever_hemoptysis_chest", "近一天没觉得发热，没有咳血或新出现胸痛。"),
        ("inhaler_device", "药和用的吸入器具都带来了，可以给你看我平时怎么用。")],
}


def build_episode(case, frequency_context=None, *, candidate_override=None):
    if case["group"] not in SYMPTOMS: raise ValueError("This first implementation covers the three fixed chronic-source cases only; infection plans require a separately frozen policy")
    original = deepcopy(case); result = deepcopy(case)
    entries, exclusions = candidate_plan(case, frequency_context)
    if candidate_override is not None:
        entries, exclusions = deepcopy(candidate_override)
    items = [medication_item(case, e) for e in entries]
    eligible = [i for i in items if i.get("plan")]
    source_allergy = deepcopy(case["his_initial"].get("allergy_record", {"status": "not_supplied", "items": []}))
    source_current = deepcopy(case["his_initial"].get("current_use_record", {"status": "not_supplied", "items": []}))
    allergy_conflict = bool(source_allergy.get("items")) or any(
        "allergy" in reason for cue in case.get("evaluation", {}).get("source_risk_cues", []) for reason in cue.get("reasons", []))
    report = {"status": "unable_to_ascertain" if allergy_conflict else "reported_none_known",
        "value": "以前的过敏情况需要一起核对，我把已有记录带来了。" if allergy_conflict else "我不记得以前用药出现过过敏反应。",
        "as_of_offset_hours": VISIT_HOURS, "source_kind": "constructed", "verification_status": "patient_report_not_confirmed_absence"}
    new_allergy = deepcopy(source_allergy)
    new_allergy.update(source_record_status=source_allergy.get("status", "not_supplied"), patient_report=report,
                       reconciliation_status="current_team_verification_pending")
    current = {"status": "reported_prior_plan_available_current_review_pending", "source_kind": "constructed",
        "as_of_offset_hours": VISIT_HOURS, "items": items, "source_refs": [x["source_ref"] for x in items],
        "source_record_status": source_current.get("status", "not_supplied"),
        "scope_status": "complete_for_declared_constructed_window_not_complete_source_reconciliation",
        "scope": "Reported use after the prior simulated clinic visit; scheduled items, unresolved prior-order candidates, PRN/nonprescription reporting are explicit. Query items for actual status.",
        "reconciliation_status": "not_yet_verified_by_current_visit_team", "current_plan_item_count": len(eligible),
        "coverage_caveat": "This is a constructed new outpatient report. It does not establish that all real discharge medicines were recovered, or that drugs outside this candidate window were clinically unnecessary."}
    facts = []
    def add(topic, text, *, status="reported", knowledge="known_self_experience", refs=(), interpretation=None):
        if topic == "past_medical_history": scope="historical diagnosis with unspecified event/onset time; patient recall as of current visit"
        elif topic in {"actual_medication_use","nonprescription_use","recent_unscheduled_use","medication_scope"}:
            scope="defined six-day constructed use/report window, +24h through +168h"
        elif "最近一天" in text or "近一天" in text: scope="last24h before current visit; not the entire episode"
        elif "昨晚" in text: scope="the previous night as stated by patient; exact clock interval not supplied"
        elif "近几天" in text or "这几天" in text or "最近几天" in text: scope="recent days as narrated; precise onset/duration unspecified"
        elif topic=="allergy_history":scope="retrospective patient recall as of current visit; no confirmed lifetime-negative claim"
        else:scope="current visit statement; use only the temporal extent explicitly stated in its natural-language fact"
        fact = {"fact_id": f"episode4-{len(facts)+1:03d}", "section": "current_episode", "topic": topic, "field": topic,
            "value": text if status != "unknown" else None, "reported_expression": text if status == "unknown" else None,
            "status": status, "source_kind": "constructed", "source_refs": list(refs), "knowledge_state": knowledge,
            "episode_window": {"start_offset_hours": PLAN_HOURS, "end_offset_hours": VISIT_HOURS, "meaning":"scenario grouping only, not fact validity interval"},
            "statement_as_of_offset_hours": VISIT_HOURS, "narrative_time_scope":scope,
            "construction_version": VERSION}
        if interpretation: fact["interpretation_limit"] = interpretation
        facts.append(fact)
    add("chief_complaint", "今天来复核最近的身体情况和上次门诊带回来的药，想知道接下来怎样处理。")
    add("timeline", "上次门诊是在六天前，这次按约定回来复查。")
    add("symptom_trajectory", "最近几天整体差不多，没有突然明显变差。")
    memory = historical_patient_memory(case)
    for item in memory:
        add("past_medical_history", f"以前医生跟我说过我有{item['lay_name']}。", knowledge="constructed_memory_based_on_historical_code", refs=item["source_refs"],
            interpretation="Constructed recall based on a historical diagnosis code, not evidence of actual source patient communication or the present encounter diagnosis.")
    add("past_medical_history_documents", "以前的住院资料也带来了，其他不熟悉的病名想请医生帮我解释。", knowledge="known_from_brought_record")
    add("daily_function", "最近日常活动和前几天差不多，没有新出现的晕倒或突然不能交流。", interpretation="No independent walking or absence of historical functional/cognitive disease is inferred.")
    add("oral_intake", "这几天能吃饭喝水，也能把口服药咽下去。")
    add("diarrhea", "最近一天没有新出现的频繁水样腹泻。")
    add("medication_materials", "上次门诊整理的药袋和用法记录带来了，哪些在用、最近怎么用都可以逐个看。", knowledge="known_from_brought_record",
        interpretation="The patient's brought record is constructed background, not the current nurse's finished reconciliation or source discharge document.")
    add("medication_scope", "这六天用过的药都按这张记录整理了；以前住院用过的药不全在现在的药袋里。", knowledge="known_from_brought_record",
        interpretation="Defined episode report only. Completeness of real longitudinal treatment and all comorbid indications is not established.")
    add("nonprescription_use", "这六天没有另外买药、保健品或草药来用。", status="denied",
        interpretation="Constructed patient report in a defined window, not a source-record negative or independently verified absence.")
    add("recent_unscheduled_use", "这六天没有自己加量，也没有另外临时用药。", status="denied")
    add("allergy_history", report["value"], status="unknown" if allergy_conflict else "reported",
        knowledge="not_available" if allergy_conflict else "known_self_experience",
        interpretation="Source status is unchanged; reported none-known is not confirmed no allergy.")
    add("recent_adverse_experience", "最近一天没有新发现全身风团、嘴唇舌头肿起或突然喘不过气。")
    add("home_measurements", "这几天在家没量血糖、血压、血氧或体重，也没做新的抽血检查。", knowledge="not_measured")
    for item in items:
        if item.get("plan"):
            a, s = item["actual_use"], item["supply"]
            dose = f"每次{item['dose']} {item['dose_unit']}，{frequency_spoken(item['frequency'],item['planned_daily_dose_count'])}，{ROUTES[item['route']]}"
            add("current_medication", f"药袋上写着{item['name']}，规格{item['strength']}；用法是{dose}。", knowledge="known_from_brought_record", refs=[item["record_id"]])
            add("actual_medication_use", f"这六天{item['name']}按记录用了{a['reported_taken_doses']}次，没漏用；最后一次大约在{12/next(e['daily'] for e in entries if e['source_order']['record_id']==item['source_ref']):g}小时前。现在的量按记录还够{ s['remaining_dose_equivalents']}次。", refs=[item["record_id"]])
        else:
            add("unresolved_medication", f"旧单里还有{item['name']}，这六天我没用这一项；是否还需要用、原先具体怎样用，这次想请你一起核对。", refs=[item["record_id"]],
                interpretation="Not taken is a constructed current-window fact; historical administration and the reason for change remain unknown.")
    for topic, text in SYMPTOMS[case["group"]]: add(topic, text, knowledge="not_measured" if topic == "weight_change" else "known_self_experience")
    add("followup_questions", "我想知道药要不要继续或者调整，哪些情况需要尽快再来就医。")
    result["case_id"] = case["case_id"] + "_episode4p22"
    result["patient_initial"] = {"demographics": deepcopy(case["patient_initial"]["demographics"]), "patient_facts": facts,
        "communication_profile": {"style": "自然口语，能读手边药袋与简明用法；不熟悉术语时请解释",
            "intentional_misreport": False, "automatic_age_memory_penalty": False,
            "unlisted_fact_policy": "Do not invent an unlisted fact. Use knowledge_state to distinguish not measured, unavailable and uncertain recall; do not recite provenance or interpretation_limit as patient speech."}}
    # The relative age is not recalculated from hidden exact dates; seven days
    # does not warrant inventing a new precision beyond the existing age basis.
    for obj in (result["patient_initial"]["demographics"], result["his_initial"]["demographics"]):
        obj["age_time_basis"] = "approximate source-derived age for nearby constructed visit; exact birthday unavailable; source90+ remains nonnumeric"
    his = result["his_initial"]
    his["time_basis"] = {"history_offset_origin": "source_anchor_discharge", "new_visit_offset_hours": VISIT_HOURS,
        "source_mode": "historical_seed_plus_separate_constructed_episode",
        "meaning": "Source records keep original times. The prior simulated outpatient plan is +24h and this new review is +168h; historical tests are not current measurements."}
    his["allergy_record"], his["current_use_record"] = new_allergy, current
    for obs in his.get("current_observations", []):
        if obs.get("source_kind") != "constructed": raise ValueError("Refuse to retime a non-constructed observation")
        obs["time"] = {"basis": "source_relative_to_anchor_discharge", "event_offset_hours": VISIT_HOURS,
            "recorded_offset_hours": VISIT_HOURS, "event_time_known": True, "available_by_source_cutoff": False,
            "availability_basis": "constructed_new_episode_current_measurement"}
    evaluation = deepcopy(case.get("evaluation", {}))
    evaluation.update(reference_facts=deepcopy(facts), required_facts=[], prescription_labels=None, risk_labels=None,
        clinical_reference_status="pending_independent_review", clinical_current_suitability="pending_independent_review",
        required_fact_selection_status="pending_existing_paper_rubric_alignment", prescribing_required=False,
        source_medication_candidate_ref=None,
        input_qualification={"structural": "checked_by_generator", "clinical_sufficiency": "pending",
            "medication_plan": "constructed_from_source_patterns_not_validated", "normal_control_status": "not_assigned",
            "essential_safety_tests": "casewise_independent_assessment_pending"})
    result["evaluation"] = evaluation
    result["provenance"] = {"source_dataset": "MIMIC-IV 2.2", "uses_patient_records": True,
        "construction_version": VERSION, "parent_case_id": case["case_id"], "parent_case_sha256_canonical": digest(case),
        "original_provenance": deepcopy(case.get("provenance", {})), "source_allergy_record": source_allergy,
        "source_current_use_record": source_current, "previous_evaluation": deepcopy(case.get("evaluation", {})),
        "candidate_plan": deepcopy(entries), "candidate_exclusions": exclusions, "policy": deepcopy(POLICY),
        "source_clinical_history_hashes": {k: digest(case["his_initial"][k]) for k in HISTORY_FIELDS},
        "constructed_fields": ["patient_initial.patient_facts", "patient_initial.communication_profile", "his_initial.current_use_record",
                               "his_initial.allergy_record.patient_report", "his_initial.current_observations.time", "his_initial.time_basis"],
        "model_outputs_read": False, "clinical_review_status": "pending_independent_review",
        "real_world_current_medication_reconstruction_claimed": False}
    result.pop("runtime_overlay_provenance", None)
    result["runtime_overlay_provenance"] = {"previous_overlay": deepcopy(case.get("runtime_overlay_provenance")),
        "episode_version": VERSION, "new_run_required": True, "source_product_exact_mapping_verified": False}
    audit = validate_episode(original, result)
    audit.update(candidate_count=len(items), scheduled_plan_count=len(eligible), unresolved_candidate_count=len(items)-len(eligible),
        raw_source_current_medication_scope_unchanged=True, clinical_qualification="pending", source_history_exclusion_counts=exclusions,
        original_primary_candidate_present_in_new_plan=any(i["source_ref"] == case["provenance"]["selected_historical_order"]["record_id"] for i in eligible))
    return result, audit


def validate_episode(source, case):
    validate_case(case)
    for k in ("patient_key", "group", "split", "data_kind", "formulary"):
        if source[k] != case[k]: raise ValueError("Forbidden identity, split or formulary change")
    for k in HISTORY_FIELDS:
        if digest(source["his_initial"][k]) != digest(case["his_initial"][k]): raise ValueError("Historical source content changed")
    source_allergy = source["his_initial"]["allergy_record"]
    if any(case["his_initial"]["allergy_record"].get(k) != v for k,v in source_allergy.items()):
        raise ValueError("Original allergy status or evidence changed")
    if case["provenance"]["source_current_use_record"] != source["his_initial"]["current_use_record"]:
        raise ValueError("Original current-use uncertainty was lost")
    if case["his_initial"]["time_basis"]["new_visit_offset_hours"] != VISIT_HOURS: raise ValueError("Wrong visit time")
    for item in case["his_initial"]["current_use_record"]["items"]:
        if item["source_kind"] != "constructed": raise ValueError("Current report source is ambiguous")
        if "plan" in item:
            a, s, p = item["actual_use"], item["supply"], item["plan"]
            if not p["issued_offset_hours"] <= a["window_start_offset_hours"] < a["window_end_offset_hours"] == VISIT_HOURS:
                raise ValueError("Invalid plan/use timeline")
            if not a["window_start_offset_hours"] <= a["last_taken_offset_hours"] <= a["window_end_offset_hours"]:
                raise ValueError("Dose outside its report window")
            if s["initial_dose_equivalents"]-s["used_dose_equivalents"] != s["remaining_dose_equivalents"]:
                raise ValueError("Supply does not close")
            if a["reported_taken_doses"] != s["used_dose_equivalents"]: raise ValueError("Use and supply disagree")
            counts = [a[k] for k in ("planned_doses", "reported_taken_doses", "missed_doses", "additional_doses")]
            if any(type(v) is not int or v < 0 for v in counts): raise ValueError("Invalid dose counts")
            if a["missed_doses"] > a["planned_doses"] or a["reported_taken_doses"] != a["planned_doses"]-a["missed_doses"]+a["additional_doses"]:
                raise ValueError("Planned, taken, missed and additional doses disagree")
            expected = item["planned_daily_dose_count"] * (a["window_end_offset_hours"]-a["window_start_offset_hours"])/24
            if a["planned_doses"] != expected: raise ValueError("Scheduled frequency and report window disagree")
            if scheduled_daily_count(item.get("frequency"))[0] != item["planned_daily_dose_count"]:
                raise ValueError("Explicit frequency disagrees with planned daily count")
    old_vitals = [{k:v for k,v in o.items() if k != "time"} for o in source["his_initial"].get("current_observations", [])]
    new_vitals = [{k:v for k,v in o.items() if k != "time"} for o in case["his_initial"].get("current_observations", [])]
    if old_vitals != new_vitals: raise ValueError("Vital values/conditions changed")
    for obs in case["his_initial"].get("current_observations", []):
        time = obs.get("time", {})
        if time.get("event_offset_hours") != VISIT_HOURS or time.get("recorded_offset_hours") != VISIT_HOURS or time.get("event_time_known") is not True:
            raise ValueError("Current observation time does not match current visit")
    chart = HistoryChart(case["his_initial"]); initial = chart.initial_view()
    pages, maximum = [], 0
    for section in ("current_use_record", "current_use_items", "allergy_record", "current_observations"):
        offset = 0
        while True:
            raw = chart.query(section, offset=offset, limit=20)
            projected = project_history_page(raw); verify_projection(raw, projected)
            pages.append(raw); maximum = max(maximum, json_bytes(projected))
            if raw["next_offset"] is None: break
            offset = raw["next_offset"]
    visible = runtime_visible_case(case)
    if "evaluation" in visible or "provenance" in visible: raise ValueError("Evaluator/source leakage")
    return {"source_history_unchanged": True, "source_allergy_status_unchanged": True,
        "current_source_unknown_retained_separately": True, "identity_partition_unchanged": True,
        "current_vital_values_and_conditions_unchanged": True, "no_new_labs_or_imaging": True,
        "current_view_projection_verified": True, "current_query_page_count": len(pages),
        "maximum_projected_current_page_bytes": maximum, "initial_chart_bytes": json_bytes(initial),
        "patient_fact_count": len(case["patient_initial"]["patient_facts"]), "model_calls": 0}


def generate(manifest_path, output, count=3):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists(): raise FileExistsError("Never overwrite a previous current episode version")
    m = json.loads(manifest_path.read_text()); selected = m["cases"][:count]
    if count != 3 or [e["group"] for e in selected] != ["type2_diabetes", "heart_failure", "copd"]:
        raise ValueError("Preview is restricted to the original fixed first3 source cases")
    protected = {str(manifest_path): file_hash(manifest_path)}
    for e in m["cases"]:
        if file_hash(e["path"]) != e["sha256"]: raise ValueError("Input frozen case hash mismatch")
        protected[str(Path(e["path"]).resolve())] = e["sha256"]
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = Path(tempfile.mkdtemp(prefix="."+output.name+".", dir=output.parent)); tmp.chmod(0o700)
    try:
        write(tmp/"policy_frozen_before_cases.json", {"policy": POLICY, "generator_sha256": file_hash(__file__), "model_calls": 0})
        entries, audits, product_counts = [], [], Counter()
        for e in selected:
            source = json.loads(Path(e["path"]).read_text()); result, audit = build_episode(source)
            relative = Path("cases")/(result["case_id"]+".json"); write(tmp/relative, result)
            entry = deepcopy(e); entry.update(case_id=result["case_id"], path=str(output/relative), sha256=file_hash(tmp/relative),
                clinical_suitability="pending_independent_input_review", formal_eligible=False, prescribing_required=False,
                constructed_view_complete=True, clinical_reference_status="pending_independent_review")
            entries.append(entry); audits.append({"case_id": result["case_id"], "source_case_path": e["path"],
                "source_case_sha256": e["sha256"], "new_case_sha256": entry["sha256"], **audit})
            for item in result["his_initial"]["current_use_record"]["items"]:
                product_counts[(item["name"], item["strength"], item["route"], item["current_use_status"])]+=1
        manifest = {"version": VERSION, "case_count": len(entries), "cases": entries, "purpose": "fixed development input review before model execution",
            "source_manifest": str(manifest_path), "source_manifest_sha256": protected[str(manifest_path)],
            "formal_eligible": False, "new_run_required": True, "clinical_reference_status": "pending_independent_review", "model_calls": 0}
        write(tmp/"manifest.json", manifest)
        if any(file_hash(p) != h for p,h in protected.items()): raise RuntimeError("Frozen source changed during preparation")
        summary = {"version": VERSION, "cases": len(entries), "source_history_unchanged": True,
            "source_files_unchanged": True, "source_partition_unchanged": True, "new_current_laboratory_values": 0,
            "clinical_review_pending": len(entries), "clinical_gold_labels": 0, "model_calls": 0,
            "scheduled_candidate_plan_items": sum(a["scheduled_plan_count"] for a in audits),
            "unresolved_candidate_items": sum(a["unresolved_candidate_count"] for a in audits),
            "initial_chart_max_bytes": max(a["initial_chart_bytes"] for a in audits),
            "current_page_max_bytes": max(a["maximum_projected_current_page_bytes"] for a in audits),
            "by_group": [{"group": e["group"], **{k:a[k] for k in ("candidate_count", "scheduled_plan_count", "unresolved_candidate_count", "original_primary_candidate_present_in_new_plan")}} for e,a in zip(entries,audits)]}
        write(tmp/"summary.json", summary)
        write(tmp/"audit_private.json", {"cases": audits, "protected_source_files": protected})
        write(tmp/"candidate_products_aggregate.json", {"products": [{"name_raw": k[0], "strength_raw": k[1], "route_raw": k[2], "current_status":k[3], "case_count":v} for k,v in sorted(product_counts.items())],
            "meaning": "Source-linked candidate identities; not verified source NDC matches or clinically approved current treatments"})
        write(tmp/"input_freeze.json", {"version": VERSION, "files": [{"path":str(output/p.relative_to(tmp)), "sha256":file_hash(p)} for p in sorted(tmp.rglob("*")) if p.is_file()]})
        tmp.rename(output); return summary
    except BaseException:
        shutil.rmtree(tmp); raise


if __name__ == "__main__":
    os.umask(0o077); p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True); p.add_argument("--output", required=True)
    a = p.parse_args(); print(json.dumps(generate(a.manifest, a.output), ensure_ascii=False))
