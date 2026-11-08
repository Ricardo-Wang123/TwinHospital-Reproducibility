

import json
from typing import Any, Optional, List
from jinja2 import Template
from camel.messages import BaseMessage

from .base_agent import BaseAgent
from prompts.doctor_prompt import DOCTOR_PROMPT_TEMPLATE

class DoctorAgent(BaseAgent):
    DOCTOR_PROMPT_TEMPLATE = DOCTOR_PROMPT_TEMPLATE


    def __init__(self, profile_path: str, tools: Optional[List[Any]] = None, model_config=None):
        super().__init__(profile_path=profile_path, tools=tools, model_config=model_config)

    def _create_system_message(self) -> BaseMessage:
        render_data = {
            "DOCTOR_PROFILE_JSON": json.dumps(self.profile_data, ensure_ascii=False, indent=2),
            "doc_name": self.profile_data.get("identity", {}).get("name"),
            "doc_id": self.profile_data.get("identity", {}).get("doctor_id"),
            "doc_dept": self.profile_data.get("identity", {}).get("department"),
            "doc_role": self.profile_data.get("identity", {}).get("role"),
            "doc_permissions": ", ".join(self.profile_data.get("permissions", [])) if isinstance(self.profile_data.get("permissions", []), list) else ""
        }
        template = Template(self.DOCTOR_PROMPT_TEMPLATE)
        content = template.render(**render_data)
        return BaseMessage.make_assistant_message(role_name="Doctor", content=content)

    def respond(self, message: str) -> str:
        """
        接收上游 message（可能为 JSON 或纯文本），解析后发送给模型并返回原始模型输出字符串。
        """
        if isinstance(message,dict):
            message = self.parse_json_response(message).get("reply", "")
        input_msg = BaseMessage.make_user_message(role_name="Patient", content=message)
        resp = self.agent.step(input_msg)
        return resp.msg.content

    def get_name(self) -> str:
        if not self.name:
            self.name = self.profile_data.get("identity", {}).get("name")
        return super().get_name()
