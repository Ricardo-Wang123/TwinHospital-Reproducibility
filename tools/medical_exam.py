

import json
import os
from typing import Dict, Any, List, Union, Optional
from datetime import datetime

class MedicalExamTool:
    """医学检查工具类，支持多患者的检查执行和管理，按患者ID进行索引"""
    
    def __init__(self, patient_profile_paths: Union[str, List[str]] = None):
        """
        初始化医学检查工具
        
        Args:
            patient_profile_paths: 患者档案路径（单个路径或路径列表）
        """
        # 按patient_id索引的患者数据：{patient_id: {patient_data}}
        self.patient_data = {}
        
        # 按patient_id索引的检查历史：{patient_id: [exam_history]}
        self.exam_histories = {}
        
        # 向后兼容性：保留旧的单个患者数据
        self.patient_profile_path = None
        
        # 从患者档案加载数据
        if patient_profile_paths:
            self._load_patient_profiles(patient_profile_paths)
    
    def _load_patient_profiles(self, patient_profile_paths: Union[str, List[str]]) -> None:
        """加载患者档案数据"""
        paths = [patient_profile_paths] if isinstance(patient_profile_paths, str) else patient_profile_paths
        
        for path in paths:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    profile_data = json.load(f)
                    # 提取患者ID（从文件名或数据中）
                    patient_id = profile_data.get("agent_meta", {}).get("uuid")
                    self.patient_data[patient_id] = profile_data
                    self.exam_histories[patient_id] = []
                    if not self.patient_profile_path:
                        self.patient_profile_path = path
            except Exception as e:
                print(f"⚠️ 无法加载患者档案 {path}: {e}")
    
    def check_vital_signs(self, patient_id: str) -> str:
        """
        测量患者的生命体征
        
        Args:
            patient_id: 患者ID
        
        Returns:
            生命体征的文本描述，包含体温、脉搏、呼吸、血压等指标
        """
        # 获取患者数据
        patient = self.patient_data.get(patient_id, {})
        vital_signs = patient.get("body_ground_truth", {}).get("vital_signs", {})
        parsed = vital_signs.get("parsed_values", {})
        
        result = f"""
        📊 生命体征检查结果:
        - 体温: {parsed.get('temperature', 'N/A')} °C
        - 脉搏: {parsed.get('pulse', 'N/A')} 次/分
        - 呼吸: {parsed.get('respiration', 'N/A')} 次/分
        - 血压: {parsed.get('sbp', 'N/A')}/{parsed.get('dbp', 'N/A')} mmHg
        """.strip()
        
        # 记录检查历史（初始化患者历史记录如果不存在）
        if patient_id not in self.exam_histories:
            self.exam_histories[patient_id] = []
        
        self.exam_histories[patient_id].append({
            "timestamp": datetime.now().isoformat(),
            "type": "vital_signs",
            "result": result
        })
        
        return result
    
    def perform_physical_exam(self, patient_id: str, target: str) -> str:
        """
        对患者进行体格检查
        
        Args:
            patient_id: 患者ID
            target: 检查部位，可选值为 general, head, chest, lungs, heart, abdomen, limbs, inguinal, neurological
        
        Returns:
            体格检查的结果描述
        """
        # 获取患者数据
        patient = self.patient_data.get(patient_id, {})
        physical_exam = patient.get("body_ground_truth", {}).get("physical_exam", {})
        
        # 根据target部位返回相应的检查结果
        exam_results = {
            "general": physical_exam.get("general_appearance", "未检查"),
            "head": "头颈部检查: 头型正常，无明显异常",
            "chest": "胸部检查: 胸廓对称，无异常",
            "lungs": "肺部听诊: 呼吸音清晰，未闻及干湿性罗音",
            "heart": "心脏听诊: 心率规整，心音正常",
            "abdomen": "腹部触诊: 腹部平坦，软，无压痛",
            "limbs": "四肢检查: 活动正常，无异常",
            "inguinal": physical_exam.get("detailed_findings", "腹股沟检查: 无异常发现"),
            "neurological": "神经系统检查: 反射正常，无明显异常"
        }
        
        result = f"""
        🔬 体格检查结果 ({target}):
        {exam_results.get(target, "未知检查部位")}
        """.strip()
        
        # 记录检查历史（初始化患者历史记录如果不存在）
        if patient_id not in self.exam_histories:
            self.exam_histories[patient_id] = []
        
        self.exam_histories[patient_id].append({
            "timestamp": datetime.now().isoformat(),
            "type": "physical_exam",
            "target": target,
            "result": result
        })
        
        return result
    
    def check_patient_stability(self, patient_id: str) -> bool:
        """
        以一定的标准检查患者生命体征是否稳定
        
        Args:
            patient_id: 患者ID
        
        Returns:
            布尔值，表示患者当前生命特征是否稳定
        """
        # 获取患者数据和生命体征
        patient = self.patient_data.get(patient_id, {})
        vital_signs = patient.get("body_ground_truth", {}).get("vital_signs", {})
        parsed = vital_signs.get("parsed_values", {})
        
        # 评估稳定性标准
        temp = parsed.get('temperature', 37)
        pulse = parsed.get('pulse', 70)
        respiration = parsed.get('respiration', 18)
        sbp = parsed.get('sbp', 120)
        
        # 异常范围判断
        is_stable = (
            36 <= temp <= 38 and          # 体温在36-38°C之间
            60 <= pulse <= 100 and         # 脉搏在60-100次/分之间
            12 <= respiration <= 20 and    # 呼吸在12-20次/分之间
            90 <= sbp <= 180              # 收缩压在90-180 mmHg之间
        )
        
        stability_status = "✅ 患者稳定" if is_stable else "⚠️ 患者不稳定"
        
        # 记录检查历史（初始化患者历史记录如果不存在）
        if patient_id not in self.exam_histories:
            self.exam_histories[patient_id] = []
        
        self.exam_histories[patient_id].append({
            "timestamp": datetime.now().isoformat(),
            "type": "stability_check",
            "is_stable": is_stable,
            "status": stability_status
        })
        
        return is_stable
    
    def get_exam_history(self, patient_id: str) -> List[Dict[str, Any]]:
        """
        获取患者的检查历史记录
        
        Args:
            patient_id: 患者ID
        
        Returns:
            检查历史列表，包含时间戳、检查类型和检查结果
        """
        return self.exam_histories.get(patient_id, [])
    
    def get_all_patients(self) -> List[str]:
        """
        获取所有患者ID列表
        
        Returns:
            患者ID列表
        """
        return list(self.patient_data.keys())
    
    def get_patient_exam_count(self, patient_id: str) -> int:
        """
        获取特定患者的检查数量
        
        Args:
            patient_id: 患者ID
        
        Returns:
            检查数量
        """
        return len(self.exam_histories.get(patient_id, []))
    
    def clear_patient_exams(self, patient_id: str) -> bool:
        """
        清除特定患者的所有检查历史记录
        
        Args:
            patient_id: 患者ID
        
        Returns:
            是否清除成功
        """
        if patient_id in self.exam_histories:
            self.exam_histories[patient_id] = []
            return True
        return False
    
    def load_patient_profile(self, patient_id: str, patient_profile_path: str) -> bool:
        """
        加载新患者档案
        
        Args:
            patient_id: 患者ID
            patient_profile_path: 患者档案路径
        
        Returns:
            是否加载成功
        """
        try:
            with open(patient_profile_path, 'r', encoding='utf-8') as f:
                profile_data = json.load(f)
                self.patient_data[patient_id] = profile_data
                self.exam_histories[patient_id] = []
                return True
        except Exception as e:
            print(f"⚠️ 无法加载患者档案 {patient_profile_path}: {e}")
            return False

if __name__ == "__main__":
    test_path = "MedicalAgent\sandbox\data\patients\patient_000001.json"
    tool = MedicalExamTool(test_path)
    print(tool.check_vital_signs("PAT_000001"))