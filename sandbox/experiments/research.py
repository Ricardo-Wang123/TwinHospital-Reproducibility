"""Offline research contracts, sealed exports, empirical calibration and reviews.

Only export_hospital_package/import_hospital_results write files, exclusively to
new destinations. No network or clinical runtime imports. SHA-256 checks establish
local integrity, NOT independent custody or proof that a human really executed a
workflow. Hospital provenance declarations need independent operational evidence.
All rates use fractions, durations seconds, missing results None (never NaN).
"""
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import random
import re
from statistics import fmean, stdev

from .metrics import _bool, _checked, _number, _quantile, _ratio, _text, _timestamp


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _loads(text):
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _json(path):
    return _loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path):
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            row = _loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSONL rows must be objects")
            rows.append(row)
    return rows


def _safe_child(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("artifact paths must be relative POSIX paths")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or PureWindowsPath(relative).drive or any(p in ("..", ".") for p in relative.split("/")):
        raise ValueError("artifact path escapes root")
    root = Path(root).resolve()
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink artifact paths are not allowed")
    path = path.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("artifact path escapes root")
    return path


def _hash_string(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _verified_run(run_dir):
    run = Path(run_dir).resolve()
    seal = _json(_safe_child(run, "seal.json"))
    if not isinstance(seal, dict) or not isinstance(seal.get("artifacts"), dict):
        raise ValueError("invalid seal")
    _text(seal.get("custodian"), "custodian")
    artifacts = seal["artifacts"]
    required = {"config.json", "case_inputs.jsonl", "case_results.jsonl", "events.jsonl", "human_activity.csv"}
    if not required <= artifacts.keys():
        raise ValueError("seal is missing required run artifacts")
    for name, expected in artifacts.items():
        if name == "seal.json":
            raise ValueError("seal cannot contain its own digest")
        path = _safe_child(run, name)
        _hash_string(expected, "artifact hash")
        if not path.is_file() or _sha(path) != expected:
            raise ValueError(f"sealed artifact failed integrity check: {name}")
    manifest_path = _safe_child(run, "run_manifest.json")
    if _sha(manifest_path) != _hash_string(seal.get("manifest_sha256"), "manifest_sha256"):
        raise ValueError("manifest integrity check failed")
    manifest = _json(manifest_path)
    for name in ("run_id", "purpose"):
        _text(manifest.get(name), name)
    for name in ("datasets_hash", "resolved_config_hash"):
        _hash_string(manifest.get(name), name)
    config = _json(_safe_child(run, "config.json"))
    if manifest["resolved_config_hash"] not in {_sha(run / "config.json"), _digest(config)}:
        raise ValueError("resolved configuration hash mismatch")
    sealed_at = _timestamp(seal.get("sealed_at"))
    if _timestamp(manifest.get("frozen_at")) > sealed_at:
        raise ValueError("configuration was frozen after sealing")
    configuration_ids = manifest.get("configuration_ids")
    if not isinstance(configuration_ids, list) or not configuration_ids:
        raise ValueError("configuration_ids must be a nonempty list")
    for value in configuration_ids:
        _text(value, "config_id")
    if len(set(configuration_ids)) != len(configuration_ids):
        raise ValueError("duplicate configuration IDs")
    hashes = manifest.get("case_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("case_hashes must identify the sealed cases")
    for case, digest in hashes.items():
        _text(case, "case_id")
        _hash_string(digest, "case input hash")
    cases = {}
    for row in _jsonl(run / "case_inputs.jsonl"):
        case = _text(row.get("case_id"), "case_id")
        if case in cases or case not in hashes:
            raise ValueError("duplicate or unmanifested case input")
        _text(row.get("task"), "task")
        if row.get("input_sha256", hashes[case]) != hashes[case]:
            raise ValueError("case input hash declaration mismatch")
        input_payload = row.get("input", row.get("visible_inputs"))
        if not isinstance(input_payload, (dict, list)) or _digest(input_payload) != hashes[case]:
            raise ValueError("actual case input digest does not match manifest")
        cases[case] = row
    if set(cases) != set(hashes):
        raise ValueError("manifest and case input sets differ")
    return run, manifest, seal, config, cases


def calibrate(activity_rows, anchor_rows) -> dict:
    """Return empirical task/role/activity distributions and their support.

    anchor_rows is a metadata catalog: case_id/task/split/source_group_id plus
    source_kind. split may be anchor/calibration or test/validation; test rows
    supply the explicit exclusion set and can NEVER contribute activity. A
    source group cannot occur on both sides, even across tasks. Activities must
    match anchor task and have nonnegative active_seconds, role/activity_type,
    timing_source. Only measured times from real_deidentified/real_anchor/deidentified_real/
    hospital_observed anchors are research-eligible; demo times remain marked.

    Omitting test metadata or source_group_id means isolation_verified=False,
    not an inferred guarantee of independence. Verification applies only to this
    supplied catalog. Repeated intervals do not increase n_cases/support groups.
    """
    anchors, tests = {}, {}
    case_splits = {}
    source_sets = {"anchor": set(), "test": set()}
    missing_groups = 0
    for row in anchor_rows:
        case = _text(row.get("case_id"), "case_id")
        task = _text(row.get("task"), "task")
        split = row.get("split")
        if split not in ("anchor", "calibration", "test", "validation"):
            raise ValueError("catalog must explicitly identify anchor or test split")
        side = "anchor" if split in ("anchor", "calibration") else "test"
        if case in case_splits:
            raise ValueError("duplicate case or anchor/test overlap")
        case_splits[case] = side
        group = row.get("source_group_id")
        if group:
            source_sets[side].add(_text(group, "source_group_id"))
        else:
            missing_groups += 1
        (anchors if side == "anchor" else tests)[case] = row
    if source_sets["anchor"] & source_sets["test"]:
        raise ValueError("anchor and test source groups overlap")
    bins = defaultdict(list)
    activity_ids = set()
    for activity in activity_rows:
        case = activity.get("case_id")
        if case not in anchors:
            raise ValueError("calibration activity must refer to an anchor")
        if activity.get("task") != anchors[case]["task"]:
            raise ValueError("activity task differs from anchor task")
        if activity.get("included_in_activity_id"):
            raise ValueError("calibration expects atomic activities, not nested subtotal rows")
        if activity.get("activity_id"):
            if activity["activity_id"] in activity_ids:
                raise ValueError("duplicate calibration activity_id")
            activity_ids.add(activity["activity_id"])
        key = (activity["task"], _text(activity.get("role"), "role"), _text(activity.get("activity_type"), "activity_type"))
        _number(activity.get("active_seconds"), "active_seconds", 0)
        _text(activity.get("timing_source"), "timing_source")
        bins[key].append(activity)
    distributions = []
    for (task, role, activity_type), activities in sorted(bins.items()):
        values = sorted(a["active_seconds"] for a in activities)
        used_cases = {a["case_id"] for a in activities}
        groups = {anchors[c].get("source_group_id") for c in used_cases} - {None, ""}
        distributions.append(dict(task=task, role=role, activity_type=activity_type,
            n_intervals=len(values), n_cases=len(used_cases), n_source_groups=len(groups),
            samples_seconds=values, mean_seconds=fmean(values), median_seconds=_quantile(values, .5),
            p05_seconds=_quantile(values, .05), p95_seconds=_quantile(values, .95),
            min_seconds=min(values), max_seconds=max(values), sample_sd_seconds=stdev(values) if len(values) > 1 else None,
            timing_sources=dict(Counter(a["timing_source"] for a in activities))))
    isolation = bool(anchors and tests) and missing_groups == 0
    real_sources = {"real_deidentified", "real_anchor", "deidentified_real", "hospital_observed"}
    research_eligible = isolation and bool(activity_rows) and all(a["timing_source"] == "measured" for a in activity_rows) and all(r.get("source_kind") in real_sources for r in anchors.values())
    return _checked(dict(schema_version="1", distributions=distributions, anchor_cases=len(anchors), test_cases=len(tests),
        anchors_by_task=dict(Counter(r["task"] for r in anchors.values())),
        unused_anchor_cases=len(set(anchors) - {a["case_id"] for a in activity_rows}),
        missing_source_groups=missing_groups, isolation_verified=isolation,
        isolation_scope="supplied_catalog_only", research_eligible=research_eligible,
        calibration_inputs_sha256=_digest({"catalog": anchor_rows, "activities": activity_rows})))


_FORBIDDEN_VISIBLE = {
    "gold", "groundtruth", "diagnosisgroundtruth", "reference", "referencestandard",
    "hidden", "hiddeninputs", "expectedoutput", "expectedanswer", "correctanswer",
    "platformoutput", "simulationoutput", "caseresults", "evaluationresults",
    "label", "labels", "qualified", "answerkey", "expecteddecision", "referenceplan",
    "apikey", "password", "secret", "token", "authorization",
}


def _visible_input(value):
    """Fail on nested hidden fields; never stringify an unexamined input blob."""
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _FORBIDDEN_VISIBLE or normalized.endswith("groundtruth") or normalized.startswith("gold"):
                raise ValueError("visible input contains a forbidden hidden/reference field")
            output[key] = _visible_input(item)
        return output
    if isinstance(value, list):
        return [_visible_input(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = _loads(value)
        except ValueError:
            raise ValueError("opaque serialized visible input must be structured JSON") from None
        _visible_input(decoded)
    if isinstance(value, float):
        _number(value, "visible input number")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("visible inputs must be JSON data")
    return value


def _write_new_bundle(out_dir, files):
    """Exclusive creation; callers validate the entire package before this point."""
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or any(p.is_symlink() for p in out_dir.parents):
        raise ValueError("output cannot traverse symlinks")
    out_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        options = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""}
        with (out_dir / name).open("xb" if isinstance(content, bytes) else "x", **options) as stream:
            stream.write(content)


def _configuration_plan(manifest, cases, config):
    """Use frozen task membership; legacy cross products are single-task only."""
    tasks = {row["task"] for row in cases.values()}
    configured = manifest.get("configurations")
    if configured is None:
        if len(tasks) != 1:
            raise ValueError("multi-task manifests require explicit configurations with task membership")
        task = next(iter(tasks))
        legacy = config.get("configurations", []) if isinstance(config, dict) else []
        if isinstance(legacy, dict):
            legacy = [{**v, "config_id": k} for k, v in legacy.items() if isinstance(v, dict)]
        lookup = {r.get("config_id"): r for r in legacy if isinstance(r, dict)}
        configured = [{**lookup.get(cid, {}), "config_id": cid, "task": task} for cid in manifest["configuration_ids"]]
    if not isinstance(configured, list) or not configured:
        raise ValueError("configurations must be a nonempty list")
    mapping = {}
    for row in configured:
        if not isinstance(row, dict):
            raise ValueError("configuration must be an object")
        cid = _text(row.get("config_id"), "config_id")
        task = _text(row.get("task"), "configuration task")
        if cid in mapping or task not in tasks:
            raise ValueError("duplicate configuration or task without sealed cases")
        mapping[cid] = task
    if set(mapping) != set(manifest["configuration_ids"]) or set(mapping.values()) != tasks:
        raise ValueError("configuration membership differs from sealed IDs or case tasks")
    expected = {(case, cid) for case, row in cases.items() for cid, task in mapping.items() if row["task"] == task}
    return configured, expected


def _hospital_workflow_guide(item, limit):
    """Operational instructions mirror workflows.py, without simulation actors."""
    workflow = item.get("workflow")
    common_rx = "先对原始处方运行冻结版本PPRS，保存pass/flag/reject、规则版本和处方/上下文输入摘要。"
    guides = {
        "pprs_baseline": [common_rx,
            "PPRS pass直接approve；reject直接block；flag才交真实药师，药师flag/reject/revise均记block，否则approve。",
            "保留原始处方最终决策；仅医生可另建修订草稿，须重新审核，修订不增加原测试病例数。"],
        "sequential": [common_rx,
            "PPRS硬拒绝直接block，不进入AI或药师复核；否则由所列被测AI独立读取原始处方。",
            "PPRS flag或AI flag/reject/revise时交真实药师，提供原始处方、PPRS及AI意见；无任何flag则approve。",
            "进入药师复核后按药师判断决定approve/block；保存全部阶段意见。仅医生可另建待重新审核的修订草稿，原病例不增样本。"],
        "parallel": [common_rx,
            "真实药师与被测AI分别独立审核原始处方，不互看意见，不向他们预先展示PPRS决定；即使PPRS硬拒绝也保留两路独立审核。",
            "汇总时PPRS硬拒绝始终block。否则automatic_flag=(PPRS flag或AI flag/reject/revise)，human_flag=(药师flag/reject/revise)。",
            "两路flag不一致时交另一位真实高级药师，展示PPRS/AI/药师全部意见并记录approve或reject仲裁；一致时按药师决定。",
            "parallel表示独立审核覆盖，不要求配置之间并发。保存原处方最终决定；医生修订另存草稿且不扩增样本。"],
        "nurse_only": [
            "由真实护士仅查看clinician_initial_view开始独立问诊采集；不得查看patients_view或平台摘要，不使用模拟护士模型。",
            "标准化患者/主持者按本次问题topics及release_on释放对应已知事实；未知事实保持unknown，不得提示预期答案。",
            "护士形成并保存首次交接摘要，记录每次问题/回答、采集和整理人工时间。",
            "真实医生只针对交接缺失进行补问，另存post_consultation_output和补问时间；不要用后续补齐结果覆盖first_handoff_output。"],
        "ai_only": [
            "由所列被测AI从clinician_initial_view独立采集；只按提问释放患者事实，不提供完整patients_view。",
            "保存AI首次交接摘要，不增加护士审核步骤；记录完整问答轮次与实际AI配置。",
            "真实医生只补齐交接缺失，独立记录补问与补齐后结果，首次交接产物保持不变。"],
        "ai_nurse_review": [
            "被测AI先按提问和release_on完成采集，护士不能提前查看patients_view或平台产物。",
            "真实护士审核AI已采集摘要，需要补充时独立向标准化患者提问，记录审核与补问时间。",
            "保存经过护士审核后的first_handoff_output；真实医生随后只补交接缺失，补齐后结果另存且不覆盖首轮。",
            "AI、护士与医生共同使用同一病例的问答轮次预算，所有人工投入包括失败病例。"],
        "manual_documentation": [
            "真实医生根据同案例已核验完整就诊材料人工书写病历，不调用AI或模拟医生模型。",
            "保存第一稿与每版内容/摘要/父版本，单独记录医生书写及审核时间。",
            "记录就诊结束encounter_ended_at；医生审核，不接受则追加修订版本并记录每轮时长。",
            "接受最终版本后由有权医生签署，保存签署人、版本摘要、physician_attested_at；周转为就诊结束至医生签署。"],
        "realtime": [
            "主持者按available_at_ms依次释放核验转写前缀，医生和AI均不得提前看到后续片段；每次记录已释放sequence集合。",
            "被测AI仅使用当前前缀和上一版病历产生新版本，保留父版本、首稿和实际模型规格。",
            "真实医生在每个前缀阶段只查看同一前缀审核/要求修订，每一阶段最多规定次数；保存返工轮数及人工时间。",
            "转写结束后记录encounter_ended_at，最终接受稿由有权医生签署；绑定最终输出摘要并记录physician_attested_at。"],
        "postvisit": [
            "先记录真实就诊结束encounter_ended_at，主持者此后才释放完整已核验转写；不得在结束前提前生成整稿。",
            "所列被测AI读取完整转写生成首稿，记录内容、版本、摘要和实际模型参数。",
            "真实医生审核首稿，必要时追加修订，记录各轮及对应医生/文书人工时间。",
            "最终接受后由有权医生签署，绑定实际最终稿摘要并记录physician_attested_at；周转从就诊结束计算。"],
    }
    steps = guides.get(workflow, ["此历史配置未提供受支持的workflow；必须先补齐并重新冻结操作规程，不能据标签推测流程。"])
    return dict(task=item["task"], workflow=workflow, steps=steps, supported=workflow in guides,
                max_interactions=limit,
                round_definition="预问诊：一次ask及患者一次完整回答合计1轮，多topics仍为1轮，摘要/交接不计轮；AI/护士/医生累计共享上限。病历：每个审核阶段最多max_interactions次医生审核，realtime每个前缀为一阶段；处方不按问答轮数扩增样本。")


def export_hospital_package(run_dir: Path, out_dir: Path) -> dict:
    """Export a NEW package outside the run, after verifying every sealed hash.

    Prefer explicit visible_inputs. Current engine input envelopes instead use
    a fixed allowlist: initial_visible_facts, patient_known_facts, transcript,
    prescription. Native request mappings and all other input fields are omitted.
    Full material goes ONLY to patients_view.jsonl for standardized patients/
    moderators. case_inputs.jsonl and clinician_initial_view.jsonl expose only
    initial facts and (for prescription) the submitted prescription. Transcripts
    are released by the moderator according to the exported operational steps.
    All other fields (gold, platform results, provenance internals) are omitted. Nested
    visible inputs are inspected, including JSON serialized into strings. A
    human must still verify free-text de-identification; key filtering is not PHI
    detection. The source input_sha256 comes from the sealed manifest; a separate
    visible_sha256 binds the projection, not the original full case hash. The
    manifest case hash MUST be SHA-256 of canonical JSON row.input (or legacy
    top-level visible_inputs); canonical uses UTF-8, sorted keys and separators
    (',', ':'). A declared input_sha256 cannot replace actual content verification.

    Also exports task-specific steps, frozen PPRS version, max_interactions/round
    definitions, and tested AI provider/model/revision/temperature/max_tokens only.
    Simulation actor models, endpoints, authentication and prompts are not copied.
    Hospital result and activity templates retain required recording fields but
    do not prefill observed provenance or independent execution as true.
    Existing output directories and destinations inside the sealed run are refused.
    """
    run, manifest, seal, config, cases = _verified_run(run_dir)
    configured, expected = _configuration_plan(manifest, cases, config)
    original_seal_hash = _sha(run / "seal.json")
    destination = Path(out_dir).resolve()
    if destination == run or destination.is_relative_to(run):
        raise ValueError("hospital export must be outside the sealed run")
    exported, patients = [], []
    for case, row in sorted(cases.items()):
        visible = row.get("visible_inputs")
        # Never export all of an opaque input envelope by default.
        if visible is None and isinstance(row.get("input"), dict):
            visible = row["input"].get("visible_inputs")
            if visible is None:
                public_fields = {"initial_visible_facts", "patient_known_facts", "transcript", "prescription"}
                projected = {k: row["input"][k] for k in sorted(public_fields & row["input"].keys())}
                if projected:
                    visible = projected
        if not isinstance(visible, (dict, list)):
            raise ValueError("case must supply structured visible_inputs")
        visible = _visible_input(visible)
        patients.append(dict(case_id=case, task=row["task"], input_sha256=manifest["case_hashes"][case],
                             view_role="standardized_patient_or_moderator", material=visible, material_sha256=_digest(visible),
                             release_policy="仅交标准化患者/主持者；按问题topics和release_on释放患者事实，按available_at_ms/就诊结束事件释放转写。采集护士/医生/被测AI不得直接读取此文件。"))
        initial = {"initial_visible_facts": visible.get("initial_visible_facts", [])} if isinstance(visible, dict) else {"initial_visible_facts": []}
        if row["task"] == "prescription" and isinstance(visible, dict) and "prescription" in visible:
            initial["prescription"] = visible["prescription"]
        exported.append(dict(case_id=case, task=row["task"], input_sha256=manifest["case_hashes"][case],
                             visible_inputs=initial, visible_sha256=_digest(initial),
                             patients_view_file="patients_view.jsonl", clinician_view_file="clinician_initial_view.jsonl",
                             configuration_ids=[cid for cid in manifest["configuration_ids"] if (case, cid) in expected]))
    fields = {"config_id", "task", "workflow", "model_family", "human_review", "requires_physician_attestation"}
    descriptions = []
    for item in configured:
        if not isinstance(item, dict):
            raise ValueError("configuration descriptions must be objects")
        if item.get("config_id") in manifest["configuration_ids"]:
            descriptions.append(_visible_input({k: item[k] for k in sorted(fields & item.keys())}))
    limit = config.get("max_interactions", 10)
    if type(limit) is not int or limit < 1:
        raise ValueError("max_interactions must be a positive integer")
    missing = [] if "max_interactions" in config else ["max_interactions_not_explicitly_frozen"]
    models = {}
    for family in sorted({r.get("model_family") for r in descriptions if r.get("model_family") in {"medical", "foundation"}}):
        spec = config.get("models", {}).get(family, {})
        models[family] = _visible_input({key: spec.get(key) for key in ("provider", "model", "revision", "temperature", "max_tokens")})
        missing.extend("models." + family + "." + key for key, value in models[family].items() if value is None or value == "")
        if models[family]["provider"] == "scripted":
            missing.append("models." + family + ".real_tested_model_required")
    pprs = _visible_input({key: config.get("pprs", {}).get(key) for key in ("provider", "version")})
    if any(r["task"] == "prescription" for r in descriptions):
        if not pprs["version"] or not pprs["provider"] or pprs["provider"] == "fixture":
            missing.append("frozen_hospital_pprs_or_verified_replay_required")
    guides = {r["config_id"]: _hospital_workflow_guide(r, limit) for r in descriptions}
    missing.extend("workflow." + key for key, guide in guides.items() if not guide["supported"])
    instructions = dict(run_id=manifest["run_id"], configuration_ids=manifest["configuration_ids"], configurations=descriptions,
                        protocol="Execute the corresponding workflows independently offline; do not rate simulation outputs as hospital executions.",
                        instruction_version="outpatient-hospital-1", source_purpose=manifest["purpose"],
                        staff_policy="医院所有护士/医生/药师/文书环节由真实人员独立执行；不导出或使用平台actors模拟人员模型。被测AI仅使用tested_models中的对应family。",
                        view_access={"patients_view.jsonl": "仅标准化患者/主持者，禁止直接交采集护士/医生或被测AI",
                                     "clinician_initial_view.jsonl": "临床人员初始视图；后续信息必须依规程按阶段释放",
                                     "case_inputs.jsonl": "兼容索引及临床初始视图，不含完整患者事实或转写"},
                        max_interactions=limit, workflow_instructions=guides, tested_models=models, pprs=pprs,
                        execution_spec_complete=not missing, required_before_execution=missing,
                        recording_requirements={
                            "results": "每case/config独立记录status、实际最终输出及摘要、实际开始/结束时间、source_kind、independent_execution及独立执行日志；不得预填真实声明。",
                            "reviews": "执行后独立两人盲评/必要第三人仲裁；记录artifact_sha256、reference来源source_sha256、reviewed_at及rubric_version。评分参考材料另行授权提供，不包含于执行包。",
                            "timing": "逐人工活动记录role/activity_type/active_seconds及measured来源日志，失败病例同样保留；补问子区间不得重复相加。",
                            "notes": "记录全部版本和首稿、医生审核/返工、encounter_ended_at、physician_attested_at，以及绑定最终输出摘要的真实医生attestation。"},
                        execution_not_before=seal["sealed_at"], schema_version="1")
    case_text = "".join(_canonical(row) + "\n" for row in exported)
    config_text = _canonical(instructions) + "\n"
    templates = [dict(case_id=case, config_id=cid, task=cases[case]["task"], input_sha256=manifest["case_hashes"][case],
                      source_kind=None, independent_execution=None, status=None, hospital_execution_started_at=None,
                      hospital_execution_ended_at=None, final_decision=None, output=None, output_sha256=None,
                      encounter_ended_at=None, physician_attested_at=None, reviews=[], adjudication=None, attestation=None,
                      independent_execution_evidence=dict(operator_id=None, source_run_id=None, execution_log_ref=None, execution_log_sha256=None))
                 for case, cid in sorted(expected)]
    files = {"case_inputs.jsonl": case_text, "clinician_initial_view.jsonl": case_text,
             "patients_view.jsonl": "".join(_canonical(row) + "\n" for row in patients),
             "configuration_instructions.json": config_text,
             "hospital_results_template.jsonl": "".join(_canonical(row) + "\n" for row in templates),
             "human_activity_template.csv": "activity_id,case_id,config_id,task,environment,role,activity_type,active_seconds,timing_source,included_in_activity_id,operator_id,source_run_id,source_kind,independent_execution,source_ref,source_sha256\n"}
    package_manifest = dict(schema_version="1", run_id=manifest["run_id"], n_cases=len(exported),
                            source_manifest_sha256=seal["manifest_sha256"], source_seal_sha256=_sha(run / "seal.json"),
                            artifacts={name: hashlib.sha256(content.encode("utf-8")).hexdigest() for name, content in files.items()})
    _verified_run(run)
    if _sha(run / "seal.json") != original_seal_hash:
        raise ValueError("seal changed during export")
    _write_new_bundle(out_dir, {**files, "package_manifest.json": _canonical(package_manifest) + "\n"})
    return dict(output_dir=str(destination), n_cases=len(exported), run_id=manifest["run_id"], source_seal_verified=True)


_LABELS = {"correct", "incorrect", "unsupported", "omitted", "indeterminate", "not_applicable"}


def validate_review(row) -> dict:
    """Validate one independent review: artifact_id, reviewer_id, blinded=True,
    rubric_version, and nonempty labels {item_id: label}. Returns a deep copy.
    A self-declared blinded flag is schema evidence, not proof of actual blinding.
    """
    if not isinstance(row, dict):
        raise ValueError("review must be an object")
    for key in ("artifact_id", "reviewer_id", "rubric_version"):
        _text(row.get(key), key)
    if row.get("blinded") is not True:
        raise ValueError("review must declare blinded=True")
    _validate_labels(row.get("labels"))
    return _loads(_canonical(row))


def _validate_labels(labels):
    if not isinstance(labels, dict) or not labels:
        raise ValueError("labels must be a nonempty item-to-label mapping")
    for key, value in labels.items():
        _text(key, "item_id")
        if value not in _LABELS:
            raise ValueError("unsupported review label")


def validate_adjudication(row) -> dict:
    """Validate artifact_id/adjudicator_id/rubric_version/labels/rationale.
    Independence and correspondence to two reviews are checked by adjudicate_reviews.
    """
    if not isinstance(row, dict):
        raise ValueError("adjudication must be an object")
    for key in ("artifact_id", "adjudicator_id", "rubric_version", "rationale"):
        _text(row.get(key), key)
    _validate_labels(row.get("labels"))
    return _loads(_canonical(row))


def blind_review_pack(artifacts, *, seed=20260919) -> dict:
    """Create reviewer_pack plus a PRIVATE identity mapping; no files are written.

    Input artifact_id/output and optional source_material. Reviewer items contain
    opaque shuffled artifact_id and output/source_material only; never config_id,
    case_id or environment. Keep private_mapping away from reviewers. Identifying
    prose inside outputs requires editorial blinding before calling this function.
    """
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    seen = set()
    ordered = []
    for row in artifacts:
        identifier = _text(row.get("artifact_id"), "artifact_id")
        if identifier in seen or "output" not in row or row["output"] is None:
            raise ValueError("unique artifacts with output are required")
        seen.add(identifier)
        ordered.append(row)
    ordered.sort(key=lambda r: r["artifact_id"])
    random.Random(seed).shuffle(ordered)
    items, mapping = [], []
    for i, row in enumerate(ordered):
        blind_id = f"blind-{i + 1:06d}"
        item = {"artifact_id": blind_id, "output": row["output"]}
        if "source_material" in row:
            item["source_material"] = row["source_material"]
        items.append(item)
        mapping.append(dict(blinded_artifact_id=blind_id, artifact_id=row["artifact_id"], case_id=row.get("case_id"),
                            config_id=row.get("config_id"), environment=row.get("environment")))
    return _loads(_canonical(dict(reviewer_pack=items, private_mapping=mapping, seed=seed)))


def adjudicate_reviews(reviews, adjudications) -> dict:
    """Require exactly two distinct blinded reviewers per artifact.

    Both must use identical rubric versions and item sets. Agreement preserves
    their labels. Disagreement requires a third person, distinct from both, with
    a complete label set and rationale; agreed items cannot be silently changed.
    Missing second review/adjudication is reported incomplete, not fabricated.
    Malformed schemas, duplicate reviewers or foreign adjudications raise ValueError.
    """
    groups = defaultdict(list)
    for row in reviews:
        valid = validate_review(row)
        groups[valid["artifact_id"]].append(valid)
    decisions = {}
    for row in adjudications:
        valid = validate_adjudication(row)
        key = valid["artifact_id"]
        if key in decisions or key not in groups:
            raise ValueError("duplicate or unknown adjudication")
        decisions[key] = valid
    outputs = []
    for artifact, items in sorted(groups.items()):
        people = {item["reviewer_id"] for item in items}
        if len(people) != len(items) or len(items) > 2:
            raise ValueError("need two distinct reviewers, not duplicate or extra reviews")
        if len(items) < 2:
            if artifact in decisions:
                raise ValueError("cannot adjudicate without both reviews")
            outputs.append(dict(artifact_id=artifact, complete=False, missing="second_review", labels=None))
            continue
        a, b = items
        if a["rubric_version"] != b["rubric_version"] or a["labels"].keys() != b["labels"].keys():
            raise ValueError("reviews must share rubric and item coverage")
        disagreements = [k for k in a["labels"] if a["labels"][k] != b["labels"][k]]
        decision = decisions.get(artifact)
        if decision:
            if decision["adjudicator_id"] in people or decision["rubric_version"] != a["rubric_version"] or decision["labels"].keys() != a["labels"].keys():
                raise ValueError("invalid third reviewer or adjudication coverage")
            if any(decision["labels"][k] != a["labels"][k] for k in a["labels"] if k not in disagreements):
                raise ValueError("adjudication changed an agreed item")
        complete = not disagreements or decision is not None
        outputs.append(dict(artifact_id=artifact, complete=complete, disagreement_items=disagreements,
                            missing=None if complete else "adjudication", labels=(decision or a)["labels"] if complete else None))
    return dict(complete=bool(outputs) and all(r["complete"] for r in outputs), artifacts=outputs)


def validate_reviews(reviews) -> dict:
    """Validate a batch; report disagreements requiring separate adjudication."""
    return adjudicate_reviews(reviews, [])


def _read_import(path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            value = row.get("independent_execution")
            if value not in ("true", "false"):
                raise ValueError("CSV independent_execution must be true/false")
            row["independent_execution"] = value == "true"
            for field in ("output", "final_output", "reviews", "adjudication", "attestation", "independent_execution_evidence"):
                if row.get(field):
                    row[field] = _loads(row[field])
                else:
                    row.pop(field, None)
        return rows
    if path.suffix.lower() == ".json":
        rows = _json(path)
        if isinstance(rows, dict):
            rows = rows.get("results")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("JSON import must contain a list of result objects")
        return rows
    return _jsonl(path)


def _output(row, root):
    value = row.get("output", row.get("final_output"))
    raw_hash = None
    ref = row.get("output_ref", row.get("final_output_ref"))
    if value is None and ref:
        path = _safe_child(root, ref)
        if not path.is_file():
            return None, None
        raw_hash = _sha(path)
        text = path.read_text(encoding="utf-8-sig")
        value = _loads(text) if path.suffix.lower() == ".json" else text
    if value is None:
        return None, None
    digest = _digest(value)
    declared = row.get("output_sha256", row.get("final_output_hash", row.get("final_output_sha256")))
    if declared is not None and declared not in {digest, raw_hash}:
        raise ValueError("output content hash mismatch")
    return value, digest


def _execution_evidence(evidence, root, run, platform_runs, required, files):
    """Bind and copy operator-supplied independent logs; not a truth certificate.

    Missing evidence returns issue fields. Invalid digest/path or reuse of the
    platform run identifier raises ValueError. Copied bytes have their own SHA;
    changing the external log after import cannot change the archived evidence.
    """
    if evidence is None:
        return None, ["independent_execution_evidence"] if required else []
    if not isinstance(evidence, dict):
        raise ValueError("independent_execution_evidence must be an object")
    fields = ("operator_id", "source_run_id", "execution_log_ref", "execution_log_sha256")
    absent = ["independent_execution_evidence." + field for field in fields if not evidence.get(field)]
    if absent:
        return evidence, absent
    _text(evidence["operator_id"], "operator_id")
    if _text(evidence["source_run_id"], "source_run_id") in platform_runs:
        raise ValueError("independent execution source_run_id identifies a platform run")
    path = _safe_child(root, evidence["execution_log_ref"])
    if path.is_relative_to(run):
        raise ValueError("execution evidence cannot reference a platform run file")
    digest = _hash_string(evidence["execution_log_sha256"], "execution_log_sha256")
    if not path.is_file():
        return evidence, ["independent_execution_evidence.execution_log_file"]
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("independent execution log content hash mismatch")
    name = "execution-log-" + digest + ".log"
    files[name] = content
    return {**evidence, "execution_log_ref": name}, []


def import_hospital_results(run_dir: Path, input_path: Path) -> dict:
    """Validate JSONL/JSON/CSV then create hospital/<input-file-sha256>/ exclusively.

    Required per row: case_id/config_id/input_sha256, source_kind=hospital_observed,
    independent_execution=True, hospital_execution_started_at (timezone, >= seal).
    Optional task must match the sealed case. Output is inline output/final_output
    or a relative UTF-8 output_ref next to the import file. References are copied
    inline so subsequent external changes cannot alter the imported observation.
    Identical platform/hospital output sets potential_reuse, not a fraud verdict.
    Completion then requires independent_execution_evidence with operator_id,
    source_run_id, execution_log_ref and execution_log_sha256. The external log
    is checked and copied byte-for-byte into the new import directory. Missing
    evidence is an issue, while forged hashes or platform source paths are errors.
    Equality is neither proof of human execution nor proof of copying.

    Missing output, two blinded reviews, adjudication or note physician attestation
    are reported as incomplete, never invented. Attestation fields: signer_id,
    role=physician, signed_at and artifact_sha256 of canonical JSON output. Review
    and adjudication schemas are documented by validate_review/adjudicate_reviews.
    Only documentation/emr/note tasks require physician attestation here; prescription
    workflow pharmacist handling is distinct from independent quality reviews.

    Import reports retain all missing case/config pairs. Hospital records are never
    written over sealed files. Declarations + matching hashes do not independently
    prove real human participation or prospective third-party custody.
    """
    run, manifest, seal, config, cases = _verified_run(run_dir)
    _, expected = _configuration_plan(manifest, cases, config)
    original_seal_hash = _sha(run / "seal.json")
    input_path = Path(input_path).resolve()
    if input_path.is_relative_to(run):
        raise ValueError("hospital source must not be a platform run artifact")
    original_input_hash = _sha(input_path)
    rows = _read_import(input_path)
    if not rows:
        raise ValueError("hospital import is empty")
    for name in seal["artifacts"]:
        if PurePosixPath(name).parts[0] == "hospital":
            raise ValueError("hospital namespace is already sealed")
    platform_hashes = set()
    platform_runs = {manifest["run_id"]}
    for row in _jsonl(run / "case_results.jsonl"):
        if row.get("run_id"):
            platform_runs.add(row["run_id"])
        value, digest = _output(row, run)
        if digest:
            platform_hashes.add(digest)
        if row.get("final_output_hash"):
            platform_hashes.add(row["final_output_hash"])
    seen, issues, normalized = set(), [], []
    evidence_files = {}
    reviewed_artifacts = set()
    note_tasks = {"documentation", "medical_documentation", "medical_note_documentation", "medical_note", "note", "emr"}
    known_tasks = note_tasks | {"prescription", "prescription_review", "preconsult", "preconsultation", "pre_consultation", "pre-consultation"}
    for row in rows:
        key = (row.get("case_id"), row.get("config_id"))
        if key in seen or key not in expected:
            raise ValueError("duplicate or unknown case/config pair")
        seen.add(key)
        case = cases[key[0]]
        task = row.get("task", case["task"])
        if task != case["task"] or row.get("input_sha256") != manifest["case_hashes"][key[0]]:
            raise ValueError("case task or input hash mismatch")
        if row.get("source_kind") != "hospital_observed" or row.get("independent_execution") is not True:
            raise ValueError("hospital observations require independent execution provenance")
        start = _timestamp(row.get("hospital_execution_started_at"))
        if start < _timestamp(seal["sealed_at"]):
            raise ValueError("hospital execution predates sealed predictions")
        output_ref = row.get("output_ref", row.get("final_output_ref"))
        if output_ref and _safe_child(input_path.parent, output_ref).is_relative_to(run):
            raise ValueError("hospital output cannot reference a platform run file")
        value, output_hash = _output(row, input_path.parent)
        potential_reuse = bool(output_hash and output_hash in platform_hashes)
        evidence, missing = _execution_evidence(row.get("independent_execution_evidence"), input_path.parent,
                                               run, platform_runs, potential_reuse, evidence_files)
        if value is None or value == "" or value == {}:
            missing.append("output")
        reviews = row.get("reviews")
        if not reviews:
            missing.append("reviews")
        else:
            if not isinstance(reviews, list):
                raise ValueError("reviews must be a list")
            if len({r.get("artifact_id") for r in reviews if isinstance(r, dict)}) != 1:
                raise ValueError("one output must have reviews of exactly one artifact")
            for review in reviews:
                if isinstance(review, dict) and review.get("artifact_sha256") is not None and review["artifact_sha256"] != output_hash:
                    raise ValueError("review does not bind the submitted hospital output")
            adjudications = [row["adjudication"]] if row.get("adjudication") else []
            assessment = adjudicate_reviews(reviews, adjudications)
            artifact_id = assessment["artifacts"][0]["artifact_id"]
            if artifact_id in reviewed_artifacts:
                raise ValueError("review artifact reused across hospital cases/configurations")
            reviewed_artifacts.add(artifact_id)
            if not assessment["complete"]:
                missing.extend(sorted({r["missing"] for r in assessment["artifacts"] if not r["complete"]}))
        if task in note_tasks:
            attestation = row.get("attestation")
            if not attestation:
                missing.append("physician_attestation")
            else:
                if not isinstance(attestation, dict) or attestation.get("role") != "physician":
                    raise ValueError("note requires physician attestation")
                _text(attestation.get("signer_id"), "signer_id")
                if _timestamp(attestation.get("signed_at")) < start:
                    raise ValueError("attestation predates hospital execution")
                if output_hash is None or attestation.get("artifact_sha256") != output_hash:
                    raise ValueError("attestation does not bind the submitted note")
        elif task not in known_tasks:
            missing.append("unknown_task_requirements")
        if missing:
            issues.append(dict(case_id=key[0], config_id=key[1], missing=missing))
        item = {**row, "task": task, "output": value, "output_sha256": output_hash, "potential_reuse": potential_reuse}
        if evidence is not None:
            item["independent_execution_evidence"] = evidence
        item.pop("output_ref", None)
        item.pop("final_output_ref", None)
        normalized.append(item)
    missing_pairs = [dict(case_id=c, config_id=k) for c, k in sorted(expected - seen)]
    import_hash = _sha(input_path)
    if import_hash != original_input_hash:
        raise ValueError("hospital input changed during validation")
    destination = _safe_child(run, "hospital/" + import_hash)
    normalized_text = "".join(_canonical(r) + "\n" for r in normalized)
    result = dict(schema_version="1", run_id=manifest["run_id"], imported_rows=len(rows),
                  complete=not issues and not missing_pairs, issues=issues, missing_pairs=missing_pairs,
                  potential_reuse_cases=sum(r["potential_reuse"] for r in normalized),
                  evidence_artifacts={name: hashlib.sha256(value).hexdigest() for name, value in evidence_files.items()},
                  results_sha256=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
                  source_sha256=import_hash, source_seal_sha256=_sha(run / "seal.json"),
                  import_dir=str(destination), provenance_assurance="validated_declarations_not_independent_execution_proof")
    # Verify again immediately before creating files: a changed seal/run must not
    # be silently accepted during a long evidence validation pass.
    _verified_run(run)
    if _sha(run / "seal.json") != original_seal_hash:
        raise ValueError("seal changed during hospital validation")
    _write_new_bundle(destination, {**evidence_files, "results.jsonl": normalized_text,
                                     "import_report.json": _canonical(result) + "\n"})
    return result


def subjective_metrics(rows) -> dict:
    """Within-person existing versus human_ai ratings, with missingness counts.

    Rows: participant_id, participant_group (patient/clinician), workflow
    (existing/human_ai), dimension, optional item_id/session_id, score (integer
    1..5 or None), optional valid bool and missing_reason. Repeated DISTINCT
    sessions are averaged per item, then items equally per participant/dimension.
    They never increase participant counts. Exact participant/workflow/item/session
    duplicates are errors. Per-workflow means use available participant ratings;
    paired differences use only people with both workflows for that dimension.
    """
    people, seen, ratings = {}, set(), defaultdict(list)
    missing = Counter()
    dimensions = defaultdict(set)
    for row in rows:
        person = _text(row.get("participant_id"), "participant_id")
        group = row.get("participant_group")
        workflow = row.get("workflow")
        if group not in ("patient", "clinician") or workflow not in ("existing", "human_ai"):
            raise ValueError("invalid participant group or workflow")
        if person in people and people[person] != group:
            raise ValueError("participant group changed")
        people[person] = group
        dimension = _text(row.get("dimension"), "dimension")
        item = row.get("item_id", dimension)
        _text(item, "item_id")
        key = (person, workflow, dimension, item, row.get("session_id", "single"))
        if key in seen:
            raise ValueError("duplicate subjective response")
        seen.add(key)
        dimensions[group].add(dimension)
        score = row.get("score")
        valid = _bool(row.get("valid", True), "valid")
        if score is not None and (type(score) is not int or not 1 <= score <= 5):
            raise ValueError("score must be an integer from 1 to 5 or null")
        if score is None or not valid:
            missing[group, dimension] += 1
            continue
        ratings[group, dimension, person, workflow, item].append(score)
    participant_items = defaultdict(list)
    for (group, dimension, person, workflow, item), values in ratings.items():
        participant_items[group, dimension, person, workflow].append(fmean(values))
    participant_scores = {key: fmean(values) for key, values in participant_items.items()}
    output = {}
    for group, dims in sorted(dimensions.items()):
        output[group] = {}
        for dimension in sorted(dims):
            existing = {p: s for (g, d, p, w), s in participant_scores.items() if (g, d, w) == (group, dimension, "existing")}
            human_ai = {p: s for (g, d, p, w), s in participant_scores.items() if (g, d, w) == (group, dimension, "human_ai")}
            paired = sorted(existing.keys() & human_ai.keys())
            differences = [human_ai[p] - existing[p] for p in paired]
            output[group][dimension] = dict(n_valid_existing=len(existing), n_valid_human_ai=len(human_ai), n_paired=len(paired),
                n_unpaired=len(existing.keys() ^ human_ai.keys()), missing_responses=missing[group, dimension],
                existing_mean=fmean(existing.values()) if existing else None, human_ai_mean=fmean(human_ai.values()) if human_ai else None,
                paired_mean_difference=fmean(differences) if differences else None,
                paired_existing_mean=fmean(existing[p] for p in paired) if paired else None,
                paired_human_ai_mean=fmean(human_ai[p] for p in paired) if paired else None)
    return _checked(dict(n_participants=len(people), participant_counts=dict(Counter(people.values())), groups=output))
