
import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from .base_agent import BaseAgent
from .doctor_agent import DoctorAgent

class ChatRoom:
    def __init__(self, room_id: str, patient_agent: BaseAgent, medical_agent: BaseAgent,  max_round: int = 5):
        self.room_id = room_id
        self.patient_agent = patient_agent
        self.medical_agent = medical_agent
        self.patient_agent_name = self._get_agent_name(patient_agent)
        self.medical_agent_name = self._get_agent_name(medical_agent)
        self.messages = []
        self.max_round = max_round
        self.created_at = datetime.now().isoformat()
        self.status = "active"

    def _get_agent_name(self, agent: Optional[BaseAgent]) -> str:
        if not agent:
            return "无"
        try:
            return agent.get_name()
        except Exception:
            return "Agent"

    def add_message(self, speaker: str, content: str) -> None:
        self.messages.append({
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "content": content
        })

    def get_conversation_history(self) -> List[Dict[str, str]]:
        return self.messages

    def close(self) -> None:
        self.status = "closed"

    def _doctor_response_is_end(self, answer: str) -> bool:
        """Read the doctor's JSON end flag without treating "false" as truthy."""
        try:
            parsed = self.medical_agent.parse_json_response(answer)
        except Exception:
            return False

        if not isinstance(parsed, dict):
            return False

        is_end = parsed.get("is_end", False)
        if isinstance(is_end, bool):
            return is_end
        if isinstance(is_end, str):
            return is_end.strip().lower() in {"true", "1", "yes", "y", "是"}
        return bool(is_end)

    def to_dict(self) -> Dict[str, Any]:
        """将房间信息序列化为字典，用于保存或展示"""
        return {
            "room_id": self.room_id,
            "patient": self.patient_agent_name,
            "doctor": self.medical_agent_name,
            "created_at": self.created_at,
            "status": self.status,
            "message_count": len(self.messages)
        }

    def run_conversation(
        self,
        opening_message: str = "医生我有点不舒服。",
        conversation_type: str = "consultation",
        coordinator: Any = None,
        triage_result: Dict[str, Any] = None
    ) -> Any:
        """
        在对话室中运行对话（简单循环实现）。
        conversation_type: 'triage' 或 'consultation'
        triage_result: (问诊模式) 来自分诊阶段的患者分诊信息
        返回：triage -> triage_decision dict; consultation -> reply / final json from doctor

        【关键】分诊信息通过第一条用户消息（opening_message）中包含，医生在step()时会接收到该信息
        """
        # 如果是问诊模式且有分诊信息，则在opening_message中注入分诊信息
        if conversation_type == "consultation" and triage_result:
            triage_info = json.dumps({
                "patient_id": triage_result.get('patient_id', '未知'),
                "chief_complaint": triage_result.get('chief_complaint', ''),
                "urgency_level": triage_result.get('urgency_level', 'ROUTINE'),
                "target_department": triage_result.get('target_department', ''),
                "onset_time": triage_result.get('onset_time', ''),
                "location": triage_result.get('location', ''),
                "severity": triage_result.get('severity', ''),
                "red_flags": triage_result.get('red_flags', False)
            }, ensure_ascii=False, indent=2)

            # 构建包含分诊信息的opening message
            opening_message = f"""【患者分诊信息】
{triage_info}

【患者主诉】
{opening_message}"""

            print(f"✓ 已为问诊注入分诊信息")

        # 病人发起开场
        self.add_message(self.patient_agent_name, opening_message)

        # 首次由 medical_agent 回应（护士或医生）

        answer = self.medical_agent.respond(opening_message)
        self.add_message(self.medical_agent_name, answer)
        print(f"👤 {self.medical_agent_name}: {answer}")

        for round_num in range(self.max_round):
            if self._doctor_response_is_end(answer):
                break
            print(f"\n--- 对话 第 {round_num + 1} 轮 ---")
            # 病人回应（patient_agent.respond 返回多种形式）
            patient_reply_raw = self.patient_agent.respond(answer)


            ## 调试输出工具调用历史
            print(f" {self.medical_agent_name} 工具调用历史: {getattr(self.medical_agent.agent, '_tool_output_history', [])}")


            if isinstance(patient_reply_raw, tuple):
                # (parsed, reply)
                patient_reply_text = patient_reply_raw[1]
            elif isinstance(patient_reply_raw, dict):
                patient_reply_text = patient_reply_raw.get("reply", "")
            else:
                patient_reply_text = str(patient_reply_raw)
                try:
                    parsed_patient_reply = self.patient_agent.parse_json_response(patient_reply_text)
                    if isinstance(parsed_patient_reply, dict) and parsed_patient_reply.get("reply"):
                        patient_reply_text = parsed_patient_reply["reply"]
                except Exception:
                    pass

            self.add_message(self.patient_agent_name, patient_reply_text)
            print(f"👤 {self.patient_agent_name}: {patient_reply_text}")

            if round_num == self.max_round - 1:
                patient_reply_text += "我已经没时间了医生（最后一轮对话），请在这一次对话中给出诊断并更新病历！"

            answer = self.medical_agent.respond(patient_reply_text)

            self.add_message(self.medical_agent_name, answer)
            print(f"👩‍⚕️ {self.medical_agent_name}: {answer}")

            # 解析是否结束
            if self._doctor_response_is_end(answer):
                print(f"\n 本轮对话结束")
                break

        if not self._doctor_response_is_end(answer):
            raise RuntimeError(f"{conversation_type} 达到 {self.max_round} 轮仍未完成，停止后续业务写入")

        # 返回结果（兼容 triage/consultation）
        try:
            parsed = self.medical_agent.parse_json_response(answer)
        except Exception:
            parsed = {}
        if conversation_type == 'triage':
            decision = parsed.get('triage_decision', {})
            if decision.get("status") != "routing_complete":
                raise RuntimeError("护士结束对话但未完成分诊")
            return decision

        # Consultation must preserve structured fields such as
        # prescription_reference and medication_list for downstream OpenHIS calls.
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            return {"reply": parsed}
        return {}

