"""Batch execution: restore between configurations, preserve all cases, seal predictions."""
import copy
import csv
import json
import random
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock, BoundedSemaphore

from sandbox.paths import PROJECT_ROOT
from .actors import AgentSessionFactory, FixturePPRS, ReplayPPRS
from .backends import InMemoryHIS, OpenHISPort
from .config import readiness, resolve_path, parameter_registry
from .contracts import canonical, digest, utc_now, require_finite
from .data import CaseProvider, read_jsonl
from .gateway import SandboxGateway, REQUIRED
from .lifecycle import InstanceLock, CommandSeedState, next_epoch
from .metrics import prescription_metrics, fact_metrics, effort_metrics, safety_metrics
from .store import RunStore, file_hash
from .workflows import execute_workflow
from .resource_schedule import RoleResourceScheduler

ACTIVITY_FIELDS = ["activity_id", "run_id", "case_id", "config_id", "task", "environment", "actor_id", "role",
                   "activity_type", "active_seconds", "timing_source", "start_sim_s", "end_sim_s", "resource_id", "waiting_s"]
ROLE_PERMISSIONS = {
    "doctor": ["case.bind", "triage.enqueue", "encounter.end", "note.save", "note.revise", "note.attest", "order.save", "order.attest"],
    "nurse": ["triage.enqueue"], "patient": ["payment.prepare", "payment.commit"],
    "scribe": ["note.save", "note.revise"], "pharmacist": [], "senior_pharmacist": [],
}


def variants():
    result = []
    for task, baseline, workflows in [
        ("prescription", "pprs_baseline", ("sequential", "parallel")),
        ("preconsult", "nurse_only", ("ai_only", "ai_nurse_review")),
        ("documentation", "manual_documentation", ("realtime", "postvisit"))]:
        result.append({"task": task, "workflow": baseline, "model_family": None, "config_id": task + ":" + baseline})
        result.extend({"task": task, "workflow": workflow, "model_family": family, "config_id": f"{task}:{workflow}:{family}"}
                      for workflow in workflows for family in ("medical", "foundation"))
    return result


class HumanEffortModel:
    def __init__(self, config, seed):
        self.config, self.rng = config, random.Random(seed)
        self.source = config["timing"]["source"]
        self.distributions = {}
        if self.source == "calibrated":
            value = json.loads(resolve_path(config["calibration_file"]).read_text(encoding="utf-8"))
            if not value.get("research_eligible") and config["purpose"] == "study":
                raise ValueError("calibration has no eligible independent measured source")
            for row in value["distributions"]:
                self.distributions[(row["task"], row["role"], row["activity_type"])] = row["samples_seconds"]

    def seconds(self, task, role, activity):
        if self.source == "calibrated":
            values = self.distributions.get((task, role, activity))
            if not values:
                raise ValueError("calibration missing activity: " + "/".join((task, role, activity)))
            return require_finite(self.rng.choice(values), "calibrated activity time")
        if self.source != "scripted":
            raise ValueError("simulation timing must be scripted or calibrated")
        timing = self.config["timing"]
        if "attest" in activity:
            key = "attestation"
        elif "rework" in activity or "revision" in activity:
            key = "revision"
        elif "collect" in activity or "documentation" == activity or "inquiry" in activity:
            key = "collection"
        else:
            key = "review"
        return require_finite(timing[key], "scripted " + key)


