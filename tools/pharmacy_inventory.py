import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def dose_requirement(source):
    """Describe the software dose convention without suggesting a patient dose."""
    specification = source.get("ZCGG") or source.get("SJGG") or ""
    unit = source.get("ZXZJDW") or ""
    strengths = re.findall(r"\d+(?:\.\d+)?\s*(?:mg|μg|µg|g)(?![a-zA-Z])", specification, re.I)
    multiple = len(strengths) > 1 or "复方" in str(source.get("ZCMC", ""))
    required = unit if multiple and unit in ("片", "粒") else None
    return {"requiredDoseUnit": required,
            "instruction": (f"此药含多个成分或规格含多个含量。dose 必须填写每次几{unit}，不能只填某一成分的 mg；数量由医生决定。"
                            if required else "dose 表示单次用量，必须包含明确数值和单位；片剂/胶囊优先按片/粒填写。"),
            "quantityInstruction": f"quantity 表示本次发药总量，使用发药单位 {unit or '待核实'}，不是单次用量或包装盒数。"}


def validate_medication_dose(medication, source):
    required = dose_requirement(source)["requiredDoseUnit"]
    if required:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*" + re.escape(required), str(medication.get("dose", "")).strip())
        if not match or float(match[1]) <= 0:
            raise ValueError(f"{medication.get('name', '复方药')}：单次 dose 必须为正数+{required}，不能仅写成分 mg；填写 quantity 也不能替代单次剂量")


