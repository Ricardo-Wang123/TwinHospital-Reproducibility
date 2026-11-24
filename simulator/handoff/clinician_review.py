"""Prepare a local, source-blinded clinician review from a frozen ED cohort."""

import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path

from sandbox.experiments.linked.cases import load_case
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.contracts import runtime_case
from sandbox.experiments.linked.emergency import PRIOR_REVIEW_SECTIONS, is_triage_reason, triage_reasons
from sandbox.experiments.linked.ed_treatment_review import SINGLE_PRESCRIPTION
from sandbox.experiments.visibility import assert_visible


GROUPS = {
    "type2_diabetes": "2 型糖尿病", "heart_failure": "心力衰竭", "copd": "慢性阻塞性肺疾病",
    "pneumonia": "肺炎", "uti": "尿路感染",
}
HISTORY = {
    "encounter_headers": "既往就诊",
    "diagnoses": "既往诊断",
    "historical_orders": "既往处方",
    "historical_administrations": "既往给药",
    "historical_observations": "既往检验和测量",
    "historical_outpatient_measurements": "既往门诊测量",
    "historical_procedures": "既往操作",
    "historical_microbiology": "既往微生物记录",
    "historical_dispenses": "既往调剂",
}
ED_SECTIONS = {
    "current_observations": "分诊生命体征",
    "ed_workup": "本次开方前记录",
    "ed_reassessment_workup": "后续回报的病史和用药记录",
    "ed_diagnostic_reports": "本次检查结果",
    "ed_reassessment_reports": "后续回报的检查结果",
}
SOURCE_SCOPE = (
    "以该次急诊的开方时点为界。时钟从到达急诊起算，既往记录标为负时间。"
    "检查以回报或入档时间为准。已准备的固定过敏史按模拟设定从开诊可查。"
)
INSTRUCTIONS = (
    "逐例核对急诊主病的用药需求。用通用名填写确有必要的成分。"
    "若多种方案均合适，在同一格内分行列出完整方案，并写清适用条件。",
    "伴随疾病、既往用药、过敏和检查用于判断适应证和安全性。标注以成分为单位，规格、剂量和疗程可作为依据。",
    "根据现有证据选择「需用药」「无需用药」或「资料不足」，并简写最关键的依据。各评审者独立填写一份。",
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, content, relative=""):
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title><link rel="stylesheet" href="{relative}病例样式.css">'
        f'<main><header><p>TwinHospital　急诊处方标注</p><h1>{esc(title)}</h1></header>{content}</main></html>'
    )


def table(headers, rows):
    heading = "".join(f'<th scope="col">{esc(col)}</th>' for col in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(col)}</td>" for col in row) + "</tr>" for row in rows)
    return f'<div class="table"><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>'


def details(title, content, *, opened=False):
    return f'<details{" open" if opened else ""}><summary>{esc(title)}</summary>{content}</details>'


def present(*values):
    return next((value for value in values if value is not None and value != ""), "—")


def relative_time(hours):
    if not isinstance(hours, (int, float)):
        return "—"
    minutes = round(abs(hours) * 60)
    sign = "−" if hours < 0 else "+"
    return f"T{sign}{minutes // 60} h {minutes % 60:02d} min"


def chart_records(history, section, cutoff=None):
    records = []
    offset = 0
    while offset is not None:
        options = {"release_before_seconds": cutoff} if cutoff is not None else {}
        report = history.query(section, offset=offset, **options)
        if report["requires_expansion"]:
            raise ValueError(f"Unexpanded clinical evidence in {section}")
        records.extend(record["record"] for record in report["records"])
        offset = report["next_offset"]
    assert_visible(records)
    return records


def item_text(row):
    name = present(row.get("display"), row.get("name"), row.get("drug_name_raw"), row.get("drug_name"),
                   row.get("medication"), row.get("test_name"), row.get("source_setting"), row.get("setting"))
    measurement = present(row.get("value_raw"), row.get("value"))
    unit = present(row.get("unit_raw"), row.get("unit"))
    result = " ".join(str(part) for part in (measurement, unit) if part != "—")
    regimen = row.get("regimen_raw") or {}
    dosing = " ".join(str(v) for v in (row.get("dose", regimen.get("dose_val_rx")),
          row.get("dose_unit", regimen.get("dose_unit_rx")), row.get("route", regimen.get("route")),
          row.get("frequency", regimen.get("frequency"))) if v not in (None, ""))
    diagnoses = ", ".join(item["display"] for item in row.get("diagnoses", []) if item.get("display"))
    return (str(name), " · ".join(i for i in (result, dosing, diagnoses) if i) or "—")


