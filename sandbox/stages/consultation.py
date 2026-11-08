"""
问诊Stage - 患者进入医生咨询阶段
"""

import json
from typing import Dict, Any, Optional


from sandbox.core.pipeline import PipelineStage
from sandbox.core.state_manager import StateManager
from sandbox.core.sandbox_env import SandboxEnvironment

# 导入Agent系统
from agents.patient_agent import PatientAgent

from sandbox.openhis import OpenHISAdapter

HAS_OPENHIS = True


class ConsultationStage(PipelineStage):
    """
    问诊阶段

    医生与患者进行咨询，完成诊疗决策
    每个Clock应处理：
    1. 医生与队列中的患者进行咨询
    2. 医生可能处理多个患者
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__("consultation", config)
        self.coordinator = None
        self.doctor_agents = {}

    def execute(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        input_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行问诊 - 处理从分诊阶段转来的患者

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            input_data: 输入数据（来自分诊阶段的患者分诊决策）

        Returns:
            问诊结果列表
        """
        print(f"\n>>> 问诊Stage开始执行")

        coordinator = sandbox_env.get_coordinator()

        # 获取待咨询的患者（来自分诊阶段结果）
        input_data = input_data or {}
        triage_results = input_data.get('triage_results', [])

        if not triage_results:
            print("    【无患者】没有待咨询的患者")
            return []

        print(f"    【患者队列】{len(triage_results)} 个患者待咨询")

        # 执行医生咨询
        consultation_results = self._execute_doctor_consultation(
            state_manager,
            sandbox_env,
            triage_results
        )

        return consultation_results

    def _execute_doctor_consultation(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        triage_results: list
    ) -> list:
        """
        医生咨询流程 - 一个Clock内处理多个患者

        关键特点：
        - 医生轮转处理患者（类似护士分诊）
        - 每个医生为一个患者创建room
        - 所有room创建完后统一运行

        Returns:
            问诊结果列表
        """
        coordinator = sandbox_env.get_coordinator()
        consultation_rooms = []

        # 获取医生列表
        doctor_ids = list(coordinator.doctor_agents.keys())
        if not doctor_ids:
            print("    【无医生】没有可用的医生")
            return []

        print(f"    【第一阶段】创建咨询room")

        # ========== 第一阶段：创建room ==========

        for raw_triage_result in triage_results:
            triage_result = self._normalize_triage_result(raw_triage_result)
            # 从triage_result中获取患者指定的医生
            doctor_id = triage_result.get('doctor_id') or triage_result.get('assigned_doctor')
            if not doctor_id or doctor_id not in coordinator.doctor_agents:
                print(f"      医生 {doctor_id} 不可用，跳过患者 {triage_result.get('patient_id')}")
                continue

            # 检查医生是否空闲
            doctor_info = coordinator.doctor_status.get(doctor_id, {})
            if doctor_info.get("room_id"):
                print(f"      医生 {doctor_id} 忙碌，跳过患者 {triage_result.get('patient_id')}")
                continue

            patient_id = triage_result.get('patient_id')
            patient_name = triage_result.get('patient_name')

            print(f"      ├─ 医生 {doctor_id} 创建room: 患者 {patient_id}")

            try:
                # 创建咨询room
                room_data = self._create_consultation_room(
                    state_manager,
                    sandbox_env,
                    triage_result,
                    doctor_id
                )
                consultation_rooms.append(room_data)

            except Exception as e:
                print(f"        ✗ 创建room失败: {str(e)}")

                result = {
                    "patient_id": patient_id,
                    "patient_name": patient_name,
                    "status": "failed",
                    "error": str(e)
                }
                consultation_rooms.append({
                    "result": result,
                    "room": None,
                    "doctor_id": None,
                    "triage_result": triage_result
                })

        print(f"    【第二阶段】运行咨询room ({len(consultation_rooms)} 个)")

        # ========== 第二阶段：运行所有room ==========
        consultation_results = []

        for room_data in consultation_rooms:
            doctor_id = room_data.get("doctor_id")
            triage_result = room_data.get("triage_result")
            full_result = {}

            try:
                if room_data["room"] is None:
                    # 失败的room直接使用已有结果
                    result = room_data["result"]
                    consultation_results.append(result)
                else:
                    # 运行对话
                    room = room_data["room"]
                    patient_id = triage_result.get('patient_id')

                    print(f"      └─ 运行room: {room.room_id} (患者: {patient_id})")

                    # 准备初始消息（患者的主诉）
                    chief_complaint = triage_result.get('chief_complaint', '身体不适')
                    opening_message = f"医生您好，我是{patient_id}，我主要是{chief_complaint}。"

                    # 运行对话
                    consultation_decision = room.run_conversation(
                        opening_message=opening_message,
                        conversation_type="consultation",
                        coordinator=coordinator,
                        triage_result=triage_result  # 传递分诊信息给医生
                    )

                    # 补充信息
                    consultation_decision['patient_id'] = patient_id
                    consultation_decision['patient_name'] = triage_result.get('patient_name')
                    consultation_decision['triage_info'] = triage_result
                    full_result = {"consultation_decision": consultation_decision}
                    consultation_decision = self._enrich_prescription_from_record(
                        sandbox_env,
                        patient_id,
                        consultation_decision
                    )

                    # 构建完整的咨询结果
                    full_result = {
                        "patient_id": patient_id,
                        "patient_name": triage_result.get('patient_name'),
                        "mode": "ai_consultation",
                        "chief_complaint": triage_result.get('chief_complaint'),
                        "urgency_level": triage_result.get('urgency_level'),
                        "target_department": triage_result.get('target_department'),
                        "consultation_decision": consultation_decision,
                        "assigned_doctor": doctor_id,
                        "conversation_turns": len(room.messages) // 2,
                        "status": "completed",
                        "room_id": room.room_id
                    }

                    # ========== OpenHIS集成：完成挂号 ==========
                    # 检查是否有预注册ID，并调用finalize_registration
                    preview_id = triage_result.get('openhis_preregistration_id')
                    openhis_config = sandbox_env.config.get("openhis_integration", {})
                    phases = openhis_config.get("phases", {})
                    if sandbox_env.openhis_adapter and phases.get("triage_to_registration", True):
                        openhis_result = self._call_openhis_finalize_registration(
                            state_manager,
                            sandbox_env,
                            patient_id,
                            preview_id,
                            triage_result
                        )
                        full_result['openhis_registration'] = openhis_result
                        if not openhis_result['success']:
                            raise RuntimeError(f"OpenHIS 挂号失败: {openhis_result.get('error') or openhis_result.get('response')}")
                        full_result['openhis_registration_id'] = openhis_result['registration_id']

                    # ========== OpenHIS集成：创建EMR和诊断 ==========
                    if sandbox_env.openhis_adapter and phases.get("consultation_to_emr", True):
                        # 创建EMR
                        emr_result = self._call_openhis_create_emr(
                            state_manager,
                            sandbox_env,
                            patient_id,
                            full_result
                        )
                        full_result['openhis_emr'] = emr_result
                        if not emr_result['success']:
                            raise RuntimeError(f"OpenHIS 病历失败: {emr_result.get('error') or emr_result.get('response')}")
                        full_result['openhis_emr_id'] = emr_result['emr_id']

                        # 创建诊断
                        diagnosis_result = self._call_openhis_create_diagnosis(
                            state_manager,
                            sandbox_env,
                            patient_id,
                            consultation_decision
                        )
                        full_result['openhis_diagnosis'] = diagnosis_result
                        if not diagnosis_result['success']:
                            raise RuntimeError(f"OpenHIS 诊断失败: {diagnosis_result.get('error') or diagnosis_result.get('response')}")
                        if diagnosis_result['success']:
                            full_result['openhis_diagnosis_id'] = diagnosis_result['diagnosis_id']

                            # Phase 3.3: 创建处方（在诊断成功后）
                            prescription_result = self._call_openhis_create_prescription(
                                state_manager,
                                sandbox_env,
                                patient_id,
                                consultation_decision,
                                diagnosis_result
                            )
                            full_result['openhis_prescription'] = prescription_result
                            if not prescription_result['success']:
                                raise RuntimeError(f"OpenHIS 处方失败: {prescription_result.get('error') or prescription_result.get('response')}")
                            if prescription_result['success']:
                                full_result['openhis_prescription_id'] = prescription_result.get('prescription_id')
                                full_result['openhis_prescription_request_ids'] = prescription_result.get('request_ids')

                    coordinator.complete_consultation(doctor_id, json.dumps(consultation_decision, ensure_ascii=False))
                    consultation_results.append(full_result)

            except Exception as e:
                import traceback
                tb_str = traceback.format_exc()
                print(f"        ✗ 处理失败: {str(e)}")
                print(f"        \n【完整异常堆栈】:")
                print(f"        {tb_str}")

                result = {
                    **full_result,
                    "patient_id": triage_result.get('patient_id'),
                    "patient_name": triage_result.get('patient_name'),
                    "status": "failed",
                    "error": str(e),
                    "traceback": tb_str
                }
                consultation_results.append(result)

            finally:
                # 释放医生（无论成功还是失败）
                if doctor_id is not None and doctor_id in coordinator.doctor_status:
                    doctor_state = coordinator.doctor_status[doctor_id]
                    doctor_state["room_id"] = None
                    current = doctor_state.get("current_patient")
                    if current is not None:
                        current["status"] = "failed"
                        doctor_state.setdefault("failed_patients", []).append(current)
                        doctor_state["current_patient"] = None
                if room_data.get("room"):
                    room_data["room"].close()

        print(f"    ✓ Clock内 {len(consultation_results)} 个room运行完成")
        return consultation_results

    def _normalize_triage_result(self, triage_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        run_experiment passes waiting-queue records as a wrapper containing
        patient_info and a nested triage_result. The consultation stage needs
        the actual triage fields at the top level.
        """
        nested = triage_result.get('triage_result')
        if not isinstance(nested, dict):
            return triage_result

        normalized = dict(nested)
        normalized['patient_id'] = triage_result.get('patient_id') or nested.get('patient_id')
        normalized['patient_name'] = triage_result.get('patient_name') or nested.get('patient_name')
        normalized['doctor_id'] = (
            triage_result.get('doctor_id')
            or nested.get('doctor_id')
            or nested.get('assigned_doctor')
        )
        normalized['patient_info'] = triage_result.get('patient_info')
        return normalized

    def _enrich_prescription_from_record(self, sandbox_env, patient_id, consultation_decision):
        """Use only the doctor's structured prescription; never invent medications."""
        from tools.pharmacy_inventory import PharmacyInventory

        if not isinstance(consultation_decision, dict):
            raise ValueError("医生问诊结果必须是 JSON 对象")
        record_system = getattr(sandbox_env, "record_system", None)
        if record_system is None:
            record_system = getattr(getattr(sandbox_env, "openhis_adapter", None), "record_system", None)
        record = getattr(record_system, "records", {}).get(patient_id, {})
        record_ref = (record.get("plan") or {}).get("prescription_reference")
        reference = consultation_decision.get("prescription_reference")
        if reference is None and isinstance(consultation_decision.get("plan"), dict):
            reference = consultation_decision["plan"].get("prescription_reference")
        if reference is None:
            reference = record_ref
        inventory = getattr(record_system, "pharmacy_inventory", None) or PharmacyInventory()
        inventory.validate_prescription(reference)
        if reference["decision"] == "no prescription" and self._has_unstructured_medication_advice(consultation_decision):
            raise ValueError("医生建议用药但未提供结构化处方；自动补药已取消，本次问诊失败")
        if isinstance(record_ref, dict) and record_ref.get("decision") in ("medication", "no prescription") and record_ref != reference:
            raise ValueError("医生最终处方与已保存病历不一致，请先更新并保存病历")
        medications = reference["medication"]
        if consultation_decision.get("medication_list") is not None and consultation_decision["medication_list"] != medications:
            raise ValueError("medication_list 与 prescription_reference.medication 不一致")
        consultation_decision["prescription_reference"] = reference
        consultation_decision["medication_list"] = medications
        return consultation_decision

    @staticmethod
    def _has_unstructured_medication_advice(decision):
        """Retain the old trigger only as an error check, never a prescribing rule."""
        text = "\n".join(str(decision.get(key) or "") for key in ("reply", "education", "treatment"))
        symptoms = ("退烧", "退热", "止痛", "发热", "头痛", "疼痛")
        medications = ("退烧药", "退热药", "止痛药", "对症退热", "对症止痛", "可临时服用")
        return any(term in text for term in symptoms) and any(term in text for term in medications)

    def _create_consultation_room(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        triage_result: Dict[str, Any],
        doctor_id: str
    ) -> Dict[str, Any]:
        """
        创建咨询room（第一阶段：不运行）

        Args:
            triage_result: 来自分诊阶段的患者分诊信息
            doctor_id: 医生ID

        Returns:
            包含room和相关信息的字典
        """
        coordinator = sandbox_env.get_coordinator()
        patient_id = triage_result.get('patient_id')
        patient_name = triage_result.get('patient_name')

        # 检查医生是否已初始化
        if doctor_id not in coordinator.doctor_agents:
            raise Exception(f"医生 {doctor_id} Agent未初始化")

        doctor_agent = coordinator.doctor_agents[doctor_id]

        # 检查医生是否空闲
        if coordinator.doctor_status[doctor_id].get("room_id"):
            raise Exception(f"医生 {doctor_id} 忙碌中")

        # 占用医生
        coordinator.doctor_status[doctor_id]["room_id"] = f"consultation_{patient_id}"

        try:
            # 获取患者profile路径
            profile_path = triage_result.get('profile_path')
            if not profile_path:
                # 如果没有profile_path，从patient_queue中查找
                for patient in coordinator.patient_queue:
                    if patient['patient_id'] == patient_id:
                        profile_path = patient.get('profile_path')
                        break

            # 创建PatientAgent
            patient_agent = PatientAgent(profile_path=profile_path, model_config=sandbox_env.get_model_config("patient").to_dict())

            # 创建对话室
            room_id = coordinator._create_room(
                patient_agent=patient_agent,
                medical_agent=doctor_agent,
                max_round=self.config.get('max_rounds', 10)
            )
            room = coordinator.chat_rooms[room_id]
            waiting = coordinator.doctor_status[doctor_id]["waiting_queue"]
            if not waiting or waiting[0]["patient_info"].get("patient_id") != patient_id:
                raise ValueError(f"医生等待队列与当前患者不一致: {patient_id}")
            coordinator.start_consultation(doctor_id)

            # 返回room数据（包含运行所需的所有信息）
            return {
                "room": room,
                "triage_result": triage_result,
                "doctor_id": doctor_id,
                "result": None
            }
        except Exception as e:
            # 创建room失败，释放医生
            coordinator.doctor_status[doctor_id]["room_id"] = None
            raise

    def _call_openhis_finalize_registration(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_id: str,
        preview_id: str,
        triage_result: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        调用OpenHIS完成挂号（第二步）

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            patient_id: 患者ID
            preview_id: 预挂号预览编码

        Returns:
            包含成功状态和响应的字典
        """
        if not HAS_OPENHIS:
            return {
                "success": False,
                "error": "OpenHIS适配器未安装",
                "registration_id": None
            }

        # 获取OpenHIS适配器
        openhis_adapter = sandbox_env.openhis_adapter
        if openhis_adapter is None:
            return {
                "success": False,
                "error": "OpenHIS集成未启用",
                "registration_id": None
            }

        try:
            print(f"      → 调用OpenHIS完成挂号...")

            # 调用finalize_registration
            success, registration_id, response = openhis_adapter.finalize_registration(
                preview_id=preview_id,
                local_patient_id=patient_id,
                state=state_manager.state,
                triage_result=triage_result or {}
            )

            if success:
                encounter_id = response.get('encounterId')
                # response 现在是完整的 API 返回（第一级），而不是内嵌的 data
                print(f"      ✓ OpenHIS完成挂号")
                print(f"        - 挂号ID: {registration_id}")
                print(f"        - 【完整API返回 (第一级)】:")
                print(f"          {json.dumps(response, indent=2, ensure_ascii=False)}")
                if encounter_id:
                    print(f"        - 就诊ID: {encounter_id}")
                return {
                    "success": True,
                    "registration_id": registration_id,
                    "response": response
                }
            else:
                print(f"      ⚠️ OpenHIS挂号失败: {response}")
                return {
                    "success": False,
                    "error": str(response),
                    "registration_id": None
                }

        except Exception as e:
            print(f"      ⚠️ OpenHIS调用异常: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "registration_id": None
            }

    def _call_openhis_create_emr(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_id: str,
        consultation_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        调用OpenHIS创建电子病历（EMR）

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            patient_id: 患者ID
            consultation_result: 问诊结果

        Returns:
            包含成功状态和响应的字典
        """
        if not HAS_OPENHIS:
            return {
                "success": False,
                "error": "OpenHIS适配器未安装",
                "emr_id": None
            }

        # 获取OpenHIS适配器
        openhis_adapter = sandbox_env.openhis_adapter
        if openhis_adapter is None:
            return {
                "success": False,
                "error": "OpenHIS集成未启用",
                "emr_id": None
            }

        try:
            print(f"      → 调用OpenHIS创建EMR...")

            # 【调试】打印传递给EMR创建的参数
            print(f"      \n【EMR参数调试】")
            print(f"      - patient_id: {patient_id}")
            print(f"      - consultation_result keys: {list(consultation_result.keys())}")
            print(f"      - chief_complaint: {consultation_result.get('chief_complaint')}")
            print(f"      - consultation_decision type: {type(consultation_result.get('consultation_decision'))}")
            if isinstance(consultation_result.get('consultation_decision'), dict):
                print(f"      - consultation_decision keys: {list(consultation_result.get('consultation_decision', {}).keys())}")
            print(f"      - 完整consultation_result (前1000字): {json.dumps(consultation_result, indent=2, ensure_ascii=False)[:1000]}")
            print()

            # 调用create_emr_from_consultation
            success, emr_id, response = openhis_adapter.create_emr_from_consultation(
                local_patient_id=patient_id,
                consultation_result=consultation_result,
                state=state_manager.state
            )

            if success:
                print(f"      ✓ OpenHIS EMR创建成功: {emr_id}")
                # response 现在是完整的 API 返回（第一级），而不是内嵌的 data
                print(f"      ✓ 【完整API返回 (第一级)】:")
                print(f"        {json.dumps(response, indent=2, ensure_ascii=False)}")
                return {
                    "success": True,
                    "emr_id": emr_id,
                    "response": response
                }
            else:
                print(f"      ⚠️ OpenHIS EMR创建失败: {response}")
                return {
                    "success": False,
                    "error": str(response),
                    "emr_id": None
                }

        except Exception as e:
            import traceback
            # 获取完整的异常堆栈信息
            tb_str = traceback.format_exc()
            print(f"      ✗ OpenHIS EMR创建异常:")
            print(f"      {str(e)}")
            print(f"      \n【完整异常堆栈】:")
            print(f"      {tb_str}")
            return {
                "success": False,
                "error": str(e),
                "traceback": tb_str,
                "emr_id": None
            }

    def _call_openhis_create_diagnosis(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_id: str,
        diagnosis_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        调用OpenHIS创建诊断记录

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            patient_id: 患者ID
            diagnosis_result: 诊断结果

        Returns:
            包含成功状态和响应的字典
        """
        if not HAS_OPENHIS:
            return {
                "success": False,
                "error": "OpenHIS适配器未安装",
                "diagnosis_id": None
            }

        # 获取OpenHIS适配器
        openhis_adapter = sandbox_env.openhis_adapter
        if openhis_adapter is None:
            return {
                "success": False,
                "error": "OpenHIS集成未启用",
                "diagnosis_id": None
            }

        try:
            print(f"      → 调用OpenHIS创建诊断...")
            print(f"      → 诊断结果参数:")
            print(f"        {json.dumps(diagnosis_result, indent=2, ensure_ascii=False)}")

            # 调用create_diagnosis_from_consultation
            success, diagnosis_id, response = openhis_adapter.create_diagnosis_from_consultation(
                local_patient_id=patient_id,
                diagnosis_result=diagnosis_result,
                state=state_manager.state
            )

            if success:
                print(f"      ✓ OpenHIS诊断创建成功: {diagnosis_id}")
                # response 现在是完整的 API 返回（第一级），而不是内嵌的 data
                print(f"\n" + "="*80)
                print(f"【诊断创建 - OpenHIS API 完整返回】")
                print(f"="*80)
                print(f"状态: 成功")
                print(f"诊断ID: {diagnosis_id}")
                print(f"完整返回JSON:")
                print(json.dumps(response, indent=2, ensure_ascii=False))
                print(f"="*80 + "\n")
                return {
                    "success": True,
                    "diagnosis_id": diagnosis_id,
                    "response": response
                }
            else:
                print(f"      ⚠️ OpenHIS诊断创建失败")
                print(f"\n" + "="*80)
                print(f"【诊断创建 - OpenHIS API 错误返回】")
                print(f"="*80)
                print(f"状态: 失败")
                print(f"错误信息: {json.dumps(response, indent=2, ensure_ascii=False)}")
                print(f"="*80 + "\n")
                return {
                    "success": False,
                    "error": str(response),
                    "diagnosis_id": None
                }

        except Exception as e:
            import traceback
            # 获取完整的异常堆栈信息
            tb_str = traceback.format_exc()
            print(f"      ✗ OpenHIS诊断创建异常:")
            print(f"      {str(e)}")
            print(f"\n      【完整异常堆栈】:")
            print(f"      {tb_str}")
            return {
                "success": False,
                "error": str(e),
                "traceback": tb_str,
                "diagnosis_id": None
            }

    def _call_openhis_create_prescription(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_id: str,
        doctor_output: Dict[str, Any],
        diagnosis_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        调用OpenHIS创建处方（医嘱）

        Phase 3.3: 创建处方
        - 调用save-advice保存医嘱
        - 调用sign-advice签发医嘱

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            patient_id: 患者ID
            doctor_output: 医生输出，包含medication_list
            diagnosis_result: 诊断结果，包含condition_id和encounter_diagnosis_id

        Returns:
            包含成功状态和响应的字典
        """
        if not HAS_OPENHIS:
            return {
                "success": False,
                "error": "OpenHIS适配器未安装",
                "prescription_id": None
            }

        # 获取OpenHIS适配器
        openhis_adapter = sandbox_env.openhis_adapter
        if openhis_adapter is None:
            return {
                "success": False,
                "error": "OpenHIS集成未启用",
                "prescription_id": None
            }

        try:
            print(f"      → 调用OpenHIS创建处方...")

            # 【调试】打印传递给处方创建的参数
            print(f"      \n【处方参数调试】")
            print(f"      - patient_id: {patient_id}")
            print(f"      - doctor_output type: {type(doctor_output)}")
            if isinstance(doctor_output, dict):
                print(f"      - doctor_output keys: {list(doctor_output.keys())}")
                print(f"      - medication_list: {doctor_output.get('medication_list')}")
            print(f"      - diagnosis_result keys: {list(diagnosis_result.keys()) if isinstance(diagnosis_result, dict) else 'N/A'}")
            print()

            # 调用create_prescription_from_doctor_advice
            success, request_ids, response = openhis_adapter.create_prescription_from_doctor_advice(
                local_patient_id=patient_id,
                doctor_output=doctor_output,
                diagnosis_result=diagnosis_result,
                state=state_manager.state
            )

            if success:
                print(f"      ✓ OpenHIS处方创建成功")
                # response 现在是完整的 API 返回（第一级），而不是内嵌的 data
                print(f"\n" + "="*80)
                print(f"【处方创建 - OpenHIS API 完整返回】")
                print(f"="*80)
                print(f"状态: 成功")
                if request_ids:
                    print(f"医嘱IDs: {request_ids}")
                print(f"完整返回JSON:")
                print(json.dumps(response, indent=2, ensure_ascii=False))
                print(f"="*80 + "\n")
                return {
                    "success": True,
                    "prescription_id": request_ids[0] if request_ids else None,
                    "request_ids": request_ids,
                    "response": response
                }
            else:
                print(f"      ⚠️ OpenHIS处方创建失败")
                print(f"\n" + "="*80)
                print(f"【处方创建 - OpenHIS API 错误返回】")
                print(f"="*80)
                print(f"状态: 失败")
                print(f"错误信息: {json.dumps(response, indent=2, ensure_ascii=False)}")
                print(f"="*80 + "\n")
                return {
                    "success": False,
                    "error": str(response),
                    "prescription_id": None
                }

        except Exception as e:
            import traceback
            # 获取完整的异常堆栈信息
            tb_str = traceback.format_exc()
            print(f"      ✗ OpenHIS处方创建异常:")
            print(f"      {str(e)}")
            print(f"\n      【完整异常堆栈】:")
            print(f"      {tb_str}")
            return {
                "success": False,
                "error": str(e),
                "traceback": tb_str,
                "prescription_id": None
            }
