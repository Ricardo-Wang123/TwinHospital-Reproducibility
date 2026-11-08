
import os
from typing import List, Dict, Any, Optional

from camel.toolkits import FunctionTool

from .medical_exam import MedicalExamTool
from .record_generator import MedicalRecordSystem
from .doctor_query_tool import DoctorQueryTool
from .pharmacy_inventory import PharmacyInventory, PharmacyInventoryTool

class ToolManager:
    """
    ToolManager 管理并缓存工具实例，提供便捷的 FunctionTool 列表。
    """

    def __init__(self):
        self._medical_exam_cache: Dict[Optional[str], MedicalExamTool] = {}
        self._record_system_cache: Dict[Optional[str], MedicalRecordSystem] = {}
        self._pharmacy_inventory: Optional[PharmacyInventory] = None

    # MedicalExam helpers (保留原样)
    def get_medical_exam_tool(self, patient_profile_path: Optional[str] = None) -> MedicalExamTool:
        key = patient_profile_path or "__default__"
        if key not in self._medical_exam_cache:
            self._medical_exam_cache[key] = MedicalExamTool(patient_profile_path)
        return self._medical_exam_cache[key]

    def get_medical_exam_tools(self, patient_profile_paths: Optional[str] = None) -> List[FunctionTool]:
        each_paths_list = [
            os.path.join(patient_profile_paths, f) for f in os.listdir(patient_profile_paths)
        if os.path.isfile(os.path.join(patient_profile_paths, f))
    ]
        mxt = MedicalExamTool(each_paths_list)
        tools = [
            FunctionTool(mxt.check_patient_stability),
            FunctionTool(mxt.check_vital_signs),
            FunctionTool(mxt.perform_physical_exam),
        ]
        return tools

    # MedicalRecordSystem helpers
    def get_record_system(self, patient_records_dir: Optional[str] = None, save_dir: Optional[str] = None) -> MedicalRecordSystem:
        """
        获取或创建 MedicalRecordSystem 实例。
        
        Args:
            patient_records_dir: 患者记录文件夹路径（推荐用于沙箱）或单个文件路径（向后兼容）
                                 系统会自动扫描文件夹中的所有.json文件并为其创建records
                                 传入None则创建空的EMR系统
            save_dir: 保存生成的EMR文件的目录
            
        Returns:
            MedicalRecordSystem 实例
            
        【关键行为】
        - 初始化时为所有患者创建records（在内存中）
        - 医生调用update_record()时，自动保存到磁盘
        """
        key = f"{patient_records_dir}::{save_dir}" if patient_records_dir or save_dir else "__record_default__"
        if key not in self._record_system_cache:
            # MedicalRecordSystem 现在接受文件夹路径或文件路径
            self._record_system_cache[key] = MedicalRecordSystem(
                patient_records_dir=patient_records_dir, 
                save_dir=save_dir or "MedicalAgent/data/records"
            )
        return self._record_system_cache[key]

    def get_record_tools(self, record_system: MedicalRecordSystem) -> List[FunctionTool]:
        """
        将 MedicalRecordSystem 的方法包装为 FunctionTool 返回。
        典型工具为 update_record 和 get_current_record_str（或 get_current_record_str 也可以作为只读工具）。
        """
        tools = []
        try:
            tools.append(FunctionTool(record_system.update_record))
            tools.append(FunctionTool(record_system.get_current_record_str))
            tools.append(FunctionTool(record_system.save))
        except Exception:
            pass
        return tools

    def get_tools_schemas(self, tools: List[FunctionTool]) -> List[Dict[str, Any]]:
        schemas = []
        for t in tools:
            try:
                schemas.append(t.get_openai_function_schema())
            except Exception:
                pass
        return schemas

    def get_doctor_query_tools(self, coordinator) -> List[FunctionTool]:
        """
        获取医生查询工具（护士用于查询医生状态）
        Args:
            coordinator: Coordinator 实例
        Returns:
            包含医生查询工具的 FunctionTool 列表
        """
        dqt = DoctorQueryTool(coordinator)
        tools = [
            FunctionTool(dqt.query_all_doctors_workload),
            # FunctionTool(dqt.query_doctor_by_id),
            # FunctionTool(dqt.get_available_departments),
            # FunctionTool(dqt.recommend_doctor_by_symptom)
        ]
        return tools

    def get_pharmacy_inventory(self) -> PharmacyInventory:
        if self._pharmacy_inventory is None:
            self._pharmacy_inventory = PharmacyInventory()
        return self._pharmacy_inventory

    def ensure_pharmacy_slim_file(self) -> str:
        inventory = self.get_pharmacy_inventory()
        path = inventory.write_slim_file()
        return str(path)

    def get_pharmacy_tools(self) -> List[FunctionTool]:
        pharmacy_tool = PharmacyInventoryTool(self.get_pharmacy_inventory())
        return [
            FunctionTool(pharmacy_tool.search_pharmacy_medications),
            FunctionTool(pharmacy_tool.get_pharmacy_medication),
        ]