class BudgetedFactory:
    """Conservative reservation: UTF-8 input bytes + maximum output tokens per call."""
    def __init__(self, factory, config):
        self.factory, self.config = factory, config
        self.calls = self.reserved_tokens = 0
        self.lock = Lock()
        self.role_gates = {role: BoundedSemaphore(count) for role, count in config["resources"].items()}

    def actor_id(self, role):
        return self.factory.actor_id(role)

    def session(self, case_id, role, model_family=None):
        session = self.factory.session(case_id, role, model_family)
        # RuntimeAgent carries the validated limit of the model actually selected.
        model_timeout_s = getattr(session, "model_config", {}).get("timeout_seconds", self.config["model_timeout_s"])
        parent = self
        class BoundedSession:
            history_tokens = 0
            deadline = None
            timeout = parent.config["model_timeout_s"]

            def set_deadline(self, deadline):
                self.deadline = deadline

            def set_timeout(self, seconds):
                self.timeout = seconds

            def respond(self, observation):
                gate = parent.role_gates.get(role) if model_family is None else None
                remaining = (self.deadline - time.monotonic()) if self.deadline else parent.config["case_timeout_s"]
                if remaining <= 0 or (gate is not None and not gate.acquire(timeout=remaining)):
                    raise TimeoutError("case deadline exhausted waiting for role capacity")
                try:
                    remaining = self.deadline - time.monotonic() if self.deadline else self.timeout
                    if remaining <= 0:
                        raise TimeoutError("case deadline exceeded before model invocation")
                    if hasattr(session, "set_timeout"):
                        session.set_timeout(min(remaining, self.timeout, parent.config["model_timeout_s"], model_timeout_s))
                    return self._respond(observation)
                finally:
                    if gate is not None:
                        gate.release()

            def _respond(self, observation):
                if parent.factory.provider_for(role, model_family) != "scripted":
                    spec = parent.config["models"].get(model_family, {}) if model_family else parent.config["models"].get("roles", {}).get(role, parent.config["models"]["actors"])
                    input_bound = len(canonical(observation).encode("utf-8"))
                    output_bound = spec.get("max_tokens", 2000)
                    reservation = self.history_tokens + input_bound + output_bound + 2048
                    with parent.lock:
                        budget = parent.config["budget"]
                        if parent.calls + 1 > budget["max_calls"] or parent.reserved_tokens + reservation > budget["max_tokens"]:
                            raise RuntimeError("model call/token reservation budget exhausted")
                        parent.calls += 1
                        parent.reserved_tokens += reservation
                        self.history_tokens += input_bound + output_bound
                return session.respond(observation)
        return BoundedSession()


class DeadlineFactory:
    """Per-case views limit each model request to the remaining case budget."""
    def __init__(self, factory, deadline, model_timeout_s):
        self.factory, self.deadline, self.model_timeout_s = factory, deadline, model_timeout_s

    def actor_id(self, role):
        return self.factory.actor_id(role)

    def check(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("case deadline exceeded before actor invocation")
        return min(remaining, self.model_timeout_s)

    def session(self, *args, **kwargs):
        self.check()
        session = self.factory.session(*args, **kwargs)
        parent = self
        class Session:
            def respond(self, observation):
                timeout = parent.check()
                if hasattr(session, "set_deadline"):
                    session.set_deadline(parent.deadline)
                if hasattr(session, "set_timeout"):
                    session.set_timeout(timeout)
                result = session.respond(observation)
                parent.check()
                return result
        return Session()


def _factory(config):
    def specification(value):
        return {**value, "model_type": value.get("model_type") or value.get("model"),
                "api_url": value.get("api_url") or value.get("endpoint"), "timeout": config["model_timeout_s"]}
    actors = specification(config["models"]["actors"])
    factory = AgentSessionFactory({"provider": actors.get("provider", "scripted"), "seed": config["seed"],
        "actor_ids": config.get("actor_ids", {}),
        "role_models": {role: specification(config["models"].get("roles", {}).get(role, actors))
                        for role in ["doctor", "nurse", "patient", "pharmacist", "senior_pharmacist", "scribe"]},
        "intervention_models": {family: specification(config["models"][family]) for family in ["medical", "foundation"]}})
    return BudgetedFactory(factory, config)


def _revision(path):
    def git(*args):
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, timeout=15)
        return result.stdout if result.returncode == 0 else b"unavailable"
    return {"head": git("rev-parse", "HEAD").decode().strip(), "tracked_diff_sha256": digest(git("diff", "HEAD").decode(errors="replace")),
            "status_sha256": digest(git("status", "--short").decode(errors="replace"))}