def record_table(rows):
    result = []
    for row in rows:
        time = row.get("time") or {}
        seen = relative_time(present(time.get("recorded_offset_hours"), row.get("discharge_offset_hours")))
        observed = relative_time(present(time.get("event_offset_hours"), row.get("admission_offset_hours")))
        name, value = item_text(row)
        comment = present(row.get("lab_comment_raw"), row.get("flag_raw"))
        if comment != "—" and str(comment) not in value:
            value = f"{value}\n{comment}"
        status = present(row.get("status"), row.get("evidence_type"))
        coding = row.get("coding") or {}
        if coding.get("code"):
            name = f"{name} ({coding['code']})"
        result.append((seen, observed, name, value, status, present(row.get("source_kind"))))
    return table(("回报或离院", "发生或入院", "记录", "内容", "原始状态", "来源"), result)


def clinical_page(case, *, allergy_source_status=None, workbook_available=False):
    c = runtime_case(case)
    chart = c["his_initial"]
    basis = chart["time_basis"]
    if basis["ed_prescribing_schedule"] != SINGLE_PRESCRIPTION:
        raise ValueError("Clinician review expects the final single-prescription study cohort")
    (checkpoint,) = basis["ed_order_checkpoints"]
    cutoff = checkpoint["release_before_seconds"]
    history = HistoryChart(chart)
    view = history.initial_view()
    personal = view["demographics"]
    case_id, label = c["case_id"], GROUPS[c["group"]]
    complaints = triage_reasons(c)
    if not complaints:
        raise ValueError(f"No recorded ED chief complaint for {case_id}")
    patient = [fact for fact in c["patient_initial"]["patient_facts"]
               if not is_triage_reason(fact) and fact.get("status") != "unknown"]
    assert_visible(patient)
    flags = view["allergy_record"]
    drugs = view["current_use_record"]
    age = present(personal.get("age"))
    documented_allergies = flags.get("items", [])
    allergy_status = {"none_known": "无已知过敏", "allergy_reported": "有已知过敏",
                      "unknown": "未记录过敏史"}.get(flags.get("status"), flags.get("status"))
    intros = table(("主病分类", "年龄", "记录性别", "开方资料截止"), [
        (label, age, present(personal.get("sex")), relative_time(cutoff / 3600)),
    ])
    workbook = '<a href="../医生标注.xlsx">标注表与药品目录</a>' if workbook_available else ""
    content = '<nav><a href="../从这里打开.html">病例索引</a>' + workbook + '</nav>'
    content += intros + f'<p class="scope">{esc(SOURCE_SCOPE)}</p>'
    content += '<h2>分诊主诉</h2><p class="lead">' + esc(" / ".join(complaints)) + "</p>"
    if patient:
        facts = [(present(f.get("topic")), f["value"], present(f.get("source_kind"))) for f in patient]
        content += details("患者可提供的陈述", table(("主题", "陈述", "来源"), facts))
    problems = [item_text(row)[0] for row in view["opening_summary"]["problem_list"].get("items", [])]
    if problems:
        content += "<h2>开诊病史摘要</h2><p>" + esc(" · ".join(problems)) + "</p>"
    allergens = [(present(row.get("name"), row.get("substance"), row.get("display")),
                  present(row.get("reaction")), present(row.get("source_kind"))) for row in documented_allergies]
    content += f'<h2>过敏史</h2><p>{esc(present(allergy_status))} · {esc(present(flags.get("source_kind")))}</p>'
    if allergy_source_status == "simulation_default_source_missing":
        content += "<p class=\"scope\">原始资料缺少明确记录。本场景按无已知过敏提供给医生。</p>"
    if allergens:
        content += table(("过敏原", "反应", "来源"), allergens)
    content += "<h2>开诊用药记录</h2>"
    opening_medications = chart_records(history, "current_use_items")
    if opening_medications:
        content += record_table(opening_medications)
    else:
        label = drugs.get("record_label", "开诊用药栏无可列条目")
        content += f"<p>{esc(label)}。既往用药与本次后续核对见下方。</p>"
    content += "<h2>本次急诊</h2>"
    counts = {}
    for section, name in ED_SECTIONS.items():
        if section not in chart:
            continue
        records = chart_records(history, section, cutoff if section.startswith("ed_reassessment") else None)
        counts[section] = len(records)
        if records:
            content += details(f"{name}（{len(records)}）", record_table(records),
                               opened=section in {"current_observations", "ed_diagnostic_reports", "ed_reassessment_reports"})
    content += "<h2>既往记录</h2>"
    for section in PRIOR_REVIEW_SECTIONS:
        if not view["sections"].get(section, {}).get("total_count"):
            continue
        records = chart_records(history, section)
        counts[section] = len(records)
        content += details(f"{HISTORY[section]}（{len(records)}）", record_table(records))
    return (case_id, label, page(case_id, content, "../"), counts)


