import json
from typing import Any, Dict, List, Optional
from jinja2 import Template
from camel.messages import BaseMessage

from .base_agent import BaseAgent
from prompts.evaluation_prompt import (
    EVALUATION_SYSTEM_PROMPT,
    PRESCRIPTION_RATIONALITY_PROMPT,
    PRESCRIPTION_SAFETY_PROMPT
)


class EvaluationAgent(BaseAgent):
    """
    评估 Agent - 评估分诊、问诊的质量和指标

    职责：
    1. 计算直接计算指标：分诊正确率等
    2. 使用 LLM as Judge 评估：处方合理性、安全性、EMR 完整性、SOAP 覆盖等
    """

    # 系统提示词模板 - 与其他Agent保持一致的模式
    PROMPT_TEMPLATE = Template(EVALUATION_SYSTEM_PROMPT)

    def __init__(self, profile_path: Optional[str] = None, tools: Optional[List] = None, model_config=None):
        """
        初始化评估 Agent

        Args:
            profile_path: Agent profile 路径（可选，评估agent不需要但维持接口一致）
            tools: 工具列表（评估agent通常不需要但维持接口一致）
        """
        super().__init__(profile_path=profile_path, tools=tools or [], model_config=model_config)

    def _create_system_message(self) -> BaseMessage:
        """创建系统消息"""
        return BaseMessage.make_assistant_message(
            role_name="Evaluator",
            content=EVALUATION_SYSTEM_PROMPT
        )

    def respond(self, message: str) -> str:
        """处理输入消息并返回响应（实现BaseAgent的抽象方法）"""
        input_msg = BaseMessage.make_user_message(role_name="User", content=message)
        response = self.agent.step(input_msg)
        return response.msg.content

    def evaluate_triage_accuracy(self, triage_result: Dict[str, Any], ground_truth: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估分诊正确率

        Args:
            triage_result: 分诊结果（包含 target_department）
            ground_truth: ground truth 数据（包含 acceptable_departments）

        Returns:
            {
                "metric": "triage_accuracy",
                "correct": bool,
                "assigned_department": str,
                "acceptable_departments": list,
                "score": 0 or 1
            }
        """
        assigned_dept = triage_result.get("target_department")
        acceptable_depts = ground_truth.get("plan", {}).get("department_routing_reference", {}).get("acceptable_departments", [])

        is_correct = assigned_dept in acceptable_depts if acceptable_depts else False

        return {
            "metric": "triage_accuracy",
            "correct": is_correct,
            "assigned_department": assigned_dept,
            "acceptable_departments": acceptable_depts,
            "score": 1.0 if is_correct else 0.0
        }

    def evaluate_prescription_accuracy(self, prescription_given: bool, ground_truth: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估处方正确率（是否需要开处方）

        Args:
            prescription_given: 医生是否开具了处方
            ground_truth: ground truth 数据

        Returns:
            {
                "metric": "prescription_accuracy",
                "prescription_given": bool,
                "required": bool,
                "score": 0 or 1
            }
        """
        # 检查 ground truth 中是否需要处方
        prescription_ref = ground_truth.get("plan", {}).get("prescription_reference", {})
        prescription_required = prescription_ref.get("decision") == "medication"

        is_correct = prescription_given == prescription_required

        return {
            "metric": "prescription_accuracy",
            "prescription_given": prescription_given,
            "prescription_required": prescription_required,
            "score": 1.0 if is_correct else 0.0
        }

    def evaluate_prescription_rationality(
        self,
        prescribed_medications: List[Dict[str, Any]],
        ground_truth: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        使用 LLM 评估处方合理性：药物是否涵盖ground truth，用量是否合理

        Args:
            prescribed_medications: 医生开具的药物列表
            ground_truth: ground truth 数据

        Returns:
            {
                "metric": "prescription_rationality",
                "score": float (0-1),
                "reasoning": str
            }
        """
        if not prescribed_medications:
            return {
                "metric": "prescription_rationality",
                "score": 0.0,
                "reasoning": "未处方"
            }

        ground_truth_medications = ground_truth.get("plan", {}).get("prescription_reference", {}).get("medication", [])

        prompt = Template(PRESCRIPTION_RATIONALITY_PROMPT).render(
            prescribed_medications=json.dumps(prescribed_medications, ensure_ascii=False, indent=2),
            ground_truth_medications=json.dumps(ground_truth_medications, ensure_ascii=False, indent=2)
        )

        try:
            result_text = self.respond(prompt)

            # 尝试从响应中提取 score
            try:
                result_json = self.parse_json_response(result_text)
                score = result_json.get("score", 0.5)
                reasoning = result_json.get("reasoning", result_text)
            except:
                score = 0.5
                reasoning = result_text

            return {
                "metric": "prescription_rationality",
                "score": min(1.0, max(0.0, float(score))),
                "reasoning": reasoning
            }
        except Exception as e:
            return {
                "metric": "prescription_rationality",
                "score": 0.5,
                "reasoning": f"评估失败: {str(e)}"
            }

    def evaluate_prescription_safety(
        self,
        prescribed_medications: List[Dict[str, Any]],
        patient_profile: Dict[str, Any],
        ground_truth: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        使用 LLM 评估处方安全性：用量是否合理、是否与过敏冲突等

        Args:
            prescribed_medications: 医生开具的药物列表
            patient_profile: 患者信息（包含过敏史）
            ground_truth: ground truth 数据

        Returns:
            {
                "metric": "prescription_safety",
                "score": float (0-1),
                "reasoning": str,
                "concerns": list
            }
        """
        if not prescribed_medications:
            return {
                "metric": "prescription_safety",
                "score": 1.0,
                "reasoning": "未处方",
                "concerns": []
            }

        # 收集患者背景信息
        allergy_history = ground_truth.get("narrative_memory", {}).get("allergy_history", "无")
        past_medical_history = ground_truth.get("narrative_memory", {}).get("past_medical_history", [])
        diagnosis = ground_truth.get("diagnosis_ground_truth", {}).get("raw_text", {}).get("admission_diagnosis", "")

        # 提取年龄和性别进行 demographic 描述
        age = ground_truth.get("identity", {}).get("raw_data", {}).get("age", "")
        gender = ground_truth.get("identity", {}).get("raw_data", {}).get("gender", "")
        patient_demographics = f"{gender}{age}岁患者" if age and gender else "患者"

        prompt = Template(PRESCRIPTION_SAFETY_PROMPT).render(
            prescribed_medications=json.dumps(prescribed_medications, ensure_ascii=False, indent=2),
            ground_truth_medications=json.dumps(ground_truth.get("treatment_plan", {}).get("prescription_reference", {}), ensure_ascii=False, indent=2),
            diagnosis=diagnosis,
            patient_demographics=patient_demographics,
            past_medical_history=", ".join(past_medical_history) if isinstance(past_medical_history, list) else str(past_medical_history),
            allergies=allergy_history,
            current_medications="未知"
        )

        try:
            result_text = self.respond(prompt)

            try:
                result_json = self.parse_json_response(result_text)
                score = result_json.get("score", 0.5)
                reasoning = result_json.get("reasoning", "")
                concerns = result_json.get("concerns", [])
            except:
                score = 0.5
                reasoning = result_text
                concerns = []

            return {
                "metric": "prescription_safety",
                "score": min(1.0, max(0.0, float(score))),
                "reasoning": reasoning,
                "concerns": concerns
            }
        except Exception as e:
            return {
                "metric": "prescription_safety",
                "score": 0.5,
                "reasoning": f"评估失败: {str(e)}",
                "concerns": ["评估过程出错"]
            }

    def evaluate_emr_completeness(self, emr_record: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估 EMR 完整性 - 检查必填字段是否已填写

        Args:
            emr_record: 电子病历记录

        Returns:
            {
                "metric": "emr_completeness",
                "score": float (0-1),
                "filled_fields": list,
                "empty_fields": list,
                "reasoning": str
            }
        """
        required_fields = {
            "subjective": ["chief_complaint", "history_present_illness", "past_medical_history"],
            "objective": ["vital_signs", "physical_exam"],
            "assessment": ["diagnosis"],
            "plan": ["treatment", "education", "prescription"]
        }

        filled_fields = []
        empty_fields = []

        for section, fields in required_fields.items():
            section_data = emr_record.get(section, {})
            for field in fields:
                value = section_data.get(field)
                # 检查是否为空（空字符串、空列表、空字典等）
                if value and (
                    (isinstance(value, str) and value.strip()) or
                    (isinstance(value, list) and len(value) > 0) or
                    (isinstance(value, dict) and len(value) > 0)
                ):
                    filled_fields.append(f"{section}.{field}")
                else:
                    empty_fields.append(f"{section}.{field}")

        total_required = len(required_fields) * len(next(iter(required_fields.values())))
        filled_count = len(filled_fields)
        score = filled_count / total_required if total_required > 0 else 1.0

        return {
            "metric": "emr_completeness",
            "score": score,
            "filled_fields": filled_fields,
            "empty_fields": empty_fields,
            "total_required_fields": total_required,
            "filled_count": filled_count,
            "reasoning": f"填写了 {filled_count}/{total_required} 个必填字段"
        }

    def evaluate_soap_coverage(self, emr_record: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估 SOAP 元素覆盖率

        SOAP:
        - S (Subjective): 主诉、现病史、既往史
        - O (Objective): 体征、检查、化验
        - A (Assessment): 诊断、鉴别诊断
        - P (Plan): 治疗、教育、处方、转诊

        Args:
            emr_record: 电子病历记录

        Returns:
            {
                "metric": "soap_coverage",
                "score": float (0-1),
                "s_score": float,
                "o_score": float,
                "a_score": float,
                "p_score": float,
                "details": dict
            }
        """

        def has_content(value):
            return bool(value and (
                (isinstance(value, str) and value.strip()) or
                (isinstance(value, (list, dict)) and len(value) > 0)
            ))

        # S: Subjective
        s_elements = [
            has_content(emr_record.get("subjective", {}).get("chief_complaint")),
            has_content(emr_record.get("subjective", {}).get("history_present_illness")),
            has_content(emr_record.get("subjective", {}).get("past_medical_history"))
        ]
        s_score = sum(s_elements) / len(s_elements) if s_elements else 0

        # O: Objective
        o_elements = [
            has_content(emr_record.get("objective", {}).get("vital_signs")),
            has_content(emr_record.get("objective", {}).get("physical_exam")),
            has_content(emr_record.get("objective", {}).get("auxiliary_exam"))
        ]
        o_score = sum(o_elements) / len(o_elements) if o_elements else 0

        # A: Assessment
        a_elements = [
            has_content(emr_record.get("assessment", {}).get("diagnosis")),
            has_content(emr_record.get("assessment", {}).get("differential_diagnosis"))
        ]
        a_score = sum(a_elements) / len(a_elements) if a_elements else 0

        # P: Plan
        p_elements = [
            has_content(emr_record.get("plan", {}).get("treatment")),
            has_content(emr_record.get("plan", {}).get("education")),
            has_content(emr_record.get("plan", {}).get("prescription_reference"))
        ]
        p_score = sum(p_elements) / len(p_elements) if p_elements else 0

        overall_score = (s_score + o_score + a_score + p_score) / 4

        return {
            "metric": "soap_coverage",
            "score": overall_score,
            "s_score": s_score,
            "o_score": o_score,
            "a_score": a_score,
            "p_score": p_score,
            "details": {
                "subjective_elements": s_elements,
                "objective_elements": o_elements,
                "assessment_elements": a_elements,
                "plan_elements": p_elements
            }
        }
