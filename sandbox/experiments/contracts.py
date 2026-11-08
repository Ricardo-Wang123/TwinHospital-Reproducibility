"""Serializable contracts shared by runners, tools, and independent observers."""
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol
from datetime import datetime, timezone
import hashlib
import json
import math


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    task: str
    input: dict
    source_group_id: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class StageResult:
    case_id: str
    task: str
    config_id: str
    status: str
    final_output: Any = None
    qualified: bool | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_id: str
    version: int
    content_hash: str
    parent_version: int | None = None
    status: str = "draft"
    author_id: str = ""


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    run_id: str
    case_id: str
    seq: int
    event_type: str
    occurred_at: str
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ActionEnvelope:
    run_id: str
    config_id: str
    case_id: str
    request_id: str
    epoch: int
    operation: str
    payload: dict
    attempt_id: str = "1"
    schema_version: str = "1"

    def to_dict(self):
        return asdict(self)


class HISPort(Protocol):
    def execute(self, action: dict, context: dict) -> dict: ...
    def snapshot(self, case_id: str) -> dict: ...
    def restore(self, seed: dict) -> None: ...
    def fingerprint(self) -> str: ...


def require_finite(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return value
