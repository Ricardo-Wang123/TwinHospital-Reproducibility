import json
import os
from datetime import datetime
from typing import Dict, Any, List, Optional, Union
from camel.toolkits import FunctionTool
from .pharmacy_inventory import PharmacyInventory

class MedicalRecordSystem:
    """
    电子病历(EMR)管理系统（支持多患者 - 沙箱模式）
    
    负责维护多个患者的结构化病历数据，每个患者以唯一 patient_id 为索引。
    
    初始化方式：
    - 传入患者记录文件夹路径（patient_records_dir）
    - 系統扫描文件夹中的所有.json文件，为每个患者创建record
    - 所有records加载到内存中，但不立即保存到磁盘
    - 为这次实验创建一个独立的时间戳文件夹（save_dir/experiment_timestamp/）
    - 医生对患者进行update_record()时，自动触发save到磁盘
    
    文件结构：
    save_dir/
      ├── 20260417_045102/          # 实验1的时间戳文件夹
      │   ├── PAT_00000901_V2.json
      │   ├── PAT_00000905_V2.json
      │   └── ...
      ├── 20260417_050000/          # 实验2的时间戳文件夹
      │   └── ...
    """

    def __init__(self, 
                 patient_records_dir: Union[str, List[str]] = None,  # 患者记录文件夹 或 单个文件路径（向后兼容）
                 record_id_prefix: str = None, 
                 save_dir: str = "data/records"):
        # 为这次实验创建一个独立的时间戳文件夹
        experiment_timestamp = record_id_prefix or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.experiment_dir = os.path.join(save_dir, experiment_timestamp)
        os.makedirs(self.experiment_dir, exist_ok=True)
        
        # 保存实验时间戳和基础save_dir供外部查询
        self.save_dir = save_dir
        self.experiment_timestamp = experiment_timestamp

        # 存放 patient_id -> record 数据结构，以及 patient_id -> file_path 映射
        self.records: Dict[str, Dict[str, Any]] = {}
        self.file_paths: Dict[str, str] = {}
        self.pharmacy_inventory = PharmacyInventory()

        # 处理输入参数：可以是文件夹路径、单个文件路径或文件路径列表
        patient_files = []
        
        if patient_records_dir:
            if isinstance(patient_records_dir, str):
                # 如果是文件夹，扫描其中的所有.json文件
                if os.path.isdir(patient_records_dir):
                    for filename in os.listdir(patient_records_dir):
                        if filename.endswith('.json'):
                            filepath = os.path.join(patient_records_dir, filename)
                            patient_files.append(filepath)
                # 如果是单个文件，添加到列表（向后兼容）
                elif os.path.isfile(patient_records_dir):
                    patient_files.append(patient_records_dir)
            elif isinstance(patient_records_dir, list):
                # 如果是列表，逐个处理（可能是混合的文件和文件夹）
                for item in patient_records_dir:
                    if os.path.isdir(item):
                        for filename in os.listdir(item):
                            if filename.endswith('.json'):
                                filepath = os.path.join(item, filename)
                                patient_files.append(filepath)
                    elif os.path.isfile(item):
                        patient_files.append(item)

        # 初始化每个患者的病历（从文件夹或文件列表）
        for idx, path in enumerate(patient_files):
            # 从文件提取 patient_id
            patient_id = None
            if path and os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        patient_data = json.load(f)
                    # 尝试从患者数据中读取 patient_id 字段 (优先 meta.patient_id, 再尝试 agent_meta.uuid)
                    if isinstance(patient_data, dict):
                        patient_id = (
                            patient_data.get("meta", {}).get("patient_id") or
                            patient_data.get("agent_meta", {}).get("uuid")
                        )
                except Exception:
                    patient_id = None
            
            if not patient_id:
                # fallback: use filename (without ext)
                if path:
                    patient_id = os.path.splitext(os.path.basename(path))[0]
                else:
                    patient_id = f"PAT_{idx}"

            # 确保患者ID唯一
            if patient_id in self.records:
                patient_id = f"{patient_id}_{idx}"

            # 创建该患者的默认病历结构（record_id改为只含patient_id，不包含时间戳）
            record_id = patient_id
            file_path = os.path.join(self.experiment_dir, f"{record_id}.json")
            self.file_paths[patient_id] = file_path

            data = {
                "meta": {
                    "patient_id": patient_id,
                    "record_id": record_id,
                    "created_at": datetime.now().isoformat(),
                    "status": "DRAFT"  # DRAFT, FINALIZED
                },
                "subjective": {
                    "chief_complaint": "",
                    "history_present_illness": "",
                    "past_medical_history": [],
                    "personal_history": "",
                    "family_history": ""
                },
                "objective": {
                    "vital_signs": {},
                    "physical_exam": [],
                    "auxiliary_exam": []
                },
                "assessment": {
                    "diagnosis": [],
                    "differential_diagnosis": []
                },
                "plan": {
                    "treatment": [],
                    "education": "",
                    "prescription_reference": {
                        "decision": "no prescription",
                        "medication": [
                            {
                                "name": None,
                                "dose": None,
                                "route": None,
                                "frequency": None,
                                "duration": None,
                                "patient_instructions": None
                            }
                        ]
                    }
                }
            }

            # 存储到内存中
            self.records[patient_id] = data

            # 尝试从患者profile文件初始化EMR数据
            if path:
                try:
                    self._initialize_emr(path, patient_id)
                except Exception:
                    # 如果初始化失败，保持默认结构
                    pass
            
            # 初始化后立即为该患者保存空白或初始化的record到磁盘
            # 这确保每个患者的record_id和file_path都被正确创建
            self.save(patient_id)

    def update_record(self, patient_id: str, updates: Dict[str, Any]) -> str:
        """
        更新病历数据。⚠️ 此函数会自动调用 save()，无需手动保存。

        【前置条件】请先调用 get_current_record_str(patient_id) 确认患者ID已在系统中注册！

        【支持的更新字段 - 共13个】

        问诊信息 (Subjective):
            • chief_complaint (str): 主诉  
            • history_present_illness (str): 现病史
            • past_medical_history (str/list): 既往史
            • personal_history (str): 个人史
            • family_history (str): 家族史

        体格检查 (Objective):
            • vital_signs (dict): 生命体征，如 {"temperature": "37.2", "bp": "120/80"}
            • physical_exam (str/list): 体格检查
            • auxiliary_exam (str/list): 辅助检查

        诊断与方案 (Assessment & Plan):
            • diagnosis (str/list): 诊断
            • treatment (str/list): 治疗方案
            • education (str): 患者教育

        处方与转诊 (Prescription & Routing) 【新增】:
            • prescription_reference (dict): {"decision": "no prescription"|"medication", "medication": [...]}
            • department_routing_reference (dict): {"acceptable_departments": ["科室1", "科室2"]}

        【使用示例 1】更新主诉和现病史
            update_record("PAT_00000901_V2", {
                "chief_complaint": "腹股沟肿块",
                "history_present_illness": "6个月前发现左侧腹股沟软性包块..."
            })

        【使用示例 2】记录体检结果
            update_record("PAT_00000901_V2", {
                "vital_signs": {"temperature": "36.8", "bp": "120/78"},
                "physical_exam": ["左侧腹股沟可触及包块", "包块按压消失"]
            })

        【使用示例 3】作出诊断并建议治疗
            update_record("PAT_00000901_V2", {
                "diagnosis": "腹股沟疝",
                "treatment": "择期疝修补术",
                "education": "建议患者避免重体力活动"
            })

        【使用示例 4】开具处方
            update_record("PAT_00000901_V2", {
                "prescription_reference": {
                    "decision": "medication",
                    "medication": [{"name": "药房返回的精确名称", "dose": "单次正数+单位", "route": "口服", "frequency": "QD", "duration": "医生确定的天数", "quantity": "医生确定的总数量+发药单位"}]
                }
            })

        Args:
            patient_id (str): 患者ID，必须已通过 get_current_record_str 验证存在
            updates (Dict[str, Any]): 要更新的字段字典
         
            {'past_medical_history': '有“高血压”史10余年，近2年来有时心前区痛，曾作心电图检查，诊为“冠状动脉供血不足”。有时头晕、心悸、胸闷、心率较慢，50次/分左右，经常服用降压片。无 肝炎、结核等传染病史、预防接种史不祥，无手术、外伤、输血及药物过敏史。', 'personal_history': '生于济南，原在济南市土产公司工作，现退休在家。由吸烟嗜好，每日20支左右。饮酒 少量。22岁结婚，妻子健康，有一子一女均健在。', 'family_history': '父于1982年因患“高血压、脑出血”病故，母于1990年因患“慢性肺心病”病故，家中无结核病、肿瘤及精神病患者。'}
        Returns:
            str: 返回的日志包括：已更新字段、忽略的未知字段、完整的病历记录快照。
            
            ⚠️ 重要提示：此函数已内置 prescription_reference 字段的支持！
            处方必须使用结构化药品对象，不接受字符串列表。复方片剂/胶囊单次 dose 按片/粒填写，不能只写某个成分的 mg。
            quantity 是发药总量，不替代 dose。校验失败时本次更新不保存，必须修正后重新调用；系统不会自动补药。
            department_routing_reference 是 ground truth 数据，不可由医生更新。
        """
        if patient_id not in self.records:
            return f"⚠️ 未找到 patient_id: {patient_id}"

        if "prescription_reference" in updates:
            try:
                self.pharmacy_inventory.validate_prescription(updates["prescription_reference"])
            except ValueError as exc:
                return f"❌ 处方校验失败，本次病历更新未保存：{exc}。请复核药房信息并修正后重新调用 update_record。"

        logs = []
        mapping = {
            "chief_complaint": ("subjective", "chief_complaint"),
            "history_present_illness": ("subjective", "history_present_illness"),
            "past_medical_history": ("subjective", "past_medical_history"),
            "personal_history": ("subjective", "personal_history"),
            "family_history": ("subjective", "family_history"),
            "vital_signs": ("objective", "vital_signs"),
            "physical_exam": ("objective", "physical_exam"),
            "auxiliary_exam": ("objective", "auxiliary_exam"),
            "diagnosis": ("assessment", "diagnosis"),
            "treatment": ("plan", "treatment"),
            "education": ("plan", "education"),
            "prescription_reference": ("plan", "prescription_reference")
        }

        record = self.records[patient_id]

        for key, value in updates.items():
            if key in mapping:
                section, field = mapping[key]

                if isinstance(record[section][field], list):
                    if isinstance(value, list):
                        for item in value:
                            if item not in record[section][field]:
                                record[section][field].append(item)
                    else:
                        if value not in record[section][field]:
                            record[section][field].append(value)
                    logs.append(f"已追加 {key}: {value}")
                else:
                    record[section][field] = value
                    logs.append(f"已更新 {key}")
            else:
                logs.append(f"⚠️ 忽略未知字段: {key}")

        logs.append(f"目前数据 patient_id={patient_id} {record}")
        # 每次更新后自动保存该患者的记录
        self.save(patient_id)
        return "; ".join(logs)

    def get_current_record_str(self, patient_id: str) -> str:
        """
        获取并返回指定患者的完整病历（JSON格式字符串）。

        ⚠️ 【重要】请在问诊开始时立即调用此函数，以验证patient_id是否存在并查看现有病历。
        如果返回 "⚠️ 未找到 patient_id" 错误，说明患者未在系统注册，不能继续诊疗。

        参数说明：
            patient_id (str): 患者ID，由患者在首次沟通中提供（如 "PAT_00000901_V2"）

        返回值说明：
            str: JSON格式的完整病历，包含以下结构:
                - meta: 元数据（patient_id, record_id, created_at, status）
                - subjective: 问诊信息（主诉、现病史、既往史等）
                - objective: 体格检查（生命体征、体格检查、辅助检查）
                - assessment: 诊断信息（诊断、鉴别诊断）
                - plan: 处理计划（治疗、教育、处方、科室转诊）

        常见返回场景：
        1. 成功 - 返回完整的JSON病历对象
        2. 失败 - 返回错误提示: "⚠️ 未找到 patient_id: {patient_id}"
           如果收到此错误，表示该患者未在系统注册，无法继续问诊。

        使用示例：
            # 患者报告ID后立即调用
            record_str = get_current_record_str("PAT_00000901_V2")
            # 查看病历中哪些字段已填写，哪些为空
            # 然后针对空白字段进行逐步填写
        """
        if patient_id not in self.records:
            return f"⚠️ 未找到 patient_id: {patient_id}"
        return json.dumps(self.records[patient_id], ensure_ascii=False, indent=2)

    def save(self, patient_id: Optional[str] = None):
        """
        保存患者病历到磁盘文件。

        ⚠️ 【注意】update_record() 已自动调用 save()，医生通常无需手动调用。
        仅在以下情况需要手动调用：
        - 直接调用了 update_record 之外的其他修改操作后
        - 诊疗结束时确保所有数据已保存

        参数说明：
            patient_id (Optional[str]): 
                - 若指定patient_id: 仅保存该患者的病历
                - 若为None: 保存系统中所有患者的病历

        文件位置：
            所有病历保存到实验时间戳文件夹内，格式为:
            <experiment_dir>/{patient_id}.json
            或 <experiment_dir>/{record_id}.json

        返回值：
            无返回值，但成功时病历已保存到磁盘

        错误处理：
            - 如果指定的patient_id不存在，抛出 FileNotFoundError
            - 磁盘写入失败时抛出相应异常

        使用示例：
            # 【通常情况】update_record 已自动保存，无需调用
            update_record("PAT_00000901_V2", {"chief_complaint": "胸痛"})

            # 【可选】诊疗结束时再确保保存一次
            save("PAT_00000901_V2")
        """
        if patient_id:
            if patient_id not in self.records:
                raise FileNotFoundError(f"patient_id not found: {patient_id}")
            # 优先使用预设的file_path，fallback使用experiment_dir
            file_path = self.file_paths.get(patient_id) or os.path.join(self.experiment_dir, f"{self.records[patient_id]['meta'].get('record_id', patient_id)}.json")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.records[patient_id], f, ensure_ascii=False, indent=2)
            return
        # save all
        for pid, record in self.records.items():
            # 优先使用预设的file_path，fallback使用experiment_dir
            file_path = self.file_paths.get(pid) or os.path.join(self.experiment_dir, f"{record['meta'].get('record_id', pid)}.json")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

    def _initialize_emr(self, emr_path: str, patient_id: str) -> None:
        """
        从患者记录文件初始化指定 patient_id 的 EMR 数据。
        """
        if not os.path.exists(emr_path):
            raise FileNotFoundError(f"患者记录文件不存在: {emr_path}")

        with open(emr_path, 'r', encoding='utf-8') as f:
            patient_data = json.load(f)

        updates = {}
        if 'narrative_memory' in patient_data:
            narrative = patient_data['narrative_memory']

            if narrative.get('past_medical_history'):
                updates['past_medical_history'] = narrative['past_medical_history']

            if narrative.get('personal_history'):
                updates['personal_history'] = narrative['personal_history']

            if narrative.get('family_history'):
                updates['family_history'] = narrative['family_history']

        # 更新指定患者的 EMR 系统
        if updates:
            self.update_record(patient_id, updates)


