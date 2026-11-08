"""Finalize private cohort indexes, time-basis metadata, and aggregate-only report."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from ..cases import runtime_visible_case, write_case
from .mimic_cohort import save
from .audit_runtime_sizes import audit


TIME_BASIS = {"history_offset_origin": "source_anchor_discharge", "new_visit_offset_hours": 24,
              "source_mode": "historical_seed",
              "meaning": "Offset 0 belongs to yesterday's source discharge, not to the new outpatient encounter."}


def finalize(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest_private.json").read_text())
    summary = json.loads((folder / "conversion_summary.json").read_text())
    summary["time_basis_checked"] = True
    root = folder.parent
    development, test = [], []
    for record in manifest["cases"]:
        path = folder / record["path"]
        case = json.loads(path.read_text())
        if case["his_initial"].get("time_basis") != TIME_BASIS:
            case["his_initial"]["time_basis"] = dict(TIME_BASIS)
            write_case(path, case)
            save(path.parent / "runtime_input.json", runtime_visible_case(case))
        item = {**record, "path": str(path), "data_kind": "mimic_derived", "external_api_allowed": False}
        (development if record["split"] == "development" else test).append(item)
    independent = json.loads((root / "synthetic_independent" / "development_manifest.json").read_text())["cases"]
    combined = development + independent
    # An opaque source-patient key is enough to verify no cross-split duplicates.
    all_patients = [x["patient_key"] for x in combined + test]
    if len(all_patients) != len(set(all_patients)):
        raise ValueError("patient overlap in combined development/test manifests")
    save(root / "development1000_private_manifest.json", {
        "target_count": 1000, "case_count": len(combined), "cases": combined,
        "counts_by_data_kind": dict(Counter(r["data_kind"] for r in combined)),
        "clinical_reference_status": "pending_independent_review",
        "origin_boundary": "Only synthetic_independent rows may use the currently unverified external endpoint."})
    save(root / "test2000_private_manifest.json", {
        "target_count": 2000, "case_count": len(test), "cases": test,
        "data_kind": "mimic_derived", "external_api_allowed": False,
        "clinical_reference_status": "pending_independent_review"})
    summary["development_reference_total_prepared"] = len(combined)
    summary["development_origin_counts"] = dict(Counter(r["data_kind"] for r in combined))
    summary["test_total_prepared"] = len(test)
    summary["patient_split_overlap"] = 0
    save(folder / "conversion_summary.json", summary)
    size_report = audit(folder)
    pilot_count = json.loads((folder / "pilot50_private_manifest.json").read_text())["case_count"]
    source = json.loads((folder / "source_identity.json").read_text())
    final = {"status": "source_preparation_complete_clinical_and_runtime_qualification_pending",
             "source": source, "preparation": summary, "runtime_sizes": size_report,
             "formal_eligible_case_count": 0,
             "formal_eligibility_reason": "Independent clinical references, native medicine mapping and API data handling are not yet verified.",
             "synthetic_engineering_catalogue_items": 5,
             "synthetic_catalogue_covers_all_source_medicines": False,
             "six_error_classes_clinically_validated": False}
    save(folder / "final_data_audit.json", final)
    coverage = summary["case_field_coverage_counts"]
    size = size_report["metrics"]["runtime_compact_json_utf8_bytes"]
    lines = ["TwinHospital MIMIC 病例准备实数报告", "更新时间：2026-11-07", "所属项目：TwinHospital 实验", "",
             "一、实际完成", f"来源：MIMIC-IV 2.2，ZIP SHA256 {source['sha256']}。",
             f"选定并成功转换 {summary['converted_cases']} 名不同来源患者；按患者隔离，开发{len(development)}、测试{len(test)}；各组拆分详见conversion_summary.json。",
             f"另有{len(independent)}例独立构造开发资料，合计开发{len(combined)}例（目标1000）。它们来自五个模板的变体，并不代表同样数量的不同病理。",
             f"保存历史住院 {summary['history_encounters']} 次、处方医嘱 {summary['historical_orders']} 条、可用历史检验 {summary['historical_observations']} 条。",
             "完整来源底稿、原始来源索引、场景补充、角色输入和评价配置分别保存。患者数据未发送给外部模型。",
             "参考资料：https://physionet.org/content/mimiciv/2.2/", "",
             f"二、字段实测（分母均为{summary['converted_cases']}名已转换患者）"]
    labels = {"has_completed_historical_stay": "有已完成历史住院", "has_earlier_stay_in_addition_to_anchor": "除选定来源住院外还有更早住院",
              "has_historical_orders": "有历史处方医嘱", "has_historical_labs": "有可用历史检验", "has_pharmacy_records": "有药房记录",
              "has_order_with_daily_dose_count": "至少一条医嘱含每日次数字段", "has_pharmacy_frequency": "至少一条药房记录含频次",
              "has_ndc_or_gsn_or_formulary_code": "至少一条医嘱药物代码字段非空（未核验代码有效性）", "topcoded_age_90_plus": "90岁以上非精确年龄"}
    lines += [f"{labels.get(k,k)}：{v}" for k, v in coverage.items()]
    lines += ["以上“至少一条”不表示该患者所有医嘱字段完整，也不证明药物适应证、对照处方或门诊情境已经合适。",
              "参考资料：https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html ；https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html", "",
              "三、完整视图大小与接入限制", f"完整runtime紧凑JSON（UTF-8字节）：median={size['median']}，p95={size['p95_nearest_rank']}，max={size['max']}。",
              f"超过120000字节：{size['over_limit_count']} / {size['count']}。字节数不是token数；系统提示和历史会话还会增加请求大小。",
              "完整病史没有截断。正式接入需有可追溯的视图选择/分页查询，记录选择规则、返回记录ID、时间范围和是否还有下一页。",
              "五药formulary只供独立合成工程测试；不覆盖所有真实药物，也不能证明六类用药错误已全部临床验证。真实病例formulary仍为空，等待药物规范化与原生目录映射。",
              "参考资料：本地 runtime_view_size_summary.json、runtime_view_sizes_private.json。", "",
              "四、仍待完成", "正式临床可用数量尚不能等同于已转换数量；当前formal_eligible均为false。来源整理通过与实验适用性、模型运行成功是不同状态。",
              "新门诊主诉、起病和当前用药自述为明确构造，临床合理性与独立参考标签待审核；不能将历史住院医嘱当作家庭当前服药。",
              "没有Note/ED包；本轮未提取eMAR、OMR或ICU事件，不能将未提取说成无记录。当前门诊检查按场景需要另补。",
              "来源历史offset=0是源出院，新模拟门诊在+24小时，已在运行视图明确标注；90岁以上不生成精确年龄。",
              f"MIMIC开发清单已准备{pilot_count}例（目标50）但不外发。外部API需核验零留存、不训练、无人工审阅等条件后方可接入。",
              "参考资料：https://physionet.org/news/post/llm-responsible-use/", ""]
    report = folder / "MIMIC_病例准备实数报告_20261107.txt"
    report.write_text("\n".join(lines))
    report.chmod(0o600)
    return {"converted": summary["converted_cases"], "development": len(combined), "test": len(test),
            "runtime_size_summary": size_report, "formal_eligible": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True)
    args = p.parse_args()
    print(json.dumps(finalize(args.folder), ensure_ascii=False))
