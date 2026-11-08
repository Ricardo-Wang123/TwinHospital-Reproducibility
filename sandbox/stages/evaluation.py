"""
评测Stage - 对全流程进行评估和指标计算
"""

import json
from typing import Dict, Any, List


from sandbox.core.pipeline import PipelineStage
from sandbox.core.state_manager import StateManager
from sandbox.core.sandbox_env import SandboxEnvironment
from agents.evaluation_agent import EvaluationAgent


class EvaluationStage(PipelineStage):
    """
    评测阶段 - 对分诊、问诊、处方等进行质量评估

    评估指标：
    1. 分诊正确率（直接计算）：target_department 是否在 acceptable_departments 中
    2. 处方正确率（直接计算）：是否应该开处方
    3. 处方合理性（LLM评估）：涵盖所有必要药物、用量合理
    4. 处方安全性（LLM评估）：无过敏冲突、用法用量安全
    5. EMR完整性（直接计算）：关键字段填写完整度
    6. SOAP覆盖率（直接计算）：主观/客观/评估/计划各部分覆盖度
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__("evaluation", config)
        self.evaluation_agent = None

    def execute(
        self,
        state_manager: StateManager,
        sandbox_env: SandboxEnvironment,
        input_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行评测 - 评估分诊和问诊的质量

        Args:
            state_manager: 状态管理器
            sandbox_env: 沙箱环境
            input_data: 输入数据（来自分诊和问诊阶段的结果）

        Returns:
            评估结果列表
        """
        print(f"\n>>> 评测Stage开始执行")

        input_data = input_data or {}
        triage_results = input_data.get('triage_results', [])
        consultation_results = input_data.get('consultation_results', [])
        patient_profiles = sandbox_env.get_patient_profiles()

        if not triage_results or not consultation_results:
            print("    【无数据】缺少分诊或问诊数据")
            return []

        print(f"    【评估对象】{len(consultation_results)} 个患者")

        # 初始化评估 Agent
        if not self.evaluation_agent:
            try:
                self.evaluation_agent = sandbox_env.get_evaluation_agent() or EvaluationAgent(model_config=sandbox_env.get_model_config("evaluation").to_dict())
                print(f"    ✓ 评估Agent已初始化")
            except Exception as e:
                print(f"    ✗ 评估Agent初始化失败: {e}")
                return []

        # 执行评估
        evaluation_results = self._evaluate_all_patients(
            triage_results,
            consultation_results,
            patient_profiles,
            sandbox_env
        )

        print(f"    ✓ 评测完成: {len(evaluation_results)} 个患者已评估")

        return evaluation_results

    def _evaluate_all_patients(
        self,
        triage_results: List[Dict[str, Any]],
        consultation_results: List[Dict[str, Any]],
        patient_profiles: List[str],
        sandbox_env: SandboxEnvironment
    ) -> List[Dict[str, Any]]:
        """
        评估所有患者

        Returns:
            [{大概env初始化评估agent? Let me check config first
            "patient_id": "PAT_000001",
            "metrics": {
                "triage_accuracy": 1.0 or 0.0,
                "prescription_accuracy": 1.0 or 0.0,
                "prescription_rationality": 0.0-1.0,
                "prescription_safety": 0.0-1.0,
                "emr_completeness": 0.0-1.0,
                "soap_coverage": 0.0-1.0
            },
            "details": {...}
        }, ...]
        """
        evaluation_results = []

        # 加载所有患者的 ground truth 数据
        patient_ground_truth = self._load_patient_ground_truth(patient_profiles)

        # 构建咨询结果映射（patient_id -> consultation_result）
        consultation_map = {cr.get('patient_id'): cr for cr in consultation_results}

        # 逐患者进行评估
        for triage_result in triage_results:
            patient_id = triage_result.get('patient_id')
            consultation_result = consultation_map.get(patient_id)
            ground_truth = patient_ground_truth.get(patient_id)

            if not consultation_result:
                print(f"    ⚠️ 患者 {patient_id}: 缺少咨询结果")
                continue

            if not ground_truth:
                print(f"    ⚠️ 患者 {patient_id}: 缺少ground truth数据")
                continue

            try:
                evaluation = self._evaluate_single_patient(
                    patient_id,
                    triage_result,
                    consultation_result,
                    ground_truth
                )
                evaluation_results.append(evaluation)
            except Exception as e:
                print(f"    ✗ 患者 {patient_id} 评估失败: {e}")
                continue

        return evaluation_results

    def _load_patient_ground_truth(self, patient_profiles: List[str]) -> Dict[str, Dict]:
        """
        加载患者的 ground truth 数据

        Args:
            patient_profiles: 患者profile文件列表

        Returns:
            {patient_id: ground_truth_data}
        """
        ground_truth = {}

        for profile_path in patient_profiles:
            try:
                with open(profile_path, 'r', encoding='utf-8') as f:
                    profile = json.load(f)
                    # 提取 patient_id
                    patient_id = profile.get('agent_meta', {}).get('uuid')
                    if patient_id:
                        ground_truth[patient_id] = profile
            except Exception as e:
                print(f"    ⚠️ 加载profile失败 {profile_path}: {e}")
                continue

        return ground_truth

    def _evaluate_single_patient(
        self,
        patient_id: str,
        triage_result: Dict[str, Any],
        consultation_result: Dict[str, Any],
        ground_truth: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        评估单个患者

        Returns:
            {
                "patient_id": str,
                "metrics": {
                    "triage_accuracy": float,
                    "prescription_accuracy": float,
                    "prescription_rationality": float,
                    "prescription_safety": float,
                    "emr_completeness": float,
                    "soap_coverage": float,
                    "overall_score": float
                },
                "details": {...}
            }
        """
        metrics = {}
        details = {}

        # 1. 分诊正确率（直接计算）
        metrics['triage_accuracy'], details['triage'] = self._evaluate_triage_accuracy(
            triage_result,
            ground_truth
        )

        # 2. 处方正确率（直接计算）
        has_prescription = bool(consultation_result.get('assessment', {}).get('diagnosis'))  # 简化：如果有诊断就视为可能有处方
        metrics['prescription_accuracy'], details['prescription_accuracy_detail'] = \
            self._evaluate_prescription_accuracy(
                has_prescription,
                ground_truth
            )

        # 3. 处方合理性（LLM评估）
        prescribed_meds = consultation_result.get('plan', {}).get('prescription', [])
        eval_result = self.evaluation_agent.evaluate_prescription_rationality(
            prescribed_meds,
            ground_truth
        )
        metrics['prescription_rationality'] = eval_result.get('score', 0.5)
        details['prescription_rationality'] = eval_result

        # 4. 处方安全性（LLM评估）
        eval_result = self.evaluation_agent.evaluate_prescription_safety(
            prescribed_meds,
            ground_truth,
            ground_truth
        )
        metrics['prescription_safety'] = eval_result.get('score', 0.5)
        details['prescription_safety'] = eval_result

        # 5. EMR 完整性（直接计算）
        eval_result = self.evaluation_agent.evaluate_emr_completeness(
            consultation_result
        )
        metrics['emr_completeness'] = eval_result.get('score', 0.5)
        details['emr_completeness'] = eval_result

        # 6. SOAP 覆盖率（直接计算）
        eval_result = self.evaluation_agent.evaluate_soap_coverage(
            consultation_result
        )
        metrics['soap_coverage'] = eval_result.get('score', 0.5)
        details['soap_coverage'] = eval_result

        # 计算综合评分（平均值）
        all_scores = list(metrics.values())
        overall_score = sum(all_scores) / len(all_scores) if all_scores else 0.5

        return {
            "patient_id": patient_id,
            "metrics": metrics,
            "overall_score": overall_score,
            "details": details,
            "timestamp": consultation_result.get('timestamp', 'unknown')
        }

    def _evaluate_triage_accuracy(
        self,
        triage_result: Dict[str, Any],
        ground_truth: Dict[str, Any]
    ) -> tuple:
        """
        评估分诊正确率

        Returns:
            (score: 0 or 1, details: dict)
        """
        eval_result = self.evaluation_agent.evaluate_triage_accuracy(
            triage_result,
            ground_truth
        )

        return (
            eval_result.get('score', 0.0),
            {
                'assigned_department': eval_result.get('assigned_department'),
                'acceptable_departments': eval_result.get('acceptable_departments'),
                'correct': eval_result.get('correct'),
                'reasoning': f"分诊到{eval_result.get('assigned_department')}"
            }
        )

    def _evaluate_prescription_accuracy(
        self,
        prescription_given: bool,
        ground_truth: Dict[str, Any]
    ) -> tuple:
        """
        评估处方正确率

        Returns:
            (score: 0 or 1, details: dict)
        """
        eval_result = self.evaluation_agent.evaluate_prescription_accuracy(
            prescription_given,
            ground_truth
        )

        return (
            eval_result.get('score', 0.0),
            eval_result
        )
