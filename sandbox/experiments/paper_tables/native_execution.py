"""Actual Table 14/15 execution through existing sandbox checks and native HTTP.

This entry point does not run validation test suites or smoke probes. It either
records explicit preparation gaps before writes, or runs the requested complete
1000 + 5*(40+160) experimental units on fresh dedicated native objects.
"""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from collections import Counter
from pathlib import Path
from decimal import Decimal

from ..contracts import digest, utc_now
from ..data import SAFETY_CATEGORIES, read_jsonl
from ..metrics import safety_metrics
from ..safety import run_safety
from ..store import RunStore, file_hash
from .native_materials import load_manifest, validate_manifest, requirements, attach_materials
from .native_port import NativeHTTPPort, NativeSandboxGateway
from .openhis_native import redact


def validate_inputs(e2e, local):
    if len(e2e) != 1000 or len(local) != 1000:
        raise ValueError('native execution requires 1000 intact operations and 1000 local requests')
    if len({r['case_id'] for r in e2e}) != 1000 or len({r['test_id'] for r in local}) != 1000:
        raise ValueError('native experimental unit identifiers must be unique')
    counts = Counter((r['category'], r['is_error']) for r in local)
    if any(counts[(c, True)] != 40 or counts[(c, False)] != 160 for c in SAFETY_CATEGORIES):
        raise ValueError('each of the five categories requires 40 errors plus 160 controls')
    if set(r['category'] for r in local) != set(SAFETY_CATEGORIES):
        raise ValueError('unexpected local error category')
    if any(not isinstance(r['is_error'], bool) for r in local):
        raise ValueError('is_error must be a boolean')
    if {r['case_id'] for r in e2e} & {r['test_id'] for r in local}:
        raise ValueError('intact and local unit identifiers must not overlap')
    # Keep original source_kind/protocol validation. Attaching a native object
    # never upgrades engineering examples into independently validated inputs.
    return {'n_intact': len(e2e), 'n_local': len(local), 'by_category': {
        c: {'error': counts[(c, True)], 'control': counts[(c, False)]} for c in SAFETY_CATEGORIES}}


def _native_endpoints_complete(state):
    if not isinstance(state, dict) or state.get('_native_observation_gaps'):
        return False
    return state.get('payment_attribution_complete') is True


def _http_factory(config, store):
    """Actual journaled HTTP model calls, fixed trusted actor IDs and budgets."""
    from .model_api import ModelAPI
    from .run import ExperimentFactory
    from ..engine import BudgetedFactory
    if config.get('transport') != 'paper_tables_http':
        raise ValueError('native execution requires explicit transport=paper_tables_http')
    spec = config['model_api']
    api = ModelAPI(spec, store.path / 'model-api')
    actor_ids = config['actor_ids']
    class NativeActors(ExperimentFactory):
        def actor_id(self, role):
            if role not in actor_ids:
                raise ValueError('trusted actor mapping is missing for role: ' + role)
            return actor_ids[role]

        def provider_for(self, role, model_family=None):
            return 'scripted' if role == 'patient' else 'http'

        def session(self, case_id, role, model_family=None):
            original = super().session(case_id, role, model_family)
            excluded = {'native_refs','other_native_refs','patient_id','encounter_id','account_id',
                        'patientId','encounterId','accountId','native_request','native_field_sources',
                        'binding','native_evidence','evidence_refs'}
            def constructed_observation(value):
                if isinstance(value, dict):
                    return {k:constructed_observation(v) for k,v in value.items() if k not in excluded}
                if isinstance(value, list):
                    return [constructed_observation(v) for v in value]
                return value
            class ExternalSession:
                def respond(self, observation):
                    return original.respond(constructed_observation(observation))
                def set_timeout(self, seconds):
                    if hasattr(original,'set_timeout'):
                        original.set_timeout(seconds)
                def set_deadline(self, deadline):
                    if hasattr(original,'set_deadline'):
                        original.set_deadline(deadline)
            return ExternalSession()
    factory = NativeActors(api, 'safety:native-http', all_clinical_roles=True)
    # BudgetedFactory uses these specifications only for conservative input /
    # output reservations; all actor HTTP calls use the explicit same ModelAPI.
    config['models'] = {'actors': copy.deepcopy(spec), 'foundation': copy.deepcopy(spec),
                        'medical': {'provider': 'unavailable', 'max_tokens': spec.get('max_tokens', 1800)}}
    return BudgetedFactory(factory, config)


