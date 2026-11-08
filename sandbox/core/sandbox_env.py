"""
沙箱环境 - 管理模型、API、工具等的注入和生命周期
"""

import os
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from sandbox.openhis import OpenHISAdapter


@dataclass
class ModelConfig:
    """模型配置"""
    model_type: str          # 如 "deepseek-chat", "gpt-4"
    api_key: str
    api_url: str = None
    temperature: float = 0.7
    max_tokens: int = 2000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_type": self.model_type,
            "api_key": self.api_key,
            "api_url": self.api_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


class SandboxEnvironment:
    """
    沙箱环境管理

    负责：
    - 管理模型配置（支持per-agent或全局）
    - 管理API密钥和URL
    - 管理工具配置
    - 管理外部资源（文件路径等）
    - 支持实验模式切换（如mock、live等）
    - 管理Coordinator和Agent实例
    """

    def __init__(self, config_dict: Dict[str, Any]):
        """
        初始化沙箱环境

        Args:
            config_dict: 配置字典，应包含model、paths等字段
        """
        self.config = config_dict
        self.models: Dict[str, ModelConfig] = {}
        self.experiment_mode = config_dict.get('experiment_mode', 'live')
        self.paths = config_dict.get('paths', {})

        # Agent系统实例
        self.coordinator = None
        self.nurse_agents: List[Any] = []
        self.doctor_profiles_dir = None
        self.patient_profiles = []
        self.evaluation_agent = None
        self.record_system = None

        # OpenHIS适配器（集成支持）
        self.openhis_adapter = None

        self._init_models()
        self._validate_environment()
        self._init_openhis_adapter()

    def _init_models(self) -> None:
        defaults = self.config.get("model", {})
        for role in ("default", "doctor", "nurse", "patient", "evaluation"):
            values = {**defaults, **self.config.get("models", {}).get(role, {})}
            self.models[role] = ModelConfig(
                model_type=values.get("model_type", "deepseek-chat"),
                api_key=values.get("api_key", ""),
                api_url=values.get("api_url", "https://api.deepseek.com/v1"),
                temperature=float(values.get("temperature", 0.7)),
                max_tokens=int(values.get("max_tokens", 2000)),
            )

    def _validate_environment(self) -> None:
        """验证沙箱环境配置"""
        # 在非mock模式下检查API密钥
        if self.experiment_mode != 'mock':
            for agent_type, model in self.models.items():
                if not model.api_key:
                    raise ValueError(
                        f"模型 {agent_type} 缺少 API_KEY，请在配置或环境变量中设置"
                    )

    def get_model_config(self, agent_type: str = None) -> ModelConfig:
        """
        获取模型配置

        Args:
            agent_type: agent类型（如'doctor', 'nurse'等），默认为'default'

        Returns:
            模型配置对象
        """
        key = agent_type or 'default'
        if key not in self.models:
            key = 'default'

        if key not in self.models:
            raise ValueError(f"未找到模型配置: {key}")

        return self.models[key]

    def get_all_models(self) -> Dict[str, ModelConfig]:
        """获取所有模型配置"""
        return self.models.copy()

    def get_path(self, path_key: str) -> str:
        """
        获取资源路径

        Args:
            path_key: 路径键

        Returns:
            路径字符串
        """
        return self.paths.get(path_key, '')

    def set_experiment_mode(self, mode: str) -> None:
        """
        设置实验模式

        Args:
            mode: 模式类型（'mock', 'live'等）
        """
        self.experiment_mode = mode
        if mode != 'mock':
            self._validate_environment()

    def is_mock_mode(self) -> bool:
        """检查是否为mock模式"""
        return self.experiment_mode == 'mock'

    def get_tool_config(self, tool_name: str) -> Dict[str, Any]:
        """
        获取工具配置

        Args:
            tool_name: 工具名称

        Returns:
            工具配置字典
        """
        tools_config = self.config.get('tools', {})
        return tools_config.get(tool_name, {})

    def should_enable_tool(self, tool_name: str) -> bool:
        """
        检查工具是否应该启用（用于消融实验）

        Args:
            tool_name: 工具名称（如'emr', 'exam', 'pubmed'等）

        Returns:
            是否启用
        """
        ablation = self.config.get('ablation', {})
        tool_disabled = ablation.get('disabled_tools', [])
        return tool_name not in tool_disabled and self.get_tool_config(tool_name).get("enabled", True)

    # ==================== OpenHIS 集成方法 ====================

    def _init_openhis_adapter(self) -> None:
        """
        初始化OpenHIS适配器（可选）

        如果配置中启用了OpenHIS集成，则创建并初始化适配器
        """
        if not OpenHISAdapter:
            return  # OpenHIS模块不可用

        openhis_config = self.config.get('openhis_integration', {})
        if not openhis_config.get('enabled', False):
            return  # 未启用OpenHIS集成

        try:
            # 构建适配器配置
            adapter_config = dict(openhis_config)
            adapter_config["persist_dir"] = self.paths.get("openhis_persist_dir", "data/openhis")

            # 【立即创建adapter（不带record_system）】
            # 这样可以在initialize_agents中加载档案
            # 后续会在initialize_agents中将record_system绑定到该adapter
            self.openhis_adapter = OpenHISAdapter(adapter_config, record_system=None)
            self._openhis_adapter_config = adapter_config  # 保存配置以备后续更新
            print(f"✓ OpenHIS适配器已初始化 (record_system将在initialize_agents中绑定)")

        except Exception as e:
            print(f"✗ OpenHIS适配器初始化失败: {e}")
            self.openhis_adapter = None
            if openhis_config.get('fail_on_init_error', True):
                raise

    def get_openhis_adapter(self):
        """
        获取OpenHIS适配器

        Returns:
            OpenHISAdapter实例或None
        """
        return getattr(self, 'openhis_adapter', None)

    # ==================== Agent系统管理 ====================

    def initialize_agents(self, config_loader: "ConfigLoader") -> None:
        """
        初始化Agent系统（Coordinator + 医生 + 护士）

        这是关键方法：根据配置文件中的resources参数，
        自动加载医生/护士profile并创建对应的Agent实例及Coordinator

        Args:
            config_loader: ConfigLoader实例，提供资源配置和profile路径
        """
        print("\n【初始化Agent系统】")

        try:
            # Step 1: 收集profile文件
            print("  ① 收集Agent profiles...")
            profiles = config_loader.collect_agent_profiles()

            doctor_profiles = profiles['doctors']
            nurse_configs = profiles['nurses']
            patient_profiles = profiles['patients']

            print(f"     ✓ 医生profiles: {len(doctor_profiles)}")
            print(f"     ✓ 护士: {len(nurse_configs)}")
            print(f"     ✓ 患者profiles: {len(patient_profiles)}")

            # ===== NEW Step 1.5: 初始化OpenHIS系统（自动加载本地档案）=====
            print("\n  ① OpenHIS: 初始化并加载本地档案...")
            if self.openhis_adapter and self.config.get("openhis_integration", {}).get("phases", {}).get("init", True):
                try:
                    doctor_profiles_dir = config_loader.resolve_path("doctor_profiles")
                    patient_profiles_dir = config_loader.resolve_path("patient_profiles")

                    # 调用OpenHIS适配器加载档案并初始化
                    profiles_list = self.openhis_adapter.load_profiles_from_directory(
                        doctor_profiles_dir=doctor_profiles_dir,
                        patient_profiles_dir=patient_profiles_dir,
                        doctor_paths=doctor_profiles,
                        patient_paths=patient_profiles,
                    )

                    if profiles_list:
                        success, errors, mappings = self.openhis_adapter.init_data_from_local_profiles(
                            profiles_list
                        )

                        if success:
                            print(f"     ✓ OpenHIS初始化成功: {len(mappings)} 个患者ID映射")
                        else:
                            if self.config.get("openhis_integration", {}).get("fail_on_init_error", True):
                                raise RuntimeError(f"OpenHIS 初始化失败: {len(errors)} 个错误")
                            print(f"     ⚠️ OpenHIS初始化有错误，但继续: {len(errors)} 个错误")
                            for error in errors[:3]:  # 只显示前3个错误
                                print(f"        - {error}")
                    else:
                        print(f"     ⚠️ 未加载到任何档案")

                except Exception as e:
                    if self.config.get("openhis_integration", {}).get("fail_on_init_error", True):
                        raise
                    print(f"     ⚠️ OpenHIS初始化失败（继续执行）: {e}")
            else:
                print(f"     ⊘ OpenHIS适配器未启用，跳过")

            # ===== END Step 1.5 =====

            # Step 2: 创建ToolManager
            print("\n  ② 创建ToolManager...")
            from tools.manager import ToolManager
            tool_manager = ToolManager()
            print(f"     ✓ ToolManager创建成功")

            # Step 2.5: 初始化EMR系统（医生记录工具）
            print("\n  ② 初始化EMR系统...")
            # 修复bug: 使用 resolve_path() 统一处理相对路径，确保YAML配置修改生效
            patient_records_dir = config_loader.resolve_path('patient_profiles', 'sandbox/data/patients')
            records_save_dir = config_loader.resolve_path('records_save_dir', 'data/records')

            os.makedirs(records_save_dir, exist_ok=True)

            # 【关键改进】改为传入患者记录文件夹（而不是单个文件）
            # MedicalRecordSystem会自动扫描文件夹中的所有.json文件并创建records
            record_system = tool_manager.get_record_system(
                patient_records_dir=patient_records_dir,  # ← 文件夹路径
                save_dir=records_save_dir
            )
            self.record_system = record_system
            print(f"     ✓ EMR系统初始化: 患者文件夹 = {patient_records_dir}")
            print(f"     ✓ EMR系统初始化: 已加载 {len(record_system.records)} 个患者records到内存")
            print(f"     ✓ 保存目录 = {records_save_dir}")

            # 获取医生所需的工具集：EMR工具 + 医疗检查工具 + 药房查询工具
            record_tools = tool_manager.get_record_tools(record_system) if self.should_enable_tool("emr") else []
            exam_tools = tool_manager.get_medical_exam_tools(patient_records_dir) if self.should_enable_tool("exam") else []
            pharmacy_slim_path = tool_manager.ensure_pharmacy_slim_file() if self.should_enable_tool("pharmacy") else "disabled"
            pharmacy_tools = tool_manager.get_pharmacy_tools() if self.should_enable_tool("pharmacy") else []
            doctor_tools = record_tools + exam_tools + pharmacy_tools
            print(
                f"     ✓ 医生工具集: {len(record_tools)} EMR工具 + "
                f"{len(exam_tools)} 检查工具 + {len(pharmacy_tools)} 药房工具 = "
                f"{len(doctor_tools)} 总工具"
            )
            print(f"     ✓ 精简药房表: {pharmacy_slim_path}")

            # 【关键】将record_system绑定到已有的adapter
            print("\n  ② 绑定record_system到OpenHIS适配器...")
            if self.openhis_adapter:
                try:
                    # 直接将record_system绑定到现有的adapter
                    self.openhis_adapter.record_system = record_system
                    print(f"     ✓ record_system已绑定到OpenHIS适配器")
                except Exception as e:
                    print(f"     ✗ record_system绑定失败: {e}")
                    import traceback
                    traceback.print_exc()

            # Step 3: 创建Coordinator（管理医生和护士）
            print("\n  ③ 创建Coordinator...")
            from agents.coordinator import Coordinator

            # 从doctor_profiles生成doctor info列表
            available_doctors = []
            for i, doctor_profile_path in enumerate(doctor_profiles):
                try:
                    with open(doctor_profile_path, 'r', encoding='utf-8') as f:
                        import json
                        doc_data = json.load(f)
                        doctor_info = {
                            'id': doc_data.get('identity', {}).get('doctor_id', f'DOC_{i}'),
                            'name': doc_data.get('identity', {}).get('name', f'医生{i+1}'),
                            'department': doc_data.get('identity', {}).get('department', '未知'),
                            'profile_path': doctor_profile_path
                        }
                        available_doctors.append(doctor_info)
                except Exception as e:
                    print(f"     ⚠️ 加载医生profile失败: {doctor_profile_path} - {e}")

            coordinator = Coordinator(
                available_doctors=available_doctors,
                tools=doctor_tools,  # ← 现在包含EMR工具
                doctor_profiles_dir=str(os.path.dirname(doctor_profiles[0])) if doctor_profiles else '',
                model_config=self.get_model_config('doctor').to_dict(),
            )
            if not coordinator.doctor_agents:
                raise ValueError("没有可用的医生智能体，请检查医生档案和模型配置")
            print(f"     ✓ Coordinator创建成功: {len(coordinator.doctor_agents)} 医生")

            # Step 4: 创建NurseAgents并添加到Coordinator
            print("\n  ④ 创建NurseAgents...")
            from agents.nurse_agent import NurseAgent

            nurse_agents = []
            departments_str = ",".join(coordinator.department_mapping.keys())

            for nurse_config in nurse_configs:
                try:
                    nurse = NurseAgent(
                        departments=departments_str,
                        profile_path=None,  # 护士没有profile文件
                        tools=tool_manager.get_doctor_query_tools(coordinator),
                        model_config=self.get_model_config("nurse").to_dict(),
                    )
                    nurse.name = nurse_config.get('nurse_id') or nurse_config.get('name') or nurse.get_name()
                    nurse_agents.append(nurse)
                    print(f"     ✓ 护士Agent创建: {nurse.get_name()}")
                except Exception as e:
                    print(f"     ✗ 护士Agent创建失败: {e}")

            if not nurse_agents:
                raise ValueError("没有可用的护士智能体，请检查模型配置")

            # 将护士添加到Coordinator
            if nurse_agents:
                coordinator.add_nurse(nurse_agents)
                print(f"     ✓ 护士已添加到Coordinator")

            # Evaluation is optional; only allocate its model when enabled.
            if config_loader.get_stage_config("evaluation").get("enabled", False):
                from agents.evaluation_agent import EvaluationAgent
                self.evaluation_agent = EvaluationAgent(model_config=self.get_model_config("evaluation").to_dict())

            # Step 6: 保存agents到SandboxEnvironment中
            self.set_coordinator(coordinator)
            self.set_nurse_agents(nurse_agents)
            self.set_doctor_profiles_dir(str(os.path.dirname(doctor_profiles[0])) if doctor_profiles else '')

            # Step 7: 保存patient profiles供后续使用
            self.patient_profiles = patient_profiles

            print(f"\n  ✓ Agent系统初始化完成")
            print(f"     - Coordinator: 管理 {len(coordinator.doctor_agents)} 医生")
            print(f"     - NurseAgents: {len(nurse_agents)} 护士")
            print(f"     - EvaluationAgent: {'已初始化' if self.evaluation_agent else '未初始化'}")
            print(f"     - 可用患者profiles: {len(patient_profiles)}")

        except Exception as e:
            print(f"\n  ✗ Agent系统初始化失败: {e}")
            import traceback
            traceback.print_exc()
            raise

    def get_patient_profiles(self) -> List[str]:
        """获取患者profiles列表"""
        return getattr(self, 'patient_profiles', [])

    def to_dict(self) -> Dict[str, Any]:
        """导出环境配置为字典"""
        return {
            "experiment_mode": self.experiment_mode,
            "models": {k: v.to_dict() for k, v in self.models.items()},
            "paths": self.paths,
            "agent_count": {
                "coordinator": 1 if self.coordinator else 0,
                "nurses": len(self.nurse_agents)
            }
        }

    def __repr__(self) -> str:
        return f"SandboxEnvironment(mode={self.experiment_mode}, models={list(self.models.keys())}, coordinator={'Yes' if self.coordinator else 'No'}, nurses={len(self.nurse_agents)})"

    def set_coordinator(self, coordinator: Any) -> None:
        """设置Coordinator实例"""
        self.coordinator = coordinator

    def get_coordinator(self) -> Optional[Any]:
        """获取Coordinator实例"""
        return self.coordinator

    def set_nurse_agents(self, agents: List[Any]) -> None:
        """设置护士Agents"""
        self.nurse_agents = agents

    def get_nurse_agents(self) -> List[Any]:
        """获取所有护士Agents"""
        return self.nurse_agents

    def get_nurse_agent(self, index: int = 0) -> Optional[Any]:
        """获取护士Agent（默认第一个）"""
        if index < len(self.nurse_agents):
            return self.nurse_agents[index]
        return None

    def set_doctor_profiles_dir(self, dir_path: str) -> None:
        """设置医生profiles目录"""
        self.doctor_profiles_dir = dir_path

    def get_doctor_profiles_dir(self) -> Optional[str]:
        """获取医生profiles目录"""
        return self.doctor_profiles_dir

    def set_evaluation_agent(self, agent: Any) -> None:
        """设置评估Agent"""
        self.evaluation_agent = agent

    def get_evaluation_agent(self) -> Optional[Any]:
        """获取评估Agent"""
        return self.evaluation_agent