def _demo_score(row, reference):
    """Literal fixture checks, explicitly excluded from clinical evidence."""
    row["scoring_source"] = "engineering_literal_check"
    row["quality_source"] = "engineering_script"
    if row["status"] != "completed":
        row.update(qualified=False, metrics={"qualified": 0.0}, annotations=[])
        return
    if row["task"] == "prescription":
        decision = "block" if row.get("final_decision") in {"reject", "block"} else row.get("final_decision")
        row["final_decision"] = decision
        correct = (decision == "block") == reference["positive"] if decision else False
        row.update(qualified=correct, metrics={"qualified": float(correct)}, annotations=[])
        return
    output = row.get("final_output") or {}
    known = {f["fact_id"]: f for f in output.get("facts", [])}
    text = output.get("text", "")
    annotations = []
    for item in reference["required_items"]:
        fact = known.get(item["fact_id"])
        if row["task"] == "documentation":
            correct = item["fact_id"] + ": " + str(item["value"]) in text
            label = "correct" if correct else "omitted"
        elif fact is None or fact.get("status") in {"unknown", "not_asked"}:
            label = "omitted"
        else:
            label = "correct" if fact.get("value") == item["value"] else "incorrect"
        annotations.append({"case_id": row["case_id"], "item_id": item["fact_id"], "label": label, "unit": "fact"})
    scores = fact_metrics(annotations)
    correct = all(a["label"] == "correct" for a in annotations)
    row.update(qualified=correct, annotations=annotations, metrics={"qualified": float(correct),
        "factual_accuracy": scores["factual_accuracy"], "omission_rate": scores["omission_rate"], "unsupported_rate": scores["unsupported_rate"]})
    if row["task"] == "preconsult":
        row["first_pass_completed"] = correct


class CaseRuntime:
    def __init__(self, config, store, backend, gateway, factory, case, variant, run_id, epoch):
        self.config, self.store, self.backend, self.gateway, self.factory = config, store, backend, gateway, factory
        self.case, self.variant, self.run_id, self.epoch = case, variant, run_id, epoch
        self.activities = []
        self.seq = self.seconds = 0
        self.started = time.monotonic()
        self.deadline = self.started + config["case_timeout_s"]
        self.case_factory = DeadlineFactory(factory, self.deadline, config["model_timeout_s"])
        self.prohibited_effects = set()
        # Clinical tasks are independently standardized; APP III shares resources.
        self.resource_scheduler = getattr(gateway, "resource_scheduler", None) if variant["config_id"].startswith("safety:") else RoleResourceScheduler(config["resources"])
        self.seconds = require_finite(case.get("input", {}).get("arrival_time_s", 0), "arrival_time_s")
        self.effort_model = HumanEffortModel(config, int(digest([config["seed"], case["case_id"]])[:12], 16))
        refs = case.get("input", {}).get("native_refs", {})
        if config["his_backend"] == "openhis" and (not refs.get("patient_id") or not refs.get("encounter_id")):
            raise ValueError("OpenHIS requires prevalidated native_refs in the fixed seed")
        self.patient_id = refs.get("patient_id", "patient:" + case["case_id"])
        self.encounter_id = refs.get("encounter_id", "encounter:" + case["case_id"])
        self.native_refs = refs
        self.milestones = {}

    def emit(self, event_type, **fields):
        if event_type == "transcript_available":
            self.seconds = max(self.seconds, fields.get("available_at_ms", 0) / 1000)
        if event_type in {"encounter_timeline", "encounter_end_reached"}:
            self.milestones["fixed_encounter_end"] = fields["encounter_end_ms"] / 1000
        self.seq += 1
        event = {"event_id": f"{self.run_id}:{self.case['case_id']}:{self.seq}", "run_id": self.run_id,
            "case_id": self.case["case_id"], "config_id": self.variant["config_id"], "seq": self.seq,
            "event_type": event_type, "occurred_at": utc_now(), "sim_time_s": self.seconds, **fields}
        self.store.append("events.jsonl", event)
        return event

    def effort(self, role, activity_type, seconds=None):
        amount = self.effort_model.seconds(self.case.get("task", "end_to_end"), role, activity_type) if seconds is None else require_finite(seconds, "active_seconds")
        reservation = self.resource_scheduler.reserve(self.case["case_id"], role, amount, earliest_s=self.seconds) if self.resource_scheduler else {
            "scheduled_start": self.seconds, "scheduled_end": self.seconds + amount, "resource_id": None, "waiting_s": 0}
        row = {"activity_id": uuid.uuid4().hex, "run_id": self.run_id, "case_id": self.case["case_id"],
            "config_id": self.variant["config_id"], "task": self.case.get("task", "end_to_end"), "environment": "simulation",
            "actor_id": self.factory.actor_id(role), "role": role, "activity_type": activity_type,
            "active_seconds": amount, "timing_source": self.effort_model.source,
            "start_sim_s": reservation["scheduled_start"], "end_sim_s": reservation["scheduled_end"],
            "resource_id": reservation["resource_id"], "waiting_s": reservation["waiting_s"]}
        self.seconds = reservation["scheduled_end"]
        self.activities.append(row)
        self.emit("human_activity", activity_id=row["activity_id"], role=role, activity_type=activity_type, active_seconds=amount)

    def context(self, role):
        return {"actor_id": self.factory.actor_id(role), "role": role, "patient_id": self.patient_id,
                "encounter_id": self.encounter_id, "epoch": self.epoch, "permissions": ROLE_PERMISSIONS.get(role, [])}

    def perform(self, operation, payload, role="doctor"):
        if time.monotonic() > self.deadline:
            raise TimeoutError("case deadline exceeded before operation")
        if self.config["his_backend"] == "openhis" and operation in {"order.save", "payment.prepare", "payment.commit"}:
            from .native_mapping import map_native_request
            payload = {**payload, "native_request": map_native_request(operation, payload, self.native_refs)}
        action = {"schema_version": "1", "run_id": self.run_id, "config_id": self.variant["config_id"],
            "case_id": self.case["case_id"], "attempt_id": "1", "request_id": uuid.uuid4().hex,
            "epoch": self.epoch, "operation": operation, "payload": payload}
        result = self.gateway.execute(action, self.context(role))
        self.prohibited_effects.update(result.get("prohibited_effects", []))
        self.emit("action_completed", operation=operation, outcome=result["outcome"], request_id=action["request_id"])
        if result.get("status") in {"executed", "replayed"}:
            self.milestones[operation] = self.milestones.get("fixed_encounter_end", self.seconds) if operation == "encounter.end" else self.seconds
        return result

    def bind(self):
        result = self.perform("case.bind", {"patient_id": self.patient_id, "encounter_id": self.encounter_id})
        if result.get("status") not in {"executed", "replayed"}:
            raise RuntimeError("case bind failed: " + str(result.get("reason")))


