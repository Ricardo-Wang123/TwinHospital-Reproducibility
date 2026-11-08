"""Small public contracts for linked encounters; no models or HIS calls on import."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json


MODEL_FAMILIES = ("deepseek", "glm")
SUPPORTED_MODEL_CONDITIONS = MODEL_FAMILIES + ('glm51','glm52','panguflash','pangupro','qwen32b','qwen30b','kimi26','tool_a','tool_b')
WORKFLOWS = {
    "preconsult": ("nurse_only", "ai_only", "ai_nurse_review"),
    "prescription": ("pprs_baseline", "sequential", "parallel"),
    "documentation": ("manual_documentation", "realtime", "postvisit"),
}
BASELINES = {"nurse_only", "pprs_baseline", "manual_documentation"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class TaskCondition:
    task: str
    workflow: str
    model_family: str | None = None

    def __post_init__(self):
        if self.task not in WORKFLOWS or self.workflow not in WORKFLOWS[self.task]:
            raise ValueError("unsupported paper task or workflow")
        if self.workflow in BASELINES:
            if self.model_family is not None:
                raise ValueError("baseline must use fixed role model")
        elif self.model_family not in SUPPORTED_MODEL_CONDITIONS:
            raise ValueError("AI tool condition must be an explicitly supported model label")

    @property
    def config_id(self):
        return ":".join(x for x in (self.task, self.workflow, self.model_family) if x)

    def to_dict(self):
        return {**asdict(self), "config_id": self.config_id}


def paper_conditions(task=None, model_families=None):
    """Three workflow conditions per task for one AI tool; legacy pairs retained."""
    families=MODEL_FAMILIES if model_families is None else tuple(model_families)
    if len(families) not in (1,2) or len(set(families))!=len(families) or any(f not in SUPPORTED_MODEL_CONDITIONS for f in families):
        raise ValueError('Declare one fixed AI tool or two distinct legacy tool conditions')
    conditions = []
    for name, flows in WORKFLOWS.items():
        conditions.append(TaskCondition(name, flows[0]))
        conditions.extend(TaskCondition(name, flow, family)
                          for flow in flows[1:] for family in families)
    return [item for item in conditions if task is None or item.task == task]


def baseline_conditions():
    return {task: TaskCondition(task, flows[0]) for task, flows in WORKFLOWS.items()}


def linked_config_id(conditions):
    """Identify the full ordered workflow, rather than its one modified stage."""
    return 'linked:' + digest([conditions[task].to_dict() for task in WORKFLOWS])[:12]


def runtime_case(value):
    """The clinical runner never takes an evaluator/provenance object as an observation."""
    keys = ("case_id", "patient_key", "group", "data_kind", "split",
            "patient_initial", "his_initial", "formulary")
    result = {key: deepcopy(value[key]) for key in keys if key in value}
    for key in ("case_id", "patient_key", "data_kind", "patient_initial", "his_initial", "formulary"):
        if key not in result:
            raise ValueError("case missing " + key)
    if not isinstance(result["patient_initial"].get("patient_facts"), list):
        raise ValueError("patient_initial.patient_facts must be a list")
    if not isinstance(result["his_initial"], dict) or not isinstance(result["formulary"], list):
        raise ValueError("invalid hospital initial view or formulary")
    ids = [item.get("fact_id") for item in result["patient_initial"]["patient_facts"]]
    if None in ids or len(set(ids)) != len(ids):
        raise ValueError("patient facts require unique fact_id values")
    return result


class WorkflowFailure(RuntimeError):
    """Observable protocol or execution failure, never silently dropped from a cohort."""


class ClinicalDecisionIncomplete(WorkflowFailure):
    """A recorded clinical decision leaves the specified task chain incomplete.

    This is neither an independently established clinical error nor an
    infrastructure exception. The source decision remains in the run evidence.
    """


def validate_prescription(value):
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise WorkflowFailure("prescription must contain items")
    if not value["items"]:
        raise WorkflowFailure("no prescription issued; medication workflow is not applicable")
    for item in value["items"]:
        if not isinstance(item, dict) or not item.get("drug_code"):
            raise WorkflowFailure("prescription item requires an explicit drug_code")
        for key in ("dose", "dose_unit", "route", "frequency", "quantity", "quantity_unit"):
            if key not in item or item[key] in (None, ""):
                raise WorkflowFailure("prescription item missing " + key)
    # Clinical dose correctness belongs to the review/independent evaluator, not this parser.
    return deepcopy(value)
