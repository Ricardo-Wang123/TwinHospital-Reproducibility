"""Typed parameter registry, explicit provenance, and research readiness gates."""
import copy
from pathlib import Path
from sandbox.core.config_loader import ConfigLoader
from sandbox.paths import PROJECT_ROOT
from .contracts import require_finite

PARAMETERS = {
    "selected_configurations": ("array|null", None, "engineering_preset", "不填执行完整矩阵；工程接入可显式选择配置，正式研究须全矩阵"),
    "schema_version": ("string", "1", "engineering_preset", "文件契约版本"),
    "protocol_version": ("string", "twinhospital-outpatient-1", "user_fixed", "门诊实验协议"),
    "purpose": ("enum", "engineering_demo", "engineering_preset", "engineering_demo / integration / study"),
    "setting": ("enum", "outpatient", "user_fixed", "门诊场景"),
    "seed": ("integer", 20260919, "engineering_preset", "生成和抽样种子，不保证远程模型确定性"),
    "dataset": ("path", "fixtures/twinhospital/demo", "engineering_preset", "可替换数据包目录"),
    "output": ("path", "output/twinhospital", "engineering_preset", "封存结果，不在清场范围内"),
    "state_dir": ("path", "output/twinhospital-control", "engineering_preset", "实例锁和epoch，不在清场范围内"),
    "configuration_concurrency": ("integer", 1, "user_fixed", "不同配置严格顺序运行"),
    "case_concurrency": ("integer", 4, "engineering_preset", "APP III 内部并发数；临床任务顺序运行"),
    "max_interactions": ("integer", 10, "engineering_preset", "完整问答轮次"),
    "http_timeout_s": ("number", 15, "existing_default", "HTTP时限，秒"),
    "model_timeout_s": ("number", 120, "engineering_preset", "模型请求时限，秒"),
    "case_timeout_s": ("number", 600, "engineering_preset", "病例执行预算，秒"),
    "submission_window_s": ("number", 10, "engineering_preset", "APP III提交窗口，秒"),
    "effect_observation_window_s": ("number", 30, "engineering_preset", "异步副作用观察窗口，秒"),
    "read_retries": ("integer", 2, "engineering_preset", "只读请求可重试次数"),
    "mutation_retries": ("integer", 0, "user_fixed", "写请求不盲目重试"),
    "his_backend": ("enum", "in_memory", "engineering_preset", "in_memory / openhis"),
    "instance_id": ("string", "demo-only", "engineering_preset", "集成时必须替换成专用实例标识"),
    "seed_manifest": ("path", None, "required_input", "真实实例恢复清单"),
    "external_effects_mode": ("enum", "stub_only", "user_fixed", "真实资金/医保/发票通道不参与实验"),
    "calibration_file": ("path", None, "required_input", "真实anchor拟合的时间/行为参数"),
    "clinical_verification_file": ("path", None, "required_input", "病例临床核验和仲裁记录"),
    "authorization_file": ("path", None, "required_input", "正式人员签署资格"),
    "catalog_file": ("path", None, "required_input", "药品/诊断/费用/单位版本及原生ID映射"),
    "quality_rubric": ("object", None, "required_input", "主要质量指标和合格阈值"),
    "fidelity_tolerances": ("object", None, "required_input", "逐指标允许差异与方向容差"),
    "confidence": ("number", 0.95, "engineering_preset", "统计置信度，正式运行前冻结"),
    "bootstrap_samples": ("integer", 10000, "engineering_preset", "病例/来源组配对重采样次数"),
    "custodian": ("string", "local-engineering", "engineering_preset", "正式封存需独立保管记录"),
    "anchor_count_per_task": ("integer", 100, "paper_fixed", "各任务独立真实校准记录"),
    "validation_count_per_task": ("integer", 200, "paper_fixed", "各任务临床核验合成测试病例"),
    "budget.max_calls": ("integer", None, "required_input", "实际模型调用上限"),
    "budget.max_tokens": ("integer", None, "required_input", "实际模型token预算"),
}