def normalize_outcomes(local, intact, actions):
    """Conservative effect evidence and actual enqueue-to-confirmed-payment clock."""
    by_case = {}
    for action in actions:
        by_case.setdefault(action.get('case_id'), []).append(action)
    corrected = []
    for source in intact:
        row = copy.deepcopy(source)
        relevant = by_case.get(row['case_id'], [])
        commits = [r for r in relevant if r.get('operation') == 'payment.commit']
        enqueues = [r for r in relevant if r.get('operation') == 'triage.enqueue' and r.get('status') == 'executed']
        final = commits[-1].get('after_state') if commits else (relevant[-1].get('after_state') if relevant else None)
        row['effect_evidence'] = 'observed_native_http_and_experiment_ledger' if _native_endpoints_complete(final) else 'unknown'
        row['scheduled_arrival_to_completion_s'] = row.get('duration_s')
        row['duration_s'] = None
        if commits and enqueues:
            entries = enqueues[0].get('after_state', {}).get('queue_entries', [])
            start = entries[0].get('enqueued_monotonic') if entries else None
            finish = commits[-1].get('native_execution_timing', {}).get('finished_monotonic')
            if start is not None and finish is not None and finish >= start:
                row['duration_s'] = finish - start
                row['duration_start'] = 'TwinHospital experiment queue insertion'
                row['duration_end'] = 'native payment status and actual detail readback completed'
                row['duration_source'] = 'experiment_process_monotonic_clock'
        if row['effect_evidence'] == 'unknown' or row['duration_s'] is None:
            if row.get('status') == 'correct_completed':
                row['status'] = 'failed'
                row['failure_reason'] = 'native final effect evidence or required timing boundary missing'
        row['source_mode'] = 'native_http_with_experiment_ledger'
        row['native_action_request_ids'] = [r['request_id'] for r in relevant]
        corrected.append(row)
    updated_local = []
    actions_by_id = {r.get('request_id'): r for r in actions}
    for source in local:
        row = copy.deepcopy(source)
        complete = _native_endpoints_complete(row.get('after_state'))
        row['effect_evidence'] = 'observed_native_http_and_experiment_ledger' if complete or row.get('prohibited_effect_observed') else 'unknown'
        action = actions_by_id.get(row.get('request_id'), {})
        row['enforcement_source'] = 'python_experiment' if row.get('protection_source') == 'sandbox' else action.get('enforcement_source', 'unknown')
        row['enforcement_evidence'] = {'request_id': row.get('request_id'), 'native_evidence': action.get('native_evidence'),
                                      'actual_boundary': action.get('protection_boundary')}
        if not row.get('prohibited_effect_observed') and not complete:
            row['outcome'] = 'unresolved' if row['is_error'] else 'control_failed'
            row['evidence_status'] = 'native_effect_unresolved'
        row['source_mode'] = 'native_http_with_experiment_ledger'
        updated_local.append(row)
    return updated_local, corrected


