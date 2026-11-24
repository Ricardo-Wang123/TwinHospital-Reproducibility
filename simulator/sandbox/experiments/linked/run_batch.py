"""Resume-safe execution of the paper's existing conditions on native OpenHIS.

Engineering batches are not clinical validation. Every result, failure and
unavailable paired input is retained; resuming never reruns a completed attempt.
Use a new batch name after implementation changes and record the old batch.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import nullcontext
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import fcntl
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import sys
import time
from threading import Event, Lock

from .camel_provider import BudgetedCamelFactory, MEMORY_POLICY
from .cases import load_case
from .clinical import (LinkedEncounterRunner, DEFAULT_LIMITS, DEFAULT_TIMING,
                       HISTORY_CONTEXT_BYTE_LIMIT, CURRENT_MEASUREMENT_POLICY,
                       CLINICAL_DISPOSITION_POLICY, PRESCRIPTION_FOLLOWTHROUGH_POLICY,
                       CONSULTATION_DIALOGUE_POLICY, CONTINUATION_POLICY)
from .continuation import EncounterContinuation
from .chart import POLICY as CHART_POLICY, INITIAL_BYTE_LIMIT, PAGE_BYTE_LIMIT
from .chart_projection import PROJECTION_POLICY
from .contracts import digest, linked_config_id, runtime_case
from . import scenario_design as scenarios
from .linked_acceptance import (
    ASSIGNMENT_SCHEDULE_POLICY, BASELINE_LABEL, HELDOUT_DISPATCH_ORDER, MAX_ENGINEERING_ATTEMPTS,
    PATIENTS_PER_GROUP, SUPPORTED_PATIENTS_PER_GROUP, attempt_label, documented_unpaid_stop,
    execution_checks, freeze_design, heldout_dispatch_order, linked_profiles, ordered_units,
)
from .native_his import NativeOpenHISPort
from .protocol_his import PaperProtocolPort, PROTOCOL_VERSION
from .provider import BudgetLedger, configured_prices, check_data_policy, private_json, validate_generation_policy, STREAM_RETRY_POLICY, PROXY_AUTH_RETRY_POLICY
from .response_contracts import POLICY as RESPONSE_CONTRACT_POLICY
from .diagnosis_codes import POLICY as DIAGNOSIS_CODE_POLICY
from .campaign import admission_status, unadmitted_record, validate_window


def _read(path):
    return json.loads(Path(path).read_text())


def validate_diagnosis_mapping(mapping):
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('diagnosis catalogue must be a nonempty mapping')
    identities = set()
    for code, row in mapping.items():
        if (not isinstance(row, dict) or row.get('code') != code or
                not all(isinstance(row.get(key), str) and row[key].strip()
                        for key in ('code', 'display', 'system', 'conditionDefinitionId'))):
            raise ValueError('diagnosis catalogue requires explicit code/system/display/native identity')
        if row['conditionDefinitionId'] in identities:
            raise ValueError('diagnosis catalogue cannot bind distinct codes to one native definition')
        identities.add(row['conditionDefinitionId'])
    return mapping


def _atomic(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def record_batch_event(path, event, **fields):
    """Persist dispatcher timing without claiming a formal observation window."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'a') as stream:
        value = {'event': event, 'observed_at': datetime.now(timezone.utc).isoformat(), **fields}
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def code_fingerprint():
    root = Path(__file__).parent
    names = ('actors.py', 'camel_provider.py', 'chart.py', 'chart_projection.py', 'clinical.py', 'contracts.py',
             'ollama.py',
             'intake.py', 'patient_medications.py', 'history_examples.py',
             'native_his.py', 'protocol_his.py', 'provider.py', 'provider_profile.py', 'regimen.py', 'response_contracts.py',
             'json_boundary.py', 'diagnosis_codes.py', 'order_encoding.py', 'medication_catalogue.py', 'formulary_search.py',
             'provider_content.py', 'provider_stream.py', 'fhir_his.py', 'cases.py', 'run_batch.py',
             'batch_observation.py', 'native_observation.py', 'native_observation_analysis.py',
             'campaign.py', 'workflow_analysis.py', 'network_route.py', 'trace_evidence.py',
             'linked_acceptance.py', 'assignment_repeat.py', 'curl_transport.py',
             'standard_his.py', 'wst846.py', 'wst846_order.py', 'wst846_server.py', 'wst_workflow.py',
             'case_preparation/anthropometry.py', 'emergency.py', 'ed_order_actions.py', 'ed_order_history.py',
             'scenario_design.py', 'continuation.py', 'workflow_events.py', 'ed_treatment_review.py',
             'ed_allergy_review.py')
    sources = {name: root / name for name in names}
    agents = root.parents[2] / 'agents'
    for name in ('__init__.py', 'base_agent.py', 'doctor_agent.py', 'patient_agent.py',
                 'pharmacist_agent.py', 'ai_tools.py', 'coordinator.py'):
        sources['agents/' + name] = agents / name
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}


def select_cases(manifest, limit=None):
    """Preflight one complete file at a time; retain only immutable references."""
    rows = manifest['cases'] if limit is None else manifest['cases'][:limit]
    if not rows or len({row['case_id'] for row in rows}) != len(rows):
        raise ValueError('manifest must contain distinct case IDs')
    references = []
    for row_index, row in enumerate(rows, 1):
        path = Path(row['path']).resolve()
        raw_hash = file_sha256(path)
        case = load_case(path)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', case['case_id']):
            raise ValueError('case_id must be a safe directory name')
        for field in ('case_id', 'patient_key', 'data_kind', 'group', 'split'):
            if row.get(field) != case.get(field):
                raise ValueError('case does not match manifest: ' + field)
        reference = {key: case.get(key) for key in ('case_id', 'patient_key', 'data_kind', 'group', 'split')}
        if 'formulary_directory' in case:
            reference['formulary_directory'] = case['formulary_directory']
        reference.update(encounter_setting=case.get('encounter_setting', 'outpatient'), path=str(path), file_sha256=raw_hash, case_hash=digest(case),
                         runtime_input_hash=digest(runtime_case(case)))
        for field in ('file_sha256', 'case_hash', 'runtime_input_hash'):
            if row.get(field) is not None and row[field] != reference[field]:
                raise ValueError('case does not match manifest hash: ' + field)
        if file_sha256(path) != raw_hash:
            raise ValueError('case file changed during preflight')
        references.append(reference)
        # Do not keep the previous file alive while loading the next large chart.
        del case
        if len(rows) >= 100 and (row_index % 100 == 0 or row_index == len(rows)):
            print(json.dumps({'phase': 'case_preflight', 'validated': row_index,
                              'planned': len(rows), 'retained_full_cases': 0}), file=sys.stderr, flush=True)
    return references


