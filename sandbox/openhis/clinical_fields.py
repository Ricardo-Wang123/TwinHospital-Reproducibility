"""Convert explicit patient/prescription fields; reject ambiguous values."""

import math
import re
from datetime import date
from tools.pharmacy_inventory import validate_medication_dose


def patient_payload(info):
    identity = info.get("data", {}).get("identity", {})
    raw = identity.get("raw_data", {})
    derived = identity.get("derived_attributes", {})
    gender = str(raw.get("gender", derived.get("gender", "未知"))).strip().lower()
    genders = {"男": 0, "男性": 0, "m": 0, "male": 0, "0": 0,
               "女": 1, "女性": 1, "f": 1, "female": 1, "1": 1,
               "未知": 2, "unknown": 2, "2": 2}
    if gender not in genders:
        raise ValueError(f"无法识别患者性别: {gender}")
    age = raw.get("age", derived.get("age"))
    birthday = raw.get("birthDate") or raw.get("birth_date") or raw.get("dob")
    if birthday:
        birthday = date.fromisoformat(str(birthday)[:10])
        today = date.today()
        age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
    match = re.fullmatch(r"(\d{1,3})(?:岁)?", str(age).strip())
    if not match or not 0 <= int(match[1]) <= 130:
        raise ValueError(f"患者缺少有效年龄/出生日期: {info.get('id')}")
    result = {"name": raw.get("name") or info.get("name") or info["id"],
              "genderEnum": genders[gender], "age": int(match[1]),
              "phone": raw.get("phone") or raw.get("contact_number") or "",
              "address": raw.get("address") or derived.get("address") or ""}
    # Without a real identity number OpenHIS generates a test identity by age.
    identity_number = raw.get("idCard") or raw.get("id_number")
    if identity_number:
        if not re.fullmatch(r"\d{17}[\dXx]", str(identity_number)):
            raise ValueError("idCard 必须为18位证件号，不能使用沙盒患者ID")
        result["idCard"] = str(identity_number)
    if birthday:
        result["birthDate"] = birthday.isoformat()
    return result


def _amount(value):
    value = str(value).strip().replace("毫克", "mg").replace("微克", "μg").replace("毫升", "ml")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zA-Zμµ\u4e00-\u9fff]+)", value)
    if not match or float(match[1]) <= 0:
        raise ValueError(f"用量必须包含明确的正数和单位: {value}")
    return float(match[1]), match[2].lower().replace("µ", "μ").replace("克", "g")


def prescription_fields(medication, mapping, dictionaries):
    """Map dose/route/frequency and calculate an unambiguous dispense quantity."""
    validate_medication_dose(medication, mapping.get("sourceRecord") or {})
    def code(kind, label):
        label = str(label).strip().lower()
        matches = [str(row["dictValue"]) for row in dictionaries[kind]
                   if label in (str(row["dictLabel"]).strip().lower(), str(row["dictValue"]).lower())]
        if len(set(matches)) != 1:
            raise ValueError(f"OpenHIS {kind} 无法唯一匹配: {label}")
        return matches[0]

    dose, unit = _amount(medication.get("dose", ""))
    frequency = str(medication.get("frequency", "")).strip().replace("一日", "每日").replace("一天", "每日")
    for number, word in (("1", "一"), ("2", "两"), ("3", "三"), ("4", "四")):
        frequency = frequency.replace(f"每日{number}次", f"每日{word}次")
    frequency = frequency.replace("每日二次", "每日两次")
    route = str(medication.get("route", "")).strip()
    route = {"po": "口服", "静注": "静脉注射", "静滴": "静脉滴注", "吸入": "吸入给药"}.get(route.lower(), route)
    rate = code("rate_code", frequency)
    result = {"dose": dose, "doseUnitCode": code("unit_code", unit),
              "methodCode": code("method_code", route), "rateCode": rate}
    duration = str(medication.get("duration", "")).strip()
    days_match = re.fullmatch(r"(\d+)\s*(天|日|周)", duration)
    days = int(days_match[1]) * (7 if days_match[2] == "周" else 1) if days_match else None
    if days is not None and days <= 0:
        raise ValueError("疗程必须大于0天")
    params = mapping["prescriptionParams"]
    dispense_unit = str(params["unitCode"])
    quantity = medication.get("quantity")
    if quantity is not None:
        if isinstance(quantity, str) and not re.fullmatch(r"\d+(?:\.\d+)?", quantity):
            quantity, quantity_unit = _amount(quantity)
            if code("unit_code", quantity_unit) != dispense_unit:
                raise ValueError("处方数量单位与药房发药单位不一致")
        quantity = float(quantity)
    else:
        per_day = {"QD": 1, "BID": 2, "TID": 3, "QID": 4, "QN": 1,
                   "Q12H": 2, "Q8H": 3, "Q6H": 4}.get(rate)
        if not days or not per_day:
            raise ValueError("该频次/疗程无法计算总量，请明确填写 quantity")
        if result["doseUnitCode"] == dispense_unit:
            per_dose = dose
        else:
            source = mapping.get("sourceRecord", {})
            if source.get("ZXZJDW") not in ("片", "粒") or code("unit_code", source["ZXZJDW"]) != dispense_unit:
                raise ValueError("无法从药品规格确定发药数量，请明确填写 quantity")
            strengths = re.findall(r"(\d+(?:\.\d+)?)\s*(mg|μg|g)\b", source.get("ZCGG", ""), re.I)
            scale = {"g": 1000, "mg": 1, "μg": 0.001}
            if len(strengths) != 1 or unit not in scale or float(strengths[0][0]) <= 0:
                raise ValueError("药品规格不能唯一换算单次用量，请明确填写 quantity")
            per_dose = dose * scale[unit] / (float(strengths[0][0]) * scale[strengths[0][1].lower()])
        quantity = per_dose * per_day * days
        solid_units = {str(row["dictValue"]) for row in dictionaries["unit_code"] if row["dictLabel"] in ("片", "粒")}
        if dispense_unit in solid_units:
            quantity = math.ceil(quantity)
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("处方总量必须是有效正数")
    result.update(quantity=quantity, unitCode=dispense_unit,
                  totalPrice=round(quantity * float(params["unitPrice"]), 2),
                  dosageInstruction="; ".join(str(medication.get(k, "")) for k in ("dose", "route", "frequency", "duration")))
    if days:
        result["dispensePerDuration"] = days
    return result