CSS = """
*{box-sizing:border-box}body{margin:0;background:#f5f7f9;color:#182837;font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:auto;padding:36px 24px 72px}header{background:#162b42;color:#fff;padding:22px 27px;margin-bottom:21px;border-radius:5px}
header p{margin:0;color:#ccdbeb;font-size:13px}h1{font-size:28px;margin:6px 0 0}h2{font-size:19px;margin:24px 0 8px}
a{color:#10558c;text-underline-offset:3px}nav{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0}p{margin-top:6px}.lead{font-size:17px}.scope{color:#42576b}
.table{overflow:auto;background:white;border:1px solid #d8e2ec;border-radius:4px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 11px;vertical-align:top}
th{background:#e2eaf3;color:#1b344d;white-space:nowrap}td{border-top:1px solid #e8edf2;white-space:pre-wrap;overflow-wrap:anywhere}
td:nth-child(1),td:nth-child(2){min-width:85px}details{margin:8px 0;border:1px solid #d8e2ec;border-radius:4px;background:white}
summary{cursor:pointer;padding:8px 12px;font-weight:650;color:#1b344d}details .table{border:0}
ol{padding-left:24px}li{margin:.35em 0}
"""


def build(cohort, output, sheet_inputs, *, workbook_available=False):
    manifest = read(cohort / "manifest.json")["cases"]
    output.mkdir(mode=0o700)
    (output / "病例").mkdir(mode=0o700)
    verify = {}
    rows = []
    allergies = cohort / "过敏史来源与归并.private.json"
    allergy_sources = read(allergies)["cases"] if allergies.exists() else {}
    for entry in sorted(manifest, key=lambda row: (row["group"], row["case_id"])):
        case = load_case(cohort / entry["path"])
        case_id, label, document, counts = clinical_page(
            case, allergy_source_status=allergy_sources.get(case["case_id"], {}).get("source_status"),
            workbook_available=workbook_available)
        if case_id != entry["case_id"] or case_id in verify or case["group"] != entry["group"]:
            raise ValueError("Case identity differs from the frozen manifest")
        (output / "病例" / f"{case_id}.html").write_text(document, encoding="utf-8")
        rows.append([case_id, label])
        verify[case_id] = counts
    allowed = read(cohort / "catalog.json")
    products = sorted(read(cohort / "formulary_directory.json")["products"],
                      key=lambda product: (product["name"].casefold(), product["drug_code"]))
    formulary = [[p["drug_code"], p["name"], p.get("display_name_zh"), p.get("ingredient"), p.get("strength"),
                  p.get("dosage_form"), ", ".join(p.get("allowed_routes") or [p["route"]])]
                 for p in products if p["drug_code"] in allowed]
    titles = Counter(label for _, label in rows)
    lead = " · ".join(f"{label} {count} 例" for label, count in titles.items())
    index = '<nav><a href="医生标注.xlsx">打开标注表与院内药品目录</a></nav>' if workbook_available else ""
    index += f"<p>{esc(lead)}</p><ol>" + "".join(f"<li>{esc(t)}</li>" for t in INSTRUCTIONS) + "</ol>"
    index += (
        '<p class="scope">病例材料用于获准访问相关 PhysioNet 数据的研究人员在受控环境核阅。'
        '<a href="https://physionet.org/about/faqs/#download-and-reuse">PhysioNet 数据使用说明</a></p>'
    )
    for label in titles:
        links = '<nav>' + "".join(f'<a href="病例/{esc(case_id)}.html">{esc(case_id)}</a>'
                                  for case_id, group in rows if group == label) + '</nav>'
        index += details(label, links, opened=True)
    (output / "从这里打开.html").write_text(page(f"{len(rows)} 例病例索引", index), encoding="utf-8")
    (output / "病例样式.css").write_text(CSS, encoding="utf-8")
    data = {"cases": rows, "catalogue": formulary, "counts": verify}
    sheet_inputs.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(rows), len(formulary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sheet-inputs", type=Path, required=True)
    parser.add_argument("--link-workbook", action="store_true", help="Link to a colocated 医生标注.xlsx file.")
    args = parser.parse_args()
    os.umask(0o077)
    cases, products = build(args.cohort, args.output, args.sheet_inputs, workbook_available=args.link_workbook)
    print(f"Prepared {cases} clinician case pages and {products} orderable products.")


if __name__ == "__main__":
    main()