class PharmacyInventory:
    """Read-only helper over the OpenHIS medication mapping."""

    DEFAULT_MAPPING_PATH = (
        Path(__file__).resolve().parents[1]
        / "sandbox"
        / "data"
        / "medications"
        / "openhis_prescription_medication_mapping.json"
    )
    DEFAULT_SLIM_PATH = (
        Path(__file__).resolve().parents[1]
        / "sandbox"
        / "data"
        / "medications"
        / "pharmacy_inventory_slim.json"
    )

    def __init__(
        self,
        mapping_path: Optional[str] = None,
        slim_path: Optional[str] = None,
    ) -> None:
        self.mapping_path = Path(mapping_path) if mapping_path else self.DEFAULT_MAPPING_PATH
        self.slim_path = Path(slim_path) if slim_path else self.DEFAULT_SLIM_PATH
        self._mapping: Optional[Dict[str, Any]] = None
        self._slim_items: Optional[List[Dict[str, Any]]] = None

    @staticmethod
    def normalize_name(value: Any) -> str:
        text = str(value or "")
        text = text.replace("（", "(").replace("）", ")")
        return (
            text.replace(" ", "")
            .replace("\u3000", "")
            .replace("-", "")
            .strip()
            .lower()
        )

    def load_mapping(self) -> Dict[str, Any]:
        if self._mapping is None:
            if not self.mapping_path.exists():
                self._mapping = {"medicationsByName": {}}
            else:
                self._mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        return self._mapping

    def build_slim_items(self) -> List[Dict[str, Any]]:
        mapping = self.load_mapping().get("medicationsByName") or {}
        items: List[Dict[str, Any]] = []
        for name, entry in mapping.items():
            if not entry.get("readyForPrescription"):
                continue
            source = entry.get("sourceRecord") or {}
            openhis = entry.get("openhis") or {}
            params = entry.get("prescriptionParams") or {}
            items.append(
                {
                    "name": name,
                    "genericName": source.get("ZCMC") or name,
                    "brandName": source.get("SPMC") or "",
                    "form": source.get("SJJX") or source.get("ZCJX") or "",
                    "specification": source.get("SJGG") or source.get("ZCGG") or "",
                    "manufacturer": source.get("YPQY") or "",
                    "packageUnit": source.get("ZXBZDW") or "",
                    "minDoseUnit": source.get("ZXZJDW") or "",
                    "dispenseUnit": source.get("ZXZJDW") or "",
                    "doseRequirement": dose_requirement(source),
                    "stock": openhis.get("inventoryQuantity"),
                    "stockUnitCode": openhis.get("inventoryUnitCode"),
                    "locationName": openhis.get("inventoryLocationName"),
                    "openhisAdviceDefinitionId": params.get("adviceDefinitionId"),
                    "openhisMedicationId": params.get("medicationId"),
                }
            )
        return items

    def load_slim_items(self) -> List[Dict[str, Any]]:
        if self._slim_items is None:
            self._slim_items = self.build_slim_items()
        return self._slim_items

    def write_slim_file(self) -> Path:
        items = self.build_slim_items()
        payload = {
            "schema": "openhis-pharmacy-inventory-slim-v2",
            "sourceMapping": str(self.mapping_path),
            "recordCount": len(items),
            "items": items,
        }
        self.slim_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.slim_path.with_suffix(self.slim_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.slim_path)
        self._slim_items = items
        return self.slim_path

    def score_item(self, item_name: str, search_key: str) -> int:
        name = self.normalize_name(item_name)
        key = self.normalize_name(search_key)
        if not key:
            return 0
        if name == key:
            return 1000
        score = 0
        if name.startswith(key):
            score = 900 - min(len(name) - len(key), 80)
        elif key in name:
            score = 700 - min(name.index(key), 80) - min(len(name) - len(key), 80)
        else:
            return 0

        # Prefer simple systemic dosage forms for ambiguous names such as "布洛芬".
        penalties = ("复方", "右", "小儿", "乳膏", "凝胶", "栓", "滴眼", "滴耳", "喷雾")
        if any(token in item_name for token in penalties):
            score -= 120
        bonuses = ("片", "胶囊", "颗粒", "口服液")
        if any(token in item_name for token in bonuses):
            score += 40
        return score

    def search(self, search_key: str, limit: int = 8) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 8), 30))
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for item in self.load_slim_items():
            score = self.score_item(item.get("name", ""), search_key)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], len(pair[1].get("name", ""))))
        return [item for _, item in scored[:limit]]

    def get(self, medication_name: str) -> Optional[Dict[str, Any]]:
        target = self.normalize_name(medication_name)
        matches = [item for item in self.load_slim_items() if self.normalize_name(item.get("name")) == target]
        return matches[0] if len(matches) == 1 else None

    def resolve_mapping_entry(self, medication_name: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        mapping = self.load_mapping().get("medicationsByName") or {}
        target = self.normalize_name(medication_name)
        matches = [(name, entry) for name, entry in mapping.items()
                   if self.normalize_name(name) == target and entry.get("readyForPrescription")]
        return matches[0] if len(matches) == 1 else (None, None)

    def validate_prescription(self, reference):
        if not isinstance(reference, dict) or reference.get("decision") not in ("medication", "no prescription"):
            raise ValueError("必须明确填写处方决策 medication 或 no prescription")
        medications = reference.get("medication")
        if not isinstance(medications, list):
            raise ValueError("prescription_reference.medication 必须是列表")
        if reference["decision"] == "no prescription":
            if medications:
                raise ValueError("不开药时 medication 必须为空列表")
            return
        if not medications:
            raise ValueError("选择开药但没有结构化药品，不能自动补药")
        for medication in medications:
            if not isinstance(medication, dict) or any(not medication.get(key) for key in ("name", "dose", "route", "frequency", "duration")):
                raise ValueError("每项药品必须包含 name、dose、route、frequency、duration")
            _, entry = self.resolve_mapping_entry(medication["name"])
            if entry is None:
                raise ValueError(f"药房未找到精确药品名：{medication['name']}；请重新查询，不得替换型号或使用模糊名称")
            validate_medication_dose(medication, entry.get("sourceRecord") or {})


class PharmacyInventoryTool:
    """FunctionTool facade for doctors."""

    def __init__(self, inventory: Optional[PharmacyInventory] = None) -> None:
        self.inventory = inventory or PharmacyInventory()

    def search_pharmacy_medications(self, search_key: str, limit: int = 8) -> str:
        """
        按药品名称文本搜索本院药房，不按病种、症状或药理类别推荐药物。
        搜索“降压药”等类别没有结果，不代表没有该类药品；应先由医生确定具体药品名。

        Args:
            search_key: 药品关键词，例如 "布洛芬"、"头孢曲松"。
            limit: 最多返回多少条，建议 5-8 条。

        Returns:
            JSON 字符串。医生开方时必须复制返回结果里的精确 name 字段。
        """
        items = self.inventory.search(search_key, limit)
        return json.dumps(
            {
                "search_key": search_key,
                "count": len(items),
                "items": items,
                "usage": "这里只按名称匹配，不提供病种/药理分类推荐；名称命中不代表适合患者。先比较完整 specification，再用 get_pharmacy_medication 复核所选精确名称并遵守 doseRequirement。规格含量不是推荐用量。",
            },
            ensure_ascii=False,
            indent=2,
        )

    def get_pharmacy_medication(self, medication_name: str) -> str:
        """
        按精确药品名查看本院药房药品信息。

        Args:
            medication_name: 药房药品精确名称；如果不确定，先调用 search_pharmacy_medications。

        Returns:
            JSON 字符串，包含药品剂型、规格、库存、OpenHIS 标识等精简信息。
        """
        item = self.inventory.get(medication_name)
        return json.dumps(
            {
                "found": item is not None,
                "item": item,
                "usage": "found=true 时复制 item.name 并遵守 doseRequirement；dose 是单次用量，quantity 是发药总量。found=false 时重新搜索，不可自行替换药品型号。",
            },
            ensure_ascii=False,
            indent=2,
        )