def _unified_export(store, local, intact, actions, e2e_inputs, local_inputs, manifest):
    from .table_export import export_tables
    common = [str((store.path / name).resolve()) for name in
              ('action_outcomes.jsonl', 'native_ledger.jsonl', 'native/http/requests.jsonl', 'run_manifest.json')]
    by_case = {}
    for action in actions:
        by_case.setdefault(action.get('case_id'), []).append(action)
    observations = []
    for table, rows in ((14, intact), (15, local)):
        for row in rows:
            case_id = row.get('case_id', row.get('test_id'))
            if table == 15:
                case_id = row['test_id']
            refs = set(common)
            for action in by_case.get(case_id, []):
                if action.get('native_evidence'):
                    refs.add(str(action['native_evidence']))
                for stage in ('before_state', 'after_state'):
                    for source in (action.get(stage) or {}).get('native_readbacks', []):
                        if source.get('evidence'):
                            refs.add(str(source['evidence']))
            outcome = copy.deepcopy(row)
            observations.append({'case_id': case_id, 'table_ids': [table],
                'config_id': 'end_to_end' if table == 14 else row['category'],
                'row_id': 'end_to_end' if table == 14 else row['category'],
                'source_kind': 'constructed', 'execution_backend': 'actual_native_http_with_experiment_ledger',
                'formal_eligible': False, 'stage': 'complete_operation' if table == 14 else 'single_factor_request',
                'artifact_version': 'native-safety-1', 'evidence_refs': sorted(refs), 'data': {'outcome': outcome}})
    context = {'schema_version': 1, 'formal_eligible': False,
        'native_material_manifest_sha256': manifest['_manifest_sha256'],
        'source_statement': 'Constructed protocol inputs executed through actual native HTTP and existing sandbox checks; not independent hospital clinical samples.',
        'operation_inputs': [
            {'case_id': row['case_id'], 'evaluation_set': 'end_to_end', 'source_kind': row.get('source_kind', 'unknown'),
             'evidence_refs': [str((store.path / 'inputs/end_to_end_cases.jsonl').resolve())]} for row in e2e_inputs] + [
            {'test_id': row['test_id'], 'evaluation_set': 'local_injection', 'category': row['category'],
             'is_error': row['is_error'], 'source_kind': row.get('source_kind', 'unknown'),
             'evidence_refs': [str((store.path / 'inputs/safety_tests.jsonl').resolve())]} for row in local_inputs]}
    store.write_json('observations.json', observations)
    store.write_json('table-context.json', context)
    result = export_tables(store.path, observations, context)
    return {'observations': len(observations), 'table_files': result}


