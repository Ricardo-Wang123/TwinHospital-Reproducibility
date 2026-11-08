"""Offline stage/workflow audit, optionally supplemented by isolated read-only HIS.

Inputs are finished batches. Public output has research IDs/hashes/checks only;
private crosswalk and raw fresh observations are written separately with 0600.
No model calls, clinical mutation, payment action, or source-file rewriting.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox.experiments.linked.workflow_analysis import (VERSION, audit_run, digest,
    missing_run, summarize, WINDOW_POLICY_TEMPLATE, READBACK_REQUIREMENTS)
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.contracts import linked_config_id, paper_conditions
from sandbox.experiments.linked.linked_acceptance import (
    MAX_ENGINEERING_ATTEMPTS, READER_DESIGN_VERSION, SUPPORTED_PATIENTS_PER_GROUP, attempt_label,
    documented_unpaid_stop, execution_checks, freeze_design, linked_profiles, summarize_gate,
)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def inside(path, root):
    path = Path(path).resolve()
    if root != path and root not in path.parents:
        raise ValueError('Path outside selected source batch')
    return path


class ReadOnlyObserver:
    """Per-endpoint errors are captured, never used to remove a case denominator."""
    def __init__(self, credentials_path=None, deployment_dir=None):
        self.deployment_dir=Path(deployment_dir or ROOT/'deploy/openhis-experiment').resolve()
        self.credentials_path = credentials_path or self.deployment_dir/'runtime/accounts/credentials.json'
        self.clients, self.login_errors = {}, {}

    def client(self, role):
        if role in self.login_errors:
            return None
        if role not in self.clients:
            from sandbox.experiments.linked.native_his import OpenHISClient, ROLE_SETTINGS, isolated_port
            try:
                p = Path(self.credentials_path)
                if p.stat().st_mode & 0o077:
                    raise PermissionError('Private credential mode required')
                credentials = read(p)
                name, org, _ = ROLE_SETTINGS[role]
                c = OpenHISClient(f'http://127.0.0.1:{isolated_port()}/api/app', timeout=20)
                c.login(name, credentials[name], organization=org)
                credentials.clear()
                self.clients[role] = c
            except Exception as exc:
                self.login_errors[role] = type(exc).__name__
                return None
        return self.clients[role]

    def observe(self, binding):
        out = {'observed_at': datetime.now(timezone.utc).isoformat(),
               'scope': 'post_run_current_state_not_original_observation_window', 'errors': {}}
        pid, eid = binding.get('patient_id'), binding.get('encounter_id')
        if not pid or not eid:
            out['errors']['binding'] = 'MissingScopedBinding'
            return out
        def call(name, role, endpoint, payload):
            c = self.client(role)
            if c is None:
                out['errors'][name] = self.login_errors.get(role, 'UnavailableObserver')
                return
            try:
                out[name] = c.post(endpoint, payload)
            except Exception as exc:
                out['errors'][name] = type(exc).__name__
        call('advice', 'doctor', '/doctorStationAdvice/listDoctorStationAdvice', {'patientId': pid, 'encounterId': eid})
        if (binding.get('note') or {}).get('native_id'):
            call('document', 'doctor', '/document/getContent', {'documentId': binding['note']['native_id']})
        else:
            out['errors']['document'] = 'NoDocumentAllocated'
        try:
            if not re.fullmatch(r'[0-9]{1,19}', str(eid)):
                raise ValueError('Non-numeric scoped identifier')
            location = self.deployment_dir/'manage.py'
            module_spec = importlib.util.spec_from_file_location('workflow_read_only_manage', location)
            manage = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(manage)
            # Fixed experiment project from manage.py; no user-controlled SQL.
            query = """BEGIN READ ONLY;
