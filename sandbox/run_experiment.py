"""
run_experiment.py - 沙箱实验主入口脚本

用法：
    python -m sandbox.run_experiment --exp_name my_experiment
    python -m sandbox.run_experiment --mode ai_triage
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict

# Support both `python -m sandbox.run_experiment` and direct script execution.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.paths import DEFAULT_CONFIG, project_path
from sandbox.core.config_loader import ConfigLoader
from sandbox.core.state_manager import StateManager
from sandbox.core.sandbox_env import SandboxEnvironment
from sandbox.core.pipeline import StageStatus
from sandbox.core.organization_rebuilder import rebuild_departments_if_enabled
from sandbox.stages import TriageStage, ConsultationStage, EvaluationStage

def load_test_patient_profiles(sandbox_env: SandboxEnvironment) -> list:
    """
    从沙箱环境中加载患者profiles

    由于profiles是在sandbox_env.initialize_agents()中加载的，
    这里只需要读取它们即可
    """
    patient_profiles = sandbox_env.get_patient_profiles()

    profiles_list = []
    for profile_path in patient_profiles:
        try:
            with open(profile_path, 'r', encoding='utf-8') as f:
                profile_data = json.load(f)
                # Extract patient_id: try meta.patient_id first (new format), then agent_meta.uuid or identity fields (old format)
                patient_id = (
                    profile_data.get('meta', {}).get('patient_id') or
                    profile_data.get('agent_meta', {}).get('uuid') or
                    profile_data.get('identity', {}).get('raw_data', {}).get('patient_id') or
                    f"PAT_{len(profiles_list)+1:03d}"
                )
                profiles_list.append({
                    'name': f"患者_{len(profiles_list)+1}",
                    'profile_path': profile_path,
                    'patient_id': patient_id,
                    'gender': profile_data.get('identity', {}).get('derived_attributes', {}).get('gender', 'Unknown'),
                    'age_group': profile_data.get('identity', {}).get('derived_attributes', {}).get('age_group', 'Unknown'),
                })
        except Exception as e:
            print(f"  ⚠️ 加载患者profile失败: {profile_path} - {e}")

    return profiles_list


def run_clock_loop(config, state_manager, sandbox_env):
    """Drain the triage/doctor queues, stopping on failure or lack of progress."""
    coordinator = sandbox_env.get_coordinator()
    triage = TriageStage(config.get_stage_config("triage"))
    consultation = ConsultationStage(config.get_stage_config("consultation"))
    consultation_enabled = config.get_stage_config("consultation").get("enabled", True)
    stop_on_error = config.get("pipeline.stop_on_error", True)
    max_clocks = int(config.get("pipeline.max_clocks", 100))
    triage_results, consultation_results = [], []
    clock_count = 0

    def waiting_patients():
        return [
            {
                "patient_id": record["patient_info"].get("patient_id"),
                "patient_name": record["patient_name"],
                "doctor_id": doctor_id,
                "patient_info": record["patient_info"],
                "triage_result": record.get("triage_result", {}),
            }
            for doctor_id, status in coordinator.doctor_status.items()
            for record in status.get("waiting_queue", [])
            if record.get("status") == "waiting"
        ]

    def execute(stage, stage_input):
        state_manager.transition_stage(stage.name)
        stage.set_status(StageStatus.RUNNING)
        try:
            results = stage.execute(state_manager, sandbox_env, stage_input)
            if not isinstance(results, list):
                raise TypeError(f"{stage.name} 队列执行必须返回列表")
            previous = triage_results if stage.name == "triage" else consultation_results
            state_manager.set_stage_output(stage.name, previous + results)
            failed = any(item.get("status") == "failed" for item in previous + results)
            stage.set_status(StageStatus.FAILED if failed else StageStatus.COMPLETED)
            if failed:
                state_manager.set_stage_error(stage.name, "存在失败的患者结果")
                if stop_on_error:
                    raise RuntimeError(f"{stage.name} 存在失败的患者结果")
            return results
        except Exception as exc:
            stage.set_status(StageStatus.FAILED)
            state_manager.set_stage_error(stage.name, str(exc))
            if stop_on_error:
                raise
            results = [{"status": "failed", "error": str(exc)}]
            previous = triage_results if stage.name == "triage" else consultation_results
            state_manager.set_stage_output(stage.name, previous + results)
            state_manager.set_stage_error(stage.name, str(exc))
            return results

    while coordinator.has_patients_in_queue() or (consultation_enabled and waiting_patients()):
        if clock_count >= max_clocks:
            raise RuntimeError(f"超过最大 Clock 数 {max_clocks}，仍有患者未完成")
        clock_count += 1
        print(f"\nClock {clock_count}")
        progress = []
        if coordinator.has_patients_in_queue():
            results = execute(triage, None)
            triage_results.extend(results)
            progress.extend(results)
        # Re-read the queue after triage so newly assigned patients can be seen immediately.
        waiting = waiting_patients() if consultation_enabled else []
        if waiting:
            results = execute(consultation, {"triage_results": waiting})
            consultation_results.extend(results)
            progress.extend(results)
        if not any(item.get("status") == "completed" for item in progress):
            raise RuntimeError("本轮没有患者完成任何阶段，终止以避免空转")
    return triage_results, consultation_results, clock_count


def _record_files(record_system):
    return [str(Path(path).resolve()) for path in getattr(record_system, "file_paths", {}).values() if Path(path).is_file()]


def _save_failed_result(config, experiment_id, error, state_manager=None, record_system=None):
    """Keep a failure artifact even when initialization stops before the Clock loop."""
    output_dir = Path(config.resolve_path("output", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    result_file = output_dir / f"{experiment_id}_result.json"
    records = _record_files(record_system)
    result = {"status": "failed", "experiment_id": experiment_id,
              "error": str(error), "record_files": records, "output_files": [str(result_file)] + records}
    if state_manager is not None:
        state_file = output_dir / f"{experiment_id}_state.json"
        state_manager.export_to_file(str(state_file))
        result["output_files"].append(str(state_file))
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run_experiment(config_path: str, experiment_id: str = None, *, mode=None, disabled_tools=None) -> Dict:
    """
    运行实验

    Args:
        config_path: 配置文件路径
        experiment_id: 实验ID（如不提供则自动生成）

    Returns:
        实验结果字典
    """
    print("\n" + "="*80)
    print("医院模拟沙箱系统 - 分诊实验运行")
    print("="*80)

    # 生成实验ID
    if experiment_id is None:
        experiment_id = f"triage_exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"\n【实验ID】: {experiment_id}")
    print(f"【配置文件】: {config_path}")

    # 1. 加载配置
    try:
        config = ConfigLoader(config_path)
        config.apply_cli_overrides(mode, disabled_tools)
        if not config.get_stage_config("triage").get("enabled", True):
            raise ValueError("医院流程入口需要分诊阶段；只做分诊请关闭 consultation.enabled")
        if config.get_stage_config("followup").get("enabled", False):
            raise ValueError("回访阶段尚未实现")
        if config.get_stage_config("triage").get("mode", "ai") not in ("ai", "no_ai"):
            raise ValueError("分诊模式必须是 ai 或 no_ai")
        config.collect_agent_profiles()  # Fail before contacting OpenHIS if inputs are missing.
        print(f"✓ 配置加载成功")
    except Exception as e:
        print(f"✗ 配置加载失败: {e}")
        return {"status": "failed", "error": str(e)}

    # 2. 初始化沙箱环境
    try:
        config_dict = config.to_dict()
        sandbox_env = SandboxEnvironment(config_dict)
        print(f"✓ 沙箱环境初始化成功")
        print(f"  - 实验模式: {sandbox_env.experiment_mode}")
        print(f"  - 模型配置: {list(sandbox_env.models.keys())}")

        # 检查OpenHIS集成状态
        if sandbox_env.openhis_adapter:
            print(f"  - OpenHIS适配器: ✓ 已初始化")
        else:
            openhis_config = config_dict.get('openhis_integration', {})
            if openhis_config.get('enabled', False):
                print(f"  - OpenHIS适配器: ⚠️ 未启用或初始化失败")
            else:
                print(f"  - OpenHIS适配器: ⊘ 已禁用（可选）")
    except Exception as e:
        print(f"✗ 沙箱环境初始化失败: {e}")
        return _save_failed_result(config, experiment_id, e)

    # 2.5. 可选：重建OpenHIS业务科室
    try:
        rebuild_result = rebuild_departments_if_enabled(config)
        if rebuild_result.get("enabled"):
            print(f"✓ OpenHIS科室重建完成")
            print(f"  - 医院: {rebuild_result.get('hospital_name')} ({rebuild_result.get('hospital_bus_no')})")
            print(f"  - 删除旧科室: {rebuild_result.get('deleted_count')}")
            print(f"  - 新增科室: {rebuild_result.get('added_count')}")
            if rebuild_result.get("dry_run"):
                print(f"  - dry_run: 已启用，未写入OpenHIS")
        else:
            print(f"⊘ OpenHIS科室重建跳过: {rebuild_result.get('skipped_reason')}")
    except Exception as e:
        print(f"⚠️ OpenHIS科室重建失败: {e}")
        if config.get('openhis_integration.department_rebuild.fail_on_error', False):
            return _save_failed_result(config, experiment_id, e)

    # 3. 初始化Agent系统（关键步骤）
    try:
        sandbox_env.initialize_agents(config)
        print(f"✓ Agent系统初始化成功")
    except Exception as e:
        print(f"✗ Agent系统初始化失败: {e}")
        return _save_failed_result(config, experiment_id, e)

    # 4. 初始化状态管理器
    state_manager = StateManager(experiment_id)
    print(f"✓ 状态管理器初始化成功")

    # 6. 加载患者profiles
    print(f"\n【加载患者数据】")
    try:
        patient_profiles = load_test_patient_profiles(sandbox_env)
        print(f"✓ 加载了 {len(patient_profiles)} 个患者profiles")
        for i, p in enumerate(patient_profiles, 1):
            print(f"  {i}. {p['name']} ({p['age_group']}, {p['gender']})")
    except Exception as e:
        print(f"✗ 患者数据加载失败: {e}")
        return _save_failed_result(config, experiment_id, e, state_manager)

    # 7. 初始化患者队列到Coordinator（关键改变）
    try:
        coordinator = sandbox_env.get_coordinator()
        coordinator.init_patient_queue(patient_profiles)
        print(f"✓ 患者队列已初始化到Coordinator")
    except Exception as e:
        print(f"✗ 患者队列初始化失败: {e}")
        return _save_failed_result(config, experiment_id, e, state_manager)

    try:
        triage_results, consultation_results, clock_count = run_clock_loop(config, state_manager, sandbox_env)
        evaluation_results = []
        if config.get_stage_config("evaluation").get("enabled", False):
            state_manager.transition_stage("evaluation")
            evaluation_stage = EvaluationStage(config.get_stage_config("evaluation"))
            evaluation_results = evaluation_stage.execute(
                state_manager, sandbox_env,
                {"triage_results": triage_results, "consultation_results": consultation_results},
            )
            if not evaluation_results:
                raise RuntimeError("启用了评估但没有生成评估结果")
            state_manager.set_stage_output("evaluation", evaluation_results)
    except Exception as exc:
        return _save_failed_result(config, experiment_id, exc, state_manager, sandbox_env.record_system)

    # 10. 最终结果汇总
    print(f"\n{'='*80}")
    print(f"【实验结果】")
    print(f"{'='*80}")

    failed = any(item.get("status") == "failed" for item in triage_results + consultation_results + evaluation_results)
    required_count = len(patient_profiles)
    if len(triage_results) < required_count:
        failed = True
    if config.get_stage_config("consultation").get("enabled", True) and len(consultation_results) < required_count:
        failed = True

    result = {
        "status": "failed" if failed else "completed",
        "experiment_id": experiment_id,
        "experiment_type": "full_pipeline",
        "timestamp": datetime.now().isoformat(),
        "config_used": {
            "resources": config.get_resources_config(),
            "triage_mode": config.get_stage_config('triage').get('mode', 'ai'),
            "consultation_enabled": config.get_stage_config('consultation').get('enabled', True),
        },
        "summary": {
            "total_patients": len(patient_profiles),
            "clock_count": clock_count,
            "total_triage_results": len(triage_results),
            "total_consultation_results": len(consultation_results),
            "total_evaluation_results": len(evaluation_results),
            "triage_mode": config.get_stage_config('triage').get('mode', 'ai'),
        },
        "triage_results": triage_results,
        "consultation_results": consultation_results,
        "evaluation_results": evaluation_results,
        "record_files": _record_files(sandbox_env.record_system),
        "output_files": _record_files(sandbox_env.record_system)
    }

    # 10. 保存结果
    print(f"\n【保存结果】")
    # 修复bug: 使用resolve_path()正确处理相对路径，使得修改path配置时能生效
    output_dir = config.resolve_path('output', 'output')
    os.makedirs(output_dir, exist_ok=True)

    # 可选：将本次实验生成的结构化病历导出为初诊病历 PDF
    pdf_export_result = {"enabled": False}
    pdf_config = config.get('export.medical_record_pdf', {}) or {}
    if pdf_config.get('enabled', False):
        print(f"\n【导出初诊病历PDF】")
        try:
            from sandbox.export_medical_record_pdf import export_medical_record_pdfs

            record_system = getattr(sandbox_env, "record_system", None)
            records = getattr(record_system, "records", None) or {}

            pdf_base_dir = project_path(
                pdf_config.get('output_dir', 'output/medical_record_pdfs')
            )
            if pdf_config.get('separate_experiment_dir', True):
                pdf_base_dir = os.path.join(pdf_base_dir, experiment_id)

            pdf_export_result = export_medical_record_pdfs(
                records=records,
                output_dir=pdf_base_dir,
                options=pdf_config,
                patient_profiles=patient_profiles,
                triage_results=triage_results,
                consultation_results=consultation_results,
            )
            result['medical_record_pdf_export'] = pdf_export_result
            result['output_files'].extend(pdf_export_result.get('files', []))
            print(f"✓ PDF导出完成: {pdf_export_result.get('generated_count', 0)} 份")
            print(f"  - 输出目录: {pdf_export_result.get('output_dir')}")
            if pdf_export_result.get('errors'):
                print(f"  ⚠️ PDF导出错误: {len(pdf_export_result['errors'])} 个")
        except Exception as e:
            pdf_export_result = {"enabled": True, "error": str(e), "files": []}
            result['medical_record_pdf_export'] = pdf_export_result
            print(f"⚠️ PDF导出失败: {e}")
            if pdf_config.get('fail_on_error', False):
                return {"status": "failed", "error": str(e), "partial_result": result}
    else:
        result['medical_record_pdf_export'] = pdf_export_result
        print(f"\n【导出初诊病历PDF】⊘ 已禁用")

    result_file = os.path.join(output_dir, f"{experiment_id}_result.json")
    state_file = os.path.join(output_dir, f"{experiment_id}_state.json")
    result["output_files"].extend([result_file, state_file])
    # 保存完整结果
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"✓ 结果已保存: {result_file}")

    # 保存状态
    state_manager.export_to_file(state_file)
    print(f"✓ 状态已保存: {state_file}")

    print(f"\n实验状态: {result['status']}；结果已保存到 {output_dir}\n")

    return result


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="医院模拟沙箱系统 - 实验运行脚本"
    )

    # 计算默认配置文件路径（相对于脚本所在目录）
    default_config = str(DEFAULT_CONFIG)

    parser.add_argument(
        '--config',
        type=str,
        default=default_config,
        help='配置文件路径'
    )

    parser.add_argument(
        '--exp_name',
        type=str,
        help='实验名称（可选，用于标识）'
    )

    parser.add_argument(
        '--mode',
        type=str,
        choices=['no_ai_triage', 'ai_triage'],
        help='实验模式（可选，覆盖配置文件中的设置）'
    )

    parser.add_argument(
        '--disable_tool',
        type=str,
        action='append',
        choices=['emr', 'exam', 'pharmacy'],
        help='禁用工具组；可以多次指定'
    )

    args = parser.parse_args()

    # 确保配置文件路径存在
    if not os.path.exists(args.config):
        print(f"✗ 配置文件不存在: {args.config}")
        sys.exit(1)

    # 运行实验
    result = run_experiment(args.config, args.exp_name, mode=args.mode, disabled_tools=args.disable_tool)

    # 返回状态码
    if result.get('status') == 'completed':
        print(f"✓ 实验成功完成")
        sys.exit(0)
    else:
        print(f"✗ 实验失败: {result.get('error', '存在失败或缺失的患者结果')}")
        sys.exit(1)


if __name__ == "__main__":
    # 支持直接运行
    main()