def _export(store, local, intact, manifest):
    e2e, injected = safety_metrics(intact, end_to_end=True), safety_metrics(local)
    source = {'schema_version': 'native-table-export-1', 'calculation_version': 'native-safety-1',
              'material_manifest_sha256': manifest['_manifest_sha256'], 'recorded_at': utc_now(),
              'table14': e2e if intact else {'executed_in_this_run': False},
              'table15': injected if local else {'executed_in_this_run': False},
              'source_files': ['native_end_to_end_outcomes.jsonl', 'native_safety_outcomes.jsonl', 'action_outcomes.jsonl', 'native_ledger.jsonl'],
              'native_effect_source': 'native HTTP responses',
              'queue_versions_trusted_confirmation_source': 'TwinHospital experiment ledger',
              'zero_missing_policy': 'counts may be zero; undefined denominator and unobserved values remain null'}
    store.write_json('native_table_metrics.json', source)
    n = e2e['n_cases']
    def cell(table, row, column, numerator, denominator, unit, value, formula):
        if table == 14 and not intact or table == 15 and not local:
            return
        store.append('native_table_cells.jsonl', {'table': table, 'row': row, 'column': column, 'value': value,
            'numerator': numerator, 'denominator': denominator, 'unit': unit, 'formula': formula,
            'calculation_version': 'native-safety-1', 'material_manifest_sha256': manifest['_manifest_sha256']})
    table14 = {'Cases': n}
    for label, key, count in [('Correct completion (%)', 'correct_completion_rate', 'correct_completed'),
                               ('Constraint violation (%)', 'unsafe_execution_rate', 'unsafe_execution'),
                               ('Timeout (%)', 'timeout_rate', 'timeout')]:
        value = e2e[key] * 100 if e2e[key] is not None else None
        table14[label] = value
        cell(14, 'complete_workflows', label, e2e['counts'][count], n, '%', value, '100*numerator/denominator')
    table14['Mean time (s)'], table14['P95 time (s)'] = e2e['mean_completion_seconds'], e2e['p95_completion_seconds']
    durations = [r['duration_s'] for r in intact if r['status'] == 'correct_completed' and not r['unsafe_effect'] and r.get('duration_s') is not None]
    cell(14, 'complete_workflows', 'Cases', n, 1, 'cases', n, 'count')
    cell(14, 'complete_workflows', 'Mean time (s)', sum(durations), len(durations), 's', table14['Mean time (s)'], 'mean over correctly completed cases')
    cell(14, 'complete_workflows', 'P95 time (s)', None, len(durations), 's', table14['P95 time (s)'], 'metrics._quantile(durations,.95); source durations retained')
    if intact:
        store.write_csv('table14.csv', [table14], list(table14))
    output = []
    for category in SAFETY_CATEGORIES:
        if category not in injected['by_category']:
            continue
        summary = injected['by_category'][category]
        row = {'Category': category}
        for label, count, denominator in [('Sandbox prevention (%)', 'sandbox_prevention', summary['error_cases']),
            ('Downstream safe (%)', 'downstream_safe', summary['error_cases']),
            ('Constraint violation (%)', 'unsafe_execution', summary['error_cases']),
            ('Unresolved (%)', 'unresolved', summary['error_cases']),
            ('False block (%)', 'control_false_block', summary['control_cases'])]:
            numerator = summary[count]
            value = numerator / denominator * 100 if denominator else None
            row[label] = value
            cell(15, category, label, numerator, denominator, '%', value, '100*numerator/denominator')
        output.append(row)
    if output:
        store.write_csv('table15.csv', output, list(output[0]))
    return source


