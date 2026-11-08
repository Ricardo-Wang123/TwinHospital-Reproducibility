"""Prospective execution policy; no retrospective promotion of engineering runs."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from .cases import GROUPS
from .contracts import digest
from .linked_acceptance import PATIENTS_PER_GROUP, SUPPORTED_PATIENTS_PER_GROUP, linked_profiles, summarize_gate
from .native_observation import filehash
from .workflow_analysis import validate_policy


def validate_window(policy, *, workers=None, now=None):
    now = now or datetime.now(timezone.utc)
    run = {'started_at': now.isoformat(), 'workflow_policy_hash': digest(policy)}
    valid, errors = validate_policy({'workflow_observation_policy': policy}, run)
    if errors:
        raise ValueError('invalid prospective workflow policy: ' + ', '.join(errors))
    if workers is not None and valid['workers'] != workers:
        raise ValueError('worker count differs from the frozen observation policy')
    if valid.get('admission_policy') != 'closed_loop_worker_availability':
        raise ValueError('unsupported prospective admission policy')
    opened = datetime.fromisoformat(valid['submission_window']['opens_at'].replace('Z', '+00:00'))
    frozen = datetime.fromisoformat(valid['frozen_at'].replace('Z', '+00:00'))
    if frozen > opened:
        raise ValueError('policy must be frozen before its submission window opens')
    if valid.get('late_outcomes') != 'retain_effects_and_final_disposition_exclude_late_completion':
        raise ValueError('late effects and final outcomes must remain observable')
    return deepcopy(valid)


def admission_status(policy, *, now=None):
    if policy is None:
        return 'open'
    now = now or datetime.now(timezone.utc)
    window = policy['submission_window']
    if now < datetime.fromisoformat(window['opens_at'].replace('Z', '+00:00')):
        return 'not_open'
    if now >= datetime.fromisoformat(window['closes_at'].replace('Z', '+00:00')):
        return 'closed'
    return 'open'


def unadmitted_record(config_id, reason):
    return {'status': 'not_run_submission_window', 'config_id': config_id,
            'stop_reason': reason, 'native_effects_started': False, 'model_requests_sent': False,
            'retained_in_planned_denominator': True}


def validate_formal_design(args, cases, config, policy):
    """Validate the approved cohort/conditions, not clinical quality or API health."""
    if not getattr(args, 'formal_experiment', False):
        return
    validate_window(policy, workers=args.workers)
    if (args.mode != 'matrix' or args.matrix_task is not None or args.limit is not None
            or args.anchor_batch or args.controlled_prescription_variant or args.engineering_only):
        raise ValueError('formal study requires new canonical runs and all 9 single-tool task conditions')
    if not args.close_prescription_loop or not args.capture_native:
        raise ValueError('formal study requires prescription followthrough and native capture')
    if args.rule_engine != 'documented-candidate':
        raise ValueError('engineering placeholder rules cannot be the formal rule baseline')
    if (len(cases) != 1000 or len({r['patient_key'] for r in cases}) != 1000
            or len({r['case_id'] for r in cases}) != 1000
            or Counter(r['group'] for r in cases) != Counter({g: 200 for g in GROUPS})):
        raise ValueError('formal study requires 1000 distinct patients, 200 per disease under the user-resized design')
    if any(r['data_kind'] != 'mimic_derived' or r['split'] != 'test' for r in cases):
        raise ValueError('formal study excludes engineering patients and fictional fixtures')
    if set(config['tool_models']) != {'tool_a'} or not config['tool_models']['tool_a']:
        raise ValueError('formal study requires one fixed AI tool model')
    if policy.get('clinical_reference_scope') != 'declared_programmatic_checks_not_unrestricted_gold_labels':
        raise ValueError('formal clinical evaluation scope must be explicitly limited to declared checks')


def bind_formal_inputs(manifest_path):
    """Match the prepared cohort and local schedule to the prospective receipt."""
    manifest = Path(manifest_path).resolve()
    receipt_path = manifest.with_name('preparation_receipt.json')
    receipt_hash = filehash(receipt_path)
    receipt = json.loads(receipt_path.read_text())
    schedule = manifest.with_name('app3_local500_manifest.json')
    cohort_hash, schedule_hash = filehash(manifest), filehash(schedule)
    if (receipt.get('source_patients') != 1000
            or receipt.get('local_tests') != {'single_fault': 100, 'compliant_control': 400}
            or receipt.get('patient_or_assignment_replacements') != 0
            or receipt.get('execution_outcomes_read') is not False
            or not cohort_hash or cohort_hash != receipt.get('prepared_manifest_sha256')
            or not schedule_hash or schedule_hash != receipt.get('prepared_operation_schedule_sha256')
            or receipt_hash != filehash(receipt_path)):
        raise ValueError('formal cohort or APP III schedule differs from the prepared input receipt')
    return {'cohort_sha256': cohort_hash, 'operation_schedule_sha256': schedule_hash,
            'preparation_receipt_sha256': receipt_hash}


def bind_formal_analysis(plan_path, binding, specification):
    plan_path = Path(plan_path).resolve()
    fingerprint = filehash(plan_path)
    plan = json.loads(plan_path.read_text())
    frozen_at = datetime.fromisoformat(plan['frozen_at'].replace('Z', '+00:00'))
    models = specification['models']
    expected_inputs = {k: binding[k] for k in ('cohort_sha256', 'operation_schedule_sha256')}
    code = Path(__file__).resolve().parent
    scope = plan.get('prescription_scope_freeze') or {}
    if (frozen_at.tzinfo is None or frozen_at > datetime.now(timezone.utc)
            or plan.get('formal_results_present') is not False
            or plan.get('runtime_inputs') != expected_inputs
            or plan.get('conditions') != specification['conditions']
            or plan.get('model_assignment') != {'role': models['role_model'], 'tools': models['tool_models']}
            or plan.get('bootstrap') != {'seed': 20261107, 'repeats': 10000, 'alpha': .05, 'stratify_by_disease': True}
            or plan.get('delivery_criteria_sha256') != filehash(code/'programmatic_delivery.py')
            or scope.get('scorer_sha256') != filehash(code/'prescription_analysis.py')
            or scope.get('registry_sha256') != filehash(code/'clinical_rules_registry_v2.json')
            or filehash(plan_path) != fingerprint):
        raise ValueError('Formal analysis plan differs from the prepared inputs, models, or scoring criteria')
    return fingerprint


def verify_engineering_acceptance(batch_dir, audit_path, formal_spec, formal_cases):
    """Require one complete, immutable paid regression campaign on the same runtime."""
    batch = Path(batch_dir).resolve()
    audit_path = Path(audit_path).resolve()
    with (batch/'batch.lock').open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        spec_path, summary_path = batch/'specification.json', batch/'batch_summary.json'
        spec = json.loads(spec_path.read_text())
        summary = json.loads(summary_path.read_text())
        audit_hash = filehash(audit_path)
        evidence = json.loads(audit_path.read_text())
        report = evidence['audit']
        gate = report['linked_acceptance_gate']
        if (spec.get('mode') != 'linked-acceptance' or spec.get('formal_experiment') is not False
                or summary.get('batch') != spec.get('batch') or not summary.get('session_finished_at')
                or (summary.get('native_observation') or {}).get('status') != 'closed'
                or report.get('native_observation_requested') is not True
                or report.get('source_files_unchanged') is not True
                or report.get('source_specification_sha256') != filehash(spec_path)):
            raise ValueError('engineering acceptance requires a closed batch and its native audit')
        target = formal_spec.get('engineering_acceptance_patients_per_disease', PATIENTS_PER_GROUP)
        factorial = formal_spec.get('engineering_acceptance_factorial', False)
        if target not in SUPPORTED_PATIENTS_PER_GROUP or (target == 2 and not factorial):
            raise ValueError('Unsupported formal engineering acceptance target')
        profiles, _ = linked_profiles(formal_spec['models']['tool_models'], factorial=factorial)
        planned_patients = target * len(GROUPS)
        planned_encounters = planned_patients * len(profiles)
        design = spec['linked_acceptance']
        if (design['target_per_disease_and_condition'] != target
                or design.get('factorial', False) != factorial
                or design['planned_unique_encounters'] != planned_encounters):
            raise ValueError(f'Engineering acceptance must be frozen for {target} patients per disease')
        recomputed = summarize_gate(design, report['runs'])
        if (not recomputed['passed'] or recomputed['verified_unique_encounters'] != planned_encounters
                or gate.get('reused_native_identity_runs') != 0
                or any(gate.get(k) != v for k, v in recomputed.items())):
            raise ValueError('engineering acceptance incomplete '
                             f"({recomputed['verified_unique_encounters']}/{planned_encounters} verified paid)")
        fields = ('code_hashes', 'models', 'transport_policy', 'generation', 'generation_policy',
                  'data_policy_hash', 'catalog_hash', 'diagnosis_catalog_hash', 'rules',
                  'workflow_limits', 'modeled_activity_seconds', 'prescription_followthrough_policy')
        changed = [k for k in fields if k not in spec or spec[k] != formal_spec[k]]
        if spec['native_observation']['source_hashes'] != formal_spec['native_observation']['source_hashes']:
            changed.append('native_deployment')
        if changed:
            raise ValueError('formal runtime differs from the verified engineering runtime: ' + ', '.join(changed))
        source_patients = set(spec.get('acceptance_source_patient_hashes', []))
        if len(source_patients) != planned_patients or source_patients & {digest(r['patient_key']) for r in formal_cases}:
            raise ValueError('engineering patient identities must be verified and separate from formal patients')
        hashes = evidence['source_file_hashes']
        if {row['label'] for row in design['profiles']} != set(profiles):
            raise ValueError('Engineering acceptance profiles differ from the frozen formal model assignment')
        expected = {(case['case_id'], label) for case in spec['cases'] for label in profiles}
        crosswalk = evidence['crosswalk']
        if (len(expected) != planned_encounters or len(crosswalk) != planned_encounters
                or {(r['case_id'], r['condition']) for r in crosswalk} != expected
                or len({r['run_id'] for r in crosswalk if r.get('run_id')}) != planned_encounters
                or len({r['result_path'] for r in crosswalk if r.get('result_path')}) != planned_encounters):
            raise ValueError('engineering audit does not bind each planned encounter to a distinct run')
        required = {str(spec_path), str(summary_path)} | {r['result_path'] for r in crosswalk}
        if (not required <= set(hashes)
                or any(not Path(p).resolve().is_relative_to(batch) or filehash(p) != h for p, h in hashes.items())
                or filehash(audit_path) != audit_hash):
            raise ValueError('engineering audit inputs changed after verification')
    return {'batch': spec['batch'], 'specification_sha256': report['source_specification_sha256'],
            'audit_sha256': audit_hash, 'verified_paid': planned_encounters}
