import json
from typing import List, Dict, Any, Optional


class DoctorQueryTool:
    """
    提供护士查询医生工作负荷和忙碌情况的工具。
    该工具访问 Coordinator 实例以获取医生的实时状态。
    """

    def __init__(self, coordinator):
        """
        Args:
            coordinator: Coordinator 实例，用于查询医生状态
        """
        self.coordinator = coordinator

    def query_all_doctors_workload(self, department: Optional[str] = None, sort_by_workload: bool = True) -> str:
        """
        查询所有医生的工作负荷情况
        Args:
            department: 指定科室进行筛选（可选）
            sort_by_workload: 是否按空闲程度排序（默认排序）
        Returns:
            JSON 格式的医生信息字符串: 例如
            {
            "status": "success",
            "timestamp": "2026-03-15T23:29:48.227973",
            "filters": {
                "department": "外科",
                "sort_by_workload": true
            },
            "doctors": [
                {
                "doctor_id": "DOC_1063",
                "doctor_name": "何南",
                "department": "外科",
                "waiting_patients": 0,
                "is_consulting": false,
                "current_patient": null,
                "completed_patients": 0,
                "workload_level": "空闲"
                }
            ],
            "summary": {
                "total_doctors": 1,
                "idle_doctors": 1,
                "busy_doctors": 0
            }
            }
        """
        try:
            workload_info = self.coordinator.get_doctor_workload_info(
                department=department,
                sort_by_workload=sort_by_workload
            )
            
            result = {
                "status": "success",
                "timestamp": str(__import__('datetime').datetime.now().isoformat()),
                "filters": {
                    "department": department,
                    "sort_by_workload": sort_by_workload
                },
                "doctors": workload_info,
                "summary": {
                    "total_doctors": len(workload_info),
                    "idle_doctors": len([d for d in workload_info if d["workload_level"] == "空闲"]),
                    "busy_doctors": len([d for d in workload_info if d["workload_level"] == "忙碌"])
                }
            }
            print("医生情况收集完成")
        except:
            print("错误，收集失败")
        return json.dumps(result, ensure_ascii=False, indent=2)


    def query_doctor_by_id(self, doctor_id: str) -> str:
        """
        查询指定医生的详细信息
        Args:
            doctor_id: 医生 ID
        Returns:
            医生的详细信息（JSON 格式）
        """
        try:
            if doctor_id not in self.coordinator.doctor_status:
                return json.dumps({
                    "status": "error",
                    "message": f"未找到医生: {doctor_id}"
                }, ensure_ascii=False)
            
            status = self.coordinator.doctor_status[doctor_id]
            result = {
                "status": "success",
                "doctor_id": doctor_id,
                "doctor_name": status["doctor_name"],
                "department": status["department"],
                "waiting_patients_count": len(status["waiting_queue"]),
                "is_consulting": status["current_patient"] is not None,
                "current_patient": status["current_patient"]["patient_name"] if status["current_patient"] else None,
                "completed_count": len(status["completed_patients"]),
                "waiting_queue": [p["patient_name"] for p in status["waiting_queue"]],
                "completed_patients": [p["patient_name"] for p in status["completed_patients"]]
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_result = {
                "status": "error",
                "message": f"查询失败: {str(e)}"
            }
            return json.dumps(error_result, ensure_ascii=False)

    def get_available_departments(self) -> str:
        """
        获取所有可用的科室列表
        Returns:
            科室列表（JSON 格式）
        """
        try:
            departments = list(self.coordinator.department_mapping.keys())
            result = {
                "status": "success",
                "departments": departments,
                "total_departments": len(departments)
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_result = {
                "status": "error",
                "message": f"获取科室失败: {str(e)}"
            }
            return json.dumps(error_result, ensure_ascii=False)

    def recommend_doctor_by_symptom(self, symptoms: str, department: Optional[str] = None) -> str:
        """
        根据症状和科室推荐最空闲的医生
        Args:
            symptoms: 患者症状描述
            department: 目标科室（可选）
        Returns:
            推荐的医生信息（JSON 格式）
        """
        try:
            workload_info = self.coordinator.get_doctor_workload_info(
                department=department,
                sort_by_workload=True
            )
            
            if not workload_info:
                return json.dumps({
                    "status": "error",
                    "message": "没有可用医生"
                }, ensure_ascii=False)
            
            recommended_doctor = workload_info[0]  # 已按空闲程度排序，取第一个
            
            result = {
                "status": "success",
                "original_symptom": symptoms,
                "recommended_doctor": {
                    "doctor_id": recommended_doctor["doctor_id"],
                    "doctor_name": recommended_doctor["doctor_name"],
                    "department": recommended_doctor["department"],
                    "workload_level": recommended_doctor["workload_level"],
                    "waiting_patients": recommended_doctor["waiting_patients"],
                    "reason": f"该医生目前最空闲，等待患者数: {recommended_doctor['waiting_patients']}"
                },
                "alternative_doctors": workload_info[1:3]  # 备选医生
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_result = {
                "status": "error",
                "message": f"推荐失败: {str(e)}"
            }
            return json.dumps(error_result, ensure_ascii=False)
