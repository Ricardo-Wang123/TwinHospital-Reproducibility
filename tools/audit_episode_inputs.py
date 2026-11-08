#!/usr/bin/env python3
"""Independent, local-only v4 input audit. No model or network calls.

The generator snapshot must match the input freeze. Private JSON includes exact
evidence references; public JSON/HTML contain aliases, aggregate checks only.
"""
import argparse
from copy import deepcopy
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import sys


VERSION = "independent-current-episode-input-audit/3"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def fixture():
    from sandbox.experiments.linked.cases import build_synthetic_case
    c = build_synthetic_case("type2_diabetes", 0)
    c["data_kind"] = "mimic_derived"
    c["provenance"] = {"source_dataset": "independent_invented_fixture", "uses_patient_records": False}
    order = {"record_id": "rx_" + "1" * 24, "source_encounter_ref": "enc_" + "2" * 24,
        "drug_name_raw": "fictional alpha tablet", "prescription_component_type_raw": "MAIN",
        "regimen_raw": {"prod_strength": "5 mg Tablet", "dose_val_rx": "5", "dose_unit_rx": "mg",
            "doses_per_24_hrs": "2", "route": "PO", "form_rx": None, "frequency": "BID"},
        "time": {"event_offset_hours": -48}, "planned_stop_offset_hours": 1}
    c["his_initial"]["historical_orders"] = [order]
    c["his_initial"]["allergy_record"] = {"status": "unknown", "items": [], "source_refs": []}
    c["his_initial"]["current_use_record"] = {"status": "not_reconciled", "items": []}
    c["provenance"]["selected_historical_order"] = deepcopy(order)
    c["evaluation"]["source_risk_cues"] = []
    return c


def guardrail_probes(module):
    source = fixture()
    source["his_initial"]["historical_orders"][0]["regimen_raw"]["frequency"] = "BID PRN"
    entries, _ = module.candidate_plan(source)
    prn_rejected = bool(entries[0]["reasons"])
    def rejects_mutation(mutator):
        source = fixture(); case, _ = module.build_episode(source)
        mutator(case)
        try:
            module.validate_episode(source, case)
            return False
        except ValueError:
            return True
    def stale_time(case):
        case["his_initial"]["current_observations"][0]["time"]["event_offset_hours"] = 24
    def inconsistent_use(case):
        item = case["his_initial"]["current_use_record"]["items"][0]
        item["actual_use"]["reported_taken_doses"] = 1
        item["supply"]["used_dose_equivalents"] = 1
        item["supply"]["remaining_dose_equivalents"] = item["supply"]["initial_dose_equivalents"] - 1
    return {"explicit_prn_with_positive_daily_count_not_adopted": prn_rejected,
        "stale_current_observation_time_rejected": rejects_mutation(stale_time),
        "taken_planned_missed_inventory_inconsistency_rejected": rejects_mutation(inconsistent_use)}


