
import logging
from typing import Dict, Any, List, Optional
from jinja2 import Template
from camel.messages import BaseMessage
from prompts.nurse_prompt import NURSE_PROMPT

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

class NurseAgent(BaseAgent):
    NURSE_PROMPT_TEMPLATE = NURSE_PROMPT
    _next_id = 0

    def __init__(self, departments: str, profile_path: Optional[str] = None, tools: List[Any] = None, model_config=None):
        self.departments = departments
        super().__init__(profile_path=profile_path, tools=tools, model_config=model_config)
        NurseAgent._next_id += 1

    def _create_system_message(self) -> BaseMessage:
        template = Template(self.NURSE_PROMPT_TEMPLATE)
        content = template.render(departments=self.departments)
        return BaseMessage.make_assistant_message(role_name="Nurse", content=content)

    def respond(self, message: str) -> Dict[str, Any]:
        if isinstance(message,dict):
            message = self.parse_json_response(message).get("reply", "")
        input_msg = BaseMessage.make_user_message(role_name="Patient", content=message)
        response = self.agent.step(input_msg)
        return response.msg.content

    def get_name(self) -> str:
        if not self.name:
            self.name = f'{self.__class__.__name__}_{self.__class__._next_id}'
        return self.name
