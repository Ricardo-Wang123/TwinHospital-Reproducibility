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
                       CONSULTATION_DIALOGUE_POLICY)
from .chart import POLICY as CHART_POLICY, INITIAL_BYTE_LIMIT, PAGE_BYTE_LIMIT
from .chart_projection import PROJECTION_POLICY
from .contracts import (baseline_conditions as _baseline_conditions, digest,
                        linked_config_id, paper_conditions, runtime_case)
from .linked_acceptance import (
    ASSIGNMENT_SCHEDULE_POLICY, BASELINE_LABEL, HELDOUT_DISPATCH_ORDER, MAX_ENGINEERING_ATTEMPTS,
    PATIENTS_PER_GROUP, SUPPORTED_PATIENTS_PER_GROUP, attempt_label, documented_unpaid_stop,
    execution_checks, freeze_design, heldout_dispatch_order, linked_profiles, ordered_units,
)
from .native_his import NativeOpenHISPort
from .protocol_his import PaperProtocolPort, PROTOCOL_VERSION
from .provider import BudgetLedger, configured_prices, check_data_policy, private_json, validate_generation_policy
from .rules import EngineeringPPRS
from .response_contracts import POLICY as RESPONSE_CONTRACT_POLICY
from .diagnosis_codes import POLICY as DIAGNOSIS_CODE_POLICY
from .campaign import (admission_status, bind_formal_analysis, bind_formal_inputs, unadmitted_record, validate_formal_design,
                       validate_window, verify_engineering_acceptance)


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
             'native_his.py', 'protocol_his.py', 'provider.py', 'provider_profile.py', 'regimen.py', 'response_contracts.py',
             'json_boundary.py', 'rules.py', 'documented_pprs.py', 'clinical_faults.py', 'order_encoding.py',
             'prescription_variants.py', 'exact_product_route_reference_v1.json',
             'safe_context_adapter.py', 'evidence_bindings.py', 'run_batch.py',
             'batch_observation.py', 'native_observation.py', 'native_observation_analysis.py',
             'campaign.py', 'workflow_analysis.py', 'network_route.py', 'trace_evidence.py',
             'linked_acceptance.py')
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


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
        reference.update(path=str(path), file_sha256=raw_hash, case_hash=digest(case),
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
    case = load_verified_reference(reference)
    return execute_case(case, **kwargs)


def planned_attempts(mode, reuse_anchor=False, matrix_task=None, model_families=None):
    if mode == 'linked-acceptance':
        return [(label, None) for label in linked_profiles(model_families)[0]]
    return ([] if reuse_anchor else [(BASELINE_LABEL, None)]) + ([(c.config_id.replace(':', '__'), c)
            for c in paper_conditions(matrix_task,model_families)] if mode == 'matrix' else [])


def prescription_continuation_state(anchor, stage_input):
    """Recover used budgets at the original consultation, never after its repair."""
    if anchor.get('stage_inputs', {}).get('prescription') != stage_input:
        raise ValueError('continuation input differs from the frozen source prescription stage')
    context = stage_input['clinical_context']
    terminal_response = anchor.get('artifacts', {}).get('consultation')
    expected_consultation = deepcopy(terminal_response)
    inherited_services = context.get('consultation', {}).get('in_hospital_services')
    if inherited_services:
        if (anchor.get('artifacts', {}).get('in_hospital_services') or [])[:len(inherited_services)] != inherited_services:
            raise ValueError('frozen consultation contains unobserved prior reconciliation services')
        expected_consultation['in_hospital_services'] = deepcopy(inherited_services)
    if context['consultation'] != expected_consultation:
        raise ValueError('frozen consultation does not match the actual physician response and service prefix')
    terminal_hash = digest(terminal_response)
    boundaries = [i for i,e in enumerate(anchor.get('events', []))
                  if e.get('event') == 'consultation_completed' and e.get('content_hash') == terminal_hash]
    if len(boundaries) != 1:
        raise ValueError('source lacks one observed original consultation boundary')
    counts = {}
    inherited_service_counts = {}
    for event in anchor['events'][:boundaries[0]+1]:
        if event.get('event') == 'history_query_requested':
            phase, number = event.get('phase'), event.get('lookup_number')
            service_phase = (isinstance(phase,str) and phase.startswith('in_hospital_reconciliation_')
                and phase[len('in_hospital_reconciliation_'):].isdigit())
            target = inherited_service_counts if service_phase else counts
            if (not service_phase and phase not in {'consultation', 'doctor_handoff_verification'}) or number != target.get(phase,0)+1:
                raise ValueError('source history query budget evidence is not contiguous')
            target[phase] = number
    return {'version': 'observed_original_consultation_budget_v1',
        'stage_input_hash': digest(stage_input), 'source_run_id': anchor['run_id'],
        'source_consultation_event_hash': digest(anchor['events'][boundaries[0]]),
        'consultation_interactions_used': sum(s.get('phase') == 'consultation' and
            s.get('speaker_role') == 'doctor' for s in context['conversation']),
        'history_query_counts': counts, 'new_budget_allocated': False,
        'inherited_reconciliation_history_query_counts':inherited_service_counts,
        'inherited_services_scope':'Already executed source-stage work; retained as frozen context, not replayed or counted as new branch queries'}


def prepare_anchor_reuse(source_batch, references, config, *, mode, engineering_only, allow_clinical_dispositions=False):
    """Pin actual stage sources; partial clinical encounters require explicit opt-in."""
    if mode != 'matrix' or engineering_only is not True:
        raise ValueError('--anchor-batch requires --mode matrix and --engineering-only')
    source = Path(source_batch).resolve()
    if not (source/'batch_summary.json').is_file():
        raise ValueError('anchor source batch has not finished')
    with (source/'batch.lock').open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        spec_path = source/'specification.json'
        spec_hash = file_sha256(spec_path)
        spec = _read(spec_path)
        models = {k: config[k] for k in ('role_model', 'tool_models', 'endpoint')}
        if spec.get('models') != models:
            raise ValueError('anchor source/current model configuration differs')
        source_cases = {x['case_id']: x for x in spec['cases']}
        if len(source_cases) != len(spec['cases']):
            raise ValueError('duplicate source case identity')
        if allow_clinical_dispositions and {r['case_id'] for r in references} != set(source_cases):
            raise ValueError('clinical-disposition reuse must retain every planned source case')
        entries = []
        for reference in references:
            case_id = reference['case_id']
            if source_cases.get(case_id, {}).get('runtime_input_hash') != reference['runtime_input_hash']:
                raise ValueError('anchor source/current runtime case hash differs')
            index_path = source/case_id/'attempts.json'
            index_hash = file_sha256(index_path)
            index = _read(index_path)
            record = index.get('attempts', {}).get('linked_baseline_anchor', {})
            partial = allow_clinical_dispositions and record.get('status') == 'incomplete_clinical_decision'
            if index.get('case_id') != case_id or (not partial and (record.get('status') != 'completed' or record.get('native_complete') is not True)):
                raise ValueError('source anchor is not indexed complete with native readback')
            path = Path(record['result_path']).resolve()
            if source not in path.parents or path.name != 'run.json':
                raise ValueError('source anchor path escapes source batch')
            raw_hash = file_sha256(path)
            run = _read(path)
            if run.get('case_id') != case_id or run.get('input_hash') != reference['runtime_input_hash'] or run.get('status') != record.get('status') or (not partial and run.get('native_complete') is not True) or run.get('evaluation_unit') != 'complete_encounter':
                raise ValueError('source anchor identity/status/input does not qualify')
            stages = run.get('stage_inputs', {})
            if not partial and any(not isinstance(stages.get(task), dict) or not stages[task] for task in ('prescription', 'documentation')):
                raise ValueError('source anchor lacks complete frozen task inputs')
            validate_anchor_evidence(run, models, allow_clinical_disposition=partial)
            entries.append({'case_id': case_id, 'runtime_input_hash': reference['runtime_input_hash'],
                'source_run_id': run['run_id'], 'source_run_path': str(path), 'source_run_file_sha256': raw_hash,
                'source_run_canonical_hash': digest(run), 'stage_input_hashes': {k: digest(v) for k,v in stages.items()},
                'source_index_path': str(index_path), 'source_index_sha256': index_hash,
                'source_status': run['status'], 'source_native_complete': run.get('native_complete'),
                'missing_task_inputs': [task for task in ('prescription', 'documentation') if task not in stages]})
            if file_sha256(path) != raw_hash or file_sha256(index_path) != index_hash:
                raise ValueError('source anchor changed during preflight')
            del run, stages
        if file_sha256(spec_path) != spec_hash:
            raise ValueError('source specification changed during preflight')
    return {'policy': 'engineering_frozen_actual_stage_sources_v2' if allow_clinical_dispositions else 'engineering_frozen_completed_anchor_v1',
        'engineering_only': True, 'allow_clinical_dispositions': allow_clinical_dispositions,
        'source_batch_path': str(source), 'source_generation_version': spec['batch'],
        'source_specification_sha256': spec_hash, 'source_models': models,
        'source_generation_policy': spec.get('generation_policy'), 'cases': entries,
        'new_anchor_execution': False, 'source_anchors_counted_as_new_runs': False,
        'paired_task_conditions_per_case': len(paper_conditions(model_families=config['tool_models'])),
        'clinical_quality_of_source': 'not_inferred_from_business_completion'}


def validate_anchor_evidence(run, models, *, allow_clinical_disposition=False):
    """Check observed model routes and artifact equality, not clinical correctness."""
    expected_conditions = {task: condition.to_dict() for task, condition in _baseline_conditions().items()}
    if run.get('conditions') != expected_conditions:
        raise ValueError('source anchor conditions are not the declared three baselines')
    if not run.get('calls'):
        raise ValueError('source anchor lacks actual model evidence')
    clinical_roles = {'patient', 'nurse', 'doctor', 'pharmacist', 'senior_pharmacist'}
    for call in run['calls']:
        role, family = call.get('role'), call.get('model_family')
        if role in clinical_roles and family is None:
            expected_model = models['role_model']
        else:
            raise ValueError('baseline source anchor must contain clinical roles with no tool family only')
        if call.get('model_name') != expected_model:
            raise ValueError('source anchor actual model evidence differs or is missing')
        if digest(call.get('observation')) != call.get('observation_hash') or ('response' in call and digest(call['response']) != call.get('response_hash')):
            raise ValueError('source anchor model input/output evidence hash mismatch')
    artifacts, stages = run['artifacts'], run['stage_inputs']
    partial = allow_clinical_disposition and run.get('status') == 'incomplete_clinical_decision'
    if partial:
        validate_disposition_stage_sources(run)
        return
    rx, doc = stages['prescription'], stages['documentation']
    transcript = [row for row in artifacts.get('transcript', [])
                  if row.get('phase') in {'doctor_handoff_verification', 'consultation'}]
    # Stage inputs belong to the time they were observed. Later history lookups
    # and prescription followups must not replace an earlier frozen source.
    consultation_calls = [c for c in run['calls'] if c.get('observation', {}).get('action') == 'consult'
        and c.get('response') == artifacts.get('consultation')]
    if len(consultation_calls) != 1:
        raise ValueError('source anchor lacks one actual original consultation response')
    original_observation = consultation_calls[0]['observation']
    final_consultation = deepcopy(artifacts.get('consultation'))
    if artifacts.get('prescription_followups'):
        final_consultation['prescription_review_followups'] = deepcopy(artifacts['prescription_followups'])
    if artifacts.get('in_hospital_services'):
        final_consultation['in_hospital_services'] = deepcopy(artifacts['in_hospital_services'])
    expected_doc = {'transcript': transcript, 'handoff': artifacts.get('physician_verified_handoff'),
        'consultation': final_consultation,
        'current_encounter_measurements': artifacts.get('current_encounter_measurements'),
        'observed_business_state': {'prescription': artifacts.get('prescription_business_state')},
        'approved_prescription': artifacts.get('final_prescription')}
    expected_context = {key: deepcopy(original_observation.get(key)) for key in
        ('hospital_history', 'handoff', 'current_encounter_measurements', 'conversation')}
    expected_context['consultation'] = deepcopy(artifacts.get('consultation'))
    # Reconciliation completed before the first actual prescription belongs to
    # its frozen input. Later review-loop services must not leak backwards.
    initial_services = (original_observation.get('consultation') or {}).get('in_hospital_services')
    if initial_services:
        all_services = artifacts.get('in_hospital_services') or []
        if all_services[:len(initial_services)] != initial_services:
            raise ValueError('pre-prescription service evidence is not an actual chronological prefix')
        expected_context['consultation']['in_hospital_services'] = deepcopy(initial_services)
    context = rx.get('clinical_context') or {}
    extensions = {'documented_rule_context', 'documented_rule_evidence'}
    if (any(doc.get(key) != value for key,value in expected_doc.items())
        or rx.get('prescription') != artifacts.get('original_prescription')
        or rx.get('prescription') != artifacts['consultation'].get('prescription')
        or {k:v for k,v in context.items() if k not in extensions} != expected_context):
        raise ValueError('source anchor frozen stage/artifact consistency mismatch')
    if not any(c.get('observation', {}).get('action') == 'document'
        and c['observation'].get('is_final_prefix') is True
        and all(c['observation'].get(k) == v for k,v in doc.items()) for c in run['calls']):
        raise ValueError('source anchor documentation differs from actual generation input')
    if set(context) & extensions:
        metadata = context.get('documented_rule_evidence') or {}
        index = artifacts.get('rule_evidence_indexes', {}).get(metadata.get('evidence_index_hash')) or {}
        if (not extensions <= set(context)
            or digest({k:v for k,v in index.items() if k != 'index_hash'}) != index.get('index_hash')
            or metadata.get('visible_snapshot_hash') != index.get('snapshot_hash')
            or index.get('snapshot_hash') != digest({'clinical_context': expected_context, 'formulary': original_observation.get('formulary')})
            or metadata.get('structured_context_hash') != digest(context['documented_rule_context'])
            or not any(e.get('event') == 'rule_context_prepared'
                and e.get('evidence_index_hash') == index.get('index_hash')
                and e.get('structured_context_hash') == metadata['structured_context_hash'] for e in run.get('events', []))):
            raise ValueError('source anchor shared rule evidence is not bound to its actual visible input')
    state = artifacts.get('prescription_business_state') or {}
    if state.get('status') != 'signed' or state.get('readback_verified') is not True or state.get('items') != artifacts.get('final_prescription', {}).get('items'):
        raise ValueError('source anchor lacks consistent signed prescription evidence')


def validate_disposition_stage_sources(run):
    """Evidence equality only: an available task input is not encounter success."""
    a, stages = run['artifacts'], run['stage_inputs']
    if run.get('native_complete') is not False or run.get('three_task_eligible') is not False:
        raise ValueError('clinical disposition cannot claim completed encounter')
    disposition = a.get('clinical_disposition')
    if not isinstance(disposition, dict) or disposition.get('status') != 'incomplete_clinical_decision':
        raise ValueError('missing actual clinical disposition')
    consultation = a.get('consultation')
    consultation_calls=[c for c in run['calls'] if c.get('response') == consultation and
            c.get('observation', {}).get('action') == 'consult']
    if not isinstance(consultation, dict) or len(consultation_calls)!=1:
        raise ValueError('consultation does not match actual model response')
    transcript = [r for r in a.get('transcript', []) if r.get('phase') in {'doctor_handoff_verification', 'consultation'}]
    context = {'hospital_history': a.get('visible_history'), 'handoff': a.get('physician_verified_handoff'),
        'current_encounter_measurements': a.get('current_encounter_measurements'),
        'consultation': consultation, 'conversation': transcript}
    if 'prescription' in stages:
        rx = stages['prescription']
        if not a.get('original_prescription', {}).get('items') or rx.get('prescription') != a['original_prescription'] or rx['prescription'] != consultation.get('prescription'):
            raise ValueError('prescription source is not the actual original physician candidate')
        initial_context={key:deepcopy(consultation_calls[0]['observation'].get(key)) for key in
            ('hospital_history','handoff','current_encounter_measurements','conversation')}
        initial_context['consultation']=consultation
        if any(rx.get('clinical_context', {}).get(k) != v for k,v in initial_context.items()):
            raise ValueError('prescription context changed from visible source evidence')
        if not any(c.get('observation', {}).get('action') == 'review_prescription' and
                c['observation'].get('prescription') == rx['prescription'] and
                c['observation'].get('clinical_context') == rx['clinical_context'] for c in run['calls']):
            raise ValueError('prescription source lacks an exact actual reviewer input')
    elif a.get('original_prescription') is not None:
        raise ValueError('actual prescription was omitted from stage availability')
    if 'documentation' in stages:
        doc = stages['documentation']
        final_consultation=deepcopy(consultation)
        if a.get('prescription_followups'):final_consultation['prescription_review_followups']=deepcopy(a['prescription_followups'])
        if a.get('in_hospital_services'):final_consultation['in_hospital_services']=deepcopy(a['in_hospital_services'])
        expected = {'transcript': transcript, 'handoff': context['handoff'], 'consultation': final_consultation,
            'hospital_history': context['hospital_history'], 'current_encounter_measurements': context['current_encounter_measurements'],
            'observed_business_state': {'prescription': a.get('prescription_business_state')},
            'approved_prescription': {'items': []}, 'clinical_disposition': disposition}
        if doc != expected or not any(c.get('observation', {}).get('action') == 'document' and
                all(c['observation'].get(k) == v for k,v in doc.items()) for c in run['calls']):
            raise ValueError('decision documentation source differs from actual generation input')
    if set(stages) - {'prescription', 'documentation'}:
        raise ValueError('undeclared frozen stage input')


def verify_anchor_source_files(reuse, entry):
    checks = [(Path(reuse['source_batch_path'])/'specification.json', reuse['source_specification_sha256']),
              (entry['source_run_path'], entry['source_run_file_sha256']),
              (entry['source_index_path'], entry['source_index_sha256'])]
    if any(file_sha256(path) != expected for path, expected in checks):
        raise ValueError('frozen anchor source file changed; refusing reuse/resume')


def load_anchor_for_case(case, reuse, config):
    entries = [x for x in reuse['cases'] if x['case_id'] == case['case_id']]
    if len(entries) != 1 or reuse.get('engineering_only') is not True:
        raise ValueError('missing/ambiguous engineering anchor reference')
    entry = entries[0]
    if entry['runtime_input_hash'] != digest(runtime_case(case)):
        raise ValueError('current case differs from frozen source input')
    if reuse['source_models'] != {k: config[k] for k in ('role_model', 'tool_models', 'endpoint')}:
        raise ValueError('frozen anchor model configuration differs')
    verify_anchor_source_files(reuse, entry)
    run = _read(entry['source_run_path'])
    if digest(run) != entry['source_run_canonical_hash'] or {k: digest(v) for k,v in run['stage_inputs'].items()} != entry['stage_input_hashes']:
        raise ValueError('frozen anchor stage contents changed')
    partial = reuse.get('allow_clinical_dispositions') is True and entry.get('source_status') == 'incomplete_clinical_decision'
    if run['case_id'] != case['case_id'] or run['run_id'] != entry['source_run_id'] or run.get('status') != entry.get('source_status', 'completed') or (not partial and run.get('native_complete') is not True):
        raise ValueError('frozen anchor identity/status changed')
    validate_anchor_evidence(run, reuse['source_models'], allow_clinical_disposition=partial)
    verify_anchor_source_files(reuse, entry)
    return run, entry


def _expected_run(case, run_id, condition=None, stage_input=None, controlled_variant=None,
                  *, linked_conditions=None):
    if condition is not None and linked_conditions is not None:
        raise ValueError('fixed-input task and complete-encounter conditions are mutually exclusive')
    linked_conditions = linked_conditions or _baseline_conditions()
    conditions = {condition.task: condition.to_dict()} if condition else {
        task: value.to_dict() for task, value in linked_conditions.items()}
    config_id = condition.config_id if condition else linked_config_id(linked_conditions)
    identity = {'schema_version': 'twinhospital.linked.run/1', 'case_id': case['case_id'],
                'run_id': run_id, 'config_id': config_id, 'input_hash': digest(runtime_case(case)),
                'conditions': conditions,
                'evaluation_unit': 'fixed_input_task' if condition else 'complete_encounter'}
    if condition:
        identity['stage_input_hash'] = digest(stage_input or {})
    if controlled_variant is not None:identity['controlled_variant_hash']=digest(controlled_variant)
    return identity


def validate_saved_result(result, expected, stage_input=None):
    for field, value in expected.items():
        if result.get(field) != value:
            raise ValueError('saved run identity/input mismatch: ' + field)
    if expected['evaluation_unit'] == 'fixed_input_task':
        task = next(iter(expected['conditions']))
        if digest(result.get('stage_inputs', {}).get(task)) != expected['stage_input_hash']:
            raise ValueError('saved run frozen input content does not match its hash')
    if result.get('status') not in {'completed', 'failed', 'stopped_budget', 'stopped_provider', 'incomplete_clinical_decision'}:
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


def make_rule_engine(args, formulary):
    if getattr(args, 'rule_engine', 'engineering') == 'engineering':
        return EngineeringPPRS(formulary)
    if args.rule_engine != 'documented-candidate':
        raise ValueError('unsupported declared rule engine')
    from .safe_context_adapter import VisibleDocumentedCandidatePPRS
    source = getattr(args, 'clinical_catalogue_data', None)
    if not isinstance(source, dict) or not isinstance(source.get('products'), list):
        raise ValueError('documented candidate rules need a frozen source product catalogue')
    return VisibleDocumentedCandidatePPRS(formulary, source_catalogue=source['products'])


def execute_case(case, *, args, config, mapping, ledger, batch_root, stop_event=None,
                 acceptance_profile=None, acceptance_attempt=1, isolated_acceptance_index=False):
    stop_event = stop_event or Event()
    variant_kind=getattr(args,'controlled_prescription_variant',None)
    if variant_kind is not None and (variant_kind!='exact-product-route' or args.mode!='matrix'
            or getattr(args,'matrix_task',None)!='prescription'
            or getattr(args,'engineering_only',False) is not True
            or getattr(args,'close_prescription_loop',False) is not True):
        raise ValueError('controlled variants require the explicit engineering prescription continuation protocol')
    reuse = getattr(args, 'anchor_reuse', None)
    source_anchor, source_entry = None, None
    if reuse:
        if args.mode != 'matrix' or getattr(args, 'engineering_only', False) is not True:
            raise ValueError('anchor reuse is restricted to explicit engineering matrix execution')
        source_anchor, source_entry = load_anchor_for_case(case, reuse, config)
    elif getattr(args, 'anchor_batch', None):
        raise ValueError('anchor reuse was not frozen by preflight')
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
    if reuse:
        if 'linked_baseline_anchor' in index['attempts']:
            raise ValueError('reused-source batch must not contain a new anchor run')
        provenance = {'source_generation_version': reuse['source_generation_version'],
                      'source_specification_sha256': reuse['source_specification_sha256'], **source_entry}
        if index.get('anchor_source', provenance) != provenance:
            raise ValueError('indexed anchor provenance changed')
        index['anchor_source'] = provenance

    def execute(label, condition=None, stage_input=None, controlled_variant=None, *, linked_conditions=None):
        if reuse:
            verify_anchor_source_files(reuse, source_entry)
        run_id = args.batch + ':' + case['case_id'] + ':' + label
        expected = _expected_run(case, run_id, condition, stage_input, controlled_variant,
                                 linked_conditions=linked_conditions)
        if label in index['attempts']:
            record = index['attempts'][label]
            if not record.get('result_path'):
                return None
            result = _read(record['result_path'])
            validate_saved_result(result, expected, stage_input)
            if result['status'] in {'stopped_budget','stopped_provider'}:
                stop_event.set()
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
            validate_saved_result(result, expected, stage_input)
            result_path = existing[0]
        else:
            admission = admission_status(getattr(args, 'workflow_policy_data', None))
            if admission != 'open':
                save_attempt(label, unadmitted_record(expected['config_id'], admission))
                return None
            if _batch_stopped(ledger, stop_event, batch_root):
                save_attempt(label, unstarted_stop_record(ledger, expected['config_id'], batch_root))
                return None
            attempt_root.mkdir(mode=0o700)
            factory = BudgetedCamelFactory(config, ledger, args.model_evidence,
                case_id=case['case_id'], data_kind=case['data_kind'], run_id=run_id,
                config_id=expected['config_id'] if condition or linked_conditions is not None else BASELINE_LABEL)
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
            with interface as his:
                runner = LinkedEncounterRunner(factory, his, make_rule_engine(args, case['formulary']),
                    output_dir=attempt_root/'runs', close_prescription_loop=getattr(args,'close_prescription_loop',False),
                    workflow_policy=getattr(args, 'workflow_policy_data', None))
                if condition:
                    continuation=None
                    if condition.task == 'prescription' and getattr(args,'close_prescription_loop',False):
                        if controlled_variant is None:continuation=prescription_continuation_state(anchor,stage_input)
                        else:
                            from .prescription_variants import continuation_state as variant_continuation_state
                            continuation=variant_continuation_state(anchor,controlled_variant,case['formulary'])
                    result = runner.run_task(case, condition, run_id=run_id, stage_input=stage_input,
                        **({'continuation_state': continuation} if continuation is not None else {}),
                        **({'controlled_variant':controlled_variant} if controlled_variant is not None else {}))
                else:
                    result = runner.run_case(case, run_id=run_id, **(linked_conditions or _baseline_conditions()))
            validate_saved_result(result, expected, stage_input)
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
        profiles, _ = linked_profiles(config['tool_models'], factorial=getattr(args, 'acceptance_factorial', False))
        if acceptance_profile not in profiles:
            raise ValueError('linked acceptance dispatch must select one frozen profile')
        execute(attempt_label(acceptance_profile, acceptance_attempt), linked_conditions=profiles[acceptance_profile])
        return index

    anchor = source_anchor if reuse else execute(BASELINE_LABEL)
    if args.mode == 'matrix':
        for condition in paper_conditions(getattr(args,'matrix_task',None),config.get('tool_models')):
            label = condition.config_id.replace(':', '__')
            stage_input = None
            if _batch_stopped(ledger, stop_event, batch_root) and label not in index['attempts']:
                index['attempts'][label] = unstarted_stop_record(ledger, condition.config_id, batch_root)
                _atomic(index_path, index)
                continue
            if condition.task != 'preconsult':
                stage_input = (anchor or {}).get('stage_inputs', {}).get(condition.task)
                if stage_input is None:
                    if label not in index['attempts']:
                        index['attempts'][label] = {'status': 'not_run_missing_frozen_input',
                            'dependency': 'linked_baseline_anchor', 'config_id': condition.config_id}
                        _atomic(index_path, index)
                    continue
            controlled_variant=None
            if condition.task=='prescription' and getattr(args,'controlled_prescription_variant',None):
                from .prescription_variants import make_route_variant, VariantNotApplicable
                try:controlled_variant=make_route_variant(stage_input,case['formulary'])
                except VariantNotApplicable as exc:
                    if label not in index['attempts']:
                        index['attempts'][label]={'status':'not_run_variant_not_applicable',
                            'config_id':condition.config_id,'reason':str(exc),
                            'source_stage_input_hash':digest(stage_input),'patient_excluded':False,
                            'native_effects_started':False,'model_requests_sent':False}
                        _atomic(index_path,index)
                    continue
                stage_input=controlled_variant['stage_input']
            execute(label, condition, stage_input, controlled_variant)
    return index


def _acceptance_progression_checks(ref, record, profile, specification):
    """Use observed native readbacks before admitting another expensive run."""
    if record.get('status') != 'completed' or not record.get('result_path'):
        return {'runtime_reported_complete': False}
    from .workflow_analysis import audit_run
    path = Path(record['result_path'])
    run = _read(path)
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


def execute_acceptance_schedule(cases, acceptance, *, args, config, mapping, ledger,
                                batch_root, stop_event, specification, session_log, session_id):
    from .linked_acceptance import READER_DESIGN_VERSION
    profiles = {profile['label']: profile for profile in acceptance['profiles']}
    by_assignment = acceptance['schedule_policy'] == ASSIGNMENT_SCHEDULE_POLICY
    stop_on_failure = (acceptance['version'] != READER_DESIGN_VERSION
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
            record = {'status': 'not_run_verified_paid_prior_capture', 'config_id': profile['config_id'],
                      'native_effects_started': False, 'model_requests_sent': False,
                      'retained_in_planned_denominator': True}
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
    if acceptance.get('independent_crossed'):
        order = heldout_dispatch_order(cases, profiles)
        frozen = specification.get('execution_policy', {}).get('heldout_dispatch_sha256')
        if frozen is not None and digest(order) != frozen:
            raise ValueError('Held-out dispatch order differs from the frozen specification.')
        cases_by_id = {case['case_id']: case for case in cases}
        dispatch_cases = [cases_by_id[case_id] for case_id, _ in order]
        units = ((cases_by_id[case_id], label) for case_id, labels in order for label in labels)
    else:
        units = iter(ordered_units(cases, profiles, rotate=acceptance.get('factorial', False)))
    for _ in acceptance['first_baseline_regressions']:
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
    parser.add_argument('--rule-engine', choices=('engineering', 'documented-candidate'), default='engineering')
    parser.add_argument('--clinical-catalogue', type=Path,
        default=Path('deploy/openhis-experiment/catalogue_metadata_v2.json'),
        help='Frozen product source metadata for development candidate clinical rules')
    parser.add_argument('--his-credentials', type=Path)
    parser.add_argument('--his-url', default='http://127.0.0.1:28081/api/app')
    parser.add_argument('--his-interface', choices=('native', 'fhir-r4'), default='native',
                        help='Select the native path or the FHIR R4 workflow bridge for a fresh batch.')
    parser.add_argument('--mode', choices=('linked', 'matrix', 'linked-acceptance'), default='linked')
    parser.add_argument('--matrix-task', choices=('preconsult','prescription','documentation'),
        help='Execute all frozen configurations of this task; no selection by observed outcome')
    parser.add_argument('--close-prescription-loop', action='store_true',
        help='Retain original review endpoint, continue repair and actual disposition/note/payment')
    parser.add_argument('--controlled-prescription-variant',choices=('exact-product-route',),
        help='Explicit development-only order-entry intervention, same candidate in all prescription conditions')
    parser.add_argument('--anchor-batch', type=Path, help='Reuse frozen completed anchor inputs; engineering matrix only')
    parser.add_argument('--allow-clinical-disposition-anchors', action='store_true',
        help='Engineering only: retain all source cases, reuse actual available stage inputs and preserve missing cells')
    parser.add_argument('--engineering-only', action='store_true', help='Declare engineering execution outside the formal study')
    parser.add_argument('--formal-experiment', action='store_true',
        help='Enforce the approved 1000-patient, all-condition protocol; does not assert successful outcomes')
    parser.add_argument('--acceptance-batch', type=Path, help='Closed same-version engineering acceptance batch')
    parser.add_argument('--acceptance-audit', type=Path, help='Private native audit for the engineering acceptance batch')
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
    parser.add_argument('--heldout-operation-schedule', type=Path,
                        help='Preassigned operation tests bound to an independent crossed cohort.')
    parser.add_argument('--acceptance-attempts', type=int, choices=(1, MAX_ENGINEERING_ATTEMPTS),
                        help='Maximum independent attempts per engineering assignment')
    parser.add_argument('--acceptance-factorial', action='store_true',
                        help='Require the full crossing of preconsultation, prescription and documentation')
    parser.add_argument('--acceptance-recovery-plan', type=Path,
                        help='Skip already audited paid units in a new native capture of the same engineering design')
    parser.add_argument('--analysis-plan', type=Path, help='Prespecified analysis for the prepared formal cohort')
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
    args.workflow_policy_data = validate_window(_read(args.workflow_policy), workers=args.workers) if args.workflow_policy else None
    if args.formal_experiment and args.workflow_policy_data is None:
        parser.error('--formal-experiment requires a prospective --workflow-policy')
    if args.formal_experiment and not (args.acceptance_batch and args.acceptance_audit):
        parser.error('--formal-experiment requires --acceptance-batch and --acceptance-audit')
    if args.formal_experiment and args.analysis_plan is None:
        parser.error('--formal-experiment requires --analysis-plan')
    if ((args.acceptance_patients_per_disease != PATIENTS_PER_GROUP or args.acceptance_factorial)
            and args.mode != 'linked-acceptance' and not args.formal_experiment):
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
    if args.heldout_operation_schedule and not args.independent_crossed:
        parser.error('--heldout-operation-schedule applies only to independent crossed runs')
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
    if args.matrix_task and args.mode != 'matrix':
        parser.error('--matrix-task requires --mode matrix')
    if args.controlled_prescription_variant and not (args.mode=='matrix' and args.matrix_task=='prescription'
            and args.engineering_only and args.close_prescription_loop):
        parser.error('controlled variants require engineering prescription matrix with actual repair and settlement')
    if args.anchor_batch and (args.mode != 'matrix' or not args.engineering_only):
        parser.error('--anchor-batch requires --mode matrix --engineering-only')
    if args.allow_clinical_disposition_anchors and not args.anchor_batch:
        parser.error('--allow-clinical-disposition-anchors requires --anchor-batch')
    if args.anchor_batch and args.anchor_batch.resolve() == (args.output/args.batch).resolve():
        parser.error('source and output batches must differ')
    if args.capture_native != bool(args.observation_deploy):
        parser.error('--capture-native and --observation-deploy must be specified together')
    if args.mode == 'linked-acceptance' and (
            not (args.engineering_only and args.close_prescription_loop and args.capture_native)
            or args.formal_experiment or args.limit is not None):
        parser.error('linked acceptance requires the full engineering cohort, repair loop and native capture')
    from .batch_observation import deployment_metadata, observed_batch, capture_summary
    args.observation_metadata = deployment_metadata(args.observation_deploy,args.workflow_policy_data) if args.capture_native else None
    formal_inputs = bind_formal_inputs(args.manifest) if args.formal_experiment else None
    manifest_bytes = args.manifest.read_bytes()
    cases = select_cases(json.loads(manifest_bytes), args.limit)
    if args.independent_crossed:
        exclusion = args.exclude_development_manifest.read_bytes()
        development = json.loads(exclusion)['cases']
        if not development or any(row['split'] != 'development' or not row['patient_key'] for row in development):
            raise ValueError('A nonempty development-partition manifest is required.')
        if {row['patient_key'] for row in development} & {case['patient_key'] for case in cases}:
            raise ValueError('A source patient occurs in both partitions.')
    heldout_operations = None
    if args.heldout_operation_schedule:
        from .source_operation_replay import replay_code_hashes, validate_schedule

        schedule_bytes = args.heldout_operation_schedule.read_bytes()
        rows = validate_schedule(json.loads(schedule_bytes), {'cases': cases})
        heldout_operations = {'cohort_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                              'operation_schedule_sha256': hashlib.sha256(schedule_bytes).hexdigest(),
                              'source_profile': BASELINE_LABEL, 'planned_units': len(rows),
                              'code_sha256': replay_code_hashes()}
    if formal_inputs and bind_formal_inputs(args.manifest) != formal_inputs:
        raise ValueError('formal input files changed during case preflight')
    config, mapping = _read(args.provider_config), _read(args.catalog)
    paper_conditions(model_families=config['tool_models'])  # one fixed AI tool; legacy pairs remain readable
    validate_formal_design(args, cases, config, args.workflow_policy_data)
    acceptance = (freeze_design(cases, config['tool_models'],
                                patients_per_group=args.acceptance_patients_per_disease,
                                workers=args.workers, max_attempts=args.acceptance_attempts,
                                factorial=args.acceptance_factorial, independent=args.independent_crossed,
                                reader=args.reader_comparison, quick=args.reader_quick)
                  if args.mode == 'linked-acceptance' else None)
    if args.workers>5 and config.get('minimum_request_interval_seconds',0)<1.0:
        parser.error('more than five workers require explicit shared API pacing of at least one second')
    args.diagnosis_mapping = validate_diagnosis_mapping(_read(args.diagnosis_catalog))
    args.clinical_catalogue_data = _read(args.clinical_catalogue) if args.rule_engine == 'documented-candidate' else None
    rule_metadata = {'engine': 'engineering', 'version': EngineeringPPRS.rule_version,
                     'engineering_only': True, 'development_only': True}
    if args.rule_engine == 'documented-candidate':
        from .safe_context_adapter import POLICY as SHARED_RULE_CONTEXT_POLICY
        engine = make_rule_engine(args, [])
        rule_metadata = {'engine': args.rule_engine, 'version': engine.rule_version,
            'engineering_only': False, 'development_only': True,
            'independent_reference_status': 'pending',
            'registry_hash': engine.registry_hash,
            'product_mapping_manifest_hash': engine.product_mapping_manifest_hash,
            'source_catalogue_file_sha256': file_sha256(args.clinical_catalogue),
            'source_catalogue_hash': digest(args.clinical_catalogue_data['products']),
            'shared_context_policy': SHARED_RULE_CONTEXT_POLICY}
    generation_policy = validate_generation_policy(config)
    policy_provenance = [
        check_data_policy(kind, config.get('data_policy', {}), config['endpoint'].rstrip('/'),
                          formal=args.formal_experiment or args.independent_crossed)
        for kind in sorted({case['data_kind'] for case in cases})
    ]
    args.anchor_reuse = prepare_anchor_reuse(args.anchor_batch, cases, config,
        mode=args.mode, engineering_only=args.engineering_only,
        allow_clinical_dispositions=args.allow_clinical_disposition_anchors) if args.anchor_batch else None
    if args.anchor_reuse:
        args.anchor_reuse['executed_task_conditions_per_case'] = len(paper_conditions(args.matrix_task,config['tool_models']))
        args.anchor_reuse['executed_matrix_task'] = args.matrix_task
    variant_policy=None
    if args.controlled_prescription_variant:
        from .prescription_variants import POLICY as VARIANT_POLICY, registry as variant_registry
        variant_policy={**VARIANT_POLICY,'kind':args.controlled_prescription_variant,
            'registry_hash':digest(variant_registry()),'item_index':0,'new_route':'intravenous'}
    specification = {'batch': args.batch, 'mode': args.mode,
        'matrix_task': args.matrix_task,
        'controlled_prescription_variant_policy':variant_policy,
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
            'minimum_request_interval_seconds':config.get('minimum_request_interval_seconds',0),
            'tls_verification':True, 'follow_redirects':False},
        'budget_limit_mode': 'uncapped_user_authorized' if args.uncapped_budget else 'capped_1000_cny',
        'pricing_cny_per_million_tokens': {k: list(v) for k, v in configured_prices(config).items()},
        'memory_policy': MEMORY_POLICY,
        'generation_policy': generation_policy,
        'response_contract_policy': RESPONSE_CONTRACT_POLICY,
        'diagnosis_code_policy': DIAGNOSIS_CODE_POLICY,
        'native_observation': args.observation_metadata,
        'anchor_reuse': args.anchor_reuse,
        'workflow_limits': DEFAULT_LIMITS, 'modeled_activity_seconds': DEFAULT_TIMING,
        'consultation_dialogue_policy': CONSULTATION_DIALOGUE_POLICY,
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
            'staff_review_and_arbitration': 'simulated_role_actions',
            'complete_encounter': 'all_required_tasks_signed_order_attested_note_verified_medication_payment',
            'unrestricted_clinical_appropriateness': 'not_assessed',
            'literature_comparison': 'after_observed_results_not_case_labels',
        },
        'formal_experiment': args.formal_experiment,
        'conditions': [item.to_dict() for item in paper_conditions(args.matrix_task,config['tool_models'])] if args.mode == 'matrix' else []}
    if args.his_interface == 'fhir-r4':
        from . import fhir_his

        specification['his_interface'] = {
            'version': fhir_his.VERSION, 'fhir_version': '4.0.1',
            'operation_definitions_sha256': digest(fhir_his.operation_definitions()),
            'bridge_sha256': file_sha256(fhir_his.__file__),
        }
    if acceptance is not None:
        specification['linked_acceptance'] = acceptance
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
    if heldout_operations is not None:
        specification['heldout_operations'] = heldout_operations
    if args.acceptance_recovery_plan:
        if file_sha256(args.manifest) != _read(args.acceptance_recovery_plan)['frozen_manifest_sha256']:
            raise ValueError('Recovery requires the exact frozen case manifest')
        args.prior_verified_paid, specification['engineering_recovery'] = bind_verified_acceptance_recovery(
            args.acceptance_recovery_plan, specification)
    if formal_inputs is not None:
        specification['engineering_acceptance_patients_per_disease'] = args.acceptance_patients_per_disease
        specification['engineering_acceptance_factorial'] = args.acceptance_factorial
        specification['formal_inputs'] = {**formal_inputs, 'analysis_plan_sha256':
            bind_formal_analysis(args.analysis_plan, formal_inputs, specification)}
        specification['engineering_acceptance_proof'] = verify_engineering_acceptance(
            args.acceptance_batch, args.acceptance_audit, specification, cases)
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
                for label, condition in planned_attempts(args.mode, reuse_anchor=bool(args.anchor_reuse), matrix_task=args.matrix_task,model_families=config['tool_models']):
                    config_id = condition.config_id if condition else BASELINE_LABEL
                    record = (unadmitted_record(config_id, admission_status(args.workflow_policy_data))
                        if admission_status(args.workflow_policy_data) != 'open'
                        else unstarted_stop_record(ledger, config_id, batch_root))
                    index['attempts'].setdefault(label, record)
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
