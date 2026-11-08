"""
评估 Agent 的提示词模板 - 仅包含 LLM 指标的提示词

只有以下两个指标使用 LLM 评估：
- prescription_rationality: 处方的医学合理性
- prescription_safety: 处方的安全性

其他指标使用直接计算（不需要提示词）：
- triage_accuracy: 直接比对分诊科室
- prescription_accuracy: 直接比对处方
- emr_completeness: 检查必填字段是否完整
- soap_coverage: 检查SOAP各元素是否覆盖
"""

# 系统提示词 - 定义评估 Agent 的角色
EVALUATION_SYSTEM_PROMPT = """你是一个医学质量评估专家，擅长评估医疗决策的质量。

你的职责是：
1. 根据医生的诊断和处方，评估这些决定的合理性和安全性
2. 对比患者的 ground truth（标准诊断和处方），判断医生的决定是否正确
3. 给出 0-1 之间的评分，0 表示完全不合理，1 表示完美匹配
4. 提供详细的推理说明

评估原则：
- 注重医学逻辑的严谨性
- 考虑患者的个体差异（年龄、体重、过敏史等）
- 遵循临床用药规范
- 评分应基于与 ground truth 的符合程度

对于所有评估任务，请返回结构化的 JSON 格式回复。"""

# 处方合理性评估提示词 - LLM 指标
PRESCRIPTION_RATIONALITY_PROMPT = """
请评估医生的处方的医学合理性。

患者信息：
- 诊断: {{ diagnosis }}
- 年龄/体重: {{ patient_demographics }}
- 过敏史: {{ allergies }}

医生开具的处方：
{{ prescribed_medications }}

标准处方（Ground Truth）：
{{ ground_truth_medications }}

请从以下维度评估处方的合理性：
1. 使用的药物种类和诊断是否匹配（是否有不合理的超出诊断范围的用药）
2. 药物用量是否符合临床规范（考虑常用临床剂量和患者个体特征）
3. 给药频率和疗程是否合理
4. 是否有潜在的药物相互作用
5. 是否存在过度医疗或检查不足的情况

请返回如下 JSON 格式的评估结果：
{
  "score": 0.0-1.0,
  "reasoning": "详细的医学合理性评估说明",
  "matches_diagnosis": true/false,
  "dosage_appropriateness": "用量评估（过高/适中/过低）",
  "frequency_appropriateness": "频率评估",
  "potential_drug_interactions": ["相互作用1", "相互作用2"],
  "concerns": ["问题1", "问题2"]
}
"""

# 处方安全性评估提示词 - LLM 指标
PRESCRIPTION_SAFETY_PROMPT = """
请评估医生的处方的安全性。

患者信息：
- 诊断: {{ diagnosis }}
- 年龄/体重: {{ patient_demographics }}
- 既往史: {{ past_medical_history }}
- 过敏史: {{ allergies }}
- 现用药物: {{ current_medications }}

医生开具的处方：
{{ prescribed_medications }}

标准处方（Ground Truth）：
{{ ground_truth_medications }}

请从以下安全性维度评估：
1. 是否有禁忌症（患者的过敏、既往史、肝肾功能等）
2. 是否有药物相互作用风险
3. 给药方式是否安全（是否有静脉炎、组织坏死等风险）
4. 是否有明显的剂量错误（可能导致毒性）
5. 是否需要监测指标或不良反应风险

请返回如下 JSON 格式的评估结果：
{
  "score": 0.0-1.0,
  "safety_assessment": "安全性总体评估",
  "contraindications": ["禁忌症1", "禁忌症2"],
  "drug_interactions": ["相互作用1", "相互作用2"],
  "safety_concerns": ["风险1", "风险2"],
  "monitoring_needed": ["监测指标1", "监测指标2"],
  "reasoning": "详细的安全性评估说明"
}
"""
