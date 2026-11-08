"""
OpenHIS 适配器
将Sandbox系统集成到OpenHIS系统的核心适配层
"""

import json
import os
import csv
import io
import re
import subprocess
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
import logging
import hashlib

from .client import OpenHISAPIClient
from .id_mapper import OpenHISIDMapper
from .clinical_fields import patient_payload, prescription_fields

try:
    from tools.pharmacy_inventory import PharmacyInventory
except Exception:
    PharmacyInventory = None

logger = logging.getLogger(__name__)


class OpenHISAdapter:
    """
    OpenHIS集成适配器

    负责：
    1. 将Sandbox数据同步到OpenHIS系统
    2. 管理挂号、就诊、EMR等全流程
    3. 维护本地ID和OpenHIS ID的映射
    4. 管理错误和降级策略

    用法:
        adapter = OpenHISAdapter(config)
        adapter.init_data_from_local_profiles(profiles, state)
        adapter.create_preregistration(patient, state)
        adapter.finalize_registration(patient, registration_id, state)
        adapter.create_emr_from_consultation(consultation_result, state)
        adapter.create_diagnosis_from_consultation(diagnosis_result, state)
    """
    DEPARTMENT_ALIASES = {
        "心内科": "心血管内科",
        "心内": "心血管内科",
        "外科": "普通外科",
        "普外科": "普通外科",
        "普外": "普通外科",
        "内科": "全科医学科",
        "肺科": "呼吸内科",
        "呼吸科": "呼吸内科",
        "感染科": "感染性疾病科",
        "急诊": "急诊医学科",
        "儿科": "儿科其他",
    }

    def __init__(self, config: Dict[str, Any], record_system=None):
        """
        初始化OpenHIS适配器

        Args:
            config: 配置字典，包含：
                - api_url: OpenHIS API基础URL
                - username: 登录用户名
                - password: 登录密码
                - dry_run: 是否干运行
                - persist_dir: ID映射持久化目录
                - fail_on_error: 是否在错误时失败
                - fallback_to_local: 失败时是否回退到本地
            record_system: MedicalRecordSystem 实例（用于EMR创建时从中获取最新病历数据）
        """
        self.config = config
        self.dry_run = config.get('dry_run', False)
        self.fail_on_error = config.get('fail_on_error', False)
        self.fallback_to_local = config.get('fallback_to_local', True)

        # 【关键】注入 record_system，用于EMR创建时获取最新的病历数据
        self.record_system = record_system
        if record_system:
            logger.info(f"✓ OpenHIS适配器已绑定 record_system")
        else:
            logger.warning(f"⚠ OpenHIS适配器未绑定 record_system")

        # 初始化API客户端
        api_url = config.get('api_url', 'http://localhost:18080/openhis')
        username = config.get('username')
        password = config.get('password')

        self.client = OpenHISAPIClient(
            api_base_url=api_url,
            username=username,
            password=password,
            dry_run=self.dry_run,
            timeout=int(config.get("timeout", 15)),
            tenant_id=int(config.get("tenant_id", 1)),
        )

        # 初始化ID映射器
        persist_dir = config.get('persist_dir')
        self.id_mapper = OpenHISIDMapper(persist_dir=persist_dir)
        self._department_records: Optional[List[Dict[str, Any]]] = None
        self._preregistration_requests: Dict[str, Dict[str, Any]] = {}
        self.pharmacy_inventory = PharmacyInventory() if PharmacyInventory else None

        # 统计信息
        self.stats = {
            'patients_initialized': 0,
            'preregistrations_created': 0,
            'registrations_finalized': 0,
            'emrs_created': 0,
            'diagnoses_created': 0,
            'prescriptions_created': 0,
            'errors': 0
        }

        logger.info(
            f"✓ OpenHIS适配器已初始化 (dry_run={self.dry_run})"
        )

    # =====================================================
    # OpenHIS基础数据解析
    # =====================================================

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return str(value or "").replace(" ", "").replace("\u3000", "").strip()

    @staticmethod
    def _flatten_org_tree(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []
        for record in records or []:
            flattened.append(record)
            flattened.extend(OpenHISAdapter._flatten_org_tree(record.get("children") or []))
        return flattened

    def _get_department_records(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self.dry_run:
            return []
        if self._department_records is not None and not refresh:
            return self._department_records

        records: List[Dict[str, Any]] = []
        try:
            response = self.client.get("/charge-manage/register/org-list")
            if response.status_code == 200:
                data = response.json()
                if data.get("code") in (0, 200) and isinstance(data.get("data"), list):
                    records = [
                        {"id": str(item.get("id")), "name": item.get("name")}
                        for item in data.get("data") or []
                        if item.get("id") and item.get("name")
                    ]
        except Exception as exc:
            logger.warning(f"⚠ 获取挂号科室列表失败，将尝试组织树: {exc}")

        if not records:
            try:
                response = self.client.get(
                    "/base-data-manage/organization/organization",
                    {"pageNo": 1, "pageSize": 1000},
                )
                if response.status_code == 200:
                    data = response.json()
                    org_records = (data.get("data") or {}).get("records") or []
                    records = [
                        {"id": str(item.get("id")), "name": item.get("name")}
                        for item in self._flatten_org_tree(org_records)
                        if item.get("typeEnum") == 2 and item.get("id") and item.get("name")
                    ]
            except Exception as exc:
                logger.warning(f"⚠ 获取OpenHIS组织树失败: {exc}")

        self._department_records = records
        return records

    def resolve_department(self, department_name: Any) -> Optional[Dict[str, str]]:
        """Resolve a local/sandbox department name to a real OpenHIS clinic department."""
        raw_name = str(department_name or "").strip()
        if not raw_name:
            return None

        records = self._get_department_records()
        by_name = {self._normalize_name(item.get("name")): item for item in records}
        candidates = [raw_name]
        alias = self.DEPARTMENT_ALIASES.get(raw_name) or self.DEPARTMENT_ALIASES.get(self._normalize_name(raw_name))
        if alias:
            candidates.append(alias)

        for candidate in candidates:
            matched = by_name.get(self._normalize_name(candidate))
            if matched:
                return {"id": str(matched["id"]), "name": str(matched["name"])}

        normalized = self._normalize_name(raw_name)
        for item in records:
            item_name = self._normalize_name(item.get("name"))
            if normalized and (normalized in item_name or item_name in normalized):
                return {"id": str(item["id"]), "name": str(item["name"])}

        logger.warning(f"⚠ 未找到OpenHIS真实科室: {raw_name}")
        return None

    def _resolve_openhis_doctor_id(self, local_doctor_id: Optional[str]) -> Optional[str]:
        if not local_doctor_id:
            return None

        mapped = self.id_mapper.get_openhis_id("doctor", local_doctor_id)
        if mapped and str(mapped).isdigit():
            return str(mapped)

        username = f"doctor_{local_doctor_id}"
        try:
            response = self.client.get(
                "/base-data-manage/practitioner/user-practitioner-page",
                {"pageNo": 1, "pageSize": 100, "searchKey": username},
            )
            if response.status_code == 200:
                records = (response.json().get("data") or {}).get("records") or []
                for record in records:
                    if record.get("userName") == username:
                        practitioner_id = record.get("practitionerId")
                        if practitioner_id:
                            self.id_mapper.set_mapping("doctor", local_doctor_id, str(practitioner_id))
                            self.id_mapper.persist_to_disk()
                            return str(practitioner_id)
        except Exception as exc:
            logger.warning(f"⚠ 查询医生OpenHIS ID失败 ({local_doctor_id}): {exc}")

        if mapped:
            logger.warning(f"⚠ 医生 {local_doctor_id} 的映射不是数字practitionerId: {mapped}")
        return None

    def resolve_registration_context(self, triage_result: Dict[str, Any]) -> Dict[str, Any]:
        """Build OpenHIS registration context from sandbox triage fields."""
        context: Dict[str, Any] = {}
        department = self.resolve_department(
            triage_result.get("target_department") or triage_result.get("department")
        )
        if department:
            context.update(
                {
                    "openhis_department_id": department["id"],
                    "openhis_department_name": department["name"],
                    "organization_id": department["id"],
                    "dept_id": department["id"],
                }
            )

        doctor_id = self._resolve_openhis_doctor_id(triage_result.get("assigned_doctor"))
        if doctor_id:
            context.update(
                {
                    "openhis_practitioner_id": doctor_id,
                    "practitioner_id": doctor_id,
                }
            )

        if triage_result.get("assigned_nurse"):
            context["triage_nurse_id"] = triage_result.get("assigned_nurse")
            context["triage_nurse_department"] = (
                context.get("openhis_department_name") or triage_result.get("target_department")
            )
            if context.get("openhis_department_id"):
                context["triage_nurse_department_id"] = context["openhis_department_id"]

        return context

    # =====================================================
    # Phase 0: 加载和重置 (load_profiles_from_directory, reset_openhis_data)
    # =====================================================

    def load_profiles_from_directory(
        self,
        doctor_profiles_dir: str,
        patient_profiles_dir: str,
        doctor_paths: Optional[List[str]] = None,
        patient_paths: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        从本地目录加载所有医生和患者档案

        Args:
            doctor_profiles_dir: 医生档案目录路径（如 MedicalAgent/data/profiles/doctor_profile）
            patient_profiles_dir: 患者档案目录路径（如 MedicalAgent/data/profiles）

        Returns:
            档案列表，每个档案包含doctor和patient信息
        """
        logger.info(f"→ 开始从目录加载档案...")
        logger.info(f"  医生档案目录: {doctor_profiles_dir}")
        logger.info(f"  患者档案目录: {patient_profiles_dir}")

        profiles = []
        errors = []

        try:
            # 1. 加载医生档案
            doctor_profiles = {}
            if os.path.isdir(doctor_profiles_dir):
                for filename in (os.path.basename(p) for p in doctor_paths) if doctor_paths is not None else os.listdir(doctor_profiles_dir):
                    if filename.endswith('.json'):
                        try:
                            file_path = os.path.join(doctor_profiles_dir, filename)
                            with open(file_path, 'r', encoding='utf-8') as f:
                                doctor_data = json.load(f)
                                # 检查是否为字典格式（跳过列表或其他格式的档案数据）
                                if not isinstance(doctor_data, dict):
                                    logger.debug(f"  ⊘ 跳过非字典格式: {filename} (type: {type(doctor_data).__name__})")
                                    continue
                                doctor_id = doctor_data.get('identity', {}).get('doctor_id', filename[:-5])
                                doctor_profiles[doctor_id] = {
                                    'id': doctor_id,
                                    'name': doctor_data.get('identity', {}).get('name', ''),
                                    'department': doctor_data.get('identity', {}).get('department', ''),
                                    'role': doctor_data.get('identity', {}).get('role', ''),
                                    'data': doctor_data
                                }
                                logger.info(f"  ✓ 加载医生: {doctor_profiles[doctor_id]['name']} ({doctor_id})")
                        except Exception as e:
                            error_msg = f"加载医生档案失败: {filename} - {e}"
                            logger.error(f"  ✗ {error_msg}")
                            errors.append(error_msg)
            else:
                logger.warning(f"  ⚠ 医生档案目录不存在: {doctor_profiles_dir}")

            # 2. 加载患者档案
            patient_profiles_list = []
            selected_patient_paths = {os.path.normcase(os.path.abspath(p)) for p in (patient_paths or [])}

            def _load_patient_files(directory, is_subdirectory=False):
                """从指定目录加载患者JSON文件"""
                count = 0
                if not os.path.isdir(directory):
                    return count

                for filename in os.listdir(directory):
                    if patient_paths is not None and os.path.normcase(os.path.abspath(os.path.join(directory, filename))) not in selected_patient_paths:
                        continue
                    # 跳过子目录名称（但不跳过patient_records1目录本身的扫描）
                    if filename in ('doctor_profile', 'nurse_profile'):
                        continue

                    # 跳过doctorflan相关的ML训练数据
                    if filename.startswith('doctorflan'):
                        continue

                    if filename.endswith('.json'):
                        try:
                            file_path = os.path.join(directory, filename)
                            with open(file_path, 'r', encoding='utf-8') as f:
                                patient_data = json.load(f)

                                # 验证是否为字典格式（医生档案或患者档案）
                                if not isinstance(patient_data, dict):
                                    logger.debug(f"  ⊘ 跳过非字典格式: {filename}")
                                    continue

                                patient_id = (patient_data.get('meta', {}).get('patient_id') or patient_data.get('agent_meta', {}).get('uuid') or patient_data.get('identity', {}).get('raw_data', {}).get('patient_id') or filename[:-5])

                                # 提取患者名字，处理null值
                                patient_name = patient_data.get('identity', {}).get('raw_data', {}).get('name')
                                if not patient_name:  # 处理None或空字符串
                                    patient_name = f'患者_{len(patient_profiles_list)+1}'

                                patient_profiles_list.append({
                                    'id': patient_id,
                                    'name': patient_name,
                                    'gender': patient_data.get('identity', {}).get('derived_attributes', {}).get('gender', ''),
                                    'age_group': patient_data.get('identity', {}).get('derived_attributes', {}).get('age_group', ''),
                                    'data': patient_data
                                })
                                location = "patient_records1" if is_subdirectory else "患者档案目录"
                                logger.info(f"  ✓ 加载患者: {patient_profiles_list[-1]['name']} ({patient_id}) 来自 {location}")
                                count += 1
                        except Exception as e:
                            error_msg = f"加载患者档案失败: {filename} - {e}"
                            logger.error(f"  ✗ {error_msg}")
                            errors.append(error_msg)

                return count

            # 先加载直接目录中的患者档案
            if os.path.isdir(patient_profiles_dir):
                direct_count = _load_patient_files(patient_profiles_dir, is_subdirectory=False)
                logger.info(f"  从主目录加载: {direct_count} 个患者档案")

                # 再加载patient_records1子目录中的患者档案
                patient_records1_dir = os.path.join(patient_profiles_dir, 'patient_records1')
                if os.path.isdir(patient_records1_dir):
                    subdir_count = _load_patient_files(patient_records1_dir, is_subdirectory=True)
                    logger.info(f"  从patient_records1子目录加载: {subdir_count} 个患者档案")
                else:
                    logger.debug(f"  ⊘ patient_records1子目录不存在: {patient_records1_dir}")
            else:
                logger.warning(f"  ⚠ 患者档案目录不存在: {patient_profiles_dir}")

            # 3. 合并档案: 每个患者配对一个医生
            available_doctors = list(doctor_profiles.values())
            for i, patient in enumerate(patient_profiles_list):
                # 轮转分配医生
                doctor = available_doctors[i % len(available_doctors)] if available_doctors else {}
                profiles.append({
                    'doctor': doctor,
                    'patient': patient
                })

            logger.info(f"✓ 档案加载完成: {len(available_doctors)} 医生, {len(patient_profiles_list)} 患者, {len(profiles)} 档案对")

            if errors:
                logger.warning(f"⚠ 加载过程中出现 {len(errors)} 个错误")

            return profiles

        except Exception as e:
            error_msg = f"加载档案目录失败: {e}"
            logger.error(f"✗ {error_msg}")
            return []

    def reset_openhis_data(self) -> Tuple[bool, List[str]]:
        """
        重置OpenHIS系统：清理所有患者和医生数据

        这个方法确保沙盒环境的隔离性，每次初始化时都从干净状态开始。

        Returns:
            (成功/失败, 错误列表)
        """
        logger.info("→ Phase 0: 开始清理OpenHIS旧数据...")

        errors = []

        try:
            # 检查config是否启用重置
            if not self.config.get('phases', {}).get('reset', False):
                logger.warning("⚠ reset 已禁用，跳过清理")
                return True, errors

            # 从ID映射中获取所有已知的患者和医生ID
            existing_patients = self.id_mapper.mappings.get('patient', {})
            existing_doctors = self.id_mapper.mappings.get('doctor', {})

            if not existing_patients and not existing_doctors:
                logger.info("✓ 没有需要清理的旧数据")
                return True, errors

            # 清理患者
            if existing_patients:
                patient_ids = list(existing_patients.values())
                logger.info(f"  正在删除 {len(patient_ids)} 个患者...")
                success_count, fail_count = self.client.batch_delete_patients(patient_ids)

                if fail_count > 0:
                    error_msg = f"删除患者时有 {fail_count} 个失败"
                    logger.warning(f"  ⚠ {error_msg}")
                    errors.append(error_msg)

            # 清理医生
            if existing_doctors:
                doctor_ids = list(existing_doctors.values())
                logger.info(f"  正在删除 {len(doctor_ids)} 个医生...")
                success_count, fail_count = self.client.batch_delete_doctors(doctor_ids)

                if fail_count > 0:
                    error_msg = f"删除医生时有 {fail_count} 个失败"
                    logger.warning(f"  ⚠ {error_msg}")
                    errors.append(error_msg)

            # 清理本地映射
            self.id_mapper.mappings.clear()
            self.id_mapper.reverse_mappings.clear()
            self.id_mapper.persist_to_disk()

            logger.info(f"✓ Phase 0 完成: 清理完毕，映射已重置")

            return len(errors) == 0, errors

        except Exception as e:
            error_msg = f"Phase 0 清理失败: {e}"
            logger.error(f"✗ {error_msg}")
            errors.append(error_msg)
            return False, errors

    def init_data_from_local_profiles(self, profiles):
        """Initialize real entities; missing IDs are errors, never fake mappings."""
        errors, patient_mappings = [], {}
        if self.config.get("phases", {}).get("reset", False):
            success, errors = self.reset_openhis_data()
            if not success:
                return False, errors, patient_mappings
        if not self.config.get("phases", {}).get("init", True):
            return True, errors, patient_mappings
        initialized_doctors = {}
        for profile in profiles:
            doctor, patient = profile.get("doctor", {}), profile.get("patient", {})
            try:
                if doctor.get("id"):
                    doctor_id = initialized_doctors.get(doctor["id"])
                    if not doctor_id:
                        doctor_id = self._init_doctor(doctor)
                        if not doctor_id:
                            raise RuntimeError("医生初始化未返回真实ID")
                        initialized_doctors[doctor["id"]] = doctor_id
                    self.id_mapper.set_mapping("doctor", doctor["id"], doctor_id)
                if patient.get("id"):
                    patient_id = self._init_patient(patient)
                    if not patient_id:
                        raise RuntimeError("患者初始化未返回真实ID")
                    self.id_mapper.set_mapping("patient", patient["id"], patient_id)
                    patient_mappings[patient["id"]] = patient_id
                    self.stats["patients_initialized"] += 1
            except Exception as exc:
                errors.append(f"初始化 {patient.get('id') or doctor.get('id')} 失败: {exc}")
                self.stats["errors"] += 1
                if self.fail_on_error or self.config.get("fail_on_init_error", True):
                    break
        self.id_mapper.persist_to_disk()
        return not errors, errors, patient_mappings

    def _init_doctor(self, doctor_info):
        """Create/reuse a registration practitioner; business calls still use admin."""
        if self.dry_run:
            return f"DOCTOR_{doctor_info['id'][:8]}"
        username = f"doctor_{doctor_info['id']}"
        if len(username) > 30:
            username = "doctor_" + hashlib.sha256(str(doctor_info["id"]).encode()).hexdigest()[:23]
        department = self.resolve_department(doctor_info.get("department"))
        org_id = doctor_info.get("organization_id") or (department or {}).get("id")
        if not org_id:
            raise ValueError(f"无法匹配医生科室: {doctor_info.get('department')}")

        def lookup():
            body = self._response_json(self.client.get(
                "/base-data-manage/practitioner/user-practitioner-page",
                {"pageNo": 1, "pageSize": 100, "searchKey": username}))
            return next((r for r in (body.get("data") or {}).get("records", [])
                         if r.get("userName") == username), None)

        record = lookup()
        if record is None:
            payload = {"userName": username, "nickName": doctor_info.get("name") or username,
                       "password": "123456", "status": "0", "sex": "0",
                       "practitionerRolesDtoList": [{"roleId": 200, "roleCode": "doctor", "roleName": "医生"}],
                       "responsibilityOrgDtoList": [{"orgId": org_id, "roleCode": ""}],
                       "doctorVisitOrgDtoList": [{"orgId": org_id, "roleCode": "doctor"}],
                       "manageLocationDtoList": [], "manageMedicationLocationDtoList": [],
                       "manageOrgDtoList": [], "manageWardLocationDtoList": []}
            self._response_json(self.client.post("/base-data-manage/practitioner/user-practitioner", payload))
            record = lookup()
        practitioner_id = (record or {}).get("practitionerId")
        if not practitioner_id or not str(practitioner_id).isdigit():
            raise RuntimeError(f"医生 {username} 未查到真实 practitionerId")
        self._sync_doctor_department(record, org_id)
        return str(practitioner_id)

    def _sync_doctor_department(self, doctor_record: Dict[str, Any], organization_id: Any) -> None:
        """Ensure an existing OpenHIS doctor is available under the sandbox department."""
        if self.dry_run or not doctor_record or not organization_id:
            return

        current_org = str(doctor_record.get("orgId") or "")
        visit_orgs = doctor_record.get("doctorVisitOrgDtoList") or []
        has_visit_org = any(str(item.get("orgId")) == str(organization_id) for item in visit_orgs)
        if current_org == str(organization_id) and has_visit_org:
            return

        payload = dict(doctor_record)
        payload["responsibilityOrgDtoList"] = [{"orgId": organization_id, "roleCode": ""}]
        payload["doctorVisitOrgDtoList"] = [{"orgId": organization_id, "roleCode": "doctor"}]
        for key in (
            "practitionerRolesDtoList",
            "manageLocationDtoList",
            "manageMedicationLocationDtoList",
            "manageOrgDtoList",
            "manageWardLocationDtoList",
        ):
            if payload.get(key) is None:
                payload[key] = []
        if not payload.get("practitionerRolesDtoList"):
            payload["practitionerRolesDtoList"] = [
                {"roleId": 200, "roleCode": "doctor", "roleName": "医生"}
            ]
        payload.setdefault("status", "0")
        payload.setdefault("sex", "0")

        try:
            response = self.client.put(
                "/base-data-manage/practitioner/user-practitioner",
                payload,
            )
            self._response_json(response)
            logger.info(f"已同步医生科室: {doctor_record.get('nickName')} -> {organization_id}")
        except Exception as exc:
            raise RuntimeError(f"同步医生科室失败: {exc}") from exc

    def _init_patient(self, patient_info):
        if self.dry_run:
            return f"PAT_{patient_info['id'][:8]}"
        body = self._response_json(self.client.post(
            "/patient-manage/information/patient-information", patient_payload(patient_info)))
        data = body.get("data") or {}
        patient_id = data.get("patientId") or data.get("id")
        if not patient_id or not str(patient_id).isdigit():
            raise RuntimeError("患者创建未返回真实 patientId")
        return str(patient_id)

    # =====================================================
    # Phase 2: 挂号阶段 (create_preregistration, finalize_registration)
    # =====================================================

    def create_preregistration(
        self,
        local_patient_id: str,
        triage_result: Dict[str, Any],
        state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        创建预挂号（两阶段挂号流程的第一步）

        Phase 2.1: 调用 /charge-manage/reg-pre-pay 预览费用

        请求格式:
        {
            "patientId": "患者编码",
            "visitTypeId": "就诊类型",
            "relationshipId": "主诉关系编码",
            "diseaseId": "疾病编码",
            "doctorId": "医生编码",
            "officeDeptId": "科室编码",
            "cardNo": "卡号",
            "chargeTypeId": "费用类型编码"
        }

        Args:
            local_patient_id: 本地患者ID
            triage_result: 分诊结果
            state: 状态管理器状态

        Returns:
            (成功/失败, 费用预览ID, 响应数据)
        """
        logger.info(
            f"→ Phase 2.1: 开始创建预挂号 "
            f"(患者={local_patient_id})..."
        )

        # 检查config是否启用
        if not self.config.get('phases', {}).get('triage_to_registration', True):
            logger.warning("⚠ triage_to_registration 已禁用")
            return True, None, {}

        try:
            triage_result.update(self.resolve_registration_context(triage_result))

            # 获取OpenHIS患者ID
            openhis_patient_id = self.id_mapper.get_openhis_id(
                'patient',
                local_patient_id
            )

            if not openhis_patient_id:
                error = f"找不到患者的OpenHIS ID映射: {local_patient_id}"
                logger.error(f"✗ {error}")
                return False, None, {'error': error}

            # 构建请求体
            # _build_preregistration_request() 返回 OutpatientRegistrationAddParam 对象
            # 直接用于 POST /payment/payment/reg-pre-pay，无需额外包装
            request_body = self._build_preregistration_request(
                openhis_patient_id,
                triage_result
            )

            logger.info(f"预挂号请求 (完整): {json.dumps(request_body, ensure_ascii=False)}")
            print(f"[DEBUG] 预挂号请求 (完整): {json.dumps(request_body, ensure_ascii=False)}")

            # 调用API（使用正确的端点 /payment/payment/reg-pre-pay）
            response = self.client.post('/payment/payment/reg-pre-pay', request_body)

            # 【修改】打印原始API返回
            print(f"\n{'='*100}")
            print(f"[API_RAW_RESPONSE] 预挂号接口原始返回:")
            print(f"{'='*100}")
            print(f"状态码: {response.status_code}")
            print(f"响应头: {dict(response.headers)}")
            print(f"响应体 (原始):")
            try:
                raw_response = response.json()
                print(json.dumps(raw_response, indent=2, ensure_ascii=False))
                print(f"{'='*100}\n")
            except:
                print(response.text)
                print(f"{'='*100}\n")

            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("code") not in (0, 200):
                    error = f"API业务错误: {response_json}"
                    logger.error(f"✗ {error}")
                    self.stats['errors'] += 1
                    return False, None, response_json

                data = response_json.get('data', {})
                if data is None:
                    data = {}

                # 关键字段: chrgBchno（收费批次号）用于后续的支付/挂号步骤
                # 注意: encounterId 可能不在预注册响应中，可能需要通过其他API获取
                bill_id = data.get('chrgBchno') or data.get('busNo') or data.get('id')
                encounter_id = data.get('encounterId')
                encounter_no = data.get('encounterNo')
                bus_no = data.get('busNo')

                # 存储映射
                if bill_id:
                    self._preregistration_requests[local_patient_id] = request_body
                    self.id_mapper.set_mapping(
                        'preregistration',
                        f"{local_patient_id}_{triage_result.get('chief_complaint', '')}",
                        bill_id
                    )

                    # 如果有就诊ID，也存储映射
                    if encounter_id:
                        self.id_mapper.set_mapping('encounter', local_patient_id, encounter_id)

                    self.stats['preregistrations_created'] += 1

                    logger.info(
                        f"✓ 预挂号已创建: "
                        f"bill_id={bill_id}, "
                        f"bus_no={bus_no}, "
                        f"encounter_no={encounter_no}, "
                        f"encounter_id={encounter_id if encounter_id else '(N/A)'}"
                    )
                    return True, bill_id, data
                else:
                    logger.warning(f"⚠ API返回成功但未找到预挂号ID。响应: {response_json}")
                    self.stats['errors'] += 1

                    if self.fail_on_error:
                        return False, None, {'error': 'API未返回预挂号ID'}
                    else:
                        return True, None, data
            else:
                error = f"API返回错误 {response.status_code}: {response.text}"
                logger.error(f"✗ {error}")
                self.stats['errors'] += 1
                return False, None, {'error': error}

        except Exception as e:
            error = f"创建预挂号异常: {e}"
            logger.error(f"✗ {error}")
            self.stats['errors'] += 1

            if self.fail_on_error:
                return False, None, {'error': error}
            else:
                return False, None, {'error': error}

    def finalize_registration(
        self,
        preview_id: str,
        local_patient_id: str,
        state: Dict[str, Any],
        triage_result: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        完成挂号（两阶段挂号流程的第二步）

        Phase 2.2: 调用 /payment/payment/reg-pay 确认费用并完成挂号

        请求格式（正确的OpenHIS格式）:
        {
            "outpatientRegistrationAddParam": {
                "encounterFormData": {...},
                "accountFormData": {...},
                "encounterParticipantFormData": {...},
                "encounterLocationFormData": {...},
                "chargeItemFormData": {...},
                "YbPsnSetlWay": "01",
                "ybMdtrtCertType": "02",
                "busiCardInfo": null,
                "feedetail": []
            }
        }

        Args:
            preview_id: 预挂号预览ID (chrgBchno)
            local_patient_id: 本地患者ID
            state: 状态管理器状态

        Returns:
            (成功/失败, 挂号编码, 响应数据)
        """
        logger.info(f"→ Phase 2.2: 开始完成挂号 (preview_id={preview_id})...")

        try:
            # 获取OpenHIS患者ID
            openhis_patient_id = self.id_mapper.get_openhis_id(
                'patient',
                local_patient_id
            )

            if not openhis_patient_id:
                error = f"找不到患者的OpenHIS ID映射: {local_patient_id}"
                logger.error(f"✗ {error}")
                return False, None, {'error': error}

            triage_result = triage_result or {}
            triage_result.update(self.resolve_registration_context(triage_result))
            organization_id = (
                triage_result.get("organization_id")
                or triage_result.get("openhis_department_id")
                or triage_result.get("dept_id")
            )
            if organization_id:
                self.id_mapper.set_mapping("organization", local_patient_id, str(organization_id))

            outpatient_registration_add_param = self._preregistration_requests.get(local_patient_id)
            if not outpatient_registration_add_param:
                outpatient_registration_add_param = self._build_preregistration_request(
                    openhis_patient_id,
                    triage_result,
                )

            chrg_bchno = preview_id or self.id_mapper.get_openhis_id(
                "preregistration",
                f"{local_patient_id}_{triage_result.get('chief_complaint', '')}",
            )
            if not chrg_bchno:
                error = "缺少预挂号收费批次号(chrgBchno)，无法完成挂号"
                logger.error(f"✗ {error}")
                return False, None, {"error": error}

            total_price = (
                outpatient_registration_add_param.get("chargeItemFormData", {}).get("totalPrice")
                or 50.0
            )

            # 构建完整的请求体：OutpatientRegistrationSettleParam
            request_body = {
                "outpatientRegistrationAddParam": outpatient_registration_add_param,
                "chrgBchno": chrg_bchno,
                "busNo": None,
                "paymentDetails": [
                    {
                        "payEnum": 220100,
                        "amount": float(total_price) * 100
                    }
                ]
            }

            logger.info(f"完成挂号请求: {json.dumps(request_body, ensure_ascii=False)}")

            # 调用API（正确的endpoint）
            response = self.client.post('/payment/payment/reg-pay', request_body)

            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("code") not in (0, 200):
                    error = f"API业务错误: {response_json}"
                    logger.error(f"✗ {error}")
                    self.stats['errors'] += 1
                    return False, None, response_json

                data = response_json.get('data', {}) or {}
                registration_id = (
                    data.get('registrationId')
                    or data.get('id')
                    or data.get('encounterId')
                )
                encounter_id = data.get('encounterId')

                # 存储映射
                if registration_id:
                    self.id_mapper.set_mapping(
                        'registration',
                        local_patient_id,
                        registration_id
                    )

                if encounter_id:
                    self.id_mapper.set_mapping(
                        'encounter',
                        local_patient_id,
                        encounter_id
                    )

                # 【关键】完成挂号后，需要从当日挂号列表中获取真实的 encounterId
                # 因为 /payment/payment/reg-pay 返回的 encounterId 可能不是我们需要的格式
                logger.info(f"→ 正在获取真实的 encounterId (患者={local_patient_id})...")
                try:
                    real_encounter_id = self._fetch_real_encounter_id(
                        openhis_patient_id,
                        local_patient_id
                    )
                    if real_encounter_id:
                        encounter_id = real_encounter_id
                        if not registration_id:
                            registration_id = real_encounter_id
                        data['encounterId'] = real_encounter_id
                        response_json['encounterId'] = real_encounter_id
                        self.id_mapper.set_mapping(
                            'encounter',
                            local_patient_id,
                            real_encounter_id
                        )
                        logger.info(f"✓ 获得真实的 encounterId: {real_encounter_id}")
                except Exception as e:
                    logger.warning(f"⚠ 获取真实 encounterId 失败: {e}，将使用挂号返回的值")

                if not registration_id:
                    registration_id = data.get('paymentId') or data.get('regNo')

                if registration_id:
                    self.id_mapper.set_mapping(
                        'registration',
                        local_patient_id,
                        registration_id
                    )

                self.stats['registrations_finalized'] += 1

                logger.info(f"✓ 挂号已完成: registration_id={registration_id}, encounter_id={encounter_id}")
                return True, registration_id, response_json
            else:
                error = f"API返回错误 {response.status_code}: {response.text}"
                logger.error(f"✗ {error}")
                self.stats['errors'] += 1
                return False, None, {'error': error}

        except Exception as e:
            error = f"完成挂号异常: {e}"
            logger.error(f"✗ {error}")
            self.stats['errors'] += 1
            return False, None, {'error': error}

    def _fetch_real_encounter_id(
        self,
        openhis_patient_id: str,
        local_patient_id: str,
        max_retries: int = 3
    ) -> Optional[str]:
        """
        从当日挂号列表API中获取真实的 encounterId

        完成挂号后，从 /charge-manage/register/current-day-encounter 接口
        获取该患者的最新挂号记录，以获得真实的数字型 encounterId

        Args:
            openhis_patient_id: OpenHIS 患者ID
            local_patient_id: 本地患者ID（用于日志）
            max_retries: 最大重试次数

        Returns:
            真实的 encounterId，如果获取失败则返回 None
        """
        for attempt in range(max_retries):
            try:
                logger.debug(f"  尝试获取当日挂号记录 (尝试 {attempt + 1}/{max_retries})...")

                # 调用当日挂号列表API
                response = self.client.get(
                    '/charge-manage/register/current-day-encounter',
                    params={
                        'pageNo': 1,
                        'pageSize': 50,  # 获取足够多的记录
                        'patientId': str(openhis_patient_id)  # 按患者ID过滤
                    }
                )

                if response.status_code != 200:
                    logger.debug(f"  当日挂号列表API返回错误: {response.status_code}")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(0.5)  # 等待后重试
                        continue
                    else:
                        return None

                # 解析响应
                resp_data = response.json()
                if resp_data.get('code') not in [0, 200]:
                    logger.debug(f"  API响应错误: {resp_data.get('msg')}")
                    return None

                # 获取当日挂号记录
                records = resp_data.get('data', {}).get('records', [])
                records = [
                    record for record in records
                    if str(record.get('patientId')) == str(openhis_patient_id)
                ]
                if not records:
                    logger.debug(f"  未找到患者 {openhis_patient_id} 的当日挂号记录")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(0.5)  # 等待后重试（可能是异步创建）
                        continue
                    else:
                        return None

                # 找到最新的待诊挂号记录
                # 优先选择状态为"待诊"(statusEnum=1)的记录
                pending_records = [r for r in records if r.get('statusEnum') == 1]
                if not pending_records:
                    pending_records = records  # 如果没有待诊的，使用所有的

                if pending_records:
                    latest_record = pending_records[0]  # 通常第一条是最新的
                    encounter_id = latest_record.get('encounterId')
                    if encounter_id:
                        logger.debug(f"  ✓ 找到当日挂号记录: encounterId={encounter_id}")
                        return str(encounter_id)

                logger.debug(f"  当日挂号记录中找不到 encounterId")
                return None

            except Exception as e:
                logger.debug(f"  获取当日挂号列表异常: {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.5)
                    continue
                else:
                    return None

        return None

    def receive_encounter(
        self,
        encounter_id: str,
        local_patient_id: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        医生接诊/叫号（取号）

        Phase 2.3: 调用 /doctor-station/main/receive-encounter 医生接诊患者

        功能：
        - 医生接诊患者（取号）
        - 挂号状态从 PLANNED(1) → IN_PROGRESS(2)
        - 自动记录接诊医生 & 接诊时间
        - 插入 EncounterParticipant(typeCode=ADMITTER)

        Args:
            encounter_id: 就诊ID (encounterId)
            local_patient_id: 本地患者ID（用于日志）

        Returns:
            (成功/失败, 响应数据)
        """
        logger.info(f"→ Phase 2.3: 医生接诊/叫号 (encounter_id={encounter_id}, 患者={local_patient_id})...")

        try:
            logger.info(f"  [第1步] 调用医生接诊接口...")
            # 调用医生接诊接口
            response = self.client.get(
                '/doctor-station/main/receive-encounter',
                params={'encounterId': str(encounter_id)}
            )

            logger.info(f"  [第2步] 响应状态码: {response.status_code}")
            if response.status_code != 200:
                error = f"医生接诊API失败: HTTP {response.status_code}"
                logger.error(f"✗ {error}")
                logger.error(f"  响应: {response.text}")
                return False, {'error': error, 'status_code': response.status_code}

            logger.info(f"  [第3步] 解析响应JSON...")
            try:
                resp_data = response.json()
                logger.info(f"  [第3步] ✓ 响应JSON解析成功")
            except Exception as ex:
                logger.error(f"  [第3步] ✗ 响应JSON解析失败: {ex}")
                logger.error(f"  [第3步] 原始响应: {response.text}")
                raise

            # 【防御性检查】确保resp_data是字典
            logger.info(f"  [第4步] 检查resp_data类型...")
            if not isinstance(resp_data, dict):
                logger.warning(f"  [第4步] ⚠️ resp_data不是字典: {type(resp_data)}")
                resp_data = {}

            logger.info(f"  [第5步] 检查响应状态码...")
            code = resp_data.get('code')
            msg = resp_data.get('msg')
            logger.info(f"  [第5步] 响应 code={code}, msg={msg}")

            if code not in [0, 200]:
                error = f"医生接诈API返回错误: {msg}"
                logger.error(f"✗ {error}")
                return False, resp_data

            logger.info(f"✓ 医生接诊成功: encounterId={encounter_id}")
            return True, resp_data

        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            error = f"医生接诊异常: {str(e)}"
            logger.error(f"✗ {error}")
            logger.error(f"✗ 【完整异常堆栈】:\n{tb_str}")
            return False, {'error': error, 'traceback': tb_str}

    # =====================================================
    # Phase 3: 就诊和EMR阶段
    # =====================================================

    def create_emr_from_consultation(self, local_patient_id, consultation_result, state):
        if not self.config.get("phases", {}).get("consultation_to_emr", True):
            return True, None, {}
        encounter_id = self.id_mapper.get_openhis_id("encounter", local_patient_id)
        patient_id = self.id_mapper.get_openhis_id("patient", local_patient_id)
        if not encounter_id or not patient_id:
            return False, None, {"error": "病历缺少真实就诊/患者ID"}
        received, detail = self.receive_encounter(encounter_id, local_patient_id)
        if not received:
            return False, None, {"error": "接诊失败", "response": detail}
        payload = self._build_emr_data(encounter_id, patient_id, consultation_result, local_patient_id)
        body = self._response_json(self.client.post("/doctor-station/emr/emr", payload))
        saved = self._response_json(self.client.get("/doctor-station/emr/emr-detail", {"encounterId": encounter_id})).get("data") or {}
        emr_id = saved.get("emrId") or saved.get("id")
        if not emr_id or str(saved.get("encounterId")) != str(encounter_id) or str(saved.get("patientId")) != str(patient_id):
            raise RuntimeError("病历保存后未查到对应患者和就诊的记录")
        self.id_mapper.set_mapping("emr", local_patient_id, str(emr_id))
        self.id_mapper.persist_to_disk()
        self.stats["emrs_created"] += 1
        return True, str(emr_id), {**body, "data": saved}

    def create_diagnosis_from_consultation(self, local_patient_id, diagnosis_result, state):
        encounter_id = self.id_mapper.get_openhis_id("encounter", local_patient_id)
        patient_id = self.id_mapper.get_openhis_id("patient", local_patient_id)
        if not encounter_id or not patient_id:
            return False, None, {"error": "诊断缺少真实就诊/患者ID"}
        payload = self._build_diagnosis_data(encounter_id, patient_id, diagnosis_result)
        body = self._response_json(self.client.post("/doctor-station/diagnosis/save-doctor-diagnosis", payload))
        rows = self._get_encounter_diagnoses(encounter_id)
        expected = {str(x["definitionId"]) for x in payload["diagnosisChildList"]}
        saved = [x for x in rows if str(x.get("definitionId")) in expected]
        if {str(x.get("definitionId")) for x in saved} != expected or any(not x.get("conditionId") or not x.get("encounterDiagnosisId") for x in saved):
            raise RuntimeError("诊断保存后未查到完整的真实诊断ID")
        primary = next(x for x in saved if str(x["definitionId"]) == str(payload["diagnosisChildList"][0]["definitionId"]))
        diagnosis_id = str(primary["conditionId"])
        self.id_mapper.set_mapping("diagnosis", local_patient_id, diagnosis_id)
        self.id_mapper.set_mapping("encounter_diagnosis", local_patient_id, str(primary["encounterDiagnosisId"]))
        self.id_mapper.persist_to_disk()
        self.stats["diagnoses_created"] += 1
        return True, diagnosis_id, {**body, "data": {**primary, "diagnoses": saved}}

    # =====================================================
    # Phase 3.3: 处方创建
    # =====================================================

    def create_prescription_from_doctor_advice(
        self,
        local_patient_id: str,
        doctor_output: Dict[str, Any],
        diagnosis_result: Dict[str, Any],
        state: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        从医生输出创建处方（医嘱）

        Phase 3.3: 调用两个API：
        1. POST /doctor-station/advice/save-advice - 保存医嘱
        2. POST /doctor-station/advice/sign-advice - 签发医嘱

        必填参数：
        {
            "organizationId": 50001,      // 科室ID
            "sufferingFlag": 0,           // 代煎标识
            "adviceSaveList": [
                {
                    "dbOpType": "1",      // 新增操作
                    "adviceType": 1,      // 1=药品, 2=耗材, 3=项目
                    "patientId": 10001,   // 患者ID
                    "encounterId": 20001, // 就诊ID
                    "quantity": 100,      // 剂量
                    "unitCode": "片",     // 单位
                    "methodCode": "01",   // 用法
                    "rateCode": "01",     // 频次
                    "dose": 1,            // 单次剂量
                    "doseUnitCode": "片", // 剂量单位
                    "chargeItemId": 30001,  // 费用项ID
                    "adviceDefinitionId": 40001, // 医嘱定义ID
                    "conditionId": 70001, // 诊断ID
                    "encounterDiagnosisId": 70002 // 诊断关联ID
                }
            ]
        }

        Args:
            local_patient_id: 本地患者ID
            doctor_output: 医生输出
            diagnosis_result: 诊断创建结果
            state: 状态管理器状态

        Returns:
            (成功/失败, 请求ID列表, 响应数据)
        """
        logger.info(
            f"→ Phase 3.3: 开始创建处方 (患者={local_patient_id})..."
        )

        try:
            # 获取必需的ID信息
            encounter_id = self.id_mapper.get_openhis_id('encounter', local_patient_id)
            patient_id_num = self.id_mapper.get_openhis_id('patient', local_patient_id)

            if not encounter_id or not patient_id_num:
                error = f"缺少必需的ID: encounter={encounter_id}, patient={patient_id_num}"
                logger.error(f"✗ {error}")
                return False, None, {'error': error}

            organization_id = self._resolve_prescription_organization_id(local_patient_id, encounter_id)
            account_id = self._get_encounter_account_id(encounter_id)
            diagnosis_context = self._resolve_diagnosis_context(
                encounter_id=encounter_id,
                diagnosis_result=diagnosis_result,
            )
            logger.info(
                "  处方上下文: "
                f"organization={organization_id}, account={account_id}, "
                f"condition={diagnosis_context.get('condition_id')}, "
                f"condition_definition={diagnosis_context.get('condition_definition_id')}, "
                f"encounter_diagnosis={diagnosis_context.get('encounter_diagnosis_id')}"
            )

            medication_list = self._extract_medication_list(local_patient_id, doctor_output)

            # 如果医生没有提供用药建议，则不创建处方
            if not medication_list:
                logger.info(f"ℹ️ 医生未提供用药建议，跳过处方创建")
                return True, None, {'message': '无需创建处方'}

            # 构建医嘱列表
            advice_save_list = []
            for med in medication_list:
                advice_item = self._build_advice_item(
                    patient_id=patient_id_num,
                    encounter_id=encounter_id,
                    medication=med,
                    organization_id=organization_id,
                    account_id=account_id,
                    diagnosis_context=diagnosis_context,
                )
                if advice_item:
                    advice_save_list.append(advice_item)

            if len(advice_save_list) != len(medication_list):
                raise ValueError('部分处方药品无法匹配药房，停止保存，不能静默丢弃')

            # 第1步: 保存医嘱
            logger.info(f"  [第1步] 保存医嘱...")
            save_result, request_ids, save_response = self._save_advice(
                patient_id=patient_id_num,
                encounter_id=encounter_id,
                organization_id=organization_id,
                advice_list=advice_save_list
            )

            if not save_result:
                logger.error(f"✗ 保存医嘱失败")
                return False, None, save_response
            if not request_ids:
                logger.error(f"✗ 保存医嘱未返回requestId，无法签发")
                return False, None, save_response

            logger.info(f"✓ 医嘱保存成功: {request_ids}")

            # 第2步: 签发医嘱
            logger.info(f"  [第2步] 签发医嘱...")
            sign_result, signed_ids, sign_response = self._sign_advice(
                patient_id=patient_id_num,
                encounter_id=encounter_id,
                organization_id=organization_id,
                request_ids=request_ids,
                advice_list=advice_save_list
            )

            if not sign_result:
                logger.warning(f"⚠️ 签发医嘱失败，但保存已成功")
                # 即使签发失败也返回保存成功的ID
                return False, request_ids, {'error': '处方已保存但签发失败', 'save_response': save_response, 'sign_response': sign_response}

            logger.info(f"✓ 医嘱签发成功")
            self.stats['prescriptions_created'] += 1

            return True, request_ids, {
                'save_response': save_response,
                'sign_response': sign_response,
                'request_ids': request_ids
            }

        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            error = f"处方创建异常: {e}"
            logger.error(f"✗ {error}")
            logger.error(f"✗ 【完整异常堆栈】:\n{tb_str}")
            self.stats['errors'] += 1
            raise

    @staticmethod
    def _clean_prescription_medication_name(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _has_valid_medication(medications: Any) -> bool:
        return isinstance(medications, list) and any(
            isinstance(item, dict) and item.get("name")
            for item in medications
        )

    def _extract_medication_list(
        self,
        local_patient_id: str,
        doctor_output: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Extract prescription medications from doctor output or saved EMR."""
        medication_list: List[Dict[str, Any]] = []
        doctor_output = doctor_output or {}

        prescription_ref = doctor_output.get("prescription_reference")
        if not isinstance(prescription_ref, dict):
            plan = doctor_output.get("plan")
            if isinstance(plan, dict):
                prescription_ref = plan.get("prescription_reference")

        if isinstance(prescription_ref, dict) and prescription_ref.get("decision") == "medication":
            medications = prescription_ref.get("medication") or []
            if self._has_valid_medication(medications):
                medication_list = [item for item in medications if isinstance(item, dict) and item.get("name")]

        if not medication_list and self._has_valid_medication(doctor_output.get("medication_list")):
            medication_list = [
                item for item in doctor_output.get("medication_list")
                if isinstance(item, dict) and item.get("name")
            ]

        if not medication_list and self.record_system:
            try:
                record = self.record_system.records.get(local_patient_id)
                record_ref = (
                    record.get("plan", {}).get("prescription_reference")
                    if isinstance(record, dict)
                    else None
                )
                if isinstance(record_ref, dict) and record_ref.get("decision") == "medication":
                    medications = record_ref.get("medication") or []
                    if self._has_valid_medication(medications):
                        medication_list = [
                            item for item in medications
                            if isinstance(item, dict) and item.get("name")
                        ]
                        logger.info(f"  已从record_system补充处方: {local_patient_id}")
            except Exception as exc:
                logger.warning(f"  ⚠ 从record_system提取处方失败: {exc}")

        logger.info(f"  提取到处方药品 {len(medication_list)} 条: {medication_list}")
        return medication_list

    def _resolve_prescription_medication(
        self,
        medication: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        raw_name = (
            medication.get("pharmacy_name")
            or medication.get("openhis_name")
            or medication.get("name")
            or medication.get("drug_name")
        )
        name = self._clean_prescription_medication_name(raw_name)
        if not name or not self.pharmacy_inventory:
            return None, None
        resolved_name, entry = self.pharmacy_inventory.resolve_mapping_entry(name)
        if not entry:
            logger.warning(f"  ⚠ 药房中未匹配到可开药品: {raw_name}")
            return None, None
        if resolved_name != raw_name:
            logger.info(f"  药品匹配: {raw_name} -> {resolved_name}")
        return resolved_name, entry

    def _resolve_prescription_organization_id(
        self,
        local_patient_id: str,
        encounter_id: Any,
    ) -> Optional[str]:
        mapped = self.id_mapper.get_openhis_id("organization", local_patient_id)
        if mapped:
            return str(mapped)

        try:
            response = self.client.get(
                "/charge-manage/register/current-day-encounter",
                params={"pageNo": 1, "pageSize": 50},
            )
            if response.status_code == 200:
                data = response.json()
                records = (data.get("data") or {}).get("records") or []
                for record in records:
                    if str(record.get("encounterId")) == str(encounter_id):
                        org_id = record.get("organizationId")
                        if org_id:
                            self.id_mapper.set_mapping("organization", local_patient_id, str(org_id))
                            return str(org_id)
        except Exception as exc:
            logger.warning(f"  ⚠ 查询处方科室失败: {exc}")

        return mapped or "50001"

    def _get_encounter_account_id(self, encounter_id: Any) -> Optional[str]:
        try:
            response = self.client.get(
                "/doctor-station/advice/get-encounter-contract",
                params={"encounterId": str(encounter_id)},
            )
            if response.status_code != 200:
                logger.warning(f"  ⚠ 查询就诊账户失败: HTTP {response.status_code}")
                return None
            data = response.json().get("data")
            if isinstance(data, list) and data:
                account = data[0]
            elif isinstance(data, dict):
                records = data.get("records")
                account = records[0] if isinstance(records, list) and records else data
            else:
                account = None
            if isinstance(account, dict):
                account_id = account.get("accountId") or account.get("id")
                return str(account_id) if account_id else None
        except Exception as exc:
            logger.warning(f"  ⚠ 查询就诊账户异常: {exc}")
        return None

    def _resolve_diagnosis_context(self, encounter_id, diagnosis_result):
        data = (diagnosis_result or {}).get("response", {}).get("data") or {}
        if not isinstance(data, dict) or not data.get("encounterDiagnosisId"):
            rows = self._get_encounter_diagnoses(encounter_id)
            data = next((x for x in rows if x.get("maindiseFlag") == 1), rows[0] if rows else {})
        context = {"condition_id": data.get("conditionId"),
                   "condition_definition_id": data.get("definitionId"),
                   "encounter_diagnosis_id": data.get("encounterDiagnosisId")}
        if not all(context.values()):
            raise ValueError("处方缺少真实诊断关联，不能使用固定或生成的ID")
        return {key: str(value) for key, value in context.items()}

    def _get_encounter_diagnoses(self, encounter_id):
        body = self._response_json(self.client.get(
            "/doctor-station/diagnosis/get-encounter-diagnosis", {"encounterId": encounter_id}))
        return body.get("data") or []

    @staticmethod
    def _response_json(response):
        if response is None:
            raise RuntimeError("OpenHIS 未返回响应")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or str(body.get("code")) not in ("0", "200"):
            raise RuntimeError(f"OpenHIS 业务失败: {body.get('msg') if isinstance(body, dict) else body}")
        return body

    def _prescription_dictionaries(self):
        if not hasattr(self, "_clinical_dictionaries"):
            self._clinical_dictionaries = {
                kind: self._response_json(self.client.get(f"/system/dict/data/type/{kind}")).get("data") or []
                for kind in ("unit_code", "rate_code", "method_code")
            }
        return self._clinical_dictionaries

    def _build_prescription_remark(self, medication: Dict[str, Any], resolved_name: str) -> str:
        parts = []
        for key, label in (
            ("dose", "剂量"),
            ("route", "途径"),
            ("frequency", "频次"),
            ("duration", "疗程"),
            ("patient_instructions", "说明"),
        ):
            value = medication.get(key)
            if value:
                parts.append(f"{label}:{value}")
        return f"{resolved_name}; " + "; ".join(parts) if parts else resolved_name

    def _build_advice_item(
        self,
        patient_id: int,
        encounter_id: int,
        medication: Dict[str, Any],
        organization_id: Optional[Any],
        account_id: Optional[Any],
        diagnosis_context: Dict[str, Optional[Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        构建单个医嘱项，字段来自真实药房 mapping。
        """
        resolved_name, mapping_entry = self._resolve_prescription_medication(medication)
        if not resolved_name or not mapping_entry:
            return None

        params = dict(mapping_entry.get("prescriptionParams") or {})
        charge_item_id = params.get("chargeItemId") or params.get("definitionId")
        if not params.get("adviceDefinitionId") or not charge_item_id:
            logger.warning(f"  ⚠ 药品缺少OpenHIS处方ID: {resolved_name}")
            return None

        params["dbOpType"] = "1"
        params["adviceType"] = 1
        params["chargeItemId"] = str(charge_item_id)
        params["definitionId"] = str(params.get("definitionId") or charge_item_id)
        params["adviceDefinitionId"] = str(params["adviceDefinitionId"])
        params["medicationId"] = str(params.get("medicationId") or params["adviceDefinitionId"])
        params["patientId"] = str(patient_id)
        params["encounterId"] = str(encounter_id)
        params["organizationId"] = str(organization_id) if organization_id else None
        params["sufferingFlag"] = 0
        params["remark"] = self._build_prescription_remark(medication, resolved_name)
        params.update(prescription_fields(medication, mapping_entry, self._prescription_dictionaries()))

        if account_id:
            params["accountId"] = str(account_id)

        if diagnosis_context.get("condition_id"):
            params["conditionId"] = str(diagnosis_context["condition_id"])
        if diagnosis_context.get("condition_definition_id"):
            params["conditionDefinitionId"] = str(diagnosis_context["condition_definition_id"])
        if diagnosis_context.get("encounter_diagnosis_id"):
            params["encounterDiagnosisId"] = str(diagnosis_context["encounter_diagnosis_id"])

        for required_key, default_value in (
            ("quantity", 1),
            ("unitCode", "1"),
            ("dose", 1),
            ("doseUnitCode", params.get("unitCode") or "1"),
            ("methodCode", "1"),
            ("rateCode", "ST"),
            ("positionId", params.get("locationId")),
            ("locationId", params.get("positionId")),
            ("pharmacologyCategoryCode", "1"),
            ("unitPrice", 1.0),
            ("totalPrice", params.get("unitPrice") or 1.0),
        ):
            if params.get(required_key) in (None, ""):
                params[required_key] = default_value

        params = {key: value for key, value in params.items() if value is not None}
        logger.info(
            f"  构建医嘱项: {resolved_name}, "
            f"adviceDefinitionId={params.get('adviceDefinitionId')}, "
            f"chargeItemId={params.get('chargeItemId')}"
        )
        return params

    def _save_advice(
        self,
        patient_id: int,
        encounter_id: int,
        organization_id: Any,
        advice_list: List[Dict[str, Any]]
    ) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """
        调用API保存医嘱

        Returns:
            (成功/失败, 请求ID列表, 响应数据)
        """
        # 构建请求体
        save_request = {
            "organizationId": organization_id,
            "sufferingFlag": 0,                    # 代煎标识：0=不代煎
            "adviceSaveList": advice_list
        }

        logger.info(f"  [保存医嘱请求] organizationId={organization_id}")
        logger.info(f"  医嘱数量: {len(advice_list)}")
        logger.debug(f"  完整请求体: {json.dumps(save_request, ensure_ascii=False, indent=2)}")

        try:
            response = self.client.post(
                '/doctor-station/advice/save-advice',
                save_request
            )

            logger.info(f"  [保存医嘱响应] 状态码: {response.status_code}")

            if response.status_code == 200:
                response_json = response.json()
                api_code = response_json.get('code')
                api_msg = response_json.get('msg', '')

                logger.info(f"  API返回码: {api_code}, 消息: {api_msg}")

                if api_code != 200:
                    logger.error(f"✗ API业务错误: {api_msg}")
                    return False, None, response_json

                # 提取请求ID，OpenHIS v1.3 常返回 data: [id, ...]
                data = response_json.get('data')
                if isinstance(data, list):
                    request_ids = [str(item) for item in data if item]
                elif isinstance(data, dict):
                    request_ids_value = data.get('requestIds') or data.get('requestIdList') or data.get('adviceIdList')
                    if isinstance(request_ids_value, list):
                        request_ids = [str(item) for item in request_ids_value if item]
                    elif isinstance(request_ids_value, str):
                        request_ids = [item for item in request_ids_value.split(',') if item]
                    else:
                        request_id = data.get("requestId") or data.get("id")
                        request_ids = [str(request_id)] if request_id else []
                else:
                    request_ids = []

                logger.info(f"✓ 医嘱保存成功: {len(request_ids)} 条")
                return True, request_ids, response_json
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"✗ {error_msg}")
                return False, None, {'error': error_msg}

        except Exception as e:
            logger.error(f"✗ 保存医嘱异常: {e}")
            return False, None, {'error': str(e)}

    def _sign_advice(
        self,
        patient_id: int,
        encounter_id: int,
        organization_id: Any,
        request_ids: Optional[List[str]],
        advice_list: List[Dict[str, Any]]
    ) -> Tuple[bool, Optional[List[str]], Dict[str, Any]]:
        """
        调用API签发医嘱

        Returns:
            (成功/失败, 签发的请求ID列表, 响应数据)
        """
        # 为签发操作调整dbOpType
        sign_advice_list = []
        request_ids = request_ids or []
        for index, advice in enumerate(advice_list):
            sign_advice = advice.copy()
            sign_advice["dbOpType"] = "2"
            if index < len(request_ids):
                try:
                    sign_advice["requestId"] = int(request_ids[index])
                except (TypeError, ValueError):
                    sign_advice["requestId"] = request_ids[index]
            sign_advice_list.append(sign_advice)

        sign_request = {
            "organizationId": organization_id,
            "sufferingFlag": 0,
            "adviceSaveList": sign_advice_list
        }

        logger.info(f"  [签发医嘱请求] organizationId={organization_id}")
        logger.debug(f"  完整请求体: {json.dumps(sign_request, ensure_ascii=False, indent=2)}")

        try:
            response = self.client.post(
                '/doctor-station/advice/sign-advice',
                sign_request
            )

            logger.info(f"  [签发医嘱响应] 状态码: {response.status_code}")

            if response.status_code == 200:
                response_json = response.json()
                api_code = response_json.get('code')
                api_msg = response_json.get('msg', '')

                logger.info(f"  API返回码: {api_code}, 消息: {api_msg}")

                if api_code != 200:
                    logger.error(f"✗ API业务错误: {api_msg}")
                    return False, None, response_json

                logger.info(f"✓ 医嘱签发成功")
                return True, None, response_json
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"✗ {error_msg}")
                return False, None, {'error': error_msg}

        except Exception as e:
            logger.error(f"✗ 签发医嘱异常: {e}")
            return False, None, {'error': str(e)}



    # =====================================================
    # 辅助方法：构建请求体
    # =====================================================

    def _build_preregistration_request(
        self,
        openhis_patient_id: str,
        triage_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        构建预挂号请求 - 符合OpenHIS API规范

        预挂号（Phase 2.1）仅用于预览费用和获取preview_id，
        医生信息会在第二阶段（finalize_registration）根据preview_id自动获取。
        因此预挂号请求中不应该包含practitionerId。

        Returns:
            OutpatientRegistrationAddParam 对象（不包装）
            直接用于 POST /payment/payment/reg-pre-pay

            结构:
            {
                "encounterFormData": {...},
                "accountFormData": {...},
                "chargeItemFormData": {...},
                "encounterParticipantFormData": {},
                "encounterLocationFormData": {},
                "YbPsnSetlWay": "01",
                "ybMdtrtCertType": "02",
                "busiCardInfo": null,
                "feedetail": []
            }
        """
        # 提取必需字段 (优先使用真实OpenHIS科室ID)
        org_id = (
            triage_result.get('openhis_department_id')
            or triage_result.get('organization_id')
            or triage_result.get('dept_id')
        )
        if not org_id:
            department = self.resolve_department(triage_result.get("target_department"))
            if not department:
                department = self.resolve_department("全科医学科")
            if department:
                org_id = department["id"]

        # 如果没有科室ID，使用与finalize_registration一致的默认科室
        if not org_id:
            # 门诊科（与finalize_registration一致）
            org_id = '1989233329015803905'

        practitioner_id = (
            triage_result.get("openhis_practitioner_id")
            or triage_result.get("practitioner_id")
            or self._resolve_openhis_doctor_id(triage_result.get("assigned_doctor"))
        )

        priority_map = {
            "ROUTINE": 1,
            "SEMI_URGENT": 3,
            "SEMI-URGENT": 3,
            "URGENT": 4,
            "CRITICAL": 5,
        }
        priority_enum = priority_map.get(str(triage_result.get("urgency_level") or "").upper(), 1)

        # 医疗服务ID之前已验证存在
        service_id = 1996539556668776450  # 普通号医疗服务ID

        # 构建预挂号请求体 - 仅包含必需的患者和费用信息
        # 医生分配会在finalize_registration（Phase 2.2）中通过preview_id自动获取
        def to_int_or_str(value):
            """尝试将值转换为int，失败则返回字符串"""
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    # 如果无法转换为int，保持为字符串（可能是UUID）
                    return value
            return value

        # 使用与finalize_registration完全相同的结构
        # 注意：在finalize_registration中使用了int(openhis_patient_id)直接转换
        try:
            patient_id_int = int(openhis_patient_id)
        except (ValueError, TypeError):
            # 如果无法转换为int（比如UUID），使用字符串
            patient_id_int = openhis_patient_id

        org_id_int = int(org_id) if isinstance(org_id, str) and org_id.isdigit() else org_id
        practitioner_id_int = None
        if practitioner_id is not None:
            practitioner_id_int = (
                int(practitioner_id)
                if isinstance(practitioner_id, str) and practitioner_id.isdigit()
                else practitioner_id
            )

        # Build OutpatientRegistrationAddParam DTO directly (NOT wrapped)
        # The POST /payment/payment/reg-pre-pay endpoint expects this object directly
        request_body = {
            "encounterFormData": {
                "patientId": patient_id_int,
                "organizationId": org_id_int,
                "priorityEnum": priority_enum,
                "serviceTypeId": 1,
            },
            "accountFormData": {
                "patientId": patient_id_int,
                "contractNo": "0000",
            },
            "encounterParticipantFormData": (
                {"practitionerId": practitioner_id_int}
                if practitioner_id_int is not None
                else {}
            ),
            "encounterLocationFormData": {},
            "chargeItemFormData": {
                "patientId": patient_id_int,
                "definitionId": 1,
                "serviceId": service_id,
                "totalPrice": 50.0
            },
            "YbPsnSetlWay": "01",
            "ybMdtrtCertType": "02",
            "busiCardInfo": None,
            "feedetail": []
        }

        return request_body

    def _build_emr_data(
        self,
        encounter_id: str,
        openhis_patient_id: str,
        consultation_result: Dict[str, Any],
        local_patient_id: str = None  # ← 【新增】用于从record_system获取病历
    ) -> Dict[str, Any]:
        """
        构建EMR请求 - 符合OpenHIS API规范

        按照 POST /doctor-station/emr/emr 的要求构建请求体

        【关键】直接使用record_system中的完整病历数据作为contextJson，
        不进行二次字段构建，保证EMR中的数据与医生记录完全一致

        Args:
            encounter_id: 就诊ID
            openhis_patient_id: OpenHIS患者ID
            consultation_result: 就诊结果(可能包含consultation_decision)
            local_patient_id: 本地患者ID - 用于从record_system获取最新的完整病历

        Returns:
            符合OpenHIS API的EMR请求体
        """
        # 打印接收到的参数
        logger.info(f"\n【_build_emr_data 参数调试】")
        logger.info(f"  [第1步] encounter_id: {encounter_id}")
        logger.info(f"  [第1步] openhis_patient_id: {openhis_patient_id}")
        logger.info(f"  [第1步] local_patient_id: {local_patient_id}")
        logger.info(f"  [第1步] record_system 可用: {self.record_system is not None}")

        # ⚠️ 【第一步】从record_system获取最新的完整病历数据
        context_json_dict = None
        if self.record_system and local_patient_id:
            try:
                logger.info(f"  [第1-新增] 从record_system获取患者 {local_patient_id} 的完整病历...")
                record_str = self.record_system.get_current_record_str(local_patient_id)

                # 检查是否返回错误
                if "⚠️ 未找到" in record_str:
                    logger.error(f"  [第1-新增] ❌ record_system中未找到患者: {local_patient_id}")
                    raise ValueError(f"患者 {local_patient_id} 不在record_system中")
                else:
                    # 【关键】直接使用完整的record数据作为contextJson
                    context_json_dict = json.loads(record_str)
                    logger.info(f"  [第1-新增] ✓ 成功从record_system获取病历数据")
                    logger.info(f"  [第1-新增] contextJson 结构: {list(context_json_dict.keys())}")
            except Exception as ex:
                logger.error(f"  [第1-新增] ✗ 从record_system获取病历失败: {ex}")
                import traceback
                logger.error(f"  [第1-新增] 堆栈: {traceback.format_exc()}")
                raise
        else:
            # 如果record_system不可用，这是错误
            error_msg = f"❌ record_system不可用或local_patient_id为None: record_system={self.record_system}, local_patient_id={local_patient_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # ⚠️ 防御性检查：确保consultation_result不是None
        if not isinstance(consultation_result, dict):
            logger.warning(f"⚠️ [第2步] consultation_result不是字典: {type(consultation_result)}")
            consultation_result = {}

        # 【日志】记录数据来源
        logger.info(f"  [第2步] contextJson 数据来源: record_system (从record_system直接获取，不进行二次构建)")
        logger.info(f"  [第2步] contextJson 结构: {list(context_json_dict.keys())}")

        # 【第3步】构建 encounterId（必须转换为long类型）
        logger.info(f"  [第3步] 转换 encounterId 为整数...")
        try:
            encounter_id_long = int(encounter_id)
            logger.info(f"  [第3步] ✓ encounterId 转换成功: {encounter_id_long}")
        except (ValueError, TypeError):
            logger.warning(f"⚠ [第3步] 无法将 encounterId '{encounter_id}' 转换为 long，将使用原值")
            encounter_id_long = encounter_id

        # 【第4步】验证 openhis_patient_id
        logger.info(f"  [第4步] 检查 openhis_patient_id...")
        if not openhis_patient_id:
            logger.error(f"❌ [第4步] openhis_patient_id为None或空值，无法创建EMR")
            raise ValueError(f"openhis_patient_id不能为None")
        else:
            logger.info(f"  [第4步] ✓ openhis_patient_id 有效: {openhis_patient_id}")

        # 【第5步】构建 emr_data 字典
        logger.info(f"  [第5步] 构建 emr_data 字典...")
        try:
            emr_data = {
                "encounterId": encounter_id_long,  # 转换为 long 类型
                "patientId": openhis_patient_id,

                # ⭐ 【关键】contextJson - 直接使用record_system的完整数据
                "contextJson": context_json_dict,

                # 病历状态
                "emrStatus": "draft",

                # 记录人ID（optional）
                "recordId": consultation_result.get('doctor_id') or 1
            }
            logger.info(f"  [第5步] ✓ emr_data 构建成功")
        except Exception as ex:
            logger.error(f"  [第5步] ✗ emr_data 构建失败: {ex}")
            import traceback
            logger.error(f"  [第5步] 堆栈: {traceback.format_exc()}")
            raise

        logger.info(f"  EMR data ready: keys={list(emr_data.keys())}")
        return emr_data

    def _safe_get_float(self, data_dict: Dict, key: str, default: float = 0.0) -> float:
        """安全地从字典中获取浮点数值"""
        try:
            # 【防御性检查】确保data_dict本身不是None
            if data_dict is None:
                logger.warning(f"⚠️ _safe_get_float: data_dict为None，返回默认值{default}")
                return default

            if not isinstance(data_dict, dict):
                logger.warning(f"⚠️ _safe_get_float: data_dict不是字典({type(data_dict)})，返回默认值{default}")
                return default

            val = data_dict.get(key)
            if val is None:
                logger.debug(f"  _safe_get_float[{key}]: 值为None，返回默认值{default}")
                return default

            result = float(val)
            logger.debug(f"  _safe_get_float[{key}]: {val} → {result}")
            return result
        except (ValueError, TypeError) as ex:
            logger.warning(f"⚠️ _safe_get_float[{key}]: 转换失败 ({ex})，返回默认值{default}")
            return default
        except Exception as ex:
            logger.error(f"❌ _safe_get_float[{key}]: 意外异常: {ex}")
            import traceback
            logger.error(f"  堆栈: {traceback.format_exc()}")
            return default

    def _build_diagnosis_data(self, encounter_id, patient_id, diagnosis_result):
        """缺少数据/定义：暂沿用原有自由文本和 definitionId=1 占位方式。"""
        if not isinstance(diagnosis_result, dict):
            diagnosis_result = {}
        diagnosis_desc = (
            diagnosis_result.get("chief_complaint")
            or diagnosis_result.get("chief_complaint_desc")
            or diagnosis_result.get("diagnosis_description")
            or diagnosis_result.get("assessment")
            or diagnosis_result.get("reply")
            or "门诊就诊"
        )
        return {
            "patientId": patient_id,
            "encounterId": encounter_id,
            "diagnosisChildList": [{
                "conditionId": None,
                "encounterDiagnosisId": None,
                "definitionId": 1,
                "ybNo": None,
                "verificationStatusEnum": None,
                "maindiseFlag": 1,
                "diagSrtNo": 1,
                "medTypeCode": None,
                "syndromeGroupNo": None,
                "diagnosisDesc": str(diagnosis_desc),
                "iptDiseTypeCode": None,
                "updateId": None,
                "updateConditionId": None,
            }],
        }

    def _generate_soap_xml(self, consultation_result: Dict[str, Any]) -> str:
        """
        生成SOAP格式的XML

        Args:
            consultation_result: 就诊结果

        Returns:
            SOAP XML字符串
        """
        # 简化的SOAP XML模板
        soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP>
    <S>
        <chief>
            {consultation_result.get('chief_complaint', '')}
        </chief>
        <presentHip>
            {consultation_result.get('present_illness', '')}
        </presentHip>
    </S>
    <O>
        <physicalExam>
            {consultation_result.get('physical_exam', '')}
        </physicalExam>
        <auxiliaryExam>
            {consultation_result.get('auxiliary_exam', '')}
        </auxiliaryExam>
    </O>
    <A>
        {consultation_result.get('assessment', '')}
    </A>
    <P>
        {consultation_result.get('plan', '')}
    </P>
</SOAP>"""
        return soap

    # =====================================================
    # 工具方法
    # =====================================================

    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return self.stats.copy()

    def close(self) -> None:
        """关闭连接和资源"""
        self.client.close()
        self.id_mapper.persist_to_disk()
        logger.info("✓ OpenHIS适配器已关闭")

    def __enter__(self):
        """Context manager support"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager support"""
        self.close()