SELECT coalesce(jsonb_agg(jsonb_build_object('id',p.id::text,'patient_id',p.patient_id::text,
 'encounter_id',p.encounter_id::text,'status',p.status_enum,'amount',p.display_amount::text,
 'charge_item_ids',p.charge_item_ids,'details',(SELECT coalesce(jsonb_agg(jsonb_build_object(
 'id',d.id::text,'pay_enum',d.pay_enum,'result_enum',d.result_enum,'amount',d.amount::text)),'[]'::jsonb)
 FROM fin_payment_rec_detail d WHERE d.reconciliation_id=p.id AND NOT d.is_deleted))),'[]'::jsonb)
 FROM fin_payment_reconciliation p WHERE NOT p.is_deleted AND p.encounter_id=""" + str(eid) + '; COMMIT;'
            out['payments'] = json.loads(manage.sql(args=['-A', '-t'], text=query).stdout.strip())
        except Exception as exc:
            out['errors']['payments'] = type(exc).__name__
        document_id=(binding.get('note') or {}).get('native_id')
        if document_id:
            try:
                if not re.fullmatch(r'[0-9]{1,19}',str(document_id)):
                    raise ValueError('Non-numeric scoped document identifier')
                query="BEGIN READ ONLY; SELECT coalesce(json_agg(json_build_object('version_number',version_number,'content',content) ORDER BY version_number),'[]'::json) FROM emr_document_version WHERE document_id="+str(document_id)+" AND NOT is_deleted; COMMIT;"
                out['document_versions']=json.loads(manage.sql(args=['-A','-t'],text=query).stdout.strip())
            except Exception as exc:
                out['errors']['document_versions']=type(exc).__name__
        return out


def _html(report):
    esc = html.escape
    def mark(v):
        return {True: '已核验', False: '未满足', None: '未知'}[v]
    rows = ''.join('<tr><td>'+esc(row['study_id'])+'</td><td>'+esc(row['condition'])+'</td><td>'+esc(row['runtime_status'])+
                   '</td><td>'+mark(row.get('independently_checked_core_complete'))+'</td><td>'+mark(row['formal']['correct_completion'])+
                   '</td><td>'+mark(row['formal']['unsafe_execution'])+'</td><td>'+mark(row['formal']['timeout'])+'</td></tr>' for row in report['runs'])
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>TwinHospital 独立流程审计</title><style>body{font:15px/1.7 system-ui;max-width:1200px;margin:35px auto;padding:0 16px;color:#203047}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:7px}th{background:#edf3f8}.warn{padding:14px;background:#fff2d4}code{overflow-wrap:anywhere}</style><h1>TwinHospital 独立流程审计</h1><p class="warn">'+esc(report['experiment_status'])+'。核心业务读回不等于正式整链正确完成。未预设窗口的旧运行不追认 timeout；没有全过程原生变更覆盖时，不推定没有违规。药费核验与注册费分开。临床质量未在本工具评分。</p><p>完整流程计划单位 '+str(report['summary']['planned_full_workflow_units'])+'；固定输入阶段任务 '+str(report['summary']['fixed_input_task_units'])+'。所有缺失和失败保留在各自分母。</p><table><tr><th>研究编号</th><th>条件</th><th>实际运行状态</th><th>已观察核心完整性</th><th>正式正确完成</th><th>观察到违规</th><th>正式超时</th></tr>'+rows+'</table><p><a href="workflow_audit.json">逐项核验与证据哈希 JSON</a> · <a href="future_observation_contract.json">未来窗口与读回要求</a></p></html>'


def audit_batch(batch_dir, public_dir, private_path, *, observe_native=False, observer=None,
                deployment_dir=None, catalog_path=None):
    batch, public, private = map(lambda p: Path(p).resolve(), (batch_dir, public_dir, private_path))
    if public.exists() or private.exists():
        raise ValueError('Use new audit outputs; previous observations are retained')
    if public == private or public in private.parents or batch == public or batch in public.parents or private == batch or batch in private.parents:
        raise ValueError('Private mapping must be separate; source batch is read-only')
    if not (batch/'batch_summary.json').exists():
        raise ValueError('Finished batch summary required')
    lock_path = batch/'batch.lock'
    with lock_path.open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return _audit_locked(batch, public, private, observe_native, observer,
                            Path(deployment_dir or ROOT/'deploy/openhis-experiment').resolve(), catalog_path)


def _audit_locked(batch, public, private, observe_native, observer, deployment_dir, catalog_path):
    originals = {}
    source_paths=(Path(__file__), ROOT/'sandbox/experiments/linked/workflow_analysis.py',
        ROOT/'sandbox/experiments/linked/trace_evidence.py', ROOT/'sandbox/experiments/linked/native_observation_analysis.py',
        ROOT/'sandbox/experiments/linked/linked_acceptance.py')
    analyzer_sources={str(p.relative_to(ROOT)):{'sha256':sha(p),'source':p.read_text()} for p in source_paths}
    def tracked(path):
        path = inside(path, batch)
        originals[str(path)] = sha(path)
        return read(path)
    spec = tracked(batch/'specification.json')
    batch_summary = tracked(batch/'batch_summary.json')
    raw_capture=None
    capture_root=batch/'native_observation'
    if (capture_root/'observation_session.json').exists():
        try:
            meta=tracked(capture_root/'observation_session.json')
            data=tracked(capture_root/'native_committed_mutations.json')
            jp=inside(capture_root/'observation_intents.jsonl',batch)
            originals[str(jp)]=sha(jp)
            journal=[json.loads(line) for line in jp.read_text().splitlines() if line]
            raw_capture={'metadata':meta,'mutations':data,'journal':journal}
        except (OSError,ValueError,TypeError):
            raw_capture={'metadata':{},'mutations':{},'journal':[]}
    from sandbox.experiments.linked.trace_evidence import prepare_capture
    prepared_capture = prepare_capture(raw_capture,spec) if raw_capture is not None else None
    if spec.get('mode') not in {'linked', 'matrix', 'linked-acceptance'}:
        raise ValueError('Unsupported batch evaluation unit')
    valid_conditions = {c.config_id: c for c in paper_conditions(model_families=spec.get('models',{}).get('tool_models'))}
    planned = []
    design = spec.get('linked_acceptance') if spec['mode'] == 'linked-acceptance' else None
    profiles = {}
    if spec['mode'] == 'linked-acceptance':
        if not design:
            raise ValueError('Missing frozen linked design')
        frozen_cases = spec.get('cases', [])
        target = design.get('target_per_disease_and_condition')
        maximum = design.get('max_attempts_per_unit', 1)
        reader = design.get('version') == READER_DESIGN_VERSION
        independent = design.get('independent_crossed', False)
        if reader or independent:
            # The public specification keeps hashes of patient keys, not their values.
            keys = spec.get('acceptance_source_patient_hashes', [])
            cases = [{**case, 'patient_key': case['case_id']} for case in frozen_cases]
            reconstructed = freeze_design(
                cases, spec['models']['tool_models'], patients_per_group=target or 1,
                workers=spec['execution_policy']['workers'], max_attempts=maximum,
                factorial=design.get('factorial', False), independent=independent,
                reader=reader, quick=design.get('quick', False),
            )
            expected, cells = reconstructed['profiles'], reconstructed['task_condition_profiles']
            count = len(cases)
            valid_cohort = (reconstructed == design and len(keys) == len(set(keys)) == count
                            and all(re.fullmatch(r'[0-9a-f]{64}', key) for key in keys)
                            and (not independent or bool(spec.get('development_exclusion_sha256'))))
        else:
            expected_profiles, cells = linked_profiles(spec['models']['tool_models'],
                                                       factorial=design.get('factorial', False))
            expected = [{'label': label, 'config_id': linked_config_id(conditions),
                         'conditions': {k: c.to_dict() for k, c in conditions.items()}}
                        for label, conditions in expected_profiles.items()]
            count = len(GROUPS) * target if target in SUPPORTED_PATIENTS_PER_GROUP else None
            valid_cohort = (
                design.get('version') == 'linked_acceptance_v1'
                and count is not None and maximum in (1, MAX_ENGINEERING_ATTEMPTS)
                and (target != 2 or design.get('factorial', False))
                and len(frozen_cases) == count == len({case['case_id'] for case in frozen_cases})
                and all(sum(c.get('group') == group for c in frozen_cases) == target for group in GROUPS)
                and all(c.get('data_kind') == 'mimic_derived' and c.get('split') == 'development' for c in frozen_cases)
            )
        if (not valid_cohort or spec.get('formal_experiment') is not False or spec.get('anchor_reuse')
                or design.get('counts_as_formal_experiment') is not False
                or design.get('profiles') != expected or design.get('task_condition_profiles') != cells
                or design.get('planned_unique_encounters') != count * len(expected)
                or batch_summary.get('batch') != spec.get('batch')):
            raise ValueError('Incomplete or changed linked acceptance design')
        profiles = {p['label']: p for p in expected}
        planned = [(label, 'complete_encounter') for label in profiles]
    elif not spec.get('anchor_reuse'):
        planned.append(('linked_baseline_anchor', 'complete_encounter'))
    if spec['mode'] == 'matrix':
        for condition in spec.get('conditions', []):
            label = condition.get('config_id')
            if label not in valid_conditions:
                raise ValueError('Unknown frozen condition')
            planned.append((label.replace(':', '__'), 'fixed_input_task'))
    if catalog_path is not None:
        mapping = read(catalog_path)
        if digest(mapping) != spec.get('catalog_hash'):
            raise ValueError('Audit catalogue differs from the frozen run.')
    else:
        mapping = None
        for p in sorted(deployment_dir.glob('catalog_mapping*.json')):
            candidate = read(p)
            if digest(candidate) == spec.get('catalog_hash'):
                mapping = candidate
                break
    if observe_native and observer is None:
        observer = ReadOnlyObserver(deployment_dir=deployment_dir)
    rows, crosswalk, observations = [], [], {}
    native_bindings = {'patient_id': {}, 'encounter_id': {}}
    for n, case in enumerate(spec.get('cases', []), 1):
        case_path = inside(batch/str(case['case_id']), batch)
        index = {}
        try:
            index = tracked(case_path/'attempts.json')
            attempts = index.get('attempts') or {}
        except Exception:
            attempts = {}
        for m, (label, unit) in enumerate(planned, 1):
            study = f'S{n:04d}-R{m:02d}'
            record = attempts.get(label) or {}
            row, native, protocol, source = None, {}, {}, None
            try:
                if not record.get('result_path'):
                    raise FileNotFoundError('No indexed completed result')
                number = record.get('attempt_number', 1)
                history = (index.get('prior_attempts') or {}).get(label, [])
                if design and (type(number) is not int or not 1 <= number <= maximum or len(history) != number - 1):
                    raise ValueError('Invalid engineering retry history')
                trial = attempt_label(label, number) if design else label
                source = inside(record['result_path'], case_path/trial)
                run = tracked(source)
                if run.get('case_id') != case['case_id'] or run.get('evaluation_unit') != unit:
                    raise ValueError('Case or evaluation unit mismatch')
                if design and (run.get('run_id') != f"{spec['batch']}:{case['case_id']}:{trial}"
                        or any(run.get(k) != case[k] for k in ('group', 'split', 'data_kind'))):
                    raise ValueError('Run is not bound to the frozen acceptance unit')
                if design:
                    patient_ids = {run.get('patient_id')}
                    encounter_ids = {run.get('encounter_id')}
                    for previous_number, previous in enumerate(history, 1):
                        previous_label = attempt_label(label, previous_number)
                        previous_path = inside(previous['result_path'], case_path/previous_label)
                        prior_run = tracked(previous_path)
                        if (previous.get('attempt_number') != previous_number
                                or previous.get('status') != prior_run.get('status')
                                or not documented_unpaid_stop(prior_run)
                                or prior_run.get('run_id') != f"{spec['batch']}:{case['case_id']}:{previous_label}"
                                or prior_run.get('config_id') != profiles[label]['config_id']
                                or not prior_run.get('patient_id') or prior_run['patient_id'] in patient_ids
                                or not prior_run.get('encounter_id') or prior_run['encounter_id'] in encounter_ids
                                or any(prior_run.get(k) != case[k] for k in ('case_id', 'group', 'split', 'data_kind'))):
                            raise ValueError('Prior trial is not a documented unpaid stop of this assignment')
                        patient_ids.add(prior_run['patient_id'])
                        encounter_ids.add(prior_run['encounter_id'])
                state_dir = source.parents[2]/'native'
                errors = []
                for name in ('native_state', 'protocol_state'):
                    try:
                        value = tracked(state_dir/(name+'.json'))
                        if name == 'native_state':
                            native = value.get('bindings', {}).get(str(run['run_id'])+':'+str(run['case_id']), {})
                        else:
                            protocol = value
                    except Exception as exc:
                        errors.append({'source': name, 'error_type': type(exc).__name__})
                try:
                    observation = observer.observe(native) if observe_native else None
                except Exception as exc:
                    observation = {'errors': {'observer': type(exc).__name__}}
                if observation:
                    observations[study] = observation
                row = audit_run(run, spec, expected_case=case, binding=native, protocol=protocol,
                                fresh_observation=observation, catalogue=mapping, raw_capture=raw_capture,
                                prepared_capture=prepared_capture)
                row['source_read_errors'] = errors
                row['fresh_read_errors'] = (observation or {}).get('errors', {})
                row['source_run_file_sha256'] = originals[str(source)]
                if design:
                    checks = execution_checks(run, profiles[label], spec['models'])
                    row['linked_execution_checks'] = checks
                    row['attempts_executed'] = number
                    for field, index_by_id in native_bindings.items():
                        ident = native.get(field)
                        if ident:
                            index_by_id.setdefault(str(ident), []).append(study)
                    row['verified_paid'] = bool(native and all(checks.values())
                        and row.get('independently_checked_core_complete') is True
                        and (row.get('trace_capture_evidence') or {}).get('declared_invariant_coverage_complete') is True
                        and not row.get('observed_violations'))
                crosswalk.append({'study_id': study, 'case_id': run.get('case_id'), 'run_id': run.get('run_id'),
                    'result_path': str(source), 'native_binding': native, 'condition': label})
            except Exception as exc:
                row = missing_run('source_or_analysis_unavailable')
                row['source_read_error_type'] = type(exc).__name__
                row['runtime_status'] = record.get('status') if record.get('status') in {
                    'failed', 'completed', 'stopped_budget', 'stopped_provider', 'incomplete_clinical_decision',
                    'not_run_budget_limit', 'not_run_provider_unavailable', 'not_run_missing_frozen_input',
                    'not_run_submission_window', 'interrupted_requires_audit', 'not_run_regression_gate',
                    'not_run_operator_interruption', 'driver_error'} else 'unavailable'
                crosswalk.append({'study_id': study, 'case_id': case.get('case_id'), 'result_path': str(source) if source else None,
                                  'condition': label, 'error_type': type(exc).__name__})
            row.update(study_id=study, condition=label.replace('__', ':'), unit=unit)
            if design:
                row.update(profile=label, group=case['group'])
                row.setdefault('verified_paid', False)
            if unit == 'fixed_input_task':
                row['formal'] = {'correct_completion': None, 'unsafe_execution': None, 'timeout': None,
                                 'queue_to_target_payment_seconds': None, 'reason': 'not_a_complete_encounter_evaluation_unit'}
            rows.append(row)
    if design:
        reused = {study for index in native_bindings.values() for studies in index.values()
                  if len(studies) > 1 for study in studies}
        for row in rows:
            if row['study_id'] in reused:
                row.update(verified_paid=False, native_identity_reused=True)
    unchanged = all(Path(p).exists() and sha(p) == h for p, h in originals.items())
    if not unchanged:
        raise RuntimeError('Source changed during audit; refusing a mixed-version public result')
    if any(sha(ROOT/name)!=source['sha256'] for name,source in analyzer_sources.items()):
        raise RuntimeError('Analyzer source changed during audit')
    report = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'experiment_status': 'formal_declared_in_spec' if spec.get('formal_experiment') is True else 'engineering_only',
        'source_specification_sha256': sha(batch/'specification.json'), 'source_files_unchanged': True,
        'analyzer_code_sha256': {name:source['sha256'] for name,source in analyzer_sources.items()},
        'native_observation_requested': observe_native, 'native_mutations': 0, 'model_calls': 0,
        'source_anchor_reused_not_counted_as_new': bool(spec.get('anchor_reuse')),
        'evaluation_protocol': spec.get('evaluation_protocol', 'legacy_protocol'),
        'external_human_review_required': spec.get('external_human_review_required'),
        'catalogue_exact_hash_available': mapping is not None,
        'summary': summarize(rows), 'runs': rows,
        'limitations': list(READBACK_REQUIREMENTS.values())}
    if design:
        report['linked_acceptance_gate'] = summarize_gate(design, rows)
        report['linked_acceptance_gate']['reused_native_identity_runs'] = len(reused)
    private_json(private, {'audit': report, 'source_file_hashes': originals, 'crosswalk': crosswalk,
                           'analyzer_source_archive':analyzer_sources,
                           'raw_post_run_readonly_observations': observations})
    public.mkdir(parents=True, mode=0o755)
    (public/'workflow_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    (public/'workflow_audit.html').write_text(_html(report))
    (public/'future_observation_contract.json').write_text(json.dumps({'template_not_prespecified': WINDOW_POLICY_TEMPLATE,
        'readback_requirements': READBACK_REQUIREMENTS,
        'event_clock_fields': {'clock_id': 'same per-run identifier', 'elapsed_seconds': 'finite monotonic seconds',
            'clock_boundary': 'queue_entry_persisted on the queue event only'},
        'request_intent_fields': ['event=business_request_intent', 'request_id', 'operation', 'elapsed_seconds', 'clock_id', 'scope'],
        'freeze_fields': ['spec.workflow_observation_policy', 'run.workflow_policy_hash', 'run.workflow_clock_id'],
        'declared_business_table_and_gateway_capture_implemented': True,
        'whole_OpenHIS_effect_capture_claimed': False, 'legacy_rates_must_remain_unknown': True}, ensure_ascii=False, indent=2)+'\n')
    return {'public_directory': str(public), 'audited_units': len(rows), 'summary': report['summary'], 'source_files_unchanged': True}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch-dir', type=Path, required=True)
    p.add_argument('--public-dir', type=Path, required=True)
    p.add_argument('--private-path', type=Path, required=True)
    p.add_argument('--observe-native', action='store_true', help='Fresh isolated read-only API/SQL; login sessions only')
    p.add_argument('--deployment-dir', type=Path, help='Existing isolated deployment used by the source batch')
    p.add_argument('--catalog', type=Path, help='Exact native product mapping used by the batch.')
    a = p.parse_args()
    print(json.dumps(audit_batch(a.batch_dir, a.public_dir, a.private_path, observe_native=a.observe_native,
                                deployment_dir=a.deployment_dir, catalog_path=a.catalog), ensure_ascii=False, indent=2))
