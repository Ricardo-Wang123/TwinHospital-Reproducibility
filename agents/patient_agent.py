

import json
import re
from typing import Dict, Any, Tuple
from jinja2 import Template

from .base_agent import BaseAgent
from camel.messages import BaseMessage
from prompts.patient_prompt import PATIENT_PROMPT_TEMPLATE as PATIENT_PROMPT
import logging

logger = logging.getLogger(__name__)

class PatientState:
    def __init__(self):
        self.anxiety_level = 0
        self.pain_level = 0
        self.secrets_revealed = []
        self.conversation_round = 0
        self.symptoms_disclosed = []
        self.medical_history_disclosed = False

    def update_anxiety(self, doctor_response: str) -> None:
        calming_keywords = ["放心", "别担心", "会好的", "没事", "很快", "治疗"]
        stress_keywords = ["严重", "手术", "住院", "危险", "并发症", "紧急"]
        self.anxiety_level += 0.5
        if any(keyword in doctor_response for keyword in calming_keywords):
            self.anxiety_level = max(0, self.anxiety_level - 1)
        if any(keyword in doctor_response for keyword in stress_keywords):
            self.anxiety_level = min(10, self.anxiety_level + 2)
        self.anxiety_level = max(0, min(10, self.anxiety_level))

    def update_pain(self) -> None:
        if self.pain_level < 8:
            self.pain_level += 0.3
        self.pain_level = max(0, min(10, self.pain_level))

    def add_secret(self, secret: str) -> None:
        if secret not in self.secrets_revealed:
            self.secrets_revealed.append(secret)

    def add_symptom(self, symptom: str) -> None:
        if symptom not in self.symptoms_disclosed:
            self.symptoms_disclosed.append(symptom)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anxiety_level": round(self.anxiety_level, 1),
            "pain_level": round(self.pain_level, 1),
            "secrets_revealed": self.secrets_revealed,
            "conversation_round": self.conversation_round,
            "symptoms_disclosed": self.symptoms_disclosed,
            "medical_history_disclosed": self.medical_history_disclosed
        }

class PatientAgent(BaseAgent):
    PATIENT_PROMPT_TEMPLATE = PATIENT_PROMPT

    def __init__(self, profile_path: str, model_config=None):
        self.state = PatientState()
        super().__init__(profile_path=profile_path, tools=None, model_config=model_config)
        logger.info("病人Agent初始化完成: %s", self.get_name())

    def _create_system_message(self) -> BaseMessage:
        identity = self.profile_data.get("identity", {})
        narrative_memory = self.profile_data.get("narrative_memory", {})
        diagnosis_ground_truth = self.profile_data.get("diagnosis_ground_truth", {})

        # 构建Context Data
        context_data = {
            "identity": identity,
            "narrative_memory": narrative_memory,
            "diagnosis_ground_truth": diagnosis_ground_truth
        }

        # 提取模板变量
        derived_attrs = identity.get("derived_attributes", {})

        # 渲染模板
        template = Template(self.PATIENT_PROMPT_TEMPLATE)
        prompt_content = template.render(
            PATIENT_PROFILE_JSON=json.dumps(context_data, ensure_ascii=False, indent=2),
            age_group=derived_attrs.get("age_group", "未知"),
            gender=derived_attrs.get("gender", "未知"),
            occupation_status=derived_attrs.get("occupation_status", "未知"),
            origin_city=derived_attrs.get("origin_city", "未知"),
            personal_history=narrative_memory.get("personal_history", ""),
            chief_complaint=narrative_memory.get("chief_complaint", ""),
            history_present_illness=narrative_memory.get("history_present_illness", ""),
            past_medical_history=narrative_memory.get("past_medical_history", ""),
            family_history=narrative_memory.get("family_history", ""),
            anxiety_level=self.state.anxiety_level,
            pain_level=self.state.pain_level,
            symptoms_disclosed=self.state.symptoms_disclosed,
            secrets_revealed=self.state.secrets_revealed,
            conversation_round=self.state.conversation_round,
            medical_history_disclosed=self.state.medical_history_disclosed,
            uuid=self.profile_data.get("agent_meta", {}).get("uuid", "未知")
        )
        return BaseMessage.make_assistant_message(role_name="Patient", content=prompt_content)

    def _update_state_from_response(self, response_content: str) -> None:
        symptom_keywords = ["痛", "疼", "肿", "胀", "晕", "心悸", "胸闷", "包块", "不舒服"]
        for keyword in symptom_keywords:
            if keyword in response_content and keyword not in self.state.symptoms_disclosed:
                self.state.add_symptom(keyword)
        history_keywords = ["高血压", "心脏病", "冠状动脉", "头晕", "心悸"]
        if any(keyword in response_content for keyword in history_keywords):
            self.state.medical_history_disclosed = True
        self.state.conversation_round += 1

    def _extract_visible_message(self, message: Any) -> str:
        """Return the actual clinician text even when it is not valid JSON."""
        if isinstance(message, dict):
            return str(message.get("reply") or message.get("content") or message)

        raw_message = str(message or "").strip()
        if not raw_message:
            return ""

        try:
            parsed = self.parse_json_response(raw_message)
            if isinstance(parsed, dict) and parsed.get("reply"):
                return str(parsed["reply"])
        except Exception:
            pass

        return raw_message.replace("```json", "").replace("```", "").strip()

    def respond(self, doctor_message: str) -> Tuple[Dict[str, Any], str]:
        doctor_message = self._extract_visible_message(doctor_message)
        self.state.update_anxiety(doctor_message)
        self.state.update_pain()
        input_message = BaseMessage.make_user_message(role_name="Doctor", content=doctor_message)
        response = self.agent.step(input_message)
        response_content = response.msg.content
        cleaned_content = self._clean_json_response(response_content)
        self._update_state_from_response(cleaned_content)
        return cleaned_content

    def _clean_json_response(self, content: str) -> str:
        content = content.replace("```json", "").replace("```", "")
        content = content.strip()
        import re
        json_pattern = r'\{.*\}'
        match = re.search(json_pattern, content, re.DOTALL)
        if match:
            return match.group(0)
        return content

    def get_patient_info(self) -> Dict[str, Any]:
        identity = self.profile_data.get("identity", {})
        raw_data = identity.get("raw_data", {})
        derived_attrs = identity.get("derived_attributes", {})
        agent_meta = self.profile_data.get("agent_meta", {})
        # Extract patient_id: try meta.patient_id first (new format), then agent_meta.uuid or identity fields (old format)
        patient_id = (
            self.profile_data.get('meta', {}).get('patient_id') or
            agent_meta.get("uuid") or
            raw_data.get("patient_id") or
            "未知"
        )
        return {
            "name": raw_data.get("name", "未知"),
            "patient_id": patient_id,
            "gender": raw_data.get("gender", derived_attrs.get("gender", "未知")),
            "age_group": derived_attrs.get("age_group", "未知"),
            "occupation_status": derived_attrs.get("occupation_status", "未知"),
            "origin_city": derived_attrs.get("origin_city", "未知")
        }

    def get_name(self) -> str:
        if not self.name:
            self.name = (
                self.profile_data.get("meta", {}).get("patient_id")
                or self.profile_data.get("agent_meta", {}).get("uuid")
                or self.profile_data.get("identity", {}).get("raw_data", {}).get("patient_id")
            )
        return super().get_name()

    def get_current_state(self) -> Dict[str, Any]:
        return self.state.to_dict()

    def reset(self) -> None:
        self.state = PatientState()
        self._init_chat_agent()
        print(f"🔄 病人Agent已重置: {self.get_name()}")
