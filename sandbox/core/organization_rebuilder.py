"""
OpenHIS business department rebuild support for sandbox initialization.

This module operates on OpenHIS business organizations in adm_organization via:
  GET    /base-data-manage/organization/organization
  POST   /base-data-manage/organization/organization
  DELETE /base-data-manage/organization/organization?orgIds=...
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import yaml

from sandbox.paths import project_path


ORG_API = "/base-data-manage/organization/organization"
TYPE_HOSPITAL = 1
TYPE_DEPARTMENT = 2
DEFAULT_ORGANIZATION_CONFIG = "sandbox/config/organization.yaml"


class OrganizationRebuildError(RuntimeError):
    """Raised when OpenHIS organization rebuild fails."""


def _get_nested(config: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    value: Any = config
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return default
        value = value.get(part)
        if value is None:
            return default
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _resolve_from_project_root(config_loader: Any, path_value: str) -> Path:
    return Path(project_path(path_value))


def flatten_org_tree(records: Iterable[Dict[str, Any]], depth: int = 0) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["_depth"] = depth
        flattened.append(item)
        flattened.extend(flatten_org_tree(record.get("children") or [], depth + 1))
    return flattened


def load_organization_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"organization config not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    departments = raw.get("departments") or []
    if not isinstance(departments, list) or not departments:
        raise OrganizationRebuildError(f"departments must be a non-empty list in {path}")

    defaults = raw.get("department_defaults") or {}
    yb_no_prefix = defaults.get("yb_no_prefix", "SIM-DEPT")
    display_order_start = int(defaults.get("display_order_start", 10))
    display_order_step = int(defaults.get("display_order_step", 10))
    default_class_enum = int(defaults.get("class_enum", 1))

    normalized: List[Dict[str, Any]] = []
    seen_names = set()
    for index, item in enumerate(departments, 1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise OrganizationRebuildError(f"invalid department entry at index {index}: {item!r}")

        name = str(item.get("name") or "").strip()
        if not name:
            raise OrganizationRebuildError(f"department name is empty at index {index}")
        if name in seen_names:
            raise OrganizationRebuildError(f"duplicate department name in {path}: {name}")
        seen_names.add(name)

        class_enum = item.get("classEnum", item.get("class_enum", default_class_enum))
        yb_no = item.get("ybNo", item.get("yb_no")) or f"{yb_no_prefix}-{index:03d}"
        yb_name = item.get("ybName", item.get("yb_name")) or name
        display_order = item.get(
            "displayOrder",
            item.get("display_order", display_order_start + (index - 1) * display_order_step),
        )

        normalized.append(
            {
                "name": name,
                "typeEnum": TYPE_DEPARTMENT,
                "classEnum": int(class_enum),
                "ybNo": str(yb_no),
                "ybName": str(yb_name),
                "displayOrder": int(display_order),
            }
        )

    return {
        "hospital": raw.get("hospital") or {},
        "departments": normalized,
        "source_path": str(path),
    }


class OpenHISOrganizationClient:
    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        tenant_id: int = 1,
        timeout: int = 10,
        session: Optional[Any] = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.tenant_id = tenant_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.token: Optional[str] = None

    def login(self) -> None:
        response = self.session.post(
            f"{self.api_url}/login",
            json={"username": self.username, "password": self.password, "tenantId": self.tenant_id},
            timeout=self.timeout,
        )
        data = self._json(response, "POST /login")
        if getattr(response, "status_code", None) != 200:
            raise OrganizationRebuildError(f"login failed: HTTP {response.status_code}, body={data}")
        self.token = data.get("token")
        if not self.token:
            raise OrganizationRebuildError(f"login succeeded but token is missing: {data}")

    def headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            # OpenHIS accepts the raw token; this matches existing explore scripts.
            headers["Authorization"] = self.token
        return headers

    def get_organization_records(self, page_size: int = 1000) -> Tuple[List[Dict[str, Any]], int]:
        data = self._request("GET", ORG_API, params={"pageNo": 1, "pageSize": page_size})
        payload = data.get("data") or {}
        return payload.get("records") or [], int(payload.get("total") or 0)

    def add_department(self, hospital_bus_no: str, department: Dict[str, Any]) -> str:
        payload = dict(department)
        payload["busNo"] = hospital_bus_no
        data = self._request("POST", ORG_API, json=payload)
        org_id = data.get("data")
        if not org_id:
            raise OrganizationRebuildError(f"add department returned no id for {department['name']}: {data}")
        return str(org_id)

    def delete_departments(self, org_ids: List[str]) -> None:
        if not org_ids:
            return
        self._request("DELETE", ORG_API, params={"orgIds": ",".join(org_ids)})

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        request_method = getattr(self.session, method.lower())
        response = request_method(
            f"{self.api_url}{path}",
            headers=self.headers(),
            timeout=self.timeout,
            **kwargs,
        )
        data = self._json(response, f"{method} {path}")
        code = data.get("code")
        success = data.get("success")
        if getattr(response, "status_code", None) != 200 or (code not in (0, 200, None) and not success):
            raise OrganizationRebuildError(f"{method} {path} failed: HTTP {response.status_code}, body={data}")
        return data

    @staticmethod
    def _json(response: Any, label: str) -> Dict[str, Any]:
        try:
            return response.json()
        except Exception as exc:
            text = getattr(response, "text", "")
            raise OrganizationRebuildError(
                f"{label} returned non-JSON HTTP {getattr(response, 'status_code', '?')}: {text[:300]}"
            ) from exc


class OrganizationRebuilder:
    def __init__(
        self,
        client: OpenHISOrganizationClient,
        organization_config: Dict[str, Any],
        page_size: int = 1000,
        delete_existing: bool = True,
        dry_run: bool = False,
    ):
        self.client = client
        self.organization_config = organization_config
        self.page_size = page_size
        self.delete_existing = delete_existing
        self.dry_run = dry_run

    def rebuild(self, hospital_name: Optional[str] = None) -> Dict[str, Any]:
        configured_hospital_name = (self.organization_config.get("hospital") or {}).get("name")
        target_hospital_name = hospital_name or configured_hospital_name or None

        self.client.login()
        records, total = self.client.get_organization_records(page_size=self.page_size)
        hospital = self._find_hospital(records, target_hospital_name)
        existing_departments = self._find_child_departments(records, hospital["busNo"])
        departments_to_add = self.organization_config["departments"]

        delete_ids = [str(dept["id"]) for dept in existing_departments] if self.delete_existing else []
        added_ids: List[str] = []

        if self.dry_run:
            added_ids = [f"DRY-RUN-{index:03d}" for index, _ in enumerate(departments_to_add, 1)]
        else:
            if delete_ids:
                self.client.delete_departments(delete_ids)
            for department in departments_to_add:
                added_ids.append(self.client.add_department(hospital["busNo"], department))
            self._verify_added_departments(added_ids, [dept["name"] for dept in departments_to_add])

        return {
            "enabled": True,
            "dry_run": self.dry_run,
            "organization_total_before": total,
            "hospital_id": str(hospital.get("id")),
            "hospital_name": hospital.get("name"),
            "hospital_bus_no": hospital.get("busNo"),
            "deleted_count": len(delete_ids),
            "deleted_ids": delete_ids,
            "added_count": len(added_ids),
            "added_ids": added_ids,
            "department_names": [dept["name"] for dept in departments_to_add],
        }

    def _find_hospital(self, records: List[Dict[str, Any]], hospital_name: Optional[str]) -> Dict[str, Any]:
        hospitals = [org for org in flatten_org_tree(records) if org.get("typeEnum") == TYPE_HOSPITAL]
        if hospital_name:
            hospitals = [org for org in hospitals if org.get("name") == hospital_name]
        if not hospitals:
            raise OrganizationRebuildError(f"no hospital organization found, hospital_name={hospital_name!r}")
        return hospitals[0]

    def _find_child_departments(self, records: List[Dict[str, Any]], hospital_bus_no: str) -> List[Dict[str, Any]]:
        prefix = f"{hospital_bus_no}."
        departments = [
            org
            for org in flatten_org_tree(records)
            if org.get("typeEnum") == TYPE_DEPARTMENT and str(org.get("busNo") or "").startswith(prefix)
        ]
        return sorted(departments, key=lambda org: (org.get("_depth", 0), str(org.get("busNo") or "")), reverse=True)

    def _verify_added_departments(self, added_ids: List[str], expected_names: List[str]) -> None:
        records, _ = self.client.get_organization_records(page_size=self.page_size)
        flat = flatten_org_tree(records)
        found = {(str(org.get("id")), org.get("name")) for org in flat}
        missing = [
            {"id": org_id, "name": name}
            for org_id, name in zip(added_ids, expected_names)
            if (str(org_id), name) not in found
        ]
        if missing:
            raise OrganizationRebuildError(f"added departments not found after rebuild: {missing}")


def rebuild_departments_if_enabled(config_loader: Any, session: Optional[Any] = None) -> Dict[str, Any]:
    config = config_loader.to_dict()
    openhis_config = config.get("openhis_integration") or {}
    rebuild_config = openhis_config.get("department_rebuild") or {}

    if not _as_bool(openhis_config.get("enabled"), False):
        return {"enabled": False, "skipped_reason": "openhis_integration disabled"}
    if not _as_bool(rebuild_config.get("enabled"), False):
        return {"enabled": False, "skipped_reason": "department_rebuild disabled"}

    org_config_value = rebuild_config.get("organization_config", DEFAULT_ORGANIZATION_CONFIG)
    org_config_path = _resolve_from_project_root(config_loader, org_config_value)
    organization_config = load_organization_config(org_config_path)

    api_url = os.getenv("OPENHIS_API_URL", openhis_config.get("api_url", "http://localhost:18080/openhis"))
    username = os.getenv("OPENHIS_USERNAME", openhis_config.get("username", "admin"))
    password = os.getenv("OPENHIS_PASSWORD", openhis_config.get("password", "admin123"))
    tenant_id = int(os.getenv("OPENHIS_TENANT_ID", rebuild_config.get("tenant_id", openhis_config.get("tenant_id", 1))))
    timeout = int(rebuild_config.get("timeout", openhis_config.get("timeout", 10)))

    client = OpenHISOrganizationClient(
        api_url=api_url,
        username=username,
        password=password,
        tenant_id=tenant_id,
        timeout=timeout,
        session=session,
    )
    rebuilder = OrganizationRebuilder(
        client=client,
        organization_config=organization_config,
        page_size=int(rebuild_config.get("page_size", 1000)),
        delete_existing=_as_bool(rebuild_config.get("delete_existing"), True),
        dry_run=_as_bool(rebuild_config.get("dry_run"), _as_bool(openhis_config.get("dry_run"), False)),
    )
    return rebuilder.rebuild(hospital_name=rebuild_config.get("hospital_name"))