def file_sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_verified_reference(reference):
    """Validate the frozen file again inside the worker before any side effect."""
    path = Path(reference['path'])
    if file_sha256(path) != reference['file_sha256']:
        raise ValueError('case file changed after preflight')
    case = load_case(path)
    for field in ('case_id', 'patient_key', 'data_kind', 'group', 'split'):
        if case.get(field) != reference[field]:
            raise ValueError('loaded case identity changed: ' + field)
    if digest(case) != reference['case_hash'] or digest(runtime_case(case)) != reference['runtime_input_hash']:
        raise ValueError('case content changed after preflight')
    if file_sha256(path) != reference['file_sha256']:
        raise ValueError('case file changed during worker load')
    return case


def execute_case_reference(reference, **kwargs):
    from .formulary_search import directory_for_case
    case = load_verified_reference(reference)
    directory = directory_for_case(case, reference['path'])
    return execute_case(case, medication_directory=directory, **kwargs)


def _expected_run(case, run_id, linked_conditions):
    return {
        'schema_version': 'twinhospital.linked.run/1', 'scenario_design': scenarios.design_for(case),
        'case_id': case['case_id'], 'run_id': run_id, 'config_id': linked_config_id(linked_conditions),
        'input_hash': digest(runtime_case(case)), 'evaluation_unit': 'complete_encounter',
        'conditions': {task: condition.to_dict() for task, condition in linked_conditions.items()},
    }


def validate_saved_result(result, expected):
    for field, value in expected.items():
        if result.get(field) != value:
            raise ValueError('saved run identity/input mismatch: ' + field)
    if result.get('status') not in {'completed', 'failed', 'stopped_budget', 'stopped_provider', 'incomplete_clinical_decision', 'waiting_retry'}:
        raise ValueError('saved result has no final disposition')


def _budget_stopped(ledger, stop_event):
    budget = ledger.summary()
    if budget.get('new_requests_stopped') or budget.get('halted_for_reconciliation') or (budget['remaining_cny'] is not None and budget['remaining_cny'] <= 0):
        stop_event.set()
    return stop_event.is_set()


def operator_stop_request(batch_root):
    """A separate operator reason never masquerades as provider/budget failure."""
    path = Path(batch_root)/'operator_stop.json'
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise ValueError('Operator stop request exceeds its metadata limit')
    value = _read(path)
    if (value.get('version') != 'operator_stop_v1' or
            value.get('reason_code') not in {'implementation_repair', 'user_stop', 'resource_interruption'}):
        raise ValueError('Operator stop request needs a declared reason code')
    requested = datetime.fromisoformat(value['requested_at'].replace('Z', '+00:00'))
    if requested.tzinfo is None:
        raise ValueError('Operator stop timestamp needs a timezone')
    return {key: value[key] for key in ('version', 'reason_code', 'requested_at')} | {
        'request_sha256': file_sha256(path),
        'boundary': 'finish_inflight_attempt_then_stop_new_attempts',
        'native_effects_repeated': False,
    }


def _batch_stopped(ledger, stop_event, batch_root):
    return operator_stop_request(batch_root) is not None or _budget_stopped(ledger, stop_event)


def unstarted_stop_record(ledger, config_id, batch_root=None):
    requested = operator_stop_request(batch_root) if batch_root is not None else None
    if requested:
        return {'status': 'not_run_operator_interruption', 'config_id': config_id,
                'stop_reason': requested['reason_code'], 'operator_request': requested,
                'native_effects_started': False, 'model_requests_sent': False}
    state = ledger.summary()
    provider = state.get('provider_unavailable',False)
    return {'status':'not_run_provider_unavailable' if provider else 'not_run_budget_limit',
        'config_id':config_id,'stop_reason':state.get('halt_reason'),
        'native_effects_started':False,'model_requests_sent':False}


def execute_case(case, *, args, config, mapping, ledger, batch_root, stop_event=None,
                 acceptance_profile=None, acceptance_attempt=1, isolated_acceptance_index=False, medication_directory=None):
    stop_event = stop_event or Event()
    root = batch_root/case['case_id']
    root.mkdir(mode=0o700, exist_ok=True)
    index_path = root/'attempts.json'
    index = {'case_id': case['case_id'], 'attempts': {}}
    if isolated_acceptance_index and args.mode != 'linked-acceptance':
        raise ValueError('isolated indices are only for parallel acceptance assignments')
    if not isolated_acceptance_index and index_path.exists():
        index = _read(index_path)

    def save_attempt(label, record):
        index['attempts'][label] = record
        if not isolated_acceptance_index:
            _atomic(index_path, index)
    def execute(label, linked_conditions):
        run_id = args.batch + ':' + case['case_id'] + ':' + label
        expected = _expected_run(case, run_id, linked_conditions)
        if label in index['attempts']:
            record = index['attempts'][label]
            if not record.get('result_path'):
                return None
            result = _read(record['result_path'])
            validate_saved_result(result, expected)
            if result['status'] in {'stopped_budget','stopped_provider'}:
                stop_event.set()
            if result['status'] != 'waiting_retry':
                return result
        attempt_root = root/label
        # A directory without an indexed final result is an interrupted attempt.
        # Recover a saved result only; never repeat uncertain native effects.
        existing = list(attempt_root.glob('runs/*/run.json'))
        if attempt_root.exists() and not existing:
            record = {'status': 'interrupted_requires_audit', 'attempt_dir': str(attempt_root)}
            save_attempt(label, record)
            return None
        if existing:
            if len(existing) != 1:
                raise ValueError('ambiguous attempt directory')
            result = _read(existing[0])
            validate_saved_result(result, expected)
            result_path = existing[0]
        if not existing or result['status'] == 'waiting_retry':
            admission = admission_status(getattr(args, 'workflow_policy_data', None))
            if admission != 'open':
                save_attempt(label, unadmitted_record(expected['config_id'], admission))
                return None
            if _batch_stopped(ledger, stop_event, batch_root):
                save_attempt(label, unstarted_stop_record(ledger, expected['config_id'], batch_root))
                return None
            attempt_root.mkdir(mode=0o700, exist_ok=True)
            factory = BudgetedCamelFactory(config, ledger, args.model_evidence,
                case_id=case['case_id'], data_kind=case['data_kind'], run_id=run_id,
                config_id=expected['config_id'])
            continuation = None
            if getattr(args, 'close_prescription_loop', False):
                continuation = EncounterContinuation(attempt_root/'continuation.json', {
                    'run': expected, 'implementation': code_fingerprint(),
                    'generation': {k: config.get(k) for k in ('endpoint', 'role_model', 'tool_models', 'generation_policy', 'provider_profile')}})
                factory = continuation.factory(factory)
            backend = PaperProtocolPort(NativeOpenHISPort(attempt_root/'native', catalog_mapping=mapping,
                diagnosis_mapping=getattr(args, 'diagnosis_mapping', None),
                credentials_path=args.his_credentials, base_url=args.his_url))
            if getattr(args, 'active_native_capture', None) is not None:
                from .native_observation import ObservedProtocolPort
                backend = ObservedProtocolPort(backend, args.active_native_capture)
            interface = nullcontext(backend)
            if getattr(args, 'his_interface', 'native') == 'fhir-r4':
                from .fhir_his import fhir_workflow

                interface = fhir_workflow(backend, case_id=case['case_id'], run_id=run_id)
            elif getattr(args, 'his_interface', 'native') == 'fhir-r4+wst-8468':
                from .standard_his import standard_workflow
                interface = standard_workflow(backend, case_id=case['case_id'], run_id=run_id)
            elif getattr(args, 'his_interface', 'native') == 'wst-846':
                from .wst_workflow import wst_workflow
                interface = wst_workflow(backend, case_id=case['case_id'], run_id=run_id)
            with interface as his:
                runner = LinkedEncounterRunner(factory, continuation.backend(his) if continuation else his,
                    output_dir=attempt_root/'runs', close_prescription_loop=getattr(args,'close_prescription_loop',False),
                    workflow_policy=getattr(args, 'workflow_policy_data', None), continuation=continuation,
                    require_final_signed_medication=getattr(args, 'require_final_signed_medication', False),
                    medication_directory=medication_directory,
                    historical_examples=getattr(args, 'historical_examples', None),
                    shared_intake_directory=(batch_root / 'intake_reports' if getattr(args, 'paired_intake_report', None)
                                             else None))
                result = runner.run_case(case, run_id=run_id, **linked_conditions)
            validate_saved_result(result, expected)
            saved = list((attempt_root/'runs').glob('*/run.json'))
            if len(saved) != 1:
                raise RuntimeError('run did not create exactly one result')
            result_path = saved[0]
        if result['status'] in {'stopped_budget','stopped_provider'} or result.get('error_type') in {'BudgetExceeded','ProviderUnavailable'}:
            stop_event.set()
        record = {key: result.get(key) for key in ('status', 'native_complete', 'call_count',
                    'config_id', 'error_type', 'evaluation_unit')}
        record['result_path'] = str(result_path)
        save_attempt(label, record)
        print(json.dumps({'case_id': case['case_id'], 'group': case.get('group'),
              'attempt': label, **{k: v for k, v in record.items() if k != 'result_path'},
              'budget': ledger.summary()}, ensure_ascii=False), flush=True)
        return result

    if args.mode == 'linked-acceptance':
        profiles, _ = linked_profiles(config['tool_models'], factorial=getattr(args, 'acceptance_factorial', False),
                                      scenario_design=getattr(args, 'scenario_design', scenarios.VERSION))
        if acceptance_profile not in profiles:
            raise ValueError('linked acceptance dispatch must select one frozen profile')
        execute(attempt_label(acceptance_profile, acceptance_attempt), linked_conditions=profiles[acceptance_profile])
        return index

    execute(BASELINE_LABEL, scenarios.baselines())
    return index