def run(config):
    config = copy.deepcopy(config)
    provider = CaseProvider(resolve_path(config["dataset"]))
    ready = readiness(config, provider)
    if not ready["ready"]:
        raise ValueError("configuration is not ready: " + canonical(ready))
    # Validate pluggable resources before creating a frozen batch directory.
    factory = _factory(config)
    pprs = FixturePPRS(rule_version=config["pprs"]["version"]) if config["pprs"]["provider"] == "fixture" else ReplayPPRS(
        read_jsonl(resolve_path(config["pprs"]["replay_file"])), rule_version=config["pprs"]["version"])
    backend = InMemoryHIS() if config["his_backend"] == "in_memory" else OpenHISPort(
        {**config["openhis"], "http_timeout_s": config["http_timeout_s"], "personnel": config["personnel"]})
    state_dir = resolve_path(config["state_dir"])
    recovery = None if config["his_backend"] == "in_memory" else CommandSeedState(resolve_path(config["seed_manifest"]), config["instance_id"])
    batch_id = "th-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output = resolve_path(config["output"]) / batch_id
    store = RunStore(output)
    all_variants = variants() if "clinical" in config.get("suites", []) else []
    if config.get("selected_configurations"):
        all_variants = [v for v in all_variants if v["config_id"] in config["selected_configurations"]]
    selected_tasks = {v["task"] for v in all_variants}
    cases = [case for case in provider.execution_cases() if case["task"] in selected_tasks]
    store.write_json("config.json", config)
    safety_inputs = {}
    if "safety" in config.get("suites", []):
        for name in ("end_to_end_cases.jsonl", "safety_tests.jsonl"):
            rows = read_jsonl(provider.root / name)
            safety_inputs[name] = digest(rows)
            store._target(name).touch(exist_ok=False)
            for row in rows:
                store.append(name, row)
    for case in cases:
        store.append("case_inputs.jsonl", {**case, "input_sha256": digest(case["input"])})
    sources = {p.relative_to(PROJECT_ROOT).as_posix(): file_hash(p)
               for folder in ("sandbox", "agents", "prompts", "tools")
               for p in (PROJECT_ROOT / folder).rglob("*.py") if "output" not in p.parts}
    frozen_materials = {}
    for field in ("seed_manifest", "calibration_file", "clinical_verification_file", "authorization_file", "catalog_file"):
        if config.get(field):
            path = resolve_path(config[field])
            frozen_materials[field] = {"path": str(path), "sha256": file_hash(path)}
    if config["pprs"].get("replay_file"):
        path = resolve_path(config["pprs"]["replay_file"])
        frozen_materials["pprs.replay_file"] = {"path": str(path), "sha256": file_hash(path)}
    native_root = PROJECT_ROOT.parent / "openhis-itai-v1.3"
    native_sources = {p.relative_to(native_root).as_posix(): file_hash(p)
        for folder in (native_root / "openhis-server-new/openhis-application/src/main/java/com/openhis/web/sandbox", native_root / "sandbox-support/migrations")
        if folder.exists() for p in folder.rglob("*") if p.is_file()}
    manifest = {"schema_version": "1", "run_id": batch_id, "purpose": config["purpose"], "frozen_at": utc_now(),
        "datasets_hash": provider.dataset_hash, "resolved_config_hash": digest(config), "configuration_ids": [v["config_id"] for v in all_variants] or ["safety"],
        "configurations": all_variants,
        "case_hashes": {c["case_id"]: digest(c["input"]) for c in cases}, "code": {
            "MedicalAgent": _revision(PROJECT_ROOT), "OpenHIS": _revision(PROJECT_ROOT.parent / "openhis-itai-v1.3")},
        "source_hashes": sources, "parameter_registry": parameter_registry(config), "models": config["models"],
        "frozen_materials": frozen_materials, "native_extension_source_hashes": native_sources,
        "safety_input_hashes": safety_inputs,
        "requirements_sha256": file_hash(PROJECT_ROOT / "requirements.txt"),
        "rule_version": config["pprs"]["version"], "seed": config["seed"], "research_eligible": config["purpose"] == "study"}
    store.write_json("run_manifest.json", manifest)
    store.write_json("data_catalog.json", provider.catalog)
    for name in ["events.jsonl", "case_results.jsonl", "action_outcomes.jsonl", "safety_outcomes.jsonl", "end_to_end_outcomes.jsonl"]:
        store._target(name).touch(exist_ok=False)
    results, activities, local_rows, e2e_rows, metrics = [], [], [], [], {}
    fatal = None
    with InstanceLock(state_dir / (digest(config["instance_id"])[:16] + ".lock")):
        def configuration(variant, number):
            for material in frozen_materials.values():
                if file_hash(material["path"]) != material["sha256"]:
                    raise RuntimeError("frozen input material changed during run")
            if config["purpose"] == "study":
                for relative, expected in sources.items():
                    if file_hash(PROJECT_ROOT / relative) != expected:
                        raise RuntimeError("frozen source changed during study")
                for item in provider.catalog["artifacts"]:
                    if file_hash(provider.root / item["path"]) != item["sha256"]:
                        raise RuntimeError("frozen dataset changed during study")
            epoch = next_epoch(state_dir)
            run_id = batch_id + "-" + str(number)
            if recovery:
                proof = recovery.restore(epoch, run_id, variant["config_id"])
                store.append("restore_receipts.jsonl", {"config_id": variant["config_id"], "proof": proof, "commands": recovery.receipts})
            else:
                backend.restore({"personnel": config["personnel"]})
            if hasattr(backend, "configure_run"):
                backend.configure_run(run_id, epoch, variant["config_id"], config)
            gateway = SandboxGateway(backend, epoch=epoch, observation_window_s=config["effect_observation_window_s"],
                sink=lambda row: store.append("action_outcomes.jsonl", row))
            gateway.resource_scheduler = RoleResourceScheduler(config["resources"])
            return run_id, epoch, gateway
        try:
            for number, variant in enumerate(all_variants):
                run_id, epoch, gateway = configuration(variant, number)
                subset, subset_activities = [], []
                for case in (c for c in cases if c["task"] == variant["task"]):
                    runtime = None
                    try:
                        runtime = CaseRuntime(config, store, backend, gateway, factory, case, variant, run_id, epoch)
                        runtime.bind()
                        row = execute_workflow(case, variant, actor_factory=runtime.case_factory, pprs=pprs,
                            emit=runtime.emit, perform=runtime.perform, effort=runtime.effort, max_interactions=config["max_interactions"])
                        if time.monotonic() > runtime.deadline:
                            row.update(status="failed", failure_reason="case_timeout")
                    except Exception as error:
                        row = {"case_id": case["case_id"], **variant, "status": "failed", "failure_reason": type(error).__name__ + ": " + str(error), "final_output": None}
                    row.update(run_id=run_id, environment="simulation", source_group_id=case["source_group_id"], input_sha256=digest(case["input"]))
                    if runtime:
                        activities.extend(runtime.activities); subset_activities.extend(runtime.activities)
                        if "encounter.end" in runtime.milestones:
                            origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
                            row["encounter_ended"] = (origin + timedelta(seconds=runtime.milestones["encounter.end"])).isoformat()
                            if "note.attest" in runtime.milestones:
                                row["physician_attested"] = (origin + timedelta(seconds=runtime.seconds)).isoformat()
                            row["turnaround_time_source"] = config["timing"]["source"]
                    if config["purpose"] == "engineering_demo":
                        _demo_score(row, provider.references[case["case_id"]])
                    else:
                        row.update(qualified=None, scoring_source="awaiting_independent_review", metrics={})
                    row["final_output_sha256"] = digest(row.get("final_output"))
                    store.append("case_results.jsonl", row)
                    subset.append(row); results.append(row)
                summary = {"effort": effort_metrics(subset, subset_activities)}
                if variant["task"] == "prescription":
                    summary["quality"] = prescription_metrics([{**r, "reference": provider.references[r["case_id"]]} for r in subset])
                elif config["purpose"] == "engineering_demo":
                    summary["quality"] = fact_metrics([a for r in subset for a in r.get("annotations", [])])
                metrics[variant["config_id"]] = summary
                gateway.epoch = -1
                if hasattr(backend, "deactivate"):
                    backend.deactivate()
            if "safety" in config.get("suites", []):
                from .safety import run_safety
                local_rows, e2e_rows = run_safety(config, store.path, configuration, store, backend, factory, batch_id)
        except Exception as error:
            fatal = type(error).__name__ + ": " + str(error)
            store.append("events.jsonl", {"event_type": "batch_aborted", "reason": fatal, "occurred_at": utc_now()})
            observed = {(r["case_id"], r["config_id"]) for r in results}
            for variant in all_variants:
                for case in (c for c in cases if c["task"] == variant["task"]):
                    if (case["case_id"], variant["config_id"]) not in observed:
                        row = {"case_id": case["case_id"], **variant, "run_id": batch_id,
                            "environment": "simulation", "status": "not_run", "failure_reason": "batch_aborted: " + fatal,
                            "qualified": None, "quality_source": None, "final_output": None, "final_decision": None}
                        store.append("case_results.jsonl", row); results.append(row)
            if "safety" in config.get("suites", []):
                local_rows = read_jsonl(store.path / "safety_outcomes.jsonl")
                e2e_rows = read_jsonl(store.path / "end_to_end_outcomes.jsonl")
                observed_local = {r["test_id"] for r in local_rows}
                observed_e2e = {r["case_id"] for r in e2e_rows}
                for test in read_jsonl(store.path / "safety_tests.jsonl"):
                    if test["test_id"] not in observed_local:
                        row = {**test, "run_id": batch_id, "execution_status": "not_run",
                            "outcome": "unresolved" if test["is_error"] else "control_failed",
                            "evidence_status": "missing", "failure_reason": "batch_aborted: " + fatal}
                        store.append("safety_outcomes.jsonl", row); local_rows.append(row)
                for case in read_jsonl(store.path / "end_to_end_cases.jsonl"):
                    if case["case_id"] not in observed_e2e:
                        row = {"case_id": case["case_id"], "run_id": batch_id, "status": "failed", "execution_status": "not_run",
                            "unsafe_effect": False, "effect_evidence": "missing", "duration_s": None,
                            "failure_reason": "batch_aborted: " + fatal}
                        store.append("end_to_end_outcomes.jsonl", row); e2e_rows.append(row)
        store.write_csv("human_activity.csv", activities, ACTIVITY_FIELDS)
        summary = {"run_id": batch_id, "run_dir": str(output), "purpose": config["purpose"],
            "clinical_cases": len(results), "clinical_failures": sum(r["status"] != "completed" for r in results),
            "local_tests": len(local_rows), "end_to_end_cases": len(e2e_rows), "fatal_error": fatal,
            "clinical_claims_allowed": False, "evidence_note": "engineering results or awaiting independent blinded clinical review and hospital comparison",
            "model_calls": factory.calls, "reserved_tokens": factory.reserved_tokens,
            "metrics": metrics, "safety": safety_metrics(local_rows), "end_to_end": safety_metrics(e2e_rows, end_to_end=True)}
        store.write_json("summary.json", summary)
        store.seal(config["custodian"])
        return summary
