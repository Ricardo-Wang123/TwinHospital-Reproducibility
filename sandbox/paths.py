"""Shared paths, independent of the current working directory."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "sandbox" / "config" / "sandbox_config.yaml"


def project_path(value: str) -> str:
    if not value:
        return value
    path = Path(value)
    return str(path if path.is_absolute() else PROJECT_ROOT / path)