def audit(manifest_path, generator_path, repo):
    sys.path.insert(0, str(repo))
    from sandbox.experiments.linked.cases import runtime_visible_case
    from sandbox.experiments.linked.chart import HistoryChart, POLICY as CHART_POLICY
    from sandbox.experiments.linked.chart_projection import project_history_page, verify_projection, expand_projected_record
    manifest = load(manifest_path)
    freeze_path = manifest_path.parent / "policy_frozen_before_cases.json"
    if not freeze_path.exists():
        freeze_path = manifest_path.parent / "source_policy_frozen_before_cases.json"
    frozen = load(freeze_path)
    if sha(generator_path) != frozen.get("generator_sha256", frozen.get("episode_code_sha256")):
        raise ValueError("Generator snapshot does not match the source freeze")
    name = "sandbox.experiments.linked.case_preparation._independent_audited_snapshot"
    spec = importlib.util.spec_from_file_location(name, generator_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probes = guardrail_probes(module)
    parent_manifest_path = Path(manifest["source_manifest"])
    parent = load(parent_manifest_path)
    parent_by_id = {e["case_id"]:e for e in parent["cases"]}
    if all("parent_source_entry" in e for e in manifest["cases"]):
        parent_entries = [e["parent_source_entry"] for e in manifest["cases"]]
        if any(parent_by_id.get(e["case_id"]) != e for e in parent_entries):
            raise ValueError("Selected source entry does not match the frozen parent manifest")
        qualification = load(manifest_path.parent / "qualification50_private.json")
        eligible_t2 = [r["source_entry"] for r in qualification["cases"]
            if r["source_entry"]["group"] == "type2_diabetes"
            and r["case_id"] != parent["cases"][0]["case_id"] and r["source_material_gate"]]
        expected = [eligible_t2[0] if eligible_t2 else parent["cases"][0], *parent["cases"][1:3]]
        if parent_entries != expected:
            raise ValueError("Selection does not match the frozen source-material gate/order")
        source_audit = load(manifest_path.parent / "source_hash_audit_private.json")
        if any(sha(p) != h for p,h in source_audit["protected_source_files"].items()):
            raise ValueError("Preserved source material changed")
    else:
        parent_entries = parent["cases"][:len(manifest["cases"])]
    input_freeze = load(manifest_path.parent / "input_freeze.json")
    if any(sha(e["path"]) != e["sha256"] for e in input_freeze["files"]):
        raise ValueError("Frozen generated input changed")
    rows = []
    for index, (entry, original_entry) in enumerate(zip(manifest["cases"], parent_entries), 1):
        case, original = load(entry["path"]), load(original_entry["path"])
        h, old_h = case["his_initial"], original["his_initial"]
        items = h["current_use_record"]["items"]
        plans = [item for item in items if item.get("plan")]
        visit = h["time_basis"]["new_visit_offset_hours"]
        clinical_equal = True
        chart = HistoryChart(h)
        current_items, raw_pages, projected_pages = [], [], []
        offset = 0
        while True:
            raw = chart.query("current_use_items", offset=offset, limit=20)
            compact = project_history_page(raw)
            verify_projection(raw, compact)
            raw_pages.append(raw); projected_pages.append(compact)
            current_items.extend(expand_projected_record(compact, row) for row in compact["records"])
            if raw["next_offset"] is None:
                break
            offset = raw["next_offset"]
        # Source addresses are declared projection aliases, not clinical values.
        expected = [{k:v for k,v in item.items() if k not in ("source_ref", "dictionary_source_ref")} for item in items]
        clinical_equal = current_items == expected
        checks = {
            "source_manifest_sha256": sha(parent_manifest_path) == manifest["source_manifest_sha256"],
            "source_case_sha256": sha(original_entry["path"]) == original_entry["sha256"],
            "new_case_sha256": sha(entry["path"]) == entry["sha256"],
            "declared_source_identity_and_partition_preserved": all(case[k] == original[k] for k in ("patient_key", "group", "split", "data_kind")),
            "historical_encounters_orders_labs_unchanged": all(h[k] == old_h[k] for k in ("historical_encounters", "historical_orders", "historical_observations")),
            "original_allergy_evidence_retained": all(h["allergy_record"].get(k) == v for k,v in old_h["allergy_record"].items()),
            "source_current_use_uncertainty_retained": case["provenance"]["source_current_use_record"] == old_h["current_use_record"],
            "current_vital_values_conditions_unchanged": [{k:v for k,v in o.items() if k != "time"} for o in h["current_observations"]] == [{k:v for k,v in o.items() if k != "time"} for o in old_h["current_observations"]],
            "current_vitals_share_visit_time": all(o["source_kind"] == "constructed" and o["time"]["event_offset_hours"] == o["time"]["recorded_offset_hours"] == visit for o in h["current_observations"]),
            "plan_use_review_time_consistent": all(i["plan"]["issued_offset_hours"] <= i["plan"]["start_offset_hours"] == i["actual_use"]["window_start_offset_hours"] < i["actual_use"]["window_end_offset_hours"] == visit == i["plan"]["review_due_offset_hours"] and i["actual_use"]["window_start_offset_hours"] <= i["actual_use"]["last_taken_offset_hours"] <= visit for i in plans),
            "planned_taken_missed_additional_counts_close": all(i["actual_use"]["planned_doses"] - i["actual_use"]["missed_doses"] + i["actual_use"]["additional_doses"] == i["actual_use"]["reported_taken_doses"] for i in plans),
            "supply_and_actual_use_close": all(i["supply"]["initial_dose_equivalents"] - i["supply"]["used_dose_equivalents"] == i["supply"]["remaining_dose_equivalents"] and i["supply"]["used_dose_equivalents"] == i["actual_use"]["reported_taken_doses"] for i in plans),
            "current_plan_and_use_marked_constructed": all(i["source_kind"] == "constructed" and i["plan"]["source_kind"] == i["actual_use"]["source_kind"] == i["supply"]["source_kind"] == "constructed" for i in plans),
            "all_current_clinical_fields_survive_authorized_query_projection": clinical_equal,
            "evaluation_and_provenance_excluded_from_runtime": not (set(runtime_visible_case(case)) & {"evaluation", "provenance"}),
            "clinical_labels_remain_unassigned": case["evaluation"].get("prescription_labels") is None and case["evaluation"].get("risk_labels") is None,
            "prescribing_not_required": case["evaluation"].get("prescribing_required") is False,
        }
        facts = case["patient_initial"]["patient_facts"]
        rows.append({"alias": f"INPUT-{index:02d}", "group": case["group"], "checks": checks,
            "candidate_count": len(items), "constructed_plan_count": len(plans),
            "unresolved_candidate_count": len(items) - len(plans), "patient_fact_count": len(facts),
            "current_visit_offset_hours": visit,
            "unresolved_nonuse_is_explicit_constructed_patient_report": all(f.get("source_kind") == "constructed" and "没用" in str(f.get("value")) for f in facts if f.get("topic") == "unresolved_medication"),
            "unresolved_record_status_distinguishes_reported_nonuse": all("reported_not_taken" in i.get("current_use_status", "") for i in items if not i.get("plan")),
            "original_selected_candidate_in_constructed_plan": any(i["source_ref"] == original["provenance"]["selected_historical_order"]["record_id"] for i in plans),
            "engineering_structural_readiness": "passed" if all(checks.values()) else "failed",
            "clinical_input_appropriateness": "not_independently_reviewed",
            "private_evidence": {"case_id": case["case_id"], "source_case": original_entry["path"],
                "source_sha256": original_entry["sha256"], "new_case": entry["path"], "new_sha256": entry["sha256"],
                "queried_record_refs": [r["record_ref"] for p in raw_pages for r in p["records"]]}})
    ready = all(all(r["checks"].values()) for r in rows) and all(probes.values())
    report = {"schema_version": VERSION, "input_version": manifest["version"],
        "manifest_sha256": sha(manifest_path), "generator_snapshot_sha256": sha(generator_path),
        "auditor_script_sha256": sha(__file__), "chart_policy": CHART_POLICY,
        "auditor_meaning": "Independent software/data audit; not physician review, clinical approval, or treatment advice",
        "model_calls": 0, "cases": rows, "generator_guardrail_probes": probes,
        "engineering_release_status": "structurally_ready_for_limited_engineering_execution" if ready else "hold_for_generator_guardrail_or_structural_correction",
        "source_clinical_fact_mutations_detected": not all(r["checks"]["historical_encounters_orders_labs_unchanged"] and r["checks"]["original_allergy_evidence_retained"] for r in rows),
        "method_pending": ["Adopting an old inpatient order into a new prior outpatient plan is new clinical scenario construction, not format conversion",
            "Numeric daily frequency alone does not establish medication appropriateness, indication, product identity, or a discharge regimen",
            "All adopted plans currently report six days of adherence and one further day of dose-equivalent stock; these are authoring assumptions, not measured facts",
            "Unknown source allergies are retained separately from the constructed none-known report; this is not confirmed absence",
            "Disease-specific refill eligibility, plan appropriateness, necessary tests and required-fact/gold annotations remain pending independent review",
            "Generic nonuse of unresolved entries is an explicitly constructed episode fact, not evidence that historical PRN medication was not used",
            "No success-based reselection or retry-to-prescribe is permitted"],
        "task_boundaries": {
            "preconsultation": "May run after engineering release; actual dialogue and handoff must be generated",
            "full_linked_chronic_preview": "Only the three source cases selected by the recorded source-material policy; all clinical outcomes retained; structural readiness does not guarantee a prescription",
            "fixed_prescription_task": "Do not run until an actual frozen original prescription is available; no-Rx/defer remains incomplete and cannot be replaced with a forced prescription",
            "fixed_documentation_task": "Requires actual visible stage sources; a signed no-Rx decision note does not count as the complete three-task medication-payment chain",
            "pneumonia_and_urinary_infection": "Not covered by this chronic-only v4 preview policy; do not silently apply it",
            "formal_quality_or_appIII_success": "Not established by this software audit"},
        "preserved_previous_preview": True}
    return report


def render(report):
    esc = html.escape
    ready = report["engineering_release_status"].startswith("structurally_ready")
    rows = "".join(f'<tr><td>{esc(r["alias"])}</td><td>{esc(r["group"])}</td><td>{r["candidate_count"]}</td><td>{r["constructed_plan_count"]}</td><td>{r["unresolved_candidate_count"]}</td><td>{sum(r["checks"].values())}/{len(r["checks"])} 项</td></tr>' for r in report["cases"])
    guards = "".join(f'<li><code>{esc(k)}</code>：<strong>{"通过" if v else "未通过"}</strong></li>' for k,v in report["generator_guardrail_probes"].items())
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>当前门诊 v4 独立输入审计</title><style>body{{font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f2f5f8;color:#182a3a;margin:0}}main{{max-width:1000px;margin:35px auto;background:#fff;padding:32px;border-radius:16px}}h1{{font-size:28px}}h2{{font-size:21px;margin-top:30px}}.lead{{padding:18px;background:#eef4fa;border-left:5px solid #2a6395}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #dce3eb}}code{{font-size:13px;overflow-wrap:anywhere}}li{{margin:8px 0}}.muted{{color:#627587}}@media(max-width:700px){{main{{margin:0;padding:20px}}}}</style><main><p class="muted">独立软件／数据审查 · 无模型调用</p><h1>当前门诊 v4：具体输入与生成边界核验</h1><p class="lead">版本 <code>{esc(report['input_version'])}</code>：<strong>{'结构上可进入限定工程运行' if ready else '先修复生成器校验边界，保留本版记录'}</strong>。这不是医生审查，也不表示当前治疗计划已获临床认可，更不预测是否会开方或完成三大任务。</p><h2>实际三例</h2><table><tr><th>匿名审计号</th><th>来源类别</th><th>候选药条目</th><th>构造计划条目</th><th>待核对条目</th><th>数据检查</th></tr>{rows}</table><p>检查覆盖源文件哈希、患者分区、原历史与过敏记录、当前时序、实际服药／库存、构造来源标记、HIS 查询投影保留以及评价底稿隔离。表中条目数不代表已验证合理的当前药品数量。</p><h2>生成器反例测试</h2><ul>{guards}</ul><p>反例均为独立虚构结构。旧版实际体征日期正确，并不意味着其校验器能够拦下未来的错误日期；两者分别记录。</p><h2>时间与来源如何理解</h2><p>本次就诊为源出院后的第 168 小时；虚构前次门诊计划从第 24 小时开始，到本次复查是六天。七天的给药量是构造库存，不能写成已经服用了七天。原历史检验、医嘱与住院信息不改；旧构造体征的数值及测量条件保留，仅作为新场景当前事件重新标时。</p><p>将住院末期候选机械写成新的门诊计划、六天全部按时服用及相应剩余量，是新临床情境构造，绝非仅统一格式。其适应证、产品身份、剂量合理性和必要检查仍须独立评审。来源过敏未知与新患者口述分别保存；口述“不记得过敏”不能升级为确认无过敏。</p><h2>允许运行与不适用边界</h2><ul><li>只有结构及生成边界通过后，固定三例才进入限定工程运行；不按预期能否开药选人。</li><li>预问诊需要真实运行对话和交接。固定处方对照只有在实际原方已生成并冻结后才能运行；不能用强制处方补输入。</li><li>未开方、拒方或暂缓仍保留；决定文书可单独生成和签署，但不计完整三任务或药费结算。</li><li>原先选中的某种疾病用药不在新计划时，不声明该疾病特定续方已具备条件；也不据此预测并删除整例。</li><li>本版仅包含三类慢病来源；肺炎和尿路感染尚未适配该政策，不使用慢病模板代替。</li></ul><p class="muted">完整逐项证据与私有引用保存在独立私有审计 JSON；公开版本仅含匿名编号、计数、哈希及检查结果，不含原病例标识或临床原文。旧 preview 保留不变。</p></main></html>'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--generator-snapshot", type=Path, required=True)
    p.add_argument("--private-output", type=Path, required=True)
    p.add_argument("--public-output", type=Path, required=True)
    a = p.parse_args()
    report = audit(a.manifest.resolve(), a.generator_snapshot.resolve(), a.repo.resolve())
    a.private_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if a.private_output.exists():
        raise FileExistsError("Never overwrite an earlier independent audit")
    a.private_output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n"); a.private_output.chmod(0o600)
    public = deepcopy(report)
    for row in public["cases"]:
        row.pop("private_evidence")
    a.public_output.mkdir(parents=True, exist_ok=True)
    for filename, content in (("audit_summary.json", json.dumps(public, ensure_ascii=False, indent=2)+"\n"), ("TwinHospital_v4当前病例输入独立审计.html", render(public))):
        target = a.public_output/filename
        if target.exists():
            raise FileExistsError("Never overwrite an earlier public audit")
        target.write_text(content)
    print(json.dumps({"status": public["engineering_release_status"], "cases": len(public["cases"]),
        "guardrail_passes": sum(public["generator_guardrail_probes"].values()), "model_calls": 0}))


if __name__ == "__main__":
    main()
