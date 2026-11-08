"""Load the experiment YAML, optional local overrides, and environment values."""

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from sandbox.paths import PROJECT_ROOT, project_path


class ConfigLoader:
    def __init__(self, config_path: str, env_name: str = "local"):
        self.config_path = Path(config_path).resolve()
        self.env_name = env_name
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        self.config = self._read(self.config_path)
        override_path = self.config_path.with_name(
            f"{self.config_path.stem}.{env_name}{self.config_path.suffix}"
        )
        if override_path != self.config_path and override_path.exists():
            self._deep_merge(self.config, self._read(override_path))
        self.config = self._expand_env(self.config)
        for field in ("experiment", "model", "pipeline"):
            if not isinstance(self.config.get(field), dict):
                raise ValueError(f"配置缺少对象字段: {field}")

    @staticmethod
    def _read(path: Path) -> dict:
        with path.open(encoding="utf-8-sig") as stream:
            if path.suffix in (".yaml", ".yml"):
                value = yaml.safe_load(stream)
            elif path.suffix == ".json":
                value = json.load(stream)
            else:
                raise ValueError(f"不支持的配置格式: {path.suffix}")
        if not isinstance(value, dict):
            raise ValueError(f"配置必须是对象: {path}")
        return value

    @classmethod
    def _deep_merge(cls, base: dict, override: dict) -> None:
        for key, value in override.items():
            if isinstance(base.get(key), dict) and isinstance(value, dict):
                cls._deep_merge(base[key], value)
            else:
                base[key] = value

    @classmethod
    def _expand_env(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._expand_env(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._expand_env(item) for item in value]
        if isinstance(value, str):
            return re.sub(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}",
                lambda match: os.getenv(match[1]) or match[2] or "",
                value,
            )
        return value

    def get(self, key: str, default: Any = None) -> Any:
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return default if value is None else value

    def apply_cli_overrides(self, mode=None, disabled_tools=None) -> None:
        if mode:
            modes = {"ai_triage": "ai", "no_ai_triage": "no_ai"}
            self.config["pipeline"]["stages"]["triage"]["mode"] = modes[mode]
        if disabled_tools:
            ablation = self.config.setdefault("ablation", {})
            ablation["disabled_tools"] = list(dict.fromkeys(
                ablation.get("disabled_tools", []) + list(disabled_tools)
            ))

    def get_model_config(self, model_name=None) -> dict:
        model = deepcopy(self.config["model"])
        if model_name:
            model.update(self.get(f"models.{model_name}", {}))
        return model

    def get_pipeline_config(self) -> dict:
        return self.config["pipeline"]

    def get_stage_config(self, stage_name: str) -> dict:
        stage = deepcopy(self.get(f"pipeline.stages.{stage_name}", {}))
        stage.setdefault("execution_mode", self.get("pipeline.execution_mode", "serial"))
        return stage

    def get_resources_config(self) -> dict:
        return self.get("resources", {})

    def get_paths_config(self) -> dict:
        return self.get("paths", {})

    def resolve_path(self, path_key: str, default: str = "") -> str:
        return project_path(self.get(f"paths.{path_key}", default))

    def collect_agent_profiles(self, num_doctors=None, num_nurses=None, num_patients=None) -> dict:
        result = {}
        for role, count in (("doctor", num_doctors), ("patient", num_patients)):
            count = int(count if count is not None else self.get(f"resources.num_{role}s", 1))
            if count < 1:
                raise ValueError(f"resources.num_{role}s 必须大于 0")
            folder = self.resolve_path(f"{role}_profiles")
            files = sorted(Path(folder).glob(f"{role}_*.json")) if folder else []
            if len(files) < count:
                raise ValueError(f"{role} 档案不足：需要 {count} 个，找到 {len(files)} 个；目录：{folder}")
            result[f"{role}s"] = [str(path) for path in files[:count]]
        count = int(num_nurses if num_nurses is not None else self.get("resources.num_nurses", 1))
        if count < 1:
            raise ValueError("resources.num_nurses 必须大于 0")
        result["nurses"] = [
            {"nurse_id": f"NURSE_{i + 1:03d}", "name": f"护士{i + 1}"}
            for i in range(count)
        ]
        return result

    def to_dict(self) -> dict:
        config = deepcopy(self.config)
        config["paths"] = {key: project_path(value) for key, value in self.get_paths_config().items()}
        return config