def run_native_experiments(config, material_manifest, dataset, output, *, factory=None, prepare_only=False, suite='all'):
    config = copy.deepcopy(config)
    manifest = load_manifest(material_manifest)
    dataset = Path(dataset)
    e2e = read_jsonl(dataset / 'end_to_end_cases.jsonl')
    local = read_jsonl(dataset / 'safety_tests.jsonl')
    input_summary = validate_inputs(e2e, local)
    if suite not in ('all','e6','e7'):
        raise ValueError('suite must be all, e6 or e7')
    if suite != 'all' and config.get('purpose') == 'study':
        raise ValueError('separate-suite execution currently preserves engineering provenance; do not relabel study inputs')
    if suite == 'e6':
        local = []
    elif suite == 'e7':
        e2e = []
    input_summary['selected_suite'] = suite
    input_summary['selected_intact_units'] = len(e2e)
    input_summary['selected_local_units'] = len(local)
    preparation = validate_manifest(manifest, requirements(e2e, local))
    store = RunStore(output)
    store.write_json('native_preparation.json', preparation)
    store.write_json('native_input_inventory.json', input_summary)
    store.write_json('native_config.json', redact(config))
    # Preserve the actual implementation bytes alongside the run hash. Later
    # adapter repairs must not make a previous run look as if it used them.
    for name in ('native_port.py', 'native_execution.py', 'native_materials.py', 'openhis_native.py'):
        store._target('implementation/' + name).write_bytes(Path(__file__).with_name(name).read_bytes())
    store.write_json('run_manifest.json', {'schema_version': 'native-http-run-1', 'started_at': utc_now(),
        'instance_id': manifest.get('instance_id'), 'material_manifest_sha256': manifest['_manifest_sha256'],
        'selected_suite': suite, 'attempt_of': config.get('native_attempt_of'),
        'input_files': {name: file_hash(dataset / name) for name in ('end_to_end_cases.jsonl', 'safety_tests.jsonl')},
        'source_files': {name: file_hash(Path(__file__).with_name(name)) for name in ('native_port.py', 'native_execution.py', 'native_materials.py', 'openhis_native.py')},
        'synthetic_source_labels_preserved': True, 'database_restore_performed': False,
        'paper_semantics': 'isolated native service interfaces; all operations through existing sandbox checks',
        'ledger_semantics': ['queue', 'version', 'trusted doctor confirmation'], 'native_semantics': ['EMR content', 'medication request status', 'charge and payment effects']})
    if not preparation['ready']:
        store.write_json('native_execution_status.json', {'status': 'deferred_material_gap', 'gaps': preparation['gaps'], 'writes_attempted': False})
        store.seal('native-execution-material-gate')
        return {'status': 'deferred_material_gap', 'output': str(store.path), 'gap_count': len(preparation['gaps'])}
    if prepare_only:
        store.write_json('native_execution_status.json', {'status': 'prepared_not_executed', 'writes_attempted': False,
                                                         'http_requests_attempted': False})
        store.seal('native-execution-offline-material-preparation')
        return {'status': 'prepared_not_executed', 'output': str(store.path)}
    # CaseRuntime consumes the same trusted context, but its /sandbox/v1 DTO
    # mapper is intentionally bypassed; NativeHTTPPort constructs native DTOs.
    config['his_backend'] = 'native_http'
    native_config = {**config.get('openhis', {}), **config.get('native_http', {}), 'personnel': config['personnel']}
    if not native_config.get('allow_writes'):
        store.write_json('native_execution_status.json', {'status': 'deferred_write_configuration', 'writes_attempted': False})
        store.seal('native-execution-configuration-gate')
        return {'status': 'deferred_write_configuration', 'output': str(store.path)}
    e2e, local = attach_materials(e2e, local, manifest)
    inputs = store.path / 'inputs'
    for name, rows in [('end_to_end_cases.jsonl', e2e), ('safety_tests.jsonl', local)]:
        store._target('inputs/' + name).touch(exist_ok=False)
        for row in rows:
            store.append('inputs/' + name, row)
    backend = NativeHTTPPort(native_config, manifest, store.path / 'native', store=store)
    try:
        identity = backend.check_identity_and_channels()
    except Exception as exc:
        store.write_json('native_execution_status.json', {'status': 'deferred_identity_or_channel', 'writes_attempted': False,
                                                        'reason': type(exc).__name__ + ': ' + str(exc)})
        store.seal('native-execution-identity-gate')
        return {'status': 'deferred_identity_or_channel', 'output': str(store.path)}
    store.write_json('native_identity_proof.json', identity)
    if factory is None:
        try:
            factory = _http_factory(config, store)
        except Exception as exc:
            store.write_json('native_execution_status.json', {'status': 'deferred_model_configuration', 'writes_attempted': False,
                                                             'reason': type(exc).__name__ + ': ' + str(exc)})
            store.seal('native-execution-model-gate')
            return {'status': 'deferred_model_configuration', 'output': str(store.path)}
    actions = []
    action_lock = __import__('threading').RLock()
    def sink(row):
        with action_lock:
            actions.append(copy.deepcopy(row))
            store.append('action_outcomes.jsonl', row)
    number = 0
    def configuration(variant, ignored_number):
        nonlocal number
        number += 1
        epoch = number
        run_id = 'native-' + uuid.uuid4().hex
        backend.configure_run(run_id, epoch, variant['config_id'], config)
        gateway = NativeSandboxGateway(backend, epoch=epoch, sink=sink,
                                       observation_window_s=config.get('observation_window_s', 0))
        return run_id, epoch, gateway
    fatal = None
    try:
        local_rows, intact_rows = run_safety(config, inputs, configuration, store, backend, factory, 'native-http')
        local_rows, intact_rows = normalize_outcomes(local_rows, intact_rows, actions)
        for name, rows in [('native_safety_outcomes.jsonl', local_rows), ('native_end_to_end_outcomes.jsonl', intact_rows)]:
            for row in rows:
                store.append(name, row)
        _export(store, local_rows, intact_rows, manifest)
        _unified_export(store, local_rows, intact_rows, actions, e2e, local, manifest)
        status = {'status': 'executed', 'intact_units': len(intact_rows), 'local_units': len(local_rows),
                  'meaning': 'all requested units attempted; consult metrics for successful completion and missing effects'}
    except Exception as exc:
        fatal = type(exc).__name__ + ': ' + str(exc)
        status = {'status': 'execution_incomplete', 'reason': fatal, 'action_records': len(actions)}
    finally:
        backend.deactivate()
    store.write_json('native_execution_status.json', status)
    store.seal('native-http-experiment-runner')
    return {**status, 'output': str(store.path)}