def _acceptance_progression_checks(ref, record, profile, specification):
    """Use observed native readbacks before admitting another expensive run."""
    if record.get('status') != 'completed' or not record.get('result_path'):
        return {'runtime_reported_complete': False}
    from .workflow_analysis import audit_run
    path = Path(record['result_path'])
    run = _read(path)
    if run['scenario_design'] == scenarios.EMERGENCY:
        from .emergency import verified_closure
        return {**execution_checks(run, profile, specification['models']),
                'scoped_native_readbacks_verified': verified_closure(run) == run['artifacts'].get('ed_closure')}
    state = path.parents[2]/'native'
    native = _read(state/'native_state.json')
    protocol = _read(state/'protocol_state.json')
    binding = native.get('bindings', {}).get(run['run_id']+':'+run['case_id'], {})
    audit = audit_run(run, specification, expected_case=ref, binding=binding, protocol=protocol)
    return {**execution_checks(run, profile, specification['models']),
            'scoped_native_readbacks_verified': bool(binding) and
                audit['independently_checked_core_complete'] is True,
            'no_violation_in_retained_readbacks': not audit['observed_violations']}


def bind_verified_acceptance_recovery(path, specification):
    """Keep paid assignments from a closed, independently audited transport interruption."""
    plan = _read(path)
    source = Path(plan['source_batch'])
    source_spec = source/'specification.json'
    source_summary = source/'batch_summary.json'
    audit_path = Path(plan['native_audit'])
    if (plan['version'] != 'verified_partial_acceptance_recovery_v1'
            or file_sha256(source_spec) != plan['source_specification_sha256']
            or file_sha256(source_summary) != plan['source_summary_sha256']
            or file_sha256(audit_path) != plan['native_audit_sha256']):
        raise ValueError('Verified recovery source files changed')
    previous = _read(source_spec)
    summary = _read(source_summary)
    audit = _read(audit_path)
    report = audit['audit']
    gate = report['linked_acceptance_gate']
    # A different credential on the same declared endpoint is allowed. Only
    # the dispatcher changed in code. Inputs, clinical rules and models match.
    immutable = ('cases', 'models', 'transport_policy', 'generation', 'generation_policy',
                 'data_policy_hash', 'catalog_hash', 'diagnosis_catalog_hash', 'rules',
                 'workflow_limits', 'modeled_activity_seconds', 'prescription_followthrough_policy',
                 'linked_acceptance', 'acceptance_source_patient_hashes')
    if (previous['batch'] == specification['batch'] or previous['mode'] != 'linked-acceptance'
            or not all(previous[k] == specification[k] for k in immutable)
            or previous.get('his_interface') != specification.get('his_interface')
            or {k: v for k, v in previous['code_hashes'].items() if k != 'run_batch.py'}
            != {k: v for k, v in specification['code_hashes'].items() if k != 'run_batch.py'}
            or previous['native_observation']['source_hashes'] != specification['native_observation']['source_hashes']
            or summary['native_observation']['status'] != 'closed'
            or not report['source_files_unchanged'] or not report['native_observation_requested']
            or report['source_specification_sha256'] != plan['source_specification_sha256']
            or gate['reused_native_identity_runs'] != 0):
        raise ValueError('Recovery requires identical clinical inputs/runtime and a closed native audit')
    verified_study_ids = {row['study_id'] for row in report['runs'] if row.get('verified_paid') is True}
    verified = {(row['case_id'], row['condition']) for row in audit['crosswalk']
                if row['study_id'] in verified_study_ids}
    paid = {(row['case_id'], row['profile']) for row in plan['verified_paid']}
    pending = {(row['case_id'], row['profile']) for row in plan['pending']}
    expected = {(case['case_id'], p['label']) for case in specification['cases']
                for p in specification['linked_acceptance']['profiles']}
    if (not paid or not pending or paid != verified or paid & pending or paid | pending != expected
            or len(paid) != len(verified_study_ids) or len(paid) != gate['verified_unique_encounters']
            or len(paid) != plan['verified_paid_count'] or len(pending) != plan['pending_count']):
        raise ValueError('Recovery assignment coverage differs from the native audit')
    attempts = {row['case_id']: row['attempts'] for row in summary['cases']}
    for row in plan['verified_paid']:
        record = attempts[row['case_id']][row['profile']]
        if (record.get('status') != 'completed'
                or file_sha256(record['result_path']) != row['run_sha256']):
            raise ValueError('Previously verified paid attempt changed')
    for row in plan['pending']:
        record = attempts[row['case_id']][row['profile']]
        if record['status'] == 'not_run_regression_gate':
            if record.get('native_effects_started') is False and record.get('model_requests_sent') is False:
                continue
        elif record['status'] == 'failed' and record.get('error_type') == 'ModelCallError':
            run = _read(record['result_path'])
            if (file_sha256(record['result_path']) == row.get('source_run_sha256')
                    and row.get('provider_status') in (401, 403)
                    and run['native_complete'] is False and 'settlement' not in run['artifacts']
                    and not any((e.get('event') == 'business_request_intent' and e.get('operation') == 'payment.commit')
                                or (e.get('event') == 'business_request_outcome' and e.get('status') == 'unresolved_exception')
                                for e in run['events'])):
                continue
        raise ValueError('Recovery may only rerun documented transport failures without payment or unresolved writes')
    return frozenset(paid), {'version': plan['version'], 'plan_sha256': file_sha256(path),
            'source_batch': previous['batch'], 'source_specification_sha256': plan['source_specification_sha256'],
            'source_native_audit_sha256': plan['native_audit_sha256'], 'retained_verified_paid': len(paid),
            'new_assignment_count': len(pending), 'same_clinical_runtime': True}


