"""
Pipeline框架 - 支持多阶段串联和并行执行
"""

from typing import Dict, Any, List, Optional
from abc import ABC, abstractmethod
from enum import Enum

from .state_manager import StateManager
from .sandbox_env import SandboxEnvironment


class StageStatus(Enum):
    """Stage执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStage(ABC):
    """
    Pipeline stage基类

    所有stage都应继承此类并实现execute方法
    """

    def __init__(self, name: str, config: Dict[str, Any] = None):
        """
        初始化stage

        Args:
            name: stage名称
            config: stage配置字典
        """
        self.name = name
        self.config = config or {}
        self.status = StageStatus.PENDING
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None

    @abstractmethod
    def execute(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        input_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行stage

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            input_data: 输入数据（来自前一个stage的output）

        Returns:
            执行结果字典（会被后续stage作为input_data）
        """
        raise NotImplementedError

    def set_status(self, status: StageStatus) -> None:
        """设置stage状态"""
        self.status = status

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, status={self.status.value})"


class Pipeline:
    """
    Pipeline管理器

    负责：
    - 注册和管理stage
    - 按顺序执行stage
    - 处理stage间的通讯（通过state_manager）
    - 处理条件执行和分支
    """

    def __init__(self, name: str, config: Dict[str, Any] = None):
        """
        初始化Pipeline

        Args:
            name: pipeline名称
            config: pipeline配置
        """
        self.name = name
        self.config = config or {}
        self.stages: List[PipelineStage] = []
        self.stage_map: Dict[str, PipelineStage] = {}
        self.execution_order: List[str] = []

    def register_stage(self, stage: PipelineStage, order: int = None) -> None:
        """
        注册stage

        Args:
            stage: PipelineStage实例
            order: 执行顺序（如不指定则添加到末尾）
        """
        self.stage_map[stage.name] = stage

        if order is None:
            self.stages.append(stage)
            self.execution_order.append(stage.name)
        else:
            self.stages.insert(order, stage)
            self.execution_order.insert(order, stage.name)

    def skip_stage(self, stage_name: str) -> None:
        """
        跳过某个stage

        Args:
            stage_name: stage名称
        """
        if stage_name in self.stage_map:
            self.stage_map[stage_name].set_status(StageStatus.SKIPPED)
            self.execution_order.remove(stage_name)

    def execute(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        initial_input: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行整个pipeline

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            initial_input: 初始输入数据（第一个stage的输入）

        Returns:
            最终执行结果
        """
        results = {}
        previous_output = initial_input or {}

        for stage_name in self.execution_order:
            stage = self.stage_map[stage_name]

            print(f"\n{'='*80}")
            print(f"执行阶段: {stage_name}")
            print(f"{'='*80}")

            try:
                # 转移到新stage
                state_manager.transition_stage(stage_name)
                stage.set_status(StageStatus.RUNNING)

                # 执行stage
                output = stage.execute(
                    state_manager=state_manager,
                    sandbox_env=sandbox_env,
                    input_data=previous_output
                )

                # 记录结果
                stage.result = output
                stage.set_status(StageStatus.COMPLETED)
                state_manager.set_stage_output(stage_name, output)

                results[stage_name] = output
                previous_output = output

                print(f"✓ 阶段 {stage_name} 完成")

            except Exception as e:
                error_msg = f"Stage {stage_name} failed: {str(e)}"
                print(f"✗ {error_msg}")
                stage.error = str(e)
                stage.set_status(StageStatus.FAILED)
                state_manager.set_stage_error(stage_name, str(e))
                results[stage_name] = {"status": "failed", "error": str(e)}

                # 根据配置决定是否继续执行
                if self.config.get("stop_on_error", True):
                    raise RuntimeError(error_msg)

        return results

    def get_stage(self, stage_name: str) -> Optional[PipelineStage]:
        """获取指定stage"""
        return self.stage_map.get(stage_name)

    def get_execution_order(self) -> List[str]:
        """获取stage执行顺序"""
        return self.execution_order.copy()

    def get_results(self) -> Dict[str, Dict[str, Any]]:
        """获取所有stage的执行结果"""
        results = {}
        for stage_name in self.stages:
            stage = self.stage_map.get(stage_name.name)
            if stage:
                results[stage_name.name] = {
                    "status": stage.status.value,
                    "result": stage.result,
                    "error": stage.error
                }
        return results

    def to_dict(self) -> Dict[str, Any]:
        """导出pipeline配置为字典"""
        return {
            "name": self.name,
            "execution_order": self.execution_order,
            "stages": {
                name: {
                    "status": self.stage_map[name].status.value,
                    "config": self.stage_map[name].config
                }
                for name in self.execution_order
            }
        }

    def __repr__(self) -> str:
        return f"Pipeline(name={self.name}, stages={len(self.stages)}, order={self.execution_order})"
