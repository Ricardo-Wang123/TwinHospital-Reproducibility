import os
import json
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.types import ModelPlatformType
from camel.models import ModelFactory
from json_repair import repair_json


class BaseAgent:
    """
    抽象 Agent 基类

    子类应实现：
      - _create_system_message(self) -> BaseMessage
      - respond(self, message: str) -> str

    提供的公共功能：
      - profile 加载
      - 模型实例化（_setup_model）
      - ChatAgent 初始化（_init_chat_agent）
      - JSON 响应容错解析（parse_json_response）
      - get_name() 的默认实现
    """

    def __init__(self, profile_path: Optional[str] = None, tools: Optional[List[Any]] = None, model_config: Optional[Dict[str, Any]] = None):
        self.model_config = model_config or {}
        self.profile_path = profile_path
        self.profile_data: Dict[str, Any] = self._load_profile() if profile_path else {}
        self.tools: List[Any] = tools or []
        self.model = self._setup_model()
        # agent 会在子类的 _create_system_message 可用后初始化
        self._init_chat_agent()
        self.name = None
        self.get_name()

    def _load_profile(self) -> Dict[str, Any]:
        """加载 profile JSON；若路径为空则返回空字典"""
        with open(self.profile_path, "r", encoding="utf-8") as stream:
            profile = json.load(stream)
        if not isinstance(profile, dict):
            raise ValueError(f"档案必须是 JSON 对象: {self.profile_path}")
        return profile

    def _setup_model(self):
        """创建模型实例（可由子类覆盖以更改配置）"""
        return ModelFactory.create(
            model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
            model_type=self.model_config.get("model_type", os.getenv("MODEL_TYPE", "deepseek-chat")),
            api_key=self.model_config.get("api_key", os.getenv("API_KEY", "")),
            url=self.model_config.get("api_url", os.getenv("MODEL_BASE_URL", "https://api.deepseek.com/v1")),
            model_config_dict={
                "temperature": self.model_config.get("temperature", 0.7),
                "max_tokens": self.model_config.get("max_tokens", 2000),
            },
            timeout=60,
            max_retries=1,
        )

    def _create_system_message(self) -> BaseMessage:
        """子类必须实现：返回用于 ChatAgent 的系统消息"""
        raise NotImplementedError("_create_system_message must be implemented by subclasses")

    def _init_chat_agent(self) -> None:
        """基于系统消息、模型和可选工具初始化 ChatAgent"""
        system_message = self._create_system_message()

        if self.tools:
            self.agent = ChatAgent(system_message=system_message, model=self.model, tools=self.tools)
        else:
            self.agent = ChatAgent(system_message=system_message, model=self.model)

    def parse_json_response(self, content: Any) -> Dict[str, Any]:
        """Accept dictionaries or repair model JSON; always return an object."""
        if isinstance(content, dict):
            return content
        if not isinstance(content, str) or not content.strip():
            return {}
        try:
            value = repair_json(content, return_objects=True)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def get_name(self) -> str:
        if not self.name:
            count = len(dir(self.__class__))
            self.name = f'{self.__class__.__name__}_{count+1}'
        return self.name

    def respond(self, message: str) -> str:
        """子类必须实现：处理输入消息并返回响应"""
        raise NotImplementedError("respond must be implemented by subclasses")
