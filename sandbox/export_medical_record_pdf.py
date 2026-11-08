"""
Export sandbox EMR JSON records to outpatient medical-record PDFs.

Can be imported by run_experiment.py or used directly:
    python MedicalAgent/sandbox/export_medical_record_pdf.py --record path/to/PAT.json --output output/record.pdf
    python MedicalAgent/sandbox/export_medical_record_pdf.py --records-dir data/records/20260622_100336 --output output/pdfs
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
except ImportError as exc:  # pragma: no cover - exercised only when dependency is absent.
    raise ImportError(
        "缺少 PDF 生成依赖 reportlab，请先运行: python -m pip install reportlab"
    ) from exc


DEFAULT_FONT_CANDIDATES = [
    os.getenv("MEDICAL_RECORD_PDF_FONT"),
    r"C:\Windows\Fonts\NotoSerifSC-VF.ttf",
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\simfang.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsunb.ttf",
]

DEFAULT_BOLD_FONT_CANDIDATES = [
    os.getenv("MEDICAL_RECORD_PDF_BOLD_FONT"),
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\NotoSerifSC-VF.ttf",
]


def _first_existing_path(candidates: Iterable[Optional[str]]) -> str:
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("未找到可用于 PDF 的中文字体，请在配置中设置 font_path")


def _register_font(font_name: str, candidates: Iterable[Optional[str]]) -> str:
    font_path = _first_existing_path(candidates)
    try:
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    except Exception as exc:
        raise RuntimeError(f"注册 PDF 字体失败: {font_path} - {exc}") from exc
    return font_path


def _format_datetime(value: Any) -> str:
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    text = str(value)
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text[:16]


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_safe_text(item) for item in value if _safe_text(item))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _format_vital_signs(vital_signs: Dict[str, Any]) -> str:
    if not isinstance(vital_signs, dict) or not vital_signs:
        return ""

    parts: List[str] = []
    temperature = vital_signs.get("temperature")
    pulse = vital_signs.get("pulse")
    respiration = vital_signs.get("respiration")
    bp = vital_signs.get("bp")

    if not bp and (vital_signs.get("sbp") or vital_signs.get("dbp")):
        bp = f"{vital_signs.get('sbp', '')}/{vital_signs.get('dbp', '')}".strip("/")

    if temperature not in (None, ""):
        parts.append(f"体温 {temperature}℃")
    if pulse not in (None, ""):
        parts.append(f"脉搏 {pulse}次/分")
    if respiration not in (None, ""):
        parts.append(f"呼吸 {respiration}次/分")
    if bp not in (None, ""):
        parts.append(f"血压 {bp}mmHg")

    known_keys = {"temperature", "pulse", "respiration", "bp", "sbp", "dbp"}
    for key, value in vital_signs.items():
        if key not in known_keys and value not in (None, ""):
            parts.append(f"{key}: {value}")

    return "；".join(parts)


def _format_prescription_reference(value: Any) -> str:
    if not isinstance(value, dict):
        return ""

    decision = value.get("decision", "")
    medications = value.get("medication") or []
    if decision and decision != "medication":
        return f"处方决策：{decision}"
    if not medications:
        return ""

    lines = []
    for med in medications:
        if not isinstance(med, dict):
            text = _safe_text(med)
            if text:
                lines.append(text)
            continue

        name = med.get("name") or "未命名药品"
        dose = med.get("dose") or ""
        route = med.get("route") or ""
        frequency = med.get("frequency") or ""
        duration = med.get("duration") or ""
        instructions = med.get("patient_instructions") or ""

        main = "，".join(part for part in [name, dose, route, frequency, duration] if part)
        if instructions:
            main = f"{main}。{instructions}" if main else instructions
        if main:
            lines.append(main)

    return "\n".join(lines)


def _record_sections(record: Dict[str, Any]) -> List[Dict[str, str]]:
    subjective = record.get("subjective") or {}
    objective = record.get("objective") or {}
    assessment = record.get("assessment") or {}
    plan = record.get("plan") or {}

    physical_parts = []
    vital_text = _format_vital_signs(objective.get("vital_signs") or {})
    if vital_text:
        physical_parts.append(f"生命体征：{vital_text}")
    physical_exam = _safe_text(objective.get("physical_exam"))
    if physical_exam:
        physical_parts.append(f"体格检查：{physical_exam}")

    treatment_parts = []
    treatment = _safe_text(plan.get("treatment"))
    if treatment:
        treatment_parts.append(treatment)
    prescription = _format_prescription_reference(plan.get("prescription_reference"))
    if prescription:
        treatment_parts.append(f"处方：{prescription}")
    education = _safe_text(plan.get("education"))
    if education:
        treatment_parts.append(f"健康教育：{education}")

    return [
        {"label": "主    诉", "text": _safe_text(subjective.get("chief_complaint"))},
        {"label": "现病史", "text": _safe_text(subjective.get("history_present_illness"))},
        {"label": "既往史", "text": _safe_text(subjective.get("past_medical_history"))},
        {"label": "体格及专科检查", "text": "\n".join(physical_parts)},
        {"label": "辅助检查", "text": _safe_text(objective.get("auxiliary_exam"))},
        {"label": "诊    断", "text": _safe_text(assessment.get("diagnosis"))},
        {"label": "治疗意见", "text": "\n".join(treatment_parts)},
    ]


def _text_width(text: str, font_name: str, font_size: int) -> float:
    return pdfmetrics.stringWidth(text, font_name, font_size)


def _wrap_text(text: str, font_name: str, font_size: int, max_width: float) -> List[str]:
    if not text:
        return [""]

    lines: List[str] = []
    for raw_line in str(text).splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            lines.append("")
            continue

        current = ""
        for char in raw_line:
            candidate = current + char
            if not current or _text_width(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines or [""]


def _sanitize_filename(value: str) -> str:
    value = value or "record"
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")


def _deep_get(data: Dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _extract_registration_no(result: Dict[str, Any]) -> str:
    return (
        _deep_get(result, "openhis_registration", "response", "data", "regNo")
        or result.get("openhis_registration_id")
        or result.get("openhis_preregistration_id")
        or _deep_get(result, "triage_info", "openhis_preregistration_id")
        or ""
    )


def build_metadata_map(
    patient_profiles: Optional[List[Dict[str, Any]]] = None,
    triage_results: Optional[List[Dict[str, Any]]] = None,
    consultation_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    metadata: Dict[str, Dict[str, Any]] = {}

    for profile in patient_profiles or []:
        patient_id = profile.get("patient_id")
        if not patient_id:
            continue
        metadata.setdefault(patient_id, {}).update(
            {
                "patient_name": profile.get("name") or patient_id,
                "gender": profile.get("gender", ""),
                "age_group": profile.get("age_group", ""),
            }
        )

    for result in triage_results or []:
        patient_id = result.get("patient_id")
        if not patient_id:
            continue
        item = metadata.setdefault(patient_id, {})
        item.update(
            {
                "patient_name": result.get("patient_name") or item.get("patient_name") or patient_id,
                "department": result.get("openhis_department_name")
                or result.get("target_department")
                or item.get("department", ""),
                "doctor_name": result.get("assigned_doctor_name") or item.get("doctor_name", ""),
                "visit_no": result.get("openhis_preregistration_id") or item.get("visit_no", ""),
                "visit_time": result.get("triage_time") or result.get("timestamp") or item.get("visit_time", ""),
            }
        )

    for result in consultation_results or []:
        patient_id = result.get("patient_id")
        if not patient_id:
            continue

        decision = result.get("consultation_decision")
        if not isinstance(decision, dict):
            decision = {}

        triage_info = decision.get("triage_info") if isinstance(decision.get("triage_info"), dict) else {}
        item = metadata.setdefault(patient_id, {})
        registration_no = _extract_registration_no(result) or _extract_registration_no(triage_info)
        item.update(
            {
                "patient_name": result.get("patient_name")
                or decision.get("patient_name")
                or item.get("patient_name")
                or patient_id,
                "department": result.get("target_department")
                or decision.get("target_department")
                or triage_info.get("openhis_department_name")
                or triage_info.get("target_department")
                or item.get("department", ""),
                "doctor_name": triage_info.get("assigned_doctor_name")
                or result.get("assigned_doctor_name")
                or item.get("doctor_name", ""),
                "visit_no": registration_no or item.get("visit_no", ""),
                "visit_time": result.get("timestamp") or item.get("visit_time", ""),
            }
        )

    return metadata


def render_medical_record_pdf(
    record: Dict[str, Any],
    output_path: str,
    metadata: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    metadata = metadata or {}
    options = options or {}

    normal_font = "MedicalRecordSC"
    bold_font = "MedicalRecordSCBold"
    _register_font(normal_font, [options.get("font_path"), *DEFAULT_FONT_CANDIDATES])
    _register_font(bold_font, [options.get("bold_font_path"), *DEFAULT_BOLD_FONT_CANDIDATES])

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output), pagesize=A4)
    width, height = A4
    margin_x = 52
    y = height - 48
    bottom = 78
    line_gap = 15

    institution_name = options.get("institution_name") or "医疗机构名称"
    title = options.get("title") or "初诊病历记录"
    meta = record.get("meta") or {}
    patient_id = meta.get("patient_id") or meta.get("record_id") or ""

    patient_name = (
        metadata.get("patient_name")
        or meta.get("patient_name")
        or patient_id
        or ""
    )
    department = metadata.get("department") or meta.get("department") or ""
    visit_no = metadata.get("visit_no") or meta.get("record_id") or patient_id
    visit_time = _format_datetime(metadata.get("visit_time") or meta.get("created_at"))
    doctor_name = metadata.get("doctor_name") or options.get("doctor_name") or ""
    sign_date = _format_datetime(options.get("date") or metadata.get("visit_time") or meta.get("created_at"))[:10]

    def new_page(continued: bool = False) -> None:
        nonlocal y
        c.showPage()
        y = height - 52
        c.setFont(bold_font, 12)
        suffix = "（续页）" if continued else ""
        c.drawCentredString(width / 2, y, f"{title}{suffix}")
        y -= 34

    def ensure_space(required: float) -> None:
        if y - required < bottom:
            new_page(continued=True)

    def draw_center(text: str, font_name: str, font_size: int, y_pos: float) -> None:
        c.setFont(font_name, font_size)
        c.drawCentredString(width / 2, y_pos, text)

    def draw_wrapped(text: str, x: float, max_width: float, font_name: str, font_size: int) -> None:
        nonlocal y
        lines = _wrap_text(text, font_name, font_size, max_width)
        for line in lines:
            ensure_space(line_gap)
            c.setFont(font_name, font_size)
            c.drawString(x, y, line)
            y -= line_gap

    draw_center(institution_name, bold_font, 18, y)
    y -= 30
    draw_center(title, bold_font, 15, y)
    y -= 50

    c.setFont(normal_font, 10.5)
    c.drawString(margin_x, y, f"姓名：{patient_name}")
    c.drawString(230, y, f"科别：{department}")
    c.drawString(405, y, f"门诊病历号：{visit_no}")
    y -= 28
    c.drawString(margin_x, y, f"就诊时间：{visit_time}")
    y -= 54

    for section in _record_sections(record):
        label = section["label"]
        text = section["text"] or ""
        ensure_space(34)
        c.setFont(bold_font, 11)
        c.drawString(margin_x, y, f"{label}：")
        text_x = margin_x + 92
        max_text_width = width - text_x - margin_x
        if text:
            draw_wrapped(text, text_x, max_text_width, normal_font, 10.5)
        else:
            y -= line_gap
        y -= 6

    if y < 160:
        new_page(continued=True)

    c.setFont(bold_font, 11)
    c.drawString(420, 160, f"医师签名：{doctor_name}")
    c.drawString(420, 132, f"日期：{sign_date}")
    c.save()
    return str(output)


def export_medical_record_pdfs(
    records: Dict[str, Dict[str, Any]],
    output_dir: str,
    options: Optional[Dict[str, Any]] = None,
    patient_profiles: Optional[List[Dict[str, Any]]] = None,
    triage_results: Optional[List[Dict[str, Any]]] = None,
    consultation_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    options = options or {}
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    metadata_map = build_metadata_map(patient_profiles, triage_results, consultation_results)
    files: List[str] = []
    errors: List[Dict[str, str]] = []

    for patient_id, record in sorted((records or {}).items()):
        try:
            record_meta = record.get("meta") or {}
            record_id = record_meta.get("record_id") or patient_id
            filename = f"{_sanitize_filename(record_id)}_initial_record.pdf"
            pdf_path = output / filename
            files.append(
                render_medical_record_pdf(
                    record=record,
                    output_path=str(pdf_path),
                    metadata=metadata_map.get(patient_id, {}),
                    options=options,
                )
            )
        except Exception as exc:
            errors.append({"patient_id": patient_id, "error": str(exc)})

    return {
        "enabled": True,
        "output_dir": str(output),
        "generated_count": len(files),
        "files": files,
        "errors": errors,
    }


def load_records_from_dir(records_dir: str) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for path in sorted(Path(records_dir).glob("*.json")):
        with path.open("r", encoding="utf-8") as file:
            record = json.load(file)
        patient_id = (
            (record.get("meta") or {}).get("patient_id")
            or path.stem
        )
        records[patient_id] = record
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="将 sandbox 病历 JSON 转换为 PDF")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--record", help="单个病历 JSON 文件")
    source.add_argument("--records-dir", help="包含多个病历 JSON 的目录")
    parser.add_argument("--output", required=True, help="输出 PDF 文件或目录")
    parser.add_argument("--institution-name", default="医疗机构名称")
    parser.add_argument("--title", default="初诊病历记录")
    parser.add_argument("--department", default="")
    parser.add_argument("--doctor-name", default="")
    parser.add_argument("--visit-no", default="")
    parser.add_argument("--visit-time", default="")
    parser.add_argument("--font-path", default="")
    parser.add_argument("--bold-font-path", default="")
    args = parser.parse_args()

    options = {
        "institution_name": args.institution_name,
        "title": args.title,
        "font_path": args.font_path,
        "bold_font_path": args.bold_font_path,
        "doctor_name": args.doctor_name,
    }

    if args.record:
        with open(args.record, "r", encoding="utf-8") as file:
            record = json.load(file)
        output = Path(args.output)
        if output.suffix.lower() != ".pdf":
            record_id = (record.get("meta") or {}).get("record_id") or Path(args.record).stem
            output = output / f"{_sanitize_filename(record_id)}_initial_record.pdf"
        metadata = {
            "department": args.department,
            "doctor_name": args.doctor_name,
            "visit_no": args.visit_no,
            "visit_time": args.visit_time,
        }
        pdf_path = render_medical_record_pdf(record, str(output), metadata=metadata, options=options)
        print(f"✓ PDF 已生成: {pdf_path}")
        return

    records = load_records_from_dir(args.records_dir)
    result = export_medical_record_pdfs(records, args.output, options=options)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