def default_config():
    result = {}
    for key, (_, value, _, _) in PARAMETERS.items():
        parent = result
        parts = key.split(".")
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[parts[-1]] = copy.deepcopy(value)
    result.update({"models": {"actors": {"provider": "scripted"},
                              "medical": {"provider": "scripted", "model": None, "revision": None},
                              "foundation": {"provider": "scripted", "model": None, "revision": None}},
                   "pprs": {"provider": "fixture", "version": "demo-rules-1", "replay_file": None},
                   "openhis": {"base_url": "http://localhost:18080/openhis", "tenant_id": 1,
                       "username_env": "OPENHIS_USERNAME", "password_env": "OPENHIS_PASSWORD",
                       "token_env": "OPENHIS_TOKEN", "service_key_env": "TWINHOSPITAL_SERVICE_KEY"},
                   "timing": {"source": "scripted", "collection": 180, "review": 60, "revision": 90, "attestation": 20},
                   "personnel": {"doctor-1": ["note.attest", "order.attest"], "nurse-1": [],
                       "pharmacist-1": [], "senior_pharmacist-1": [], "scribe-1": [], "patient-1": []},
                   "resources": {"doctor": 1, "nurse": 1, "pharmacist": 1, "senior_pharmacist": 1, "scribe": 1},
                   "suites": ["clinical", "safety"]})
    return result


def resolve_path(path):
    p = Path(path)
    return p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def load_config(path=None):
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    value = default_config()
    if path:
        ConfigLoader._deep_merge(value, ConfigLoader._read(Path(path).resolve()))
    # Secrets stay as environment references; unlike the legacy loader, no expansion.
    return value


def readiness(config, dataset=None):
    missing, errors = [], []
    def need(field, reason):
        missing.append({"field": field, "reason": reason})
    def check_secrets(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"api_key", "password", "token", "service_key", "service_token"} and item:
                    errors.append("literal credential prohibited in frozen config: " + path + key + "; use an environment reference")
                check_secrets(item, path + key + ".")
        elif isinstance(value, list):
            for item in value:
                check_secrets(item, path)
    check_secrets(config)
    selected = config.get("selected_configurations")
    if selected is not None:
        allowed = set()
        for task, baseline, workflows in [("prescription", "pprs_baseline", ("sequential", "parallel")), ("preconsult", "nurse_only", ("ai_only", "ai_nurse_review")), ("documentation", "manual_documentation", ("realtime", "postvisit"))]:
            allowed.add(task + ":" + baseline)
            allowed.update(f"{task}:{workflow}:{family}" for workflow in workflows for family in ("medical", "foundation"))
        if not isinstance(selected, list) or not selected or not all(isinstance(v, str) and v in allowed for v in selected) or len(set(selected)) != len(selected):
            errors.append("selected_configurations must contain unique supported configuration IDs")
        elif config.get("purpose") == "study" and set(selected) != allowed:
            errors.append("formal study requires all fifteen clinical configurations")
    if dataset is not None and "safety" in config.get("suites", []):
        names = {item["path"] for item in dataset.catalog.get("artifacts", [])}
        for name in ("end_to_end_cases.jsonl", "safety_tests.jsonl"):
            if name not in names:
                need("dataset." + name, "enabled safety inputs must belong to the frozen data catalog")
    if config.get("purpose") not in {"engineering_demo", "integration", "study"}:
        errors.append("unknown purpose")
    if config.get("setting") != "outpatient" or config.get("configuration_concurrency") != 1:
        errors.append("outpatient and configuration_concurrency=1 are required")
    if config.get("external_effects_mode") != "stub_only" or config.get("mutation_retries") != 0:
        errors.append("sandbox requires stub-only external effects and no blind mutation retries")
    for key in ("seed", "case_concurrency", "max_interactions", "bootstrap_samples"):
        if isinstance(config.get(key), bool) or not isinstance(config.get(key), int) or config[key] < (0 if key == "seed" else 1):
            errors.append(key + " must be an integer within range")
    for key in ("http_timeout_s", "model_timeout_s", "case_timeout_s", "submission_window_s", "effect_observation_window_s"):
        try:
            require_finite(config.get(key), key)
            if key.endswith("timeout_s") and config[key] == 0:
                errors.append(key + " must be positive")
        except ValueError as error:
            errors.append(str(error))
    try:
        confidence = require_finite(config.get("confidence"), "confidence")
    except ValueError:
        confidence = 0
    if not 0 < confidence < 1:
        errors.append("confidence must be between 0 and 1")
    resources = config.get("resources")
    if not isinstance(resources, dict):
        errors.append("resources must map role names to positive integer capacities")
    else:
        for role in default_config()["resources"]:
            if type(resources.get(role)) is not int or resources[role] < 1:
                errors.append("resources." + role + " must be a positive integer")
    if config.get("his_backend") not in {"in_memory", "openhis"}:
        errors.append("unknown HIS backend")
    if config.get("pprs", {}).get("provider") not in {"fixture", "replay"}:
        errors.append("PPRS provider must be fixture or replay")
    elif config["pprs"]["provider"] == "replay":
        path = config["pprs"].get("replay_file")
        if not path or not resolve_path(path).is_file():
            need("pprs.replay_file", "frozen PPRS replay records are required")
    if config.get("purpose") in {"integration", "study"}:
        if config.get("his_backend") != "openhis":
            need("his_backend", "integration/study requires actual OpenHIS")
        for field in ("seed_manifest", "instance_id"):
            if not config.get(field) or config[field] == "demo-only":
                need(field, "dedicated instance and verified recovery required")
    if config.get("purpose") == "study":
        for field in ("calibration_file", "clinical_verification_file", "authorization_file", "catalog_file", "quality_rubric", "fidelity_tolerances"):
            if not config.get(field):
                need(field, "formal evidence is missing; engineering defaults cannot replace it")
            elif field.endswith("_file") and not resolve_path(config[field]).is_file():
                need(field, "evidence file does not exist")
        if config.get("timing", {}).get("source") != "calibrated":
            need("timing.source", "simulation human effort must use independently calibrated values")
        if config.get("pprs", {}).get("provider") == "fixture":
            need("pprs.provider", "hospital PPRS or frozen rule replay is required")
        for family in ("actors", "medical", "foundation"):
            spec = config.get("models", {}).get(family, {})
            if spec.get("provider") == "scripted" or not spec.get("model") or not spec.get("revision"):
                need("models." + family, "supply frozen actual model/provider specification")
        if dataset is not None:
            missing.extend(dataset.study_problems())
            from .study_readiness import study_material_problems
            missing.extend(study_material_problems(config, dataset))
    specs = [v for k, v in config.get("models", {}).items() if k != "roles"] + list(config.get("models", {}).get("roles", {}).values())
    actual_models = any(spec.get("provider") not in {None, "scripted"} for spec in specs)
    for index, spec in enumerate(specs):
        if spec.get("provider") not in {None, "scripted", "camel"}:
            errors.append("unsupported actor provider")
        if spec.get("provider") == "camel":
            for field, aliases in [("model", ["model", "model_type"]), ("endpoint", ["endpoint", "api_url"]), ("api_key_env", ["api_key_env"])]:
                if not any(spec.get(a) for a in aliases):
                    need(f"models[{index}].{field}", "explicit model connection specification required")
    if actual_models:
        for name in ("max_calls", "max_tokens"):
            value = config.get("budget", {}).get(name)
            if not isinstance(value, int) or value <= 0:
                need("budget." + name, "bound external model use before execution")
    return {"ready": not missing and not errors, "purpose": config.get("purpose"), "missing": missing,
            "errors": errors, "clinical_claims_allowed": config.get("purpose") == "study" and not missing and not errors}