class Coordinator:
    """
    管理多医生、多护士场景的 Coordinator（基于源代码逻辑重构）。
    支持患者队列管理，替代原有的简单忙碌/空闲状态。
    """

    def __init__(self,
                #  nurse: List[BaseAgent],
                 available_doctors: List[Dict[str, Any]],
                 tools,
                 doctor_profiles_dir: str = "", model_config=None):
        """
        Args:
            nurse_agents: 护士Agent实例列表
            available_doctors: 可用医生列表（每项为 dict 包含 id, department, 等）
            tools: ToolManager 实例（如 None 则创建默认）
            doctor_profiles_dir: 医生 profiles 目录（默认指向原项目目录以复用配置）
        """
        self.tools = tools
        self.model_config = model_config
        self.nurse_agents = {}
        # for ag in nurse:
        #     self.nurse_agents[ag.get_name()] = {"agent": ag, "busy": False, "current_room_id": None}
        self.available_doctors = available_doctors
        self.doctor_profiles_dir = doctor_profiles_dir
        self.chat_rooms: Dict[str, ChatRoom] = {}
        self.doctor_agents: Dict[str, BaseAgent] = {}
        self.doctor_status: Dict[str, Dict[str, Any]] = {}
        self.department_mapping: Dict[str, List[str]] = {}
        self.registration_records: Dict[str, Dict[str, Any]] = {}  # 保存挂号记录

        # 患者队列管理
        self.patient_queue: List[Dict[str, Any]] = []  # 待分诊患者队列
        self.queue_index: int = 0  # 当前处理的患者索引
        self.triaged_patients: List[Dict[str, Any]] = []  # 已分诊患者列表

        # 护士轮转机制（Clock机制）
        self.nurse_round_index: int = 0  # 护士轮转指针

        self._init_doctor_agents()
        print(f"Coordinator 初始化: 管理 {len(self.doctor_agents)} 医生")

    def add_nurse(self, nurse_agents: List[BaseAgent]) -> None:
        """
        添加护士 Agent 到 Coordinator
        Args:
            nurse_agent: NurseAgent 实例
        """
        for ag in nurse_agents:
             self.nurse_agents[ag.get_name()] = {
                 "agent": ag,
                 "status": "idle",  # idle, busy_triaging
                 "current_patient_id": None,
                 "patients_triaged": [],
                 "room_id": None
             }
        # 重置轮转指针
        self.nurse_round_index = 0
        print(f"Coordinator 初始化: 管理 {len(self.doctor_agents)} 医生，{len(self.nurse_agents)} 护士")


    def _init_doctor_agents(self) -> None:
        """从 available_doctors 列表初始化 DoctorAgent"""
        for doctor_info in self.available_doctors:
            try:
                profile_path = doctor_info.get('profile_path')
                doctor_id = doctor_info.get('id')
                doctor_name = doctor_info.get('name', doctor_id)
                department = doctor_info.get('department', '未知科室')

                # 创建 DoctorAgent 实例，将 tools 由 tool_manager 提供
                doctor_agent = DoctorAgent(profile_path=profile_path, tools=self.tools, model_config=self.model_config)
                self.doctor_agents[doctor_id] = doctor_agent
                # 扩展医生状态结构：包含患者队列
                self.doctor_status[doctor_id] = {
                    "doctor_id": doctor_id,
                    "doctor_name": doctor_name,
                    "department": department,
                    "current_patient": None,  # 当前接待患者
                    "waiting_queue": [],  # 等待中的患者列表
                    "completed_patients": [],  # 已完成的患者列表
                    "room_id": None  # 当前对话室 ID（向后兼容）
                }
                self.department_mapping.setdefault(department, []).append(doctor_id)
                print(f"已初始化医生 Agent: {doctor_id} ({department})")
            except Exception as e:
                print(f"医生Agent初始化失败 {doctor_info.get('id')}: {e}")

        print(f"科室分布: {self.department_mapping}")

    def add_patient_to_waiting_queue(self, doctor_id: str, patient_name: str, patient_info: Dict[str, Any], insert_at_head: bool = False, triage_result: Dict[str, Any] = None) -> bool:
        """
        将患者添加到医生的等待队列
        Args:
            doctor_id: 医生 ID
            patient_name: 患者名称
            patient_info: 患者信息字典
            insert_at_head: 是否插入到队列首位（URGENT情况）
            triage_result: 分诊结果（包含主诉、紧急级别、症状等完整信息）
        Returns:
            是否成功添加
        """
        if doctor_id not in self.doctor_status:
            print(f"医生 {doctor_id} 不存在")
            return False

        patient_record = {
            "patient_name": patient_name,
            "patient_info": patient_info,
            "triage_result": triage_result or {},  # 分诊结果便于医生查阅
            "status": "waiting",  # waiting, in_consultation, completed
            "registration_time": datetime.now().isoformat(),
            "consultation_start_time": None,
            "consultation_end_time": None
        }

        if insert_at_head:
            # 紧急患者插入队列首位
            self.doctor_status[doctor_id]["waiting_queue"].insert(0, patient_record)
            print(f"🚨 患者 {patient_name} 已作为紧急患者插入 {doctor_id} 的队列首位")
        else:
            # 常规患者添加到队列末尾
            self.doctor_status[doctor_id]["waiting_queue"].append(patient_record)
            print(f"患者 {patient_name} 已加入 {doctor_id} 的等待队列")
        return True

    def start_consultation(self, doctor_id: str) -> Optional[Dict[str, Any]]:
        """
        开始为等待队列中的下一个患者进行咨询
        Returns:
            患者记录或 None
        """
        if doctor_id not in self.doctor_status:
            return None

        doctor_stat = self.doctor_status[doctor_id]
        if not doctor_stat["waiting_queue"]:
            return None

        # 从队列中取出第一个患者
        patient_record = doctor_stat["waiting_queue"].pop(0)
        patient_record["status"] = "in_consultation"
        patient_record["consultation_start_time"] = datetime.now().isoformat()

        doctor_stat["current_patient"] = patient_record
        return patient_record

    def complete_consultation(self, doctor_id: str, consultation_notes: str = "") -> bool:
        """
        完成当前患者的咨询
        Args:
            doctor_id: 医生 ID
            consultation_notes: 咨询记录
        Returns:
            是否成功完成
        """
        if doctor_id not in self.doctor_status:
            return False

        doctor_stat = self.doctor_status[doctor_id]
        if not doctor_stat["current_patient"]:
            return False

        patient_record = doctor_stat["current_patient"]
        patient_record["status"] = "completed"
        patient_record["consultation_end_time"] = datetime.now().isoformat()
        patient_record["consultation_notes"] = consultation_notes

        doctor_stat["completed_patients"].append(patient_record)
        doctor_stat["current_patient"] = None
        return True

    def get_doctor_workload_info(self, department: Optional[str] = None, sort_by_workload: bool = True) -> List[Dict[str, Any]]:
        """
        获取医生的忙碌情况信息
        Args:
            department: 科室过滤（可选）
            sort_by_workload: 是否按空闲程度排序（等待患者数最少的排在前）
        Returns:
            医生工作负荷列表（按空闲程度排序）
        """
        result = []

        for doctor_id, status in self.doctor_status.items():
            # 如果指定了科室，则进行过滤
            if department and status["department"] != department:
                continue

            waiting_count = len(status["waiting_queue"])
            has_current = status["current_patient"] is not None
            completed_count = len(status["completed_patients"])

            workload_info = {
                "doctor_id": doctor_id,
                "doctor_name": status["doctor_name"],
                "department": status["department"],
                "waiting_patients": waiting_count,
                "is_consulting": has_current,
                "current_patient": status["current_patient"]["patient_name"] if has_current else None,
                "completed_patients": completed_count,
                "workload_level": "空闲" if (waiting_count == 0 and not has_current) else (
                    "忙碌" if waiting_count > 3 else "一般"
                )
            }
            result.append(workload_info)

        # 按等待患者数排序（空闲程度）
        if sort_by_workload:
            result.sort(key=lambda x: (x["is_consulting"], x["waiting_patients"]))

        return result

    # ==================== 患者队列管理方法 ====================

    def init_patient_queue(self, patients_list: List[Dict[str, Any]]) -> None:
        """
        初始化患者队列（从run_experiment.py调用）
        Args:
            patients_list: 患者信息列表 [{patient_id, name, profile_path, ...}, ...]
        """
        self.patient_queue = []
        for i, patient_info in enumerate(patients_list):
            patient_record = {
                'queue_index': i,
                'patient_id': patient_info.get('patient_id', f'PAT_{i:03d}'),
                'patient_name': patient_info.get('name', f'患者_{i+1}'),
                'profile_path': patient_info.get('profile_path'),
                'profile_data': patient_info,
                'status': 'waiting',  # waiting, in_triage, triaged, in_consultation, completed
                'assigned_nurse': None,
                'assigned_doctor': None,
                'triage_decision': None,
                'arrival_time': datetime.now().isoformat()
            }
            self.patient_queue.append(patient_record)
        print(f"患者队列已初始化: {len(self.patient_queue)} 个患者")

    def has_patients_in_queue(self) -> bool:
        """检查是否还有待分诊患者"""
        return self.queue_index < len(self.patient_queue)

    def get_next_patient(self) -> Optional[Dict[str, Any]]:
        """
        从队列中获取下一个待分诊患者
        关键改变：返回患者后立即增加queue_index，确保下一个护士获取不同患者
        """
        if not self.has_patients_in_queue():
            return None

        patient = self.patient_queue[self.queue_index]
        patient['status'] = 'in_triage'

        # 立即增加索引，以确保下一个护士获取不同的患者
        self.queue_index += 1

        return patient

    def mark_patient_triaged(self, patient_id: str, triage_decision: Dict[str, Any]) -> bool:
        """
        标记患者已完成分诊
        关键改变：根据patient_id在整个队列中查找，而不是依赖queue_index
        """
        # 查找对应的患者记录
        patient_record = None
        patient_record_idx = None

        for idx, patient in enumerate(self.patient_queue):
            if patient['patient_id'] == patient_id:
                patient_record = patient
                patient_record_idx = idx
                break

        if patient_record is None:
            print(f"警告: 未找到患者 {patient_id}")
            return False

        # 标记为已分诊
        patient_record['status'] = 'triaged'
        patient_record['triage_decision'] = triage_decision
        patient_record['triage_time'] = datetime.now().isoformat()
        self.triaged_patients.append(patient_record)

        return True

    def _select_available_nurse(self) -> Optional[str]:
        """
        用Round-Robin策略选择护士（Clock机制）

        流程：
        1. 从当前轮转指针位置开始
        2. 循环查找第一个idle的护士
        3. 更新轮转指针到下一个护士
        4. 所有护士都busy时返回None
        """
        if not self.nurse_agents:
            return None

        nurse_list = list(self.nurse_agents.keys())
        nurse_count = len(nurse_list)

        if nurse_count == 0:
            return None

        # 从当前轮转位置开始轮转
        for i in range(nurse_count):
            # 计算要检查的护士索引
            check_index = (self.nurse_round_index + i) % nurse_count
            nurse_name = nurse_list[check_index]

            if self.nurse_agents[nurse_name]["status"] == "idle":
                # 找到空闲护士，更新轮转指针指向下一个
                self.nurse_round_index = (check_index + 1) % nurse_count
                print(f"    [CLOCK] 选择护士: {nurse_name} (轮转位置: {check_index})")
                return nurse_name

        # 所有护士都busy
        return None

    def acquire_nurse(self, nurse_id: str, patient_id: str) -> bool:
        """占用护士"""
        if nurse_id not in self.nurse_agents:
            return False
        self.nurse_agents[nurse_id]["status"] = "busy_triaging"
        self.nurse_agents[nurse_id]["current_patient_id"] = patient_id
        return True

    def release_nurse(self, nurse_id: str) -> bool:
        """释放护士"""
        if nurse_id not in self.nurse_agents:
            return False
        self.nurse_agents[nurse_id]["status"] = "idle"
        patient_id = self.nurse_agents[nurse_id]["current_patient_id"]
        if patient_id:
            self.nurse_agents[nurse_id]["patients_triaged"].append(patient_id)
        self.nurse_agents[nurse_id]["current_patient_id"] = None
        return True

    def _create_room(self, patient_agent: BaseAgent, medical_agent: BaseAgent, max_round: int = 10) -> str:
        """
        【私有方法】创建对话室，用于护士-患者或医生-患者对话
        Args:
            patient_agent: 患者Agent
            medical_agent: 医疗专业人士（护士或医生）
            nurse_name: 指定护士名称（若为None则自动选择可用护士）
            max_round: 最大对话轮数
        Returns:
            房间ID
        """
        medical_agent = medical_agent

        room_id = f"ROOM_{len(self.chat_rooms) + 1}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        chat_room = ChatRoom(room_id=room_id, patient_agent=patient_agent, medical_agent=medical_agent,max_round=max_round)
        self.chat_rooms[room_id] = chat_room
        # 尽量安全调用 get_name（某些占位 agent 可能无 profile）
        try:
            medical_name = medical_agent.get_name()
        except Exception:
            medical_name = "医生"
        print(f"创建对话室 {room_id}: 病人 {patient_agent.get_name()} -> 医疗人员 {medical_name}")
        return room_id

    def assign_room(self, patient_agent: BaseAgent, target_department: str, conversation_type: str = "consultation", urgent: bool = False, strategy: str = "idle") -> Optional[Dict[str, Any]]:
        """
        创建对话室并分配医疗专业人士（统一入口）

        Args:
            patient_agent: 患者Agent
            target_department: 目标科室
            conversation_type: 对话类型
                - "triage": 护士-患者分诊对话
                - "consultation": 医生-患者咨询对话（默认）
            urgent: 是否紧急（URGENT级别）
            strategy: 分配策略（"idle" 或 "queue_head"）

        Returns:
            包含房间信息的字典，包括:
            {
                "room_id": "房间ID",
                "conversation_type": "对话类型",
                "patient_name": "患者名称",
                "medical_professional_id": "医疗专业人士ID",
                "medical_professional_name": "医疗专业人士名称",
                "department": "科室",
                "waiting_queue_size": "等待队列大小（仅consultation类型有）"
            }
            或 None（失败）
        """

        if conversation_type == "triage":
            # 护士-患者分诊对话
            return self._assign_nurse_room(patient_agent, target_department)
        elif conversation_type == "consultation":
            # 医生-患者咨询对话
            return self._assign_doctor_room(patient_agent, target_department, urgent=urgent, strategy=strategy)
        else:
            print(f"未知的对话类型: {conversation_type}")
            return None

    def _assign_nurse_room(self, patient_agent: BaseAgent, target_department: str) -> Optional[Dict[str, Any]]:
        """
        分配护士与患者的对话室（分诊）
        """
        # 选择可用的护士
        selected_nurse_name = self._select_available_nurse()
        if not selected_nurse_name:
            print("没有可用的护士")
            return None

        nurse_agent = self.nurse_agents[selected_nurse_name]["agent"]

        # 创建对话室
        room_id = self._create_room(patient_agent=patient_agent, medical_agent=nurse_agent)

        return {
            "room_id": room_id,
            "conversation_type": "triage",
            "patient_name": patient_agent.get_name(),
            "medical_professional_id": selected_nurse_name,
            "medical_professional_name": nurse_agent.get_name(),
            "department": target_department
        }

    def _assign_doctor_room(self, patient_agent: BaseAgent, target_department: str, urgent: bool = False, strategy: str = "idle") -> Optional[Dict[str, Any]]:
        """
        分配医生与患者的对话室（咨询），并将患者加入等待队列
        Args:
            patient_agent: 患者Agent
            target_department: 目标科室
            urgent: 是否紧急（URGENT级别）
            strategy: 分配策略 ("idle" = 最空闲医生, "queue_head" = 插入队列首位)
        """
        matched_doctor_id = None

        # 优先选出最空闲的医生（等待患者最少的）
        workload_info = self.get_doctor_workload_info(department=target_department, sort_by_workload=True)
        if workload_info:
            matched_doctor_id = workload_info[0]["doctor_id"]

        if not matched_doctor_id:
            # 尝试分配任意非忙碌医生
            candidates = [d for d, s in self.doctor_status.items() if not s.get("room_id")]
            if candidates:
                matched_doctor_id = candidates[0]

        if not matched_doctor_id:
            print(f"未找到可用医生 (科室: {target_department})")
            return None

        if matched_doctor_id not in self.doctor_agents:
            print(f"医生 Agent 未初始化: {matched_doctor_id}")
            return None

        doctor_agent = self.doctor_agents[matched_doctor_id]

        # 创建房间
        room_id = self._create_room(patient_agent=patient_agent, medical_agent=doctor_agent)

        # 将患者加入医生的等待队列（立即更新医生患者列表）
        patient_name = patient_agent.get_name()
        try:
            patient_info = patient_agent.profile_data if hasattr(patient_agent, 'profile_data') else {}
        except:
            patient_info = {}

        # 根据紧急级别决定是否插入首位
        insert_at_head = urgent and strategy == "queue_head"
        self.add_patient_to_waiting_queue(matched_doctor_id, patient_name, patient_info, insert_at_head=insert_at_head)

        if urgent:
            print(f"🚨 【URGENT】 患者 {patient_name} 已分配给 {self.doctor_status[matched_doctor_id]['doctor_name']} (策略: {strategy})")
        else:
            print(f"✓ 患者 {patient_name} 已立即加入 {self.doctor_status[matched_doctor_id]['doctor_name']} 的等待队列")

        # 更新 doctor_status room（向后兼容）
        self.doctor_status[matched_doctor_id]["room_id"] = room_id

        # 保存挂号记录
        registration_record = {
            "patient_name": patient_name,
            "doctor_id": matched_doctor_id,
            "doctor_name": self.doctor_status[matched_doctor_id]["doctor_name"],
            "department": target_department,
            "registration_time": datetime.now().isoformat(),
            "room_id": room_id
        }
        self.registration_records[room_id] = registration_record

        # 返回统一格式的字典
        return {
            "room_id": room_id,
            "conversation_type": "consultation",
            "patient_name": patient_name,
            "medical_professional_id": matched_doctor_id,
            "medical_professional_name": self.doctor_status[matched_doctor_id]["doctor_name"],
            "department": target_department,
            "waiting_queue_size": len(self.doctor_status[matched_doctor_id]["waiting_queue"])
        }

    def exit_room(self, room_id: str) -> None:
        """关闭房间并释放相关医护状态"""
        room = self.chat_rooms.get(room_id)
        if not room:
            return

        # 释放医生
        for doc_id, st in self.doctor_status.items():
            if st.get("room_id") == room_id:
                st["busy"] = False
                st["room_id"] = None
                print(f"医生 {doc_id} 已空闲")

        # 释放护士
        if room.nurse_agent:
            nurse_name = room.nurse_agent.get_name()
            if nurse_name in self.nurse_agents:
                self.nurse_agents[nurse_name]["busy"] = False
                self.nurse_agents[nurse_name]["current_room_id"] = None
                print(f"护士 {nurse_name} 已空闲")

        room.close()
        print(f"房间 {room_id} 已关闭")

    def save_registration_summary(self, save_dir: str) -> str:
        """
        保存所有患者的挂号和分配情况总结
        Args:
            save_dir: 保存目录
        Returns:
            保存文件路径
        """
        os.makedirs(save_dir, exist_ok=True)

        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_registrations": len(self.registration_records),
            "doctor_workload_summary": {},
            "registration_records": list(self.registration_records.values()),
            "doctor_detailed_info": {}
        }

        # 添加医生详细信息
        for doctor_id, status in self.doctor_status.items():
            summary["doctor_detailed_info"][doctor_id] = {
                "doctor_name": status["doctor_name"],
                "department": status["department"],
                "total_consultations": len(status["completed_patients"]),
                "waiting_patients": len(status["waiting_queue"]),
                "current_patient": status["current_patient"]["patient_name"] if status["current_patient"] else None,
                "completed_patients": [p["patient_name"] for p in status["completed_patients"]],
                "waiting_queue": [p["patient_name"] for p in status["waiting_queue"]]
            }

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(save_dir, f"registration_summary_{ts}.json")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print(f"挂号总结已保存至: {file_path}")
        return file_path
