"""Role selection and CAMEL backend integration."""

import json
from copy import deepcopy

from agents.ai_tools import DocumentationTool, IntakeTool, PrescriptionTool
from agents.doctor_agent import DoctorAgent
from agents.patient_agent import PatientAgent
from agents.pharmacist_agent import PharmacistAgent
from ..visibility import assert_visible

_TOOLS = (IntakeTool, PrescriptionTool, DocumentationTool)
TOOL_ROLES = frozenset(tool.role for tool in _TOOLS)
_ROLE_TYPES = {agent.role: agent for agent in (PatientAgent, DoctorAgent, PharmacistAgent, *_TOOLS)}


def role_type(role):
    try:
        return _ROLE_TYPES[role]
    except KeyError:
        raise ValueError("unsupported clinical role") from None


def validate_role_route(role, model_family):
    role_type(role)
    if (role in TOOL_ROLES) != (model_family is not None):
        raise ValueError("Clinical agents use the role model and AI tools require a tool model.")


def system_prompt(role):
    return role_type(role).system_prompt()


class CamelRoleFactory:
    """CAMEL ChatAgent sessions over an externally provided budgeted backend.

    backend_factory(role, model_family) -> camel.models.BaseModelBackend.
    The backend, not this class, owns TLS, credentials, provenance gates,
    max_completion_tokens, retry accounting and the global RMB budget.
    """
    def __init__(self, backend_factory):
        self.backend_factory = backend_factory

    def session(self, case_id, role, model_family=None):
        validate_role_route(role, model_family)
        from camel.agents import ChatAgent
        from camel.messages import BaseMessage
        backend = self.backend_factory(role, model_family)
        agent = ChatAgent(system_message=BaseMessage.make_assistant_message(
            role_name=role, content=system_prompt(role)), model=backend)

        class Session:
            last_usage = None
            def respond(self, observation):
                assert_visible(observation)
                response = agent.step(BaseMessage.make_user_message(
                    role_name="workflow", content=json.dumps(observation, ensure_ascii=False, allow_nan=False)))
                self.last_usage = deepcopy((getattr(response, "info", None) or {}).get("usage"))
                text = response.msg.content
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("CAMEL response is not a JSON object")
                assert_visible(value)
                return value
        return Session()
