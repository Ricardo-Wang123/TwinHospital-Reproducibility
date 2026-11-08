"""
状态管理器 - 管理模拟过程中的状态和stage间的JSON通讯
"""

import json
import copy
from typing import Dict, Any, Optional, List
from datetime import datetime


class StateManager:
    """
    状态管理器
    
    负责：
    - 维护当前状态（patient_state, workflow_state等）
    - 记录状态转移历史
    - 提供stage间JSON通讯接口
    - 状态验证和转换
    """
    
    def __init__(self, experiment_id: str):
        """
        初始化状态管理器
        
        Args:
            experiment_id: 实验ID（用于日志追踪）
        """
        self.experiment_id = experiment_id
        self.state: Dict[str, Any] = {
            "experiment_id": experiment_id,
            "created_at": datetime.now().isoformat(),
            "current_stage": None,
            "patient": {},
            "medical_record": {},
            "conversation_history": [],
            "stage_results": {},
            "metadata": {}
        }
        self.state_history: List[Dict[str, Any]] = []
        self._save_checkpoint()
    
    def _save_checkpoint(self) -> None:
        """保存当前状态快照到历史"""
        checkpoint = {
            "timestamp": datetime.now().isoformat(),
            "stage": self.state.get("current_stage"),
            "state": copy.deepcopy(self.state)
        }
        self.state_history.append(checkpoint)
    
    def transition_stage(self, new_stage: str) -> None:
        """
        转移到新stage
        
        Args:
            new_stage: 新阶段名称
        """
        self.state["current_stage"] = new_stage
        self.state["stage_results"][new_stage] = {
            "started_at": datetime.now().isoformat(),
            "status": "in_progress",
            "output": None
        }
        self._save_checkpoint()
    
    def update_patient_info(self, patient_info: Dict[str, Any]) -> None:
        """
        更新患者信息
        
        Args:
            patient_info: 患者信息字典
        """
        self.state["patient"].update(patient_info)
        self._save_checkpoint()
    
    def update_medical_record(self, record_updates: Dict[str, Any]) -> None:
        """
        更新医疗记录
        
        Args:
            record_updates: 记录更新字段
        """
        self.state["medical_record"].update(record_updates)
        self._save_checkpoint()
    
    def add_conversation_message(self, speaker: str, content: str, role: str = None) -> None:
        """
        添加对话消息
        
        Args:
            speaker: 说话者名称
            content: 消息内容
            role: 角色类型（如'patient', 'nurse', 'doctor'等）
        """
        message = {
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "role": role,
            "content": content
        }
        self.state["conversation_history"].append(message)
    
    def set_stage_output(self, stage_name: str, output: Dict[str, Any]) -> None:
        """
        设置stage的输出结果
        
        Args:
            stage_name: stage名称
            output: 输出结果（应为JSON可序列化的字典）
        """
        if stage_name not in self.state["stage_results"]:
            self.state["stage_results"][stage_name] = {}
        
        self.state["stage_results"][stage_name]["output"] = output
        self.state["stage_results"][stage_name]["completed_at"] = datetime.now().isoformat()
        self.state["stage_results"][stage_name]["status"] = "completed"
        self._save_checkpoint()
    
    def set_stage_error(self, stage_name: str, error: str) -> None:
        """
        记录stage的错误
        
        Args:
            stage_name: stage名称
            error: 错误信息
        """
        if stage_name not in self.state["stage_results"]:
            self.state["stage_results"][stage_name] = {}
        
        self.state["stage_results"][stage_name]["status"] = "failed"
        self.state["stage_results"][stage_name]["error"] = error
        self.state["stage_results"][stage_name]["failed_at"] = datetime.now().isoformat()
        self._save_checkpoint()
    
    def get_stage_input(self, source_stage: str) -> Dict[str, Any]:
        """
        获取上一个stage的输出作为当前stage的输入
        
        Args:
            source_stage: 源stage名称
            
        Returns:
            输入数据字典
        """
        output = self.state["stage_results"].get(source_stage, {}).get("output", {})
        return output or {}
    
    # ==================== OpenHIS 集成方法 ====================
    
    def set_openhis_metadata(self, key: str, value: Any) -> None:
        """
        设置OpenHIS元数据条目
        
        Args:
            key: 元数据键（如'preregistration_id', 'patient_openhis_id'等）
            value: 元数据值
        """
        if 'openhis_metadata' not in self.state:
            self.state['openhis_metadata'] = {}
        self.state['openhis_metadata'][key] = value
        self._save_checkpoint()
    
    def get_openhis_metadata(self, key: Optional[str] = None) -> Any:
        """
        获取OpenHIS元数据
        
        Args:
            key: 元数据键。如果为None，返回全部元数据字典
            
        Returns:
            元数据值或全部元数据字典
        """
        if 'openhis_metadata' not in self.state:
            return None if key else {}
        
        if key is None:
            return self.state['openhis_metadata'].copy()
        
        return self.state['openhis_metadata'].get(key)
    
    def update_openhis_metadata(self, updates: Dict[str, Any]) -> None:
        """
        批量更新OpenHIS元数据
        
        Args:
            updates: 要更新的元数据字典
        """
        if 'openhis_metadata' not in self.state:
            self.state['openhis_metadata'] = {}
        self.state['openhis_metadata'].update(updates)
        self._save_checkpoint()
    
    def get_current_state(self) -> Dict[str, Any]:
        """获取当前完整状态"""
        return copy.deepcopy(self.state)
    
    def get_state_json(self) -> str:
        """获取当前状态的JSON字符串"""
        return json.dumps(self.state, ensure_ascii=False, indent=2, default=str)
    
    def load_state_from_json(self, json_str: str) -> None:
        """
        从JSON字符串加载状态
        
        Args:
            json_str: JSON字符串
        """
        self.state = json.loads(json_str)
        self._save_checkpoint()
    
    def get_history(self) -> List[Dict[str, Any]]:
        """获取状态转移历史"""
        return self.state_history.copy()
    
    def export_to_file(self, filepath: str) -> None:
        """
        导出状态到文件
        
        Args:
            filepath: 输出文件路径
        """
        export_data = {
            "final_state": self.state,
            "history": self.state_history
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
    
    def __repr__(self) -> str:
        return f"StateManager(exp_id={self.experiment_id}, current_stage={self.state.get('current_stage')})"