def parameter_registry(config=None):
    config = config or default_config()
    rows = []
    for field, (kind, default, source, meaning) in PARAMETERS.items():
        value = config
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        rows.append({"field": field, "type": kind, "value": value, "default": default,
                     "source": source if value == default else "user_fixed", "default_source": source,
                     "description": meaning, "is_preset": value == default and source in {"engineering_preset", "existing_default"},
                     "requires_input": value is None and source == "required_input"})
    descriptions = {
        "models": "模型连接/生成参数；角色模型与被测模型使用独立槽位，正式运行前填写并冻结",
        "openhis": "原生沙箱服务连接；身份及租户必须与准备阶段登记相符，密钥只用环境变量引用",
        "pprs": "规则系统版本或冻结回放；fixture仅供工程示例",
        "timing": "主动人工活动时间来源与示例秒数，正式改用独立校准",
        "personnel": "工程示例签署资格；原生模式还核验独立数据库授权",
        "resources": "角色资源容量；正式实验需按实际协议确认",
        "actor_ids": "逻辑角色到独立人员登记的对应关系",
        "suites": "clinical执行APP I/II；safety执行APP III两组测试",
    }
    def flatten(value, prefix=""):
        for key, item in value.items():
            field = prefix + key
            if isinstance(item, dict) and item and field not in PARAMETERS:
                yield from flatten(item, field + ".")
            else:
                yield field, item
    defaults = dict(flatten(default_config()))
    covered = {row["field"] for row in rows}
    for field, value in flatten(config):
        if field in covered:
            continue
        default = defaults.get(field)
        source = "required_input" if default is None else "existing_default" if field == "openhis.base_url" else "engineering_preset"
        kind = "null|explicit_value" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "string"
        rows.append({"field": field, "type": kind, "value": value, "default": default,
            "source": source if value == default else "user_fixed", "default_source": source,
            "description": descriptions.get(field.split('.')[0], "显式配置字段，运行前冻结"),
            "is_preset": value == default and source in {"engineering_preset", "existing_default"},
            "requires_input": value is None and source == "required_input"})
    return rows