def retained_paid_record(config_id):
    return {'status': 'not_run_verified_paid_prior_capture', 'config_id': config_id,
            'native_effects_started': False, 'model_requests_sent': False, 'retained_in_planned_denominator': True}


def execute_acceptance_schedule(cases, acceptance, *, args, config, mapping, ledger,
                                batch_root, stop_event, specification, session_log, session_id):
    if (getattr(args, 'scenario_design', scenarios.VERSION) in scenarios.DESIGNS
            and getattr(args, 'close_prescription_loop', False)):
        return execute_continuation_schedule(cases, acceptance, args=args, config=config,
            mapping=mapping, ledger=ledger, batch_root=batch_root, stop_event=stop_event,
            specification=specification, session_log=session_log, session_id=session_id)
    from .linked_acceptance import READER_DESIGN_VERSION
    profiles = {profile['label']: profile for profile in acceptance['profiles']}
    by_assignment = acceptance['schedule_policy'] == ASSIGNMENT_SCHEDULE_POLICY
    stop_on_failure = (acceptance['version'] not in {READER_DESIGN_VERSION, 'linked_fixed_assignment_repeat_v1'}
                       and not acceptance.get('independent_crossed', False))
    failure_event = 'regression_gate_stopped' if stop_on_failure else 'assignment_failed'
    indices = {ref['case_id']: {'case_id': ref['case_id'], 'attempts': {}} for ref in cases}
    index_locks = {ref['case_id']: Lock() for ref in cases} if by_assignment else {}
    stopped = None
    stop_lock = Lock()

    def mark_stop(failure):
        nonlocal stopped
        with stop_lock:
            if stopped is None:
                stopped = failure

    def persist(index, case_id, label):
        path = batch_root/case_id/'attempts.json'
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if not by_assignment:
            _atomic(path, index)
            return
        with index_locks[case_id]:
            shared = indices[case_id]
            shared['attempts'][label] = index['attempts'][label]
            history = index.get('prior_attempts', {}).get(label)
            if history:
                shared.setdefault('prior_attempts', {})[label] = deepcopy(history)
            _atomic(path, shared)

    def execute_unit(ref, label, index):
        case_id = ref['case_id']
        profile = profiles[label]
        index_path = batch_root/case_id/'attempts.json'
        admission = admission_status(args.workflow_policy_data)
        if (case_id, label) in getattr(args, 'prior_verified_paid', ()):
            record = retained_paid_record(profile['config_id'])
        elif stopped:
            record = {'status': 'not_run_regression_gate', 'config_id': profile['config_id'],
                      'blocking_attempt': stopped, 'native_effects_started': False,
                      'model_requests_sent': False, 'retained_in_planned_denominator': True}
        elif admission != 'open':
            record = unadmitted_record(profile['config_id'], admission)
        elif _batch_stopped(ledger, stop_event, batch_root):
            record = unstarted_stop_record(ledger, profile['config_id'], batch_root)
        else:
            maximum = acceptance.get('max_attempts_per_unit', 1)
            failure = None
            for number in range(1, maximum + 1):
                trial = attempt_label(label, number)
                record_batch_event(session_log, 'case_dispatched', session_id=session_id,
                    case_id=case_id, acceptance_profile=label, attempt_number=number)
                retry = False
                try:
                    returned = execute_case_reference(ref, args=args, config=config, mapping=mapping,
                        ledger=ledger, batch_root=batch_root, stop_event=stop_event,
                        acceptance_profile=label, acceptance_attempt=number,
                        **({'isolated_acceptance_index': True} if by_assignment else {}))
                    if not by_assignment:
                        index = returned
                    record = returned['attempts'][trial]
                    index['attempts'][trial] = record
                    checks = _acceptance_progression_checks(ref, record, profile, specification)
                    record['progression_checks'] = checks
                    if not all(checks.values()):
                        retry = (stopped is None and number < maximum
                            and record.get('status') in ('incomplete_clinical_decision', 'failed')
                            and bool(record.get('result_path'))
                            and documented_unpaid_stop(_read(record['result_path']))
                            and admission_status(args.workflow_policy_data) == 'open'
                            and not _batch_stopped(ledger, stop_event, batch_root))
                        if not retry:
                            failure = {'case_id': case_id, 'profile': label, 'status': record['status'],
                                       'failed_checks': [name for name, passed in checks.items() if not passed]}
                except Exception as exc:
                    if not by_assignment and index_path.exists():
                        index = _read(index_path)
                    record = index['attempts'].get(trial, {'status': 'driver_error',
                                'config_id': profile['config_id']})
                    record['progression_error_type'] = type(exc).__name__
                    failure = {'case_id': case_id, 'profile': label, 'status': record['status'],
                               'error_type': type(exc).__name__}
                if failure and stop_on_failure:
                    mark_stop(failure)
                record['attempt_number'] = number
                index['attempts'].pop(trial, None)
                index['attempts'][label] = record
                record_batch_event(session_log, 'case_worker_returned', session_id=session_id,
                    case_id=case_id, acceptance_profile=label, attempt_number=number,
                    status='retrying_documented_unpaid_stop' if retry else
                    (failure_event if failure else 'index_returned'))
                if not retry:
                    break
                index.setdefault('prior_attempts', {}).setdefault(label, []).append(deepcopy(record))
                persist(index, case_id, label)
            if failure:
                print(json.dumps({'event': failure_event, **failure}), flush=True)
        index['attempts'][label] = record
        persist(index, case_id, label)
        return index

    dispatch_cases = cases
    if acceptance.get('version') == 'linked_fixed_assignment_repeat_v1':
        from .assignment_repeat import units as repeated_units
        units = iter(repeated_units(cases, acceptance))
    elif acceptance.get('independent_crossed'):
        order = heldout_dispatch_order(cases, profiles)
        frozen = specification.get('execution_policy', {}).get('heldout_dispatch_sha256')
        if frozen is not None and digest(order) != frozen:
            raise ValueError('Held-out dispatch order differs from the frozen specification.')
        cases_by_id = {case['case_id']: case for case in cases}
        dispatch_cases = [cases_by_id[case_id] for case_id, _ in order]
        units = ((cases_by_id[case_id], label) for case_id, labels in order for label in labels)
    else:
        units = iter(ordered_units(cases, profiles, rotate=acceptance.get('factorial', False)))
    initial_count = acceptance.get('initial_admission_assignments', len(acceptance['first_baseline_regressions']))
    for _ in range(initial_count):
        ref, label = next(units)
        case_id = ref['case_id']
        initial = {'case_id': case_id, 'attempts': {}} if by_assignment else indices[case_id]
        result = execute_unit(ref, label, initial)
        if not by_assignment:
            indices[case_id] = result
    workers = getattr(args, 'workers', 1)
    if workers == 1:
        for ref, label in units:
            indices[ref['case_id']] = execute_unit(ref, label, indices[ref['case_id']])
    else:
        remaining = {ref['case_id']: [] for ref in cases}
        for ref, label in units:
            remaining[ref['case_id']].append(label)

        if by_assignment:
            # Interleave patients, while each profile uses its own run and native encounter.
            lanes = [[(ref, label) for label in remaining[ref['case_id']]] for ref in dispatch_cases]
            scheduled = (unit for row in zip_longest(*lanes) for unit in row if unit is not None)

            def run_assignment(unit):
                ref, label = unit
                execute_unit(ref, label, {'case_id': ref['case_id'], 'attempts': {}})

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(run_assignment, scheduled):
                    pass
            return [indices[ref['case_id']] for ref in cases], stopped

        def run_patient(ref):
            case_id = ref['case_id']
            index = indices[case_id]
            for label in remaining[case_id]:
                index = execute_unit(ref, label, index)
            return index

        with ThreadPoolExecutor(max_workers=workers) as pool:
            indices = {ref['case_id']: index for ref, index in
                       zip(dispatch_cases, pool.map(run_patient, dispatch_cases), strict=True)}
    return [indices[ref['case_id']] for ref in cases], stopped