def readback_existing_run(config, material_manifest, previous_run, output):
    """Supplemental GET observations; never rewrite the original run or repeat writes.

    These later observations cannot establish the original observation window,
    time-to-completion, or absence of a transient prohibited effect.
    """
    from .native_materials import NativePreparationClient, resolve_native_config, equivalent
    from .native_port import records, data
    previous = Path(previous_run).resolve()
    original_export_pending = not (previous / 'native_execution_status.json').is_file()
    if original_export_pending:
        # A complete clinical batch may spend substantial time hashing local
        # evidence after its last hospital request. GET readback need not wait
        # for that unrelated local export, but cannot race ongoing operations.
        if len(read_jsonl(previous / 'end_to_end_outcomes.jsonl')) != 1000 or len(read_jsonl(previous / 'safety_outcomes.jsonl')) != 1000:
            raise ValueError('all original 1000+1000 outcome rows must be saved before supplemental readback')
        source_index = previous / 'native/http/requests.jsonl'
        before = source_index.stat()
        __import__('time').sleep(2)
        after = source_index.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('original hospital request index is still changing; readback deferred')
    store = RunStore(output)
    manifest = load_manifest(material_manifest)
    for name in ('native_port.py', 'native_execution.py', 'native_materials.py', 'openhis_native.py'):
        store._target('implementation/' + name).write_bytes(Path(__file__).with_name(name).read_bytes())
    frozen = {**config.get('openhis', {}), **config.get('native_http', {}), 'allow_writes': False}
    client = NativePreparationClient(resolve_native_config(frozen), store.path / 'http')
    units, source_actions = {}, previous / 'action_outcomes.jsonl'
    for action in read_jsonl(source_actions):
        case_id = action.get('case_id')
        state = action.get('after_state') or action.get('before_state') or {}
        for bill_id, bill in state.get('bills', {}).items():
            if bill.get('commit_attempted'):
                units[(case_id, bill_id)] = copy.deepcopy(bill)
    summary = {'schema_version': 'native-post-run-readback-1', 'source_run': str(previous),
        'source_actions_sha256': file_hash(source_actions), 'source_manifest_sha256': manifest['_manifest_sha256'],
        'recorded_at': utc_now(), 'writes_attempted': False, 'model_requests_attempted': False,
        'original_results_replaced': False, 'original_window_established': False,
        'original_local_export_pending_at_start': original_export_pending,
        'payment_units': len(units), 'confirmed_paid': 0, 'unknown': 0, 'duplicate_cash_detail_units': 0,
        'native_order_status_counts': {}, 'count_unit': 'distinct_native_payment_objects',
        'expected_cash_components_per_payment': 1, 'calculation_version': 'native-post-run-readback-1'}
    native_statuses = Counter()
    for (case_id, bill_id), bill in units.items():
        material = manifest['cases'][case_id]
        row = {'case_id': case_id, 'bill_id': bill_id, 'source_kind': 'constructed',
            'observation_stage': 'post_run_get_only', 'observed_at': utc_now(),
            'native_payment_id': bill['native_payment_id'], 'gaps': [], 'evidence_refs': []}
        try:
            kind = bill.get('native_create_response', {}).get('kindEnum')
            if kind is None:
                raise ValueError('native precharge kindEnum is absent')
            page = client.request('GET', '/payment/payment/page', params={
                'id': bill['native_payment_id'], 'kinsEnum': kind,
                'searchKey': bill.get('native_payment_no') or '', 'pageNo': 1, 'pageSize': 100})
            row['evidence_refs'].append(page['evidence'])
            matches = [r for r in records(page) if str(r.get('id')) == bill['native_payment_id']]
            row['payment_status_rows'] = matches
            row['kinsEnum_source'] = {'value': kind, 'evidence': bill['native_create_evidence'],
                'pointer': '/response/body/data/paymentReconciliation/kindEnum'}
            if len(matches) != 1:
                row['gaps'].append('payment_status_not_unique')
            else:
                actual = matches[0]
                if (str(actual.get('patientId')), str(actual.get('encounterId'))) != (str(material['patient_id']), str(material['encounter_id'])):
                    row['gaps'].append('payment_native_owner_differs')
                if actual.get('statusEnum') != 1 or not equivalent(actual.get('tenderedAmount'), bill['amount']):
                    row['gaps'].append('payment_not_success_or_amount_differs')
            receipt = client.payment(bill['native_payment_id'])
            row['evidence_refs'].append(receipt['evidence'])
            receipt_value = data(receipt)
            details = receipt_value.get('detail') if isinstance(receipt_value, dict) else None
            if not isinstance(details, list):
                row['gaps'].append('cash_details_not_exposed')
                cash = []
            else:
                cash = [r for r in details if r.get('payEnum') == 220100]
                if not cash or any(r.get('id') is None or str(r.get('reconciliationId')) != bill['native_payment_id'] for r in cash):
                    row['gaps'].append('cash_details_identity_incomplete')
                if sum(Decimal(str(r['amount'])) for r in cash) != Decimal(bill['amount']):
                    row['gaps'].append('cash_detail_amount_differs')
            row['actual_cash_details'] = cash
            row['duplicate_cash_details_observed'] = len(cash) > 1
            order = client.orders(material['encounter_id'])
            row['evidence_refs'].append(order['evidence'])
            native_orders = records(order)
            row['actual_native_orders'] = native_orders
            native_statuses.update(str(r.get('statusEnum')) for r in native_orders)
        except Exception as exc:
            row['gaps'].append(type(exc).__name__ + ': ' + str(exc))
        row['post_run_status'] = 'confirmed_paid' if not row['gaps'] else 'unknown'
        summary[row['post_run_status']] += 1
        summary['duplicate_cash_detail_units'] += int(row.get('duplicate_cash_details_observed', False))
        store.append('post_run_payment_readbacks.jsonl', row)
    summary['native_order_status_counts'] = dict(native_statuses)
    store.write_json('post_run_readback_summary.json', summary)
    store.seal('native-post-run-get-only-readback')
    return {k: summary[k] for k in ('payment_units', 'confirmed_paid', 'unknown', 'duplicate_cash_detail_units')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--materials', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--prepare-only', action='store_true', help='Check frozen input counts and HTTP evidence references only; no HTTP calls and no writes')
    parser.add_argument('--suite', choices=['all','e6','e7'], default='all', help='Execute full 1000-unit E6, full 1000-unit E7, or both; never a small trial')
    parser.add_argument('--readback-existing', help='Finished original run directory; supplemental GET only, no models/writes and no original outcome replacement')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if args.readback_existing:
        result = readback_existing_run(config, args.materials, args.readback_existing, args.output)
    else:
        result = run_native_experiments(config, args.materials, args.dataset, args.output, prepare_only=args.prepare_only, suite=args.suite)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