def get_tools(mrs: MedicalRecordSystem):
    """
    返回包装后的 FunctionTool 列表。
    注意: update_record/get_current_record_str 现在需要第一个参数 patient_id。
    """
    tools = [
        FunctionTool(mrs.update_record),
        FunctionTool(mrs.get_current_record_str),
        FunctionTool(mrs.save),
    ]
    return tools


def get_tools_schemas(tools: List[FunctionTool]) -> List[Dict[str, Any]]:
    tools_schemas = []
    for tool in tools:
        tools_schemas.append(tool.get_openai_function_schema())
    return tools_schemas


if __name__ == "__main__":
    # 测试1：使用文件夹模式（沙箱模式）- 推荐
    print("=" * 70)
    print("测试1：文件夹模式（推荐用于沙箱）")
    print("=" * 70)
    
    records_dir = 'data/profiles/patient_records1'
    if os.path.exists(records_dir):
        emr = MedicalRecordSystem(patient_records_dir=records_dir, save_dir="data/records")
        print(f"\n扫描文件夹: {records_dir}")
        print(f"初始化患者数: {len(emr.records)}")
        print("\n初始病历数据:")
        for pid in emr.records:
            print(f"  - {pid}")
        
        # 获取工具
        tools = get_tools(emr)
        print(f"\n医生工具集: {len(tools)} 个工具")
        
        # 对第一个patient做更新
        first_pid = next(iter(emr.records.keys()))
        print(f"\n对患者 {first_pid} 进行更新...")
        update_log = emr.update_record(first_pid, {
            "chief_complaint": "腹股沟肿块",
            "physical_exam": ["右侧腹股沟可触及软性肿块，无压痛"]
        })
        print(f"更新结果: {update_log}")
    else:
        print(f"❌ 文件夹不存在: {records_dir}")
    
    # 测试2：向后兼容 - 单个文件模式
    print("\n" + "=" * 70)
    print("测试2：单个文件模式（向后兼容）")
    print("=" * 70)
    
    single_file = 'data/profiles/patient_records1/patient_00000901.json'
    if os.path.exists(single_file):
        emr2 = MedicalRecordSystem(patient_records_dir=single_file, save_dir="data/records")
        print(f"\n初始化单个文件: {single_file}")
        print(f"患者数: {len(emr2.records)}")
        for pid in emr2.records:
            print(f"  - {pid}")
    else:
        print(f"❌ 文件不存在: {single_file}")