def execute_continuation_schedule(cases, acceptance, *, args, config, mapping, ledger,
                                  batch_root, stop_event, specification, session_log, session_id):
    from .continuation import run_retry_queue
    profiles = {p['label']: p for p in acceptance['profiles']}
    indices = {ref['case_id']: {'case_id': ref['case_id'], 'attempts': {}} for ref in cases}
    locks = {ref['case_id']: Lock() for ref in cases}
    if acceptance.get('version') == 'linked_fixed_assignment_repeat_v1':
        from .assignment_repeat import units as repeated_units
        units = list(repeated_units(cases, acceptance))
    elif acceptance.get('independent_crossed'):
        by_id = {ref['case_id']: ref for ref in cases}
        order = heldout_dispatch_order(cases, profiles)
        frozen = specification.get('execution_policy', {}).get('heldout_dispatch_sha256')
        if frozen is not None and digest(order) != frozen:
            raise ValueError('Held-out dispatch order differs from the frozen specification')
        units = [(by_id[case_id], label) for case_id, labels in order for label in labels]
    else:
        units = list(ordered_units(cases, profiles, rotate=acceptance.get('factorial', False)))

    def save(ref, label, record):
        with locks[ref['case_id']]:
            index = indices[ref['case_id']]
            index['attempts'][label] = deepcopy(record)
            directory = batch_root/ref['case_id']
            directory.mkdir(exist_ok=True, mode=0o700)
            _atomic(directory/'attempts.json', index)

    def execute(unit, dispatch):
        ref, label = unit
        record_batch_event(session_log, 'case_dispatched', session_id=session_id,
            case_id=ref['case_id'], acceptance_profile=label, dispatch_number=dispatch,
            same_encounter_continuation=dispatch > 1)
        returned = execute_case_reference(ref, args=args, config=config, mapping=mapping,
            ledger=ledger, batch_root=batch_root, stop_event=stop_event,
            acceptance_profile=label, acceptance_attempt=1, isolated_acceptance_index=True)
        record = returned['attempts'][attempt_label(label, 1)]
        record['dispatch_number'] = dispatch
        record['attempt_number'] = 1
        if record['status'] == 'completed':
            record['progression_checks'] = _acceptance_progression_checks(ref, record, profiles[label], specification)
        save(ref, label, record)
        record_batch_event(session_log, 'case_worker_returned', session_id=session_id,
            case_id=ref['case_id'], acceptance_profile=label, dispatch_number=dispatch,
            status=record['status'])
        return record

    def requeue(unit, dispatch, delay):
        ref, label = unit
        record_batch_event(session_log, 'provider_retry_queued', session_id=session_id,
            case_id=ref['case_id'], acceptance_profile=label, dispatch_number=dispatch,
            queue_position='tail', retry_after_seconds=delay, verified_complete=False)

    prior_paid = getattr(args, 'prior_verified_paid', ())
    pending_units = []
    for ref, label in units:
        if (ref['case_id'], label) in prior_paid:
            save(ref, label, retained_paid_record(profiles[label]['config_id']))
        else:
            pending_units.append((ref, label))
    _, pending = run_retry_queue(pending_units, execute, workers=getattr(args, 'workers', 1),
        stopped=lambda: _batch_stopped(ledger, stop_event, batch_root), on_requeue=requeue)
    for ref, label in pending:
        if label not in indices[ref['case_id']]['attempts']:
            save(ref, label, unstarted_stop_record(ledger, profiles[label]['config_id'], batch_root))
    return [indices[ref['case_id']] for ref in cases], None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--provider-config', required=True, type=Path)
    parser.add_argument('--budget-ledger', required=True, type=Path)
    parser.add_argument('--uncapped-budget', action='store_true',
        help='Use a ledger explicitly changed to uncapped by recorded user authorization')
    parser.add_argument('--model-evidence', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--catalog', type=Path, default=Path('deploy/openhis-experiment/catalog_mapping.json'))
    parser.add_argument('--diagnosis-catalog', type=Path,
        default=Path('deploy/openhis-experiment/diagnosis_mapping.json'),
        help='Explicit versioned native diagnosis mapping; code systems are not inferred')
    parser.add_argument('--his-credentials', type=Path)
    parser.add_argument('--his-url', default='http://127.0.0.1:28081/api/app')
    parser.add_argument('--his-interface', choices=('native', 'fhir-r4', 'wst-846', 'fhir-r4+wst-8468'), default='fhir-r4',
                        help='Select the native path or the FHIR R4 workflow bridge for a fresh batch.')
    parser.add_argument('--mode', choices=('linked', 'linked-acceptance'), default='linked')
    parser.add_argument('--scenario-design', choices=scenarios.DESIGNS,
                        default=scenarios.VERSION)
    parser.add_argument('--assignment-plan', type=Path,
                        help='Repeat a frozen subset of prior whole-encounter assignments once each.')
    parser.add_argument('--close-prescription-loop', action='store_true',
        help='Retain original review endpoint, continue repair and actual disposition/note/payment')
    parser.add_argument('--engineering-only', action='store_true', help='Declare engineering execution outside the formal study')
    parser.add_argument('--acceptance-patients-per-disease', type=int,
                        default=PATIENTS_PER_GROUP, help='Declared engineering target per disease and workflow')
    parser.add_argument('--independent-crossed', action='store_true',
                        help='Run every configuration on a separate test partition without clinical retries.')
    parser.add_argument('--reader-comparison', action='store_true',
                        help='Cross the paper workflows on the supplied independent dataset.')
    parser.add_argument('--reader-quick', action='store_true',
                        help='Select the three predefined whole-visit reader configurations.')
    parser.add_argument('--exclude-development-manifest', type=Path,
                        help='Private manifest with development patient keys in the same identity namespace.')
    parser.add_argument('--acceptance-attempts', type=int, choices=(1, MAX_ENGINEERING_ATTEMPTS),
                        help='Maximum independent attempts per engineering assignment')
    parser.add_argument('--acceptance-factorial', action='store_true',
                        help='Require the full crossing of preconsultation, prescription and documentation')
    parser.add_argument('--acceptance-recovery-plan', type=Path,
                        help='Skip already audited paid units in a new native capture of the same engineering design')
    parser.add_argument('--workflow-policy', type=Path,
        help='Prospectively frozen observation/dispatch policy, also usable in engineering validation')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--capture-native', action='store_true',
        help='Opt-in committed native effect measurement; new engineering batch, exclusive isolated instance')
    parser.add_argument('--observation-deploy', type=Path,
        help='Existing isolated deployment directory; never copy credentials into frozen code')
    args = parser.parse_args()
    args.workflow_policy_data = validate_window(_read(args.workflow_policy), workers=args.workers,
        scenario_design=args.scenario_design) if args.workflow_policy else None
    if ((args.acceptance_patients_per_disease != PATIENTS_PER_GROUP or args.acceptance_factorial)
            and args.mode != 'linked-acceptance'):
        parser.error('Acceptance coverage arguments apply to linked acceptance or formal admission')
    if args.acceptance_attempts is not None and args.mode != 'linked-acceptance':
        parser.error('--acceptance-attempts applies to linked acceptance')
    if args.acceptance_recovery_plan and args.mode != 'linked-acceptance':
        parser.error('Recovery requires linked engineering acceptance with native capture')
    if args.independent_crossed and not (
        args.mode == 'linked-acceptance' and args.acceptance_factorial
        and args.acceptance_attempts == 1 and args.exclude_development_manifest
        and not args.acceptance_recovery_plan
    ):
        parser.error('Independent crossed runs require a test cohort, a development exclusion manifest, '
                     'every configuration, and one attempt per assignment.')
    if args.exclude_development_manifest and not args.independent_crossed:
        parser.error('--exclude-development-manifest applies only to independent crossed runs')
    if args.reader_comparison and not (
        args.mode == 'linked-acceptance' and args.acceptance_factorial
        and args.acceptance_attempts == 1 and args.workflow_policy
        and not (args.independent_crossed or args.acceptance_recovery_plan)
    ):
        parser.error('Reader comparisons require a frozen window and one attempt per crossed assignment.')
    if args.reader_quick and not args.reader_comparison:
        parser.error('--reader-quick requires --reader-comparison')
    if not args.independent_crossed and args.acceptance_patients_per_disease not in SUPPORTED_PATIENTS_PER_GROUP:
        parser.error('Unsupported linked acceptance patient target')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', args.batch):
        parser.error('batch must be a safe directory name')
    if not 1 <= args.workers <= 32 or (args.limit is not None and args.limit <= 0):
        parser.error('workers must be 1..32 and limit positive')
    if args.capture_native != bool(args.observation_deploy):
        parser.error('--capture-native and --observation-deploy must be specified together')
    if args.mode == 'linked-acceptance' and (
            not (args.engineering_only and args.close_prescription_loop and args.capture_native)
            or args.limit is not None):
        parser.error('linked acceptance requires the full engineering cohort, repair loop and native capture')
    from .batch_observation import deployment_metadata, observed_batch, capture_summary
    args.observation_metadata = deployment_metadata(args.observation_deploy,args.workflow_policy_data) if args.capture_native else None
    manifest_bytes = args.manifest.read_bytes()
    cases = select_cases(json.loads(manifest_bytes), args.limit)
    if any(scenarios.design_for(c) != args.scenario_design for c in cases):
        parser.error('Case setting and selected workflow endpoint differ.')
    if args.scenario_design == scenarios.EMERGENCY and args.acceptance_recovery_plan:
        parser.error('Payment-based recovery is restricted to the outpatient design.')
    if args.independent_crossed:
        exclusion = args.exclude_development_manifest.read_bytes()
        development = json.loads(exclusion)['cases']
        if not development or any(row['split'] != 'development' or not row['patient_key'] for row in development):
            raise ValueError('A nonempty development-partition manifest is required.')
        if {row['patient_key'] for row in development} & {case['patient_key'] for case in cases}:
            raise ValueError('A source patient occurs in both partitions.')
    config, mapping = _read(args.provider_config), _read(args.catalog)
    if any('formulary_directory' in case for case in cases):
        from .formulary_search import directory_for_case
        native_hash = digest(mapping)
        for case in cases:
            directory = directory_for_case(case, case['path'])
            if directory is None or directory.mapping_hash != native_hash:
                raise ValueError('All assigned encounters must use the matching shared OpenHIS product map')
    scenarios.conditions(config['tool_models'])
    acceptance = (freeze_design(cases, config['tool_models'],
                                patients_per_group=args.acceptance_patients_per_disease,
                                workers=args.workers, max_attempts=args.acceptance_attempts,
                                factorial=args.acceptance_factorial, independent=args.independent_crossed,
                                reader=args.reader_comparison, quick=args.reader_quick,
                                scenario_design=args.scenario_design)
                  if args.mode == 'linked-acceptance' else None)
    if args.assignment_plan:
        if acceptance is None or not args.acceptance_factorial or args.acceptance_attempts != 1:
            raise ValueError('An assignment repetition requires a one-attempt linked factorial design')
        from .assignment_repeat import from_file
        profiles, _ = linked_profiles(config['tool_models'], factorial=True, scenario_design=args.scenario_design)
        acceptance = from_file(acceptance, args.assignment_plan, cases, profiles)
    if args.workers>5 and config.get('minimum_request_interval_seconds',0)<1.0:
        parser.error('more than five workers require explicit shared API pacing of at least one second')
    args.diagnosis_mapping = validate_diagnosis_mapping(_read(args.diagnosis_catalog))
    rule_metadata = {'engine': None, 'prescription_review': 'common_pharmacist_review_outpatient_v2'}
    generation_policy = validate_generation_policy(config)
    policy_provenance = [
        check_data_policy(kind, config.get('data_policy', {}), config['endpoint'].rstrip('/'),
                          formal=args.independent_crossed)
        for kind in sorted({case['data_kind'] for case in cases})
    ]
    specification = {'batch': args.batch, 'mode': args.mode,
        'scenario_design': args.scenario_design,
        'prescription_followthrough_policy': PRESCRIPTION_FOLLOWTHROUGH_POLICY if args.close_prescription_loop else None,
        'execution_policy': {'workers': args.workers,
            'operator_interruption_policy': 'operator_stop_v1_finish_inflight_attempt_then_record_all_unstarted',
            'dispatch_unit': ('one_isolated_encounter_per_assignment'
                              if acceptance and acceptance['schedule_policy'] == ASSIGNMENT_SCHEDULE_POLICY
                              else 'one_case_with_sequential_conditions'),
            'dispatch_order': ('interleaved_frozen_assignments'
                               if acceptance and acceptance['schedule_policy'] == ASSIGNMENT_SCHEDULE_POLICY
                               else 'frozen_manifest_order'),
            'admission_policy': 'closed_loop_worker_availability',
            'formal_submission_window': (args.workflow_policy_data or {}).get('submission_window'),
            'formal_observation_window_seconds': (args.workflow_policy_data or {}).get('window_seconds'),
            'formal_timeout_assessment': 'prospectively_frozen' if args.workflow_policy_data else 'not_prespecified_for_engineering'},
        'workflow_observation_policy': args.workflow_policy_data,
        'cases': [{k: case.get(k) for k in ('case_id', 'group', 'data_kind', 'split',
                    'file_sha256', 'case_hash', 'runtime_input_hash')} for case in cases],
        'case_loading': 'streamed_preflight_then_one_verified_case_per_worker',
        'business_protocol_version': PROTOCOL_VERSION,
        'models': {k: config[k] for k in ('role_model', 'tool_models', 'endpoint')},
        'transport_policy': {
            'network_route':config.get('network_route', {'mode':'system_default'}),
            'read_timeout_seconds':config.get('timeout_seconds',120),
            'connect_timeout_seconds':config.get('connect_timeout_seconds'),
            'response_transport':config.get('response_transport','nonstream'),
            'stream_max_duration_seconds':config.get('stream_max_duration_seconds') if config.get('response_transport')=='sse' else None,
            'incomplete_stream_retry': STREAM_RETRY_POLICY if config.get('response_transport')=='sse' else None,
            'proxy_auth_retry': PROXY_AUTH_RETRY_POLICY,
            'minimum_request_interval_seconds':config.get('minimum_request_interval_seconds',0),
            'tls_verification':True, 'follow_redirects':False},
        'budget_limit_mode': 'uncapped_user_authorized' if args.uncapped_budget else 'capped_1000_cny',
        'pricing_cny_per_million_tokens': {k: list(v) for k, v in configured_prices(config).items()},
        'memory_policy': MEMORY_POLICY,
        'generation_policy': generation_policy,
        'response_contract_policy': RESPONSE_CONTRACT_POLICY,
        'diagnosis_code_policy': DIAGNOSIS_CODE_POLICY,
        'native_observation': args.observation_metadata,
        'workflow_limits': DEFAULT_LIMITS, 'modeled_activity_seconds': DEFAULT_TIMING,
        'continuation_policy': CONTINUATION_POLICY if args.close_prescription_loop else None,
        'consultation_dialogue_policy': CONSULTATION_DIALOGUE_POLICY,
        'documentation_source_version': scenarios.DOCUMENTATION_SOURCE_VERSION,
        'current_measurement_policy': CURRENT_MEASUREMENT_POLICY,
        'clinical_disposition_policy': CLINICAL_DISPOSITION_POLICY,
        'history_access_policy': {'chart_policy': CHART_POLICY, 'initial_byte_limit': INITIAL_BYTE_LIMIT,
            'page_byte_limit': PAGE_BYTE_LIMIT, 'visible_history_byte_limit': HISTORY_CONTEXT_BYTE_LIMIT,
            'clinical_projection': PROJECTION_POLICY,
            'queries_per_physician_phase': DEFAULT_LIMITS['history_queries_per_phase'],
            'extra_reviewer_queries': False,
            'separate_reconciliation_service_queries':DEFAULT_LIMITS['history_queries_per_phase'],
            'separate_reconciliation_service_questions':DEFAULT_LIMITS['reconciliation_turns'],
            'service_budget_scope':'Each actual in-hospital reconciliation service, separately logged; prescription review itself does not query'},
        'data_policy_hash': digest(config.get('data_policy', {})),
        'data_policy_provenance': policy_provenance,
        'generation': {k: config.get(k) for k in ('temperature', 'max_completion_tokens', 'max_attempts', 'max_calls_per_run')},
        'catalog_hash': digest(mapping), 'diagnosis_catalog_hash': digest(args.diagnosis_mapping),
        'diagnosis_code_systems': sorted({row['system'] for row in args.diagnosis_mapping.values()}),
        'code_hashes': code_fingerprint(),
        'rules': rule_metadata,
        'evaluation_protocol': 'fully_virtual_workflow_v1',
        'external_human_review_required': False,
        'clinical_evaluation': 'programmatic_checks_with_declared_coverage',
        'evaluation_scope': {
            'prescription_review': 'common_pharmacist_review',
            'complete_encounter': ('signed_ED_note_signed_drugs_documented_disposition' if args.scenario_design == scenarios.EMERGENCY
                                   else 'all_required_tasks_signed_order_attested_note_verified_medication_payment'),
            'unrestricted_clinical_appropriateness': 'not_assessed',
            'literature_comparison': 'after_observed_results_not_case_labels',
        },
        'formal_experiment': False,
        'conditions': [item.to_dict() for item in scenarios.conditions(config['tool_models'])]}
    if args.his_interface == 'fhir-r4':
        from .fhir_his import interface_contract

        specification['his_interface'] = interface_contract()
    elif args.his_interface == 'fhir-r4+wst-8468':
        from .standard_his import interface_contract
        specification['his_interface'] = interface_contract()
    elif args.his_interface == 'wst-846':
        from .wst_workflow import interface_contract
        specification['his_interface'] = interface_contract()
    if args.scenario_design == scenarios.EMERGENCY:
        from . import emergency
        specification['evaluation_scope']['complete_encounter'] = emergency.ENDPOINT
        specification['continuation_policy'] = dict(emergency.WORK_POLICY)
        specification['native_registration_class'] = 'AMB'
        specification['rules'] = {**rule_metadata, 'prescription_review': 'common_pharmacist_review'}
        if specification['prescription_followthrough_policy']:
            specification['prescription_followthrough_policy'] = dict(PRESCRIPTION_FOLLOWTHROUGH_POLICY,
                success=emergency.ENDPOINT, unresolved_end='ED clinical work remains incomplete',
                followup_budgets=emergency.WORK_POLICY['clinical_work'])
    if acceptance is not None:
        specification['linked_acceptance'] = acceptance
        if args.scenario_design == scenarios.EMERGENCY and acceptance.get('factorial'):
            from .intake import SHARED_REPORT_POLICY
            specification['paired_intake_report'] = args.paired_intake_report = SHARED_REPORT_POLICY
        specification['role_effort_analysis'] = scenarios.effort_plan()
        if args.independent_crossed:
            specification['development_exclusion_sha256'] = hashlib.sha256(exclusion).hexdigest()
        specification['acceptance_source_patient_hashes'] = sorted(digest(r['patient_key']) for r in cases)
        specification['execution_policy']['dispatch_unit'] = (
            'one_isolated_encounter_per_assignment' if acceptance['schedule_policy'] == ASSIGNMENT_SCHEDULE_POLICY
            else 'one_encounter_per_patient_then_verify' if args.workers > 1
            else 'one_encounter_then_verify_before_admitting_next')
        execution = specification['execution_policy']
        execution['dispatch_order'] = acceptance['schedule_policy']
        if args.independent_crossed:
            labels = [profile['label'] for profile in acceptance['profiles']]
            execution['dispatch_order'] = HELDOUT_DISPATCH_ORDER
            execution['heldout_dispatch_sha256'] = digest(heldout_dispatch_order(cases, labels))
    if args.acceptance_recovery_plan:
        if file_sha256(args.manifest) != _read(args.acceptance_recovery_plan)['frozen_manifest_sha256']:
            raise ValueError('Recovery requires the exact frozen case manifest')
        args.prior_verified_paid, specification['engineering_recovery'] = bind_verified_acceptance_recovery(
            args.acceptance_recovery_plan, specification)
    ledger = BudgetLedger(args.budget_ledger, cap_cny=None if args.uncapped_budget else 1000)
    specification['budget_ledger'] = ledger.identity()
    if args.dry_run:
        print(json.dumps({'specification': specification, 'budget': ledger.summary()}, ensure_ascii=False, indent=2))
        return
    if admission_status(args.workflow_policy_data) == 'not_open':
        raise ValueError('submission window has not opened; no model or HIS requests were made')
    batch_root = args.output/args.batch
    batch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (batch_root/'batch.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spec_path = batch_root/'specification.json'
        if spec_path.exists():
            if _read(spec_path) != specification:
                raise ValueError('batch inputs/code/config changed; create a new versioned batch')
        else:
            private_json(spec_path, specification)
        started = time.monotonic()
        session_id = datetime.now(timezone.utc).isoformat()
        session_log = batch_root/'batch_sessions.jsonl'
        record_batch_event(session_log, 'batch_session_started', session_id=session_id,
            workers=args.workers, planned_cases=len(cases), specification_hash=digest(specification))
        results = []
        stop_event = Event()
        progression_stop = None
        remaining = deque(cases if acceptance is None else ())
        with observed_batch(args, batch_root), ThreadPoolExecutor(max_workers=args.workers) as pool:
            if acceptance is not None:
                results, progression_stop = execute_acceptance_schedule(cases, acceptance, args=args,
                    config=config, mapping=mapping, ledger=ledger, batch_root=batch_root, stop_event=stop_event,
                    specification=specification, session_log=session_log, session_id=session_id)
            futures = {}
            while remaining or futures:
                while (remaining and len(futures) < args.workers and not _batch_stopped(ledger, stop_event, batch_root)
                       and admission_status(args.workflow_policy_data) == 'open'):
                    case = remaining.popleft()
                    record_batch_event(session_log, 'case_dispatched', session_id=session_id,
                        case_id=case['case_id'])
                    futures[pool.submit(execute_case_reference, case, args=args, config=config, mapping=mapping,
                        ledger=ledger, batch_root=batch_root, stop_event=stop_event)] = case['case_id']
                if not futures:
                    break
                finished, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=1)
                for future in finished:
                    case_id = futures.pop(future)
                    try:
                        results.append(future.result())
                        record_batch_event(session_log, 'case_worker_returned',
                            session_id=session_id, case_id=case_id, status='index_returned')
                    except Exception as exc:
                        row = {'case_id': case_id, 'status': 'driver_error', 'error_type': type(exc).__name__}
                        _atomic(batch_root/(case_id+'_driver_error.json'), {**row, 'message': str(exc)[:1000]})
                        results.append(row)
                        record_batch_event(session_log, 'case_worker_returned',
                            session_id=session_id, case_id=case_id, status='driver_error',
                            error_type=type(exc).__name__)
                        print(json.dumps(row), flush=True)
            # These cases were not dispatched and therefore caused no model or
            # native side effects. Preserve every planned configuration explicitly.
            for case in remaining:
                root = batch_root/case['case_id']
                root.mkdir(mode=0o700, exist_ok=True)
                path = root/'attempts.json'
                index = _read(path) if path.exists() else {'case_id': case['case_id'], 'attempts': {}}
                config_id = linked_config_id(scenarios.baselines())
                window = admission_status(args.workflow_policy_data)
                record = (unadmitted_record(config_id, window) if window != 'open'
                          else unstarted_stop_record(ledger, config_id, batch_root))
                index['attempts'].setdefault(BASELINE_LABEL, record)
                _atomic(path, index)
                results.append(index)
        final_ledger = ledger.summary()
        summary = {'batch': args.batch, 'elapsed_seconds': time.monotonic()-started, 'cases': results,
                   'session_started_at': session_id,
                   'session_finished_at': datetime.now(timezone.utc).isoformat(),
                   'execution_policy': specification['execution_policy'],
                   'budget': final_ledger,
                   'stopped_budget': stop_event.is_set() and not final_ledger.get('provider_unavailable',False),
                   'stopped_provider': final_ledger.get('provider_unavailable',False),
                   'stopped_operator': operator_stop_request(batch_root),
                   'acceptance_progression_stop': progression_stop,
                   'native_observation': capture_summary(batch_root),
                   'evaluation_protocol': specification['evaluation_protocol'],
                   'external_human_review_required': False,
                   'clinical_evaluation': specification['clinical_evaluation']}
        _atomic(batch_root/'batch_summary.json', summary)
        record_batch_event(session_log, 'batch_session_finished', session_id=session_id,
            cases_returned=len(results), stopped_budget=summary['stopped_budget'],
            stopped_provider=summary['stopped_provider'],
            stopped_operator=summary['stopped_operator'] is not None,
            elapsed_seconds=summary['elapsed_seconds'])
        print(json.dumps({'batch': args.batch, 'cases_finished': len(results), 'budget': ledger.summary()}), flush=True)


if __name__ == '__main__':
    main()
