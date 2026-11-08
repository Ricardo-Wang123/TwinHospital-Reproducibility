"""
分诊Stage - 患者进入医院后的初诊分诊
"""

import json
import asyncio
from typing import Dict, Any


from sandbox.core.pipeline import PipelineStage
from sandbox.core.state_manager import StateManager
from sandbox.core.sandbox_env import SandboxEnvironment

# 导入Agent系统
from agents.patient_agent import PatientAgent

# 导入OpenHIS集成
try:
    from sandbox.openhis import OpenHISAdapter
    HAS_OPENHIS = True
except (ImportError, ModuleNotFoundError):
    HAS_OPENHIS = False
    print("⚠️ 警告: OpenHIS模块未安装，将跳过预注册")


class TriageStage(PipelineStage):
    """
    分诊阶段

    包含两种模式：
    1. 无AI模式：患者直接指定目标科室
    2. AI分诊模式：通过护士Agent进行智能分诊

    同时支持两种执行方式：
    1. serial（串行，默认）：传统的Clock机制，向后兼容
    2. parallel（并行）：基于asyncio的并发分诊，适合可视化场景
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__("triage", config)
        self.coordinator = None
        self.nurse_agent = None
        self.patient_agent = None

    def execute(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        input_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行分诊 - 可处理单个患者（旧模式）或患者队列（新模式）

        支持配置项：
        - mode: 'no_ai' 或 'ai' （分诊模式：无AI或AI分诊）
        - execution_mode: 'serial' 或 'parallel' （从pipeline配置继承）

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            input_data: 输入数据（可选，仅用于单个患者模式）

        Returns:
            分诊结果（单个患者）或分诊结果列表（多患者队列）
        """
        print(f"\n>>> 分诊Stage开始执行")

        coordinator = sandbox_env.get_coordinator()
        mode = self.config.get('mode', 'no_ai')  # 'no_ai' 或 'ai'

        # 获取执行模式（从pipeline配置继承）
        # 优先级：1) stage配置 > 2) pipeline配置 > 3) 默认值 'serial'
        execution_mode = self.config.get('execution_mode')
        if execution_mode is None and hasattr(self, 'parent_config'):
            execution_mode = self.parent_config.get('pipeline', {}).get('execution_mode', 'serial')
        if execution_mode is None:
            execution_mode = 'serial'

        print(f"    执行模式: {execution_mode.upper()}")

        # 检查是否有患者队列（新模式）
        if coordinator and coordinator.has_patients_in_queue():
            print(f"    【队列模式】处理 {len(coordinator.patient_queue)} 个患者")

            if execution_mode.lower() == 'parallel':
                # 使用并行模式执行
                print(f"    使用并行事件驱动模式处理患者")
                try:
                    return asyncio.run(
                        self._execute_queue_triage_parallel(state_manager, sandbox_env, mode)
                    )
                except RuntimeError as e:
                    # 如果已经在事件循环中，直接调用异步方法
                    if "asyncio.run() cannot be called from a running event loop" in str(e):
                        print(f"    在事件循环中，使用串行模式处理")
                        return self._execute_queue_triage(state_manager, sandbox_env, mode)
                    raise
            else:
                # 使用串行模式执行（默认，向后兼容）
                return self._execute_queue_triage(state_manager, sandbox_env, mode)

        # 否则使用旧模式：处理单个患者
        print(f"    【单患者模式】处理单个患者")
        input_data = input_data or {}

        if mode == 'no_ai':
            result = self._execute_no_ai_triage(state_manager, input_data)
        elif mode == 'ai':
            result = self._execute_ai_triage(state_manager, sandbox_env, input_data)
        else:
            raise ValueError(f"未知的分诊模式: {mode}")

        print(f"    分诊结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return result

    def _execute_queue_triage(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        mode: str
    ) -> Dict[str, Any]:
        """
        单个Clock内的分诊 - 创建所有room后统一运行

        关键改变（Clock机制完整版）：
        [第一阶段] 创建room
        - 一个Clock内：护士0 → 护士1 → 护士0 → ...（串行轮转）
        - 每个护士创建一个room（如果有患者的话）

        [第二阶段] 运行room
        - 统一运行这个Clock内创建的所有room
        - 每个room运行完整的conversation直到结束

        Returns:
            这个Clock内所有room的分诊结果列表
        """
        coordinator = sandbox_env.get_coordinator()

        # 检查是否还有待分诊患者
        if not coordinator.has_patients_in_queue():
            return []

        # 获取所有护士列表
        nurse_list = list(coordinator.nurse_agents.keys())
        if not nurse_list:
            return []

        # ========== 第一阶段：创建room ==========
        print(f"    [创建room阶段]")
        clock_rooms = []  # 记录这个Clock内创建的所有room

        for nurse_name in nurse_list:
            # 检查该护士的状态
            nurse_info = coordinator.nurse_agents[nurse_name]
            if nurse_info["status"] != "idle":
                print(f"      护士 {nurse_name} 忙碌，跳过")
                continue

            # 检查是否还有患者在队列中
            if not coordinator.has_patients_in_queue():
                print(f"      患者队列已空，护士 {nurse_name} 无患者可处理")
                break

            # 获取下一个患者
            patient_info = coordinator.get_next_patient()
            if not patient_info:
                break

            patient_id = patient_info['patient_id']
            patient_name = patient_info['patient_name']

            print(f"      ├─ 护士 {nurse_name} 创建room: 患者 {patient_id}")

            try:
                if mode == 'ai':
                    # 创建room（不运行）
                    room_data = self._create_triage_room(
                        state_manager,
                        sandbox_env,
                        patient_info,
                        nurse_name
                    )
                    clock_rooms.append(room_data)
                else:
                    # 无AI模式：占用护士，快速返回结果
                    if coordinator.nurse_agents[nurse_name]["status"] != "idle":
                        raise Exception(f"护士 {nurse_name} 忙碌中")

                    coordinator.acquire_nurse(nurse_name, patient_id)

                    result = {
                        "patient_id": patient_id,
                        "patient_name": patient_name,
                        "mode": "no_ai",
                        "target_department": patient_info['profile_data'].get('default_department', '心内科'),
                        "triage_note": "患者自行选择科室",
                        "status": "completed"
                    }
                    clock_rooms.append({
                        "result": result,
                        "room": None,
                        "nurse_name": nurse_name,
                        "patient_info": patient_info
                    })

            except Exception as e:
                print(f"        ✗ 创建room失败: {str(e)}")

                result = {
                    "patient_id": patient_id,
                    "patient_name": patient_name,
                    "status": "failed",
                    "error": str(e)
                }

                # 记录失败结果（不关联nurse_name，因为可能已经被释放）
                clock_rooms.append({
                    "result": result,
                    "room": None,
                    "nurse_name": None,
                    "patient_info": patient_info
                })

        # ========== 第二阶段：运行所有room ==========
        print(f"    [运行room阶段]")
        clock_results = []

        for room_data in clock_rooms:
            nurse_name = room_data.get("nurse_name")
            patient_info = room_data.get("patient_info")

            try:
                if room_data["room"] is None:
                    # 无AI模式，直接使用已生成的结果
                    result = room_data["result"]
                    target_department = result.get('target_department', '心内科')

                    # 分配医生并添加到等待队列
                    try:
                        doctors_in_dept = coordinator.department_mapping.get(target_department, [])
                        if doctors_in_dept:
                            # 查询医生工作负荷，选择最空闲的医生
                            workload_info = coordinator.get_doctor_workload_info()
                            dept_doctors_workload = [
                                d for d in workload_info
                                if d['doctor_id'] in doctors_in_dept
                            ]

                            if dept_doctors_workload:
                                # 选择等待患者最少的医生
                                best_doctor = dept_doctors_workload[0]
                                assigned_doctor_id = best_doctor['doctor_id']
                                assigned_doctor_name = best_doctor['doctor_name']

                                # 构建基本的分诊结果（非AI模式）
                                triage_result = {
                                    "patient_id": patient_info['patient_id'],
                                    "patient_name": patient_info['patient_name'],
                                    "mode": "no_ai",
                                    "chief_complaint": "自助挂号",
                                    "target_department": target_department,
                                    "urgency_level": "ROUTINE",
                                    "assigned_doctor": assigned_doctor_id,
                                    "assigned_doctor_name": assigned_doctor_name
                                }

                                # 添加患者到医生的等待队列（传递完整的分诊结果）
                                coordinator.add_patient_to_waiting_queue(
                                    assigned_doctor_id,
                                    patient_info['patient_name'],
                                    patient_info,
                                    insert_at_head=False,
                                    triage_result=triage_result
                                )

                                # 获取更新后的队列大小
                                doctor_queue_size = len(
                                    coordinator.doctor_status[assigned_doctor_id]['waiting_queue']
                                )

                                result['assigned_doctor'] = assigned_doctor_id
                                result['assigned_doctor_name'] = assigned_doctor_name
                                result['doctor_queue_size'] = doctor_queue_size

                                print(f"      ✓ 患者分配给医生 {assigned_doctor_id} (队列: {doctor_queue_size})")
                    except Exception as e:
                        print(f"      ⚠ 医生分配失败: {e}")

                    coordinator.mark_patient_triaged(patient_info['patient_id'], result)

                    # 调用OpenHIS预注册
                    self._call_openhis_preregistration(
                        patient_info, result, state_manager, sandbox_env
                    )

                    clock_results.append(result)
                else:
                    # AI模式，运行对话室
                    room = room_data["room"]

                    print(f"      └─ 运行room: {room.room_id}")

                    # 运行对话
                    triage_decision = room.run_conversation(
                        opening_message=room_data["opening_message"],
                        conversation_type="triage",
                        coordinator=coordinator
                    )

                    # 补充patient_id和patient_name
                    triage_decision['patient_id'] = patient_info['patient_id']
                    triage_decision['patient_name'] = patient_info['patient_name']

                    # 处理分诊决策结果及紧急级别升级处置
                    target_department = triage_decision.get('target_department', '心内科')
                    urgency_level = triage_decision.get('urgency_level', 'ROUTINE')
                    routing_strategy = triage_decision.get('routing_strategy', 'NORMAL')
                    chief_complaint = triage_decision.get('chief_complaint', '未记录')

                    print(f"        📋 分诊结果: {chief_complaint} / {urgency_level}")

                    # 根据紧急级别和科室分配医生（关键改动：实际分配医生）
                    assigned_doctor_id = None
                    assigned_doctor_name = "未分配"
                    doctor_waiting_size = 0

                    try:
                        # 获取目标科室的医生
                        doctors_in_dept = coordinator.department_mapping.get(target_department, [])
                        if doctors_in_dept:
                            # 查询医生工作负荷，选择最空闲的医生
                            workload_info = coordinator.get_doctor_workload_info()
                            dept_doctors_workload = [
                                d for d in workload_info
                                if d['doctor_id'] in doctors_in_dept
                            ]

                            if dept_doctors_workload:
                                # 选择等待患者最少的医生
                                best_doctor = dept_doctors_workload[0]  # 已按workload排序
                                assigned_doctor_id = best_doctor['doctor_id']
                                assigned_doctor_name = best_doctor['doctor_name']

                                # 是否作为紧急患者（插入队列首位）
                                insert_at_head = urgency_level in ['URGENT', 'CRITICAL']

                                # 构建分诊结果（包含主诉、症状等医生需要的完整信息）
                                triage_result = {
                                    "patient_id": patient_info['patient_id'],
                                    "patient_name": patient_info['patient_name'],
                                    "mode": "ai",
                                    "chief_complaint": chief_complaint,
                                    "onset_time": triage_decision.get('onset_time', '未记录'),
                                    "severity": triage_decision.get('severity', '未记录'),
                                    "target_department": target_department,
                                    "urgency_level": urgency_level,
                                    "red_flags": triage_decision.get('red_flags', False),
                                    "past_medical_history": triage_decision.get('past_medical_history', '无'),
                                    "current_medications": triage_decision.get('current_medications', '无'),
                                    "allergy_history": triage_decision.get('allergy_history', '无'),
                                    "assigned_doctor": assigned_doctor_id,
                                    "assigned_doctor_name": assigned_doctor_name
                                }

                                # 调用协调器方法：添加患者到医生的等待队列（传递完整分诊结果）
                                coordinator.add_patient_to_waiting_queue(
                                    assigned_doctor_id,
                                    patient_info['patient_name'],
                                    patient_info,
                                    insert_at_head=insert_at_head,
                                    triage_result=triage_result
                                )

                                # 获取更新后的队列大小
                                doctor_waiting_size = len(
                                    coordinator.doctor_status[assigned_doctor_id]['waiting_queue']
                                )

                                priority_label = "🚨 紧急" if insert_at_head else "常规"
                                print(f"        ✓ 患者分配给{priority_label}医生 {assigned_doctor_id} (队列: {doctor_waiting_size})")
                        else:
                            print(f"        ⚠ 科室 {target_department} 无可用医生")
                    except Exception as e:
                        print(f"        ⚠ 医生分配失败: {e}")

                    # 构建完整的分诊结果
                    full_result = {
                        "patient_id": patient_info['patient_id'],
                        "patient_name": patient_info['patient_name'],
                        "mode": "ai",
                        "routing_strategy": routing_strategy,
                        "chief_complaint": chief_complaint,
                        "onset_time": triage_decision.get('onset_time', '未记录'),
                        "location": triage_decision.get('location', '未记录'),
                        "severity": triage_decision.get('severity', '未记录'),
                        "red_flags": triage_decision.get('red_flags', False),
                        "past_medical_history": triage_decision.get('past_medical_history', '无'),
                        "current_medications": triage_decision.get('current_medications', '无'),
                        "allergy_history": triage_decision.get('allergy_history', '无'),
                        "target_department": target_department,
                        "urgency_level": urgency_level,
                        "triage_note": triage_decision.get('reason', '护士分诊完成'),
                        "assigned_doctor": assigned_doctor_id,
                        "assigned_doctor_name": assigned_doctor_name,
                        "doctor_queue_size": doctor_waiting_size,
                        "conversation_turns": len(room.messages) // 2,
                        "status": "completed",
                        "room_id": room.room_id,
                        "assigned_nurse": nurse_name
                    }

                    # 标记患者已分诊
                    coordinator.mark_patient_triaged(patient_info['patient_id'], full_result)

                    # 调用OpenHIS预注册
                    self._call_openhis_preregistration(
                        patient_info, full_result, state_manager, sandbox_env,
                        triage_decision=triage_decision
                    )

                    clock_results.append(full_result)

            except Exception as e:
                print(f"        ✗ 处理失败: {str(e)}")

                result = {
                    "patient_id": patient_info['patient_id'],
                    "patient_name": patient_info['patient_name'],
                    "status": "failed",
                    "error": str(e)
                }
                coordinator.mark_patient_triaged(patient_info['patient_id'], result)
                clock_results.append(result)

            finally:
                # 释放护士（无论成功还是失败，仅当nurse_name有效时释放）
                if nurse_name is not None and nurse_name in coordinator.nurse_agents:
                    # 只有当护士被占用时才释放（状态为busy_triaging）
                    if coordinator.nurse_agents[nurse_name]["status"] == "busy_triaging":
                        coordinator.release_nurse(nurse_name)

        print(f"    ✓ Clock内 {len(clock_results)} 个room运行完成")
        return clock_results

    def _create_triage_room(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_info: Dict[str, Any],
        nurse_name: str
    ) -> Dict[str, Any]:
        """
        创建分诊room（第一阶段：不运行）

        Returns:
            包含room和相关信息的字典
        """
        coordinator = sandbox_env.get_coordinator()
        patient_id = patient_info['patient_id']
        patient_name = patient_info['patient_name']
        profile_path = patient_info['profile_path']

        # 检查护士是否空闲
        if coordinator.nurse_agents[nurse_name]["status"] != "idle":
            raise Exception(f"护士 {nurse_name} 忙碌中")

        # 占用护士
        coordinator.acquire_nurse(nurse_name, patient_id)

        try:
            # 创建PatientAgent
            patient_agent = PatientAgent(profile_path=profile_path, model_config=sandbox_env.get_model_config("patient").to_dict())
            nurse_agent = coordinator.nurse_agents[nurse_name]["agent"]

            # 创建对话室
            room_id = coordinator._create_room(
                patient_agent=patient_agent,
                medical_agent=nurse_agent,
                max_round=self.config.get('max_rounds', 5)
            )
            room = coordinator.chat_rooms[room_id]

            # 问候语
            opening_message = f"你好，我是{patient_id}，即{patient_name}，我想来医院挂号。"

            # 返回room数据（包含运行所需的所有信息）
            return {
                "room": room,
                "patient_info": patient_info,
                "nurse_name": nurse_name,
                "opening_message": opening_message,
                "result": None
            }
        except Exception as e:
            # 创建room失败，立即释放护士
            coordinator.release_nurse(nurse_name)
            raise

    def _execute_ai_triage_for_queue(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        patient_info: Dict[str, Any],
        nurse_name: str = None
    ) -> Dict[str, Any]:
        """
        队列模式下的AI分诊 - 由指定的护士处理患者

        Args:
            nurse_name: 指定处理该患者的护士名称（由Clock循环指定）
        """
        coordinator = sandbox_env.get_coordinator()
        patient_id = patient_info['patient_id']
        patient_name = patient_info['patient_name']
        profile_path = patient_info['profile_path']

        # 如果未指定护士，自动选择（后向兼容）
        if nurse_name is None:
            nurse_name = coordinator._select_available_nurse()

        if not nurse_name:
            raise Exception("没有可用的护士（全部忙碌中）")

        # 检查该护士是否空闲
        if coordinator.nurse_agents[nurse_name]["status"] != "idle":
            raise Exception(f"护士 {nurse_name} 忙碌中")

        # 占用护士
        coordinator.acquire_nurse(nurse_name, patient_id)
        print(f"        ✓ 占用护士: {nurse_name}")

        try:
            # 3. 创建PatientAgent
            patient_agent = PatientAgent(profile_path=profile_path, model_config=sandbox_env.get_model_config("patient").to_dict())
            nurse_agent = coordinator.nurse_agents[nurse_name]["agent"]

            # 4. 创建对话室
            room_id = coordinator._create_room(
                patient_agent=patient_agent,
                medical_agent=nurse_agent,
                max_round=self.config.get('max_rounds', 5)
            )
            room = coordinator.chat_rooms[room_id]

            # 5. 运行对话 - 问候语包含患者ID
            opening_message = f"你好，我是{patient_id}，即{patient_name}，我想来医院挂号。"
            triage_decision = room.run_conversation(
                opening_message=opening_message,
                conversation_type="triage",
                coordinator=coordinator
            )

            # 5.1 程序级别补充 patient_id 和 patient_name
            triage_decision['patient_id'] = patient_id
            triage_decision['patient_name'] = patient_name

            # 6. 处理分诊决策结果及紧急级别升级处置
            target_department = triage_decision.get('target_department', '心内科')
            urgency_level = triage_decision.get('urgency_level', 'ROUTINE')
            routing_strategy = triage_decision.get('routing_strategy', 'NORMAL')
            chief_complaint = triage_decision.get('chief_complaint', '未记录')

            print(f"  📋 分诊结果:")
            print(f"    - 主诉: {chief_complaint}")
            print(f"    - 紧急级别: {urgency_level}")
            print(f"    - 目标科室: {target_department}")
            print(f"    - 处理策略: {routing_strategy}")

            # 6.1 根据紧急级别处理
            if urgency_level == "CRITICAL":
                # 极紧急 - 转急诊，不排普通队列
                assigned_doctor_id = None
                assigned_doctor_name = "急诊"
                doctor_waiting_size = 0
                print(f"  🚨 【CRITICAL】患者需立即转急诊！")

            elif urgency_level == "URGENT":
                # 紧急 - 按升级策略处理
                if routing_strategy == "IMMEDIATE_IDLE":
                    # 排到最空闲医生
                    print(f"  ⚡【URGENT】排到最空闲医生...")
                    # 实现：coordinator应该优先返回最空闲医生
                    consultation_result = coordinator.assign_room(
                        patient_agent=patient_agent,
                        target_department=target_department,
                        conversation_type="consultation",
                        urgent=True,
                        strategy="idle"
                    )
                else:
                    # QUEUE_HEAD - 排到队列首位
                    print(f"  ⚡【URGENT】排到队列首位...")
                    consultation_result = coordinator.assign_room(
                        patient_agent=patient_agent,
                        target_department=target_department,
                        conversation_type="consultation",
                        urgent=True,
                        strategy="queue_head"
                    )

                if consultation_result:
                    assigned_doctor_id = consultation_result.get('medical_professional_id')
                    assigned_doctor_name = consultation_result.get('medical_professional_name')
                    doctor_waiting_size = consultation_result.get('waiting_queue_size', 0)
                    print(f"  ✓ 紧急患者已优先分配:")
                    print(f"    - 医生: {assigned_doctor_name}")
                    print(f"    - 队列位置: 优先")
                else:
                    assigned_doctor_id = None
                    assigned_doctor_name = "未分配"
                    doctor_waiting_size = 0
                    print(f"  ⚠️ 未能将紧急患者分配给医生")

            else:
                # 半紧急或常规 - 正常排队
                if target_department:
                    consultation_result = coordinator.assign_room(
                        patient_agent=patient_agent,
                        target_department=target_department,
                        conversation_type="consultation"
                    )

                    if consultation_result:
                        assigned_doctor_id = consultation_result.get('medical_professional_id')
                        assigned_doctor_name = consultation_result.get('medical_professional_name')
                        doctor_waiting_size = consultation_result.get('waiting_queue_size', 0)

                        print(f"  ✓ 患者已注册到医生队列:")
                        print(f"    - 医生: {assigned_doctor_name}")
                        print(f"    - 队列大小: {doctor_waiting_size}")
                    else:
                        assigned_doctor_id = None
                        assigned_doctor_name = "未分配"
                        doctor_waiting_size = 0
                        print(f"  ⚠️ 未能将患者分配给医生")
                else:
                    assigned_doctor_id = None
                    assigned_doctor_name = "未分配"
                    doctor_waiting_size = 0


            # 7. 构建结果（包含新增的分诊结构化字段）
            result = {
                "patient_id": patient_id,
                "patient_name": patient_name,
                "mode": "ai",
                "routing_strategy": routing_strategy,

                # 分诊信息结构化字段（对应benchmark要求）
                "chief_complaint": chief_complaint,
                "onset_time": triage_decision.get('onset_time', '未记录'),
                "location": triage_decision.get('location', '未记录'),
                "severity": triage_decision.get('severity', '未记录'),
                "red_flags": triage_decision.get('red_flags', False),
                "past_medical_history": triage_decision.get('past_medical_history', '无'),
                "current_medications": triage_decision.get('current_medications', '无'),
                "allergy_history": triage_decision.get('allergy_history', '无'),

                "target_department": target_department,
                "urgency_level": urgency_level,
                "triage_note": triage_decision.get('reason', '护士分诊完成'),
                "assigned_doctor": assigned_doctor_id,
                "assigned_doctor_name": assigned_doctor_name,
                "doctor_queue_size": doctor_waiting_size,
                "conversation_turns": len(room.messages) // 2,
                "status": "completed",
                "room_id": room.room_id,
                "assigned_nurse": nurse_name
            }

            print(f"  ✓ 分诊完成: {result['target_department']}")
            print(f"  ✓ 推荐医生: {result['assigned_doctor_name']}")

            return result

        finally:
            # 7. 释放护士（无论成功还是失败）
            coordinator.release_nurse(nurse_name)
            print(f"  ✓ 释放护士: {nurse_name}")


    def _execute_no_ai_triage(
        self,
        state_manager: StateManager,
        input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        无AI分诊模式 - 患者直接指定科室

        这种模式用于基线实验，患者自行判断应该挂什么科
        """
        print(f"\n  【无AI分诊模式】")

        # 从input_data中获取患者信息和指定科室
        patient_profile = input_data.get('patient_profile', {})
        target_department = input_data.get('target_department')

        if not target_department:
            # 如果没有指定，则使用患者profile中的default_department
            target_department = patient_profile.get('default_department', '心内科')

        result = {
            "mode": "no_ai",
            "patient_id": patient_profile.get('patient_id', 'unknown'),
            "target_department": target_department,
            "urgency_level": input_data.get('urgency_level', 'routine'),
            "triage_note": f"患者自行判断选择{target_department}",
            "assigned_doctor": None,  # 后续stage填充
            "conversation_turns": 0,
            "status": "completed"
        }

        state_manager.update_patient_info({
            "patient_id": patient_profile.get('patient_id'),
            "target_department": target_department,
            "urgency_level": result['urgency_level']
        })

        # 分诊完成后，调用OpenHIS预注册
        if HAS_OPENHIS and result['status'] == 'completed':
            try:
                adapter = state_manager.sandbox_env.get_openhis_adapter()
                if adapter:
                    print(f"  → 调用OpenHIS预注册...")
                    patient_id = result['patient_id']
                    triage_result = {
                        'target_department': result['target_department'],
                        'urgency_level': result['urgency_level'],
                        'chief_complaint': input_data.get('chief_complaint', '未记录'),
                        'triage_note': result['triage_note']
                    }

                    success, preview_id, response = adapter.create_preregistration(
                        local_patient_id=patient_id,
                        triage_result=triage_result,
                        state=state_manager
                    )

                    if success:
                        print(f"  ✓ 预注册成功")
                        print(f"    - 账单号(chrgBchno): {preview_id}")
                        if response:
                            bus_no = response.get('busNo')
                            encounter_no = response.get('encounterNo')
                            encounter_id = response.get('encounterId')
                            if bus_no:
                                print(f"    - 业务号(busNo): {bus_no}")
                            if encounter_no:
                                print(f"    - 就诊号(encounterNo): {encounter_no}")
                            if encounter_id:
                                print(f"    - 就诊ID(encounterId): {encounter_id}")
                        result['openhis_preregistration_id'] = preview_id
                        state_manager.set_openhis_metadata('preregistration_id', preview_id)
                        state_manager.set_openhis_metadata('preregistration_status', 'created')
                    else:
                        print(f"  ⚠️ 预注册失败: {response}")
                        result['openhis_preregistration_error'] = str(response)
            except Exception as e:
                print(f"  ⚠️ OpenHIS调用异常: {str(e)}")
                result['openhis_error'] = str(e)

        return result

    def _execute_ai_triage(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        AI分诊模式 - 通过护士Agent进行智能分诊

        这种模式利用NurseAgent与患者交互获得最优科室分流
        """
        print(f"\n  【AI分诊模式】")

        patient_profile = input_data.get('patient_profile', {})
        patient_id = patient_profile.get('patient_id', 'unknown')
        patient_name = patient_profile.get('name', '患者')
        profile_path = patient_profile.get('profile_path', '')

        try:
            # Step 1: 创建PatientAgent
            print(f"\n  ① 创建患者Agent: {patient_name}")
            patient_agent = PatientAgent(profile_path=profile_path, model_config=sandbox_env.get_model_config("patient").to_dict())
            print(f"     ✓ 患者Agent创建成功")

            # Step 2: 从sandbox_env中获取coordinator或创建一个
            print(f"\n  ② 获取Coordinator和护士Agent")
            coordinator = sandbox_env.coordinator
            nurse_agent = sandbox_env.get_nurse_agent()

            if not coordinator:
                print(f"     ✗ Coordinator未初始化")
                return self._create_failed_result(patient_id, "Coordinator初始化失败")

            if not nurse_agent:
                print(f"     ✗ 护士Agent不可用")
                return self._create_failed_result(patient_id, "护士Agent不可用")

            print(f"     ✓ Coordinator: {len(coordinator.doctor_agents)} 医生")
            print(f"     ✓ 护士Agent: {nurse_agent.get_name()}")

            # Step 3: 创建对话室
            print(f"\n  ③ 创建护士-患者对话室")
            room_id = coordinator._create_room(
                patient_agent=patient_agent,
                medical_agent=nurse_agent,
                max_round=self.config.get('max_rounds', 5)
            )
            room = coordinator.chat_rooms[room_id]
            print(f"     ✓ 对话室创建: {room.room_id}")

            # Step 4: 运行护士-患者对话 - 问候语包含患者ID
            print(f"\n  ④ 开始护士-患者分诊对话")
            opening_message = f"你好，我是{patient_id}，即{patient_name}，我最近感到不舒服，想来医院挂号。"
            print(f"     【患者】: {opening_message}")

            triage_decision = room.run_conversation(
                opening_message=opening_message,
                conversation_type="triage",
                coordinator=coordinator
            )

            # 4.1 程序级别补充 patient_id 和 patient_name
            triage_decision['patient_id'] = patient_id
            triage_decision['patient_name'] = patient_name

            print(f"\n  ⑤ 分诊对话完成")
            print(f"     对话轮数: {len(room.messages)}")
            print(f"     分诊决策: {json.dumps(triage_decision, ensure_ascii=False, indent=2)}")

            # Step 5.5: 如果分诊确定了科室，则将患者添加到医生队列（正式注册）
            target_department = triage_decision.get('target_department', '心内科')
            assigned_doctor_id = None
            assigned_doctor_name = None
            doctor_queue_size = 0

            if target_department:
                # 通过 assign_room 让系统自动选择医生并添加到队列
                consultation_result = coordinator.assign_room(
                    patient_agent=patient_agent,
                    target_department=target_department,
                    conversation_type="consultation"
                )

                if consultation_result:
                    assigned_doctor_id = consultation_result.get('medical_professional_id')
                    assigned_doctor_name = consultation_result.get('medical_professional_name')
                    doctor_queue_size = consultation_result.get('waiting_queue_size', 0)

                    print(f"  ✓ 患者已注册到医生队列:")
                    print(f"    - 医生: {assigned_doctor_name}")
                    print(f"    - 队列大小: {doctor_queue_size}")
                else:
                    print(f"  ⚠️ 未能将患者分配给医生")

            # Step 6: 构建结果
            result = {
                "mode": "ai",
                "patient_id": patient_id,
                "target_department": target_department,
                "urgency_level": triage_decision.get('urgency_level', 'routine'),
                "triage_note": triage_decision.get('reason', '护士分诊完成'),
                "assigned_doctor": assigned_doctor_id,
                "assigned_doctor_name": assigned_doctor_name,
                "doctor_queue_size": doctor_queue_size,
                "conversation_turns": len(room.messages) // 2,  # 除以2因为每轮两条消息
                "status": "completed",
                "room_id": room.room_id,
                "conversation_history": [
                    {
                        "speaker": msg["speaker"],
                        "content": msg["content"]
                    }
                    for msg in room.messages
                ]
            }

            # Step 7: 更新state_manager
            state_manager.update_patient_info({
                "patient_id": patient_id,
                "target_department": result['target_department'],
                "urgency_level": result['urgency_level'],
                "assigned_doctor_id": assigned_doctor_id
            })

            # Step 8: 分诊完成后，调用OpenHIS预注册
            print(f"\n  ⑥ 调用OpenHIS预注册...")
            self._call_openhis_preregistration(
                patient_info=input_data.get('patient_profile', {}),
                result=result,
                state_manager=state_manager,
                sandbox_env=sandbox_env,
                triage_decision=triage_decision
            )

            return result

        except Exception as e:
            print(f"\n  ✗ AI分诊异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return self._create_failed_result(patient_id, str(e))

    def _create_failed_result(self, patient_id: str, error_msg: str) -> Dict[str, Any]:
        """创建失败结果"""
        return {
            "mode": "ai",
            "patient_id": patient_id,
            "target_department": "未分流",
            "urgency_level": "unknown",
            "triage_note": f"分诊失败: {error_msg}",
            "assigned_doctor": None,
            "conversation_turns": 0,
            "status": "failed",
            "error": error_msg
        }

    def _call_openhis_preregistration(
        self,
        patient_info: Dict[str, Any],
        result: Dict[str, Any],
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        triage_decision: Dict[str, Any] = None
    ) -> None:
        """
        调用OpenHIS预注册

        Args:
            patient_info: 患者信息字典
            result: 分诊结果字典
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            triage_decision: 分诊决策（可选，用于AI分诊）
        """
        if not HAS_OPENHIS or result.get('status') != 'completed':
            return

        try:
            adapter = sandbox_env.get_openhis_adapter()
            if not adapter:
                return

            patient_id = result.get('patient_id', 'unknown')
            print(f"      → 调用OpenHIS预注册...")

            registration_context = {}
            if hasattr(adapter, "resolve_registration_context"):
                registration_context = adapter.resolve_registration_context(result)
                result.update(registration_context)
            if result.get("assigned_nurse"):
                result["triage_nurse_id"] = result.get("assigned_nurse")
                result["triage_nurse_department"] = (
                    result.get("openhis_department_name") or result.get("target_department")
                )
                if result.get("openhis_department_id"):
                    result["triage_nurse_department_id"] = result.get("openhis_department_id")

            # 构建分诊结果数据
            if triage_decision:
                # AI分诊情况
                triage_result = {
                    'target_department': result.get('target_department'),
                    'urgency_level': result.get('urgency_level', 'ROUTINE'),
                    'chief_complaint': triage_decision.get('chief_complaint', '未记录'),
                    'onset_time': triage_decision.get('onset_time', '未记录'),
                    'severity': triage_decision.get('severity', '未记录'),
                    'red_flags': triage_decision.get('red_flags', False),
                    'triage_note': result.get('triage_note'),
                    'conversation_turns': result.get('conversation_turns'),
                    'assigned_doctor': result.get('assigned_doctor'),
                    'assigned_nurse': result.get('assigned_nurse'),
                    **registration_context
                }
            else:
                # 无AI分诊
                triage_result = {
                    'target_department': result.get('target_department'),
                    'urgency_level': result.get('urgency_level', 'ROUTINE'),
                    'chief_complaint': patient_info.get('profile_data', {}).get('chief_complaint', '未记录'),
                    'triage_note': result.get('triage_note'),
                    'assigned_doctor': result.get('assigned_doctor'),
                    'assigned_nurse': result.get('assigned_nurse'),
                    **registration_context
                }

            success, preview_id, response = adapter.create_preregistration(
                local_patient_id=patient_id,
                triage_result=triage_result,
                state=state_manager
            )

            if success:
                print(f"        ✓ 预注册成功: {preview_id}")
                result['openhis_preregistration_id'] = preview_id
                state_manager.set_openhis_metadata('preregistration_id', preview_id)
                state_manager.set_openhis_metadata('preregistration_status', 'created')

                # ✅ 同步waiting_queue中的triage_result
                self.set_openhis_preregistration_id(
                    patient_id=patient_id,
                    assigned_doctor=result.get('assigned_doctor'),
                    preregistration_id=preview_id,
                    sandbox_env=sandbox_env,
                    registration_context={
                        **registration_context,
                        "openhis_preregistration_id": preview_id,
                        "triage_nurse_id": result.get("triage_nurse_id"),
                        "triage_nurse_department": result.get("triage_nurse_department"),
                        "triage_nurse_department_id": result.get("triage_nurse_department_id"),
                    }
                )
            else:
                print(f"        ⚠️ 预注册失败: {response}")
                result['openhis_preregistration_error'] = str(response)

        except Exception as e:
            print(f"        ⚠️ OpenHIS调用异常: {str(e)}")
            import traceback
            traceback.print_exc()
            result['openhis_error'] = str(e)

    def set_openhis_preregistration_id(
        self,
        patient_id: str,
        assigned_doctor: str,
        preregistration_id: str,
        sandbox_env: SandboxEnvironment,
        registration_context: Dict[str, Any] = None
    ) -> None:
        """
        更新coordinator中waiting_queue中患者的preregistration_id

        这个方法解决的问题：
        - 患者在add_patient_to_waiting_queue()时被添加到waiting_queue
        - 随后调用_call_openhis_preregistration()获得preview_id
        - 但waiting_queue中的record没有被更新
        - 这导致consultation后续无法获取preview_id

        Args:
            patient_id: 患者ID
            assigned_doctor: 分配的医生ID
            preregistration_id: OpenHIS预注册ID
            sandbox_env: 沙箱环境（用于获取coordinator）
        """
        try:
            coordinator = sandbox_env.get_coordinator()
            if not coordinator:
                return

            if assigned_doctor not in coordinator.doctor_status:
                print(f"        [警告] 医生{assigned_doctor}不在doctor_status中")
                return

            waiting_queue = coordinator.doctor_status[assigned_doctor]['waiting_queue']
            updated = False
            for patient_record in waiting_queue:
                if patient_record['patient_info'].get('patient_id') == patient_id:
                    # 更新triage_result中的OpenHIS预注册ID
                    if patient_record.get('triage_result') is None:
                        patient_record['triage_result'] = {}
                    if registration_context:
                        patient_record['triage_result'].update(registration_context)
                    patient_record['triage_result']['openhis_preregistration_id'] = preregistration_id
                    print(f"        ✓ 已同步waiting_queue中的preregistration_id: {preregistration_id}")
                    updated = True
                    break

            if not updated:
                print(f"        [警告] 未找到患者{patient_id}在医生{assigned_doctor}的waiting_queue中")

        except Exception as e:
            print(f"        [错误] 更新preregistration_id时异常: {str(e)}")
            import traceback
            traceback.print_exc()

    async def _execute_queue_triage_parallel(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        mode: str
    ) -> Dict[str, Any]:
        """
        并行分诊模式 - 基于asyncio的并发分诊

        特点：
        - 多个患者可以同时在不同护士处进行分诊
        - 适合可视化和实时性需求强的场景
        - 不依赖clock机制，每个患者处理是独立的asyncio Task

        这是对串行模式 _execute_queue_triage 的补充，
        不影响现有实现，可通过配置选择执行模式

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            mode: 分诊模式 ('no_ai' 或 'ai')

        Returns:
            所有分诊结果的列表
        """
        coordinator = sandbox_env.get_coordinator()

        if not coordinator.has_patients_in_queue():
            return []

        nurse_list = list(coordinator.nurse_agents.keys())
        if not nurse_list:
            return []

        print(f"    [并行分诊初始化]")
        print(f"    患者队列: {len(coordinator.patient_queue)} 人")
        print(f"    可用护士: {len(nurse_list)} 人")

        all_results = []
        active_tasks = {}  # 记录活跃任务
        nurse_availability = {nurse: True for nurse in nurse_list}  # 护士可用性跟踪

        async def process_patient_with_nurse():
            """异步处理患者 - 找到可用护士后执行分诊"""
            while coordinator.has_patients_in_queue():
                # 等待有可用护士
                available_nurses = [n for n in nurse_list if nurse_availability[n]]
                if not available_nurses:
                    await asyncio.sleep(0.5)  # 等待护士释放
                    continue

                nurse_name = available_nurses[0]
                nurse_availability[nurse_name] = False

                # 获取下一个患者
                patient_info = coordinator.get_next_patient()
                if not patient_info:
                    nurse_availability[nurse_name] = True
                    break

                patient_id = patient_info['patient_id']
                patient_name = patient_info['patient_name']

                try:
                    print(f"    👤 [{patient_name}] → 护士 {nurse_name} 开始分诊")

                    if mode == 'ai':
                        # AI分诊
                        result = self._execute_ai_triage_for_queue(
                            state_manager, sandbox_env, patient_info, nurse_name
                        )
                    else:
                        # 无AI分诊
                        if coordinator.nurse_agents[nurse_name]["status"] != "idle":
                            raise Exception(f"护士 {nurse_name} 忙碌中")

                        coordinator.acquire_nurse(nurse_name, patient_id)

                        result = {
                            "patient_id": patient_id,
                            "patient_name": patient_name,
                            "mode": "no_ai",
                            "target_department": patient_info.get('profile_data', {}).get('default_department', '心内科'),
                            "triage_note": "患者自行选择科室",
                            "status": "completed"
                        }

                        coordinator.release_nurse(nurse_name)

                    result['assigned_nurse'] = nurse_name
                    all_results.append(result)
                    print(f"    ✓ [{patient_name}] 分诊完成 → {result['target_department']}")

                except Exception as e:
                    print(f"    ✗ [{patient_name}] 分诊失败: {str(e)}")
                    all_results.append(self._create_failed_result(patient_id, str(e)))
                finally:
                    nurse_availability[nurse_name] = True

        # 启动多个并发任务
        num_concurrent = min(len(nurse_list), 3)  # 最多同时启动3个并发任务
        print(f"    [启动{num_concurrent}个并发处理任务]")

        tasks = [
            asyncio.create_task(process_patient_with_nurse())
            for _ in range(num_concurrent)
        ]

        # 等待所有任务完成
        await asyncio.gather(*tasks)

        print(f"    [并行分诊完成]")
        print(f"    已处理患者: {len(all_results)} 人")

        return all_results
