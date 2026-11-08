"""APP III operation tests bound to preselected, actually executed source cases.

Only local HIS calls are made. Clinical artifacts are copied from a verified
canonical run into fresh business objects; original patients and runs are never
modified. This is operation-boundary re-instantiation, not a replay of the MAS or
a claim that a database snapshot has been restored. Missing sources retain their
scheduled cells. Independent fictional engineering checks cannot become formal
MIMIC tests by sharing the same test count.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .cases import GROUPS, load_case
from .batch_observation import deployment_metadata
from .contracts import runtime_case
from .linked_acceptance import BASELINE_LABEL
from .native_his import NativeBlocked, NativeOpenHISPort, digest, isolated_port, write_private
from .protocol_his import PaperProtocolPort
from .run_batch import validate_anchor_evidence
from .safety_native import append
from .safety_paper import PaperLocalSafety, DEFINITIONS

VERSION = 'source-bound-operation-replay/3-cohort1000-local500'
CATEGORIES = {
    'patient_record_mismatch': 'patient_bill',
    'missing_encounter_link': 'missing_encounter',
    'invalid_note_state': 'locked_note',
    'unauthorized_signing': 'unauthorized_signatory',
    'duplicate_transaction': 'duplicate_payment',
}
FORMAL_REQUESTS_PER_CELL = {'single_fault': 4, 'compliant_control': 16}
REQUIRED_OPS = {
    'patient_bill': ('consultation.save', 'order.save', 'order.attest', 'payment.prepare', 'payment.commit'),
    'missing_encounter': ('queue.enter',),
    'locked_note': ('note.save', 'note.attest'),
    'unauthorized_signatory': ('consultation.save', 'order.save', 'order.attest'),
    'duplicate_payment': ('consultation.save', 'order.save', 'order.attest', 'payment.prepare', 'payment.commit'),
}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay_code_hashes():
    return {name: sha(Path(__file__).with_name(name)) for name in (
        'source_operation_replay.py', 'safety_paper.py', 'safety_native.py',
        'protocol_his.py', 'native_his.py', 'run_batch.py', 'cases.py', 'contracts.py')}


def unchanged(files):
    try:
        return all(sha(p) == expected for p, expected in files.items())
    except OSError:
        return False


def validate_schedule(schedule, cohort, *, engineering=False):
    rows = schedule['cases']
    members = {r['case_id']: r for r in cohort['cases']}
    if len(members) != len(cohort['cases']):
        raise ValueError('shared cohort contains duplicate case identities')
    if not rows or len({r['local_test_id'] for r in rows}) != len(rows):
        raise ValueError('local test identities must be nonempty and unique')
    for r in rows:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', r['local_test_id']):
            raise ValueError('invalid local test identity')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', r['case_id']):
            raise ValueError('invalid source case identity')
        if r['operation_category'] not in CATEGORIES or r['request_condition'] not in {'single_fault', 'compliant_control'}:
            raise ValueError('unknown local operation or condition')
        if r['group'] not in GROUPS or not r.get('patient_key'):
            raise ValueError('source disease and patient identities are required')
        if r['case_id'] not in members:
            raise ValueError('test source is outside the frozen shared cohort')
        expected = members[r['case_id']]
        if any(r.get(k) != expected.get(k) for k in ('patient_key', 'group', 'split', 'data_kind', 'file_sha256', 'runtime_input_hash', 'path')):
            raise ValueError('local row differs from its shared-cohort source')
        if any(not re.fullmatch(r'[a-f0-9]{64}', r.get(k, '')) for k in ('file_sha256', 'runtime_input_hash')):
            raise ValueError('source file and runtime hashes must precede replay')
    if not engineering:
        if len(members) != 1000 or len({r['patient_key'] for r in members.values()}) != 1000:
            raise ValueError('formal replay requires the 1000 distinct source-patient cohort')
        if any(r['split'] != 'test' or r['data_kind'] != 'mimic_derived' for r in members.values()):
            raise ValueError('engineering material cannot be promoted into the formal cohort')
        if len(rows) != 500 or len({r['patient_key'] for r in rows}) != 500:
            raise ValueError('formal replay requires 500 distinct preselected source patients')
        counts = Counter((r['group'], r['operation_category'], r['request_condition']) for r in rows)
        expected = Counter({(g, c, condition): n for g in GROUPS for c in CATEGORIES
                            for condition, n in FORMAL_REQUESTS_PER_CELL.items()})
        if counts != expected or Counter(r['group'] for r in members.values()) != Counter({g: 200 for g in GROUPS}):
            raise ValueError('formal allocation must preserve all 25 disease/operation cells')
    return rows


def load_checkpoint(row, source_batch, source_spec):
    """Verify source identity, observed artifacts and clinical content locally."""
    root = Path(source_batch).resolve()
    entries = [r for r in source_spec['cases'] if r['case_id'] == row['case_id']]
    if len(entries) != 1 or any(entries[0].get(k) != row.get(k) for k in ('group', 'data_kind', 'split', 'file_sha256', 'runtime_input_hash')):
        raise ValueError('source batch does not contain the frozen source case')
    cp = Path(row['path'])
    protected = {str(cp.resolve()): row['file_sha256'],
                 str(root / 'specification.json'): sha(root / 'specification.json')}
    if read(root / 'specification.json') != source_spec:
        raise ValueError('source specification changed')
    if sha(cp) != row['file_sha256']:
        raise ValueError('source case file changed')
    case = load_case(cp)
    if any(case.get(k) != row.get(k) for k in ('case_id', 'patient_key', 'group', 'split', 'data_kind')):
        raise ValueError('source case identity differs from the scheduled patient')
    if digest(runtime_case(case)) != row['runtime_input_hash'] or case['case_id'] != row['case_id']:
        raise ValueError('source runtime input changed')
    ip = root / row['case_id'] / 'attempts.json'
    protected[str(ip)] = sha(ip)
    index = read(ip)
    entry = index['attempts'].get(BASELINE_LABEL) or {}
    if not entry.get('result_path'):
        raise ValueError('canonical source result unavailable')
    rp = Path(entry['result_path']).resolve()
    if not rp.is_relative_to(root / row['case_id']):
        raise ValueError('source result points outside its case directory')
    protected[str(rp)] = sha(rp)
    run = read(rp)
    if (run.get('case_id') != row['case_id'] or run.get('input_hash') != row['runtime_input_hash']
            or run.get('status') != 'completed' or run.get('native_complete') is not True):
        raise ValueError('source canonical encounter is not complete and bound to this input')
    validate_anchor_evidence(run, source_spec['models'])
    category = CATEGORIES[row['operation_category']]
    events = [e for e in run.get('events', []) if e.get('event') == 'business_operation'
              and e.get('response', {}).get('readback_verified') is True
              and e.get('response', {}).get('status') in {'executed', 'replayed', 'ok'}]
    if not set(REQUIRED_OPS[category]) <= {e['operation'] for e in events}:
        raise ValueError('required source operation or read-back unavailable')
    a = run['artifacts']
    payment = (a.get('settlement') or {}).get('payment') or {}
    if payment.get('status') != 'paid' or not any(
            e['operation'] == 'payment.commit' and e['response'].get('native_effect') is True
            and e['response'].get('data') == payment for e in events):
        raise ValueError('source encounter lacks verified medication payment')
    checkpoint = {'demographics': deepcopy(case['patient_initial'].get('demographics', {})),
                  'consultation': deepcopy(a.get('consultation')),
                  'prescription': deepcopy(a.get('final_prescription')),
                  'note': deepcopy((a.get('attested_note') or {}).get('content'))}
    if category in {'patient_bill', 'unauthorized_signatory', 'duplicate_payment'}:
        if not isinstance(checkpoint['prescription'], dict) or not checkpoint['prescription'].get('items'):
            raise ValueError('actual approved source prescription unavailable')
    if category == 'locked_note' and (not isinstance(checkpoint['note'], dict) or not checkpoint['note'].get('text')):
        raise ValueError('actual attested source content unavailable')
    proof = {'source_case_id': row['case_id'], 'source_run_id': run['run_id'],
             'source_native_patient_id': run['patient_id'], 'source_native_encounter_id': run['encounter_id'],
             'source_case_sha256': row['file_sha256'], 'source_run_sha256': sha(rp),
             'source_index_sha256': sha(ip), 'source_specification_sha256': sha(root / 'specification.json'),
             'source_result_path': str(rp), 'source_index_path': str(ip),
             'source_operation_evidence_sha256': [digest(e) for e in events if e['operation'] in REQUIRED_OPS[category]],
             'checkpoint_sha256': digest(checkpoint), 'source_data_kind': row['data_kind'],
             'source_files_sha256': protected}
    if not unchanged(protected):
        raise ValueError('source changed during checkpoint extraction')
    return checkpoint, proof


def prepare_bindings(rows, source_batch, source_spec, output):
    """Freeze every source or missing-source outcome before the first HIS write."""
    bindings, protected = {}, {}
    for row in rows:
        try:
            checkpoint, proof = load_checkpoint(row, source_batch, source_spec)
            for path, expected in proof['source_files_sha256'].items():
                if path in protected and protected[path] != expected:
                    raise ValueError('source changed while preparing the replay cohort')
                protected[path] = expected
            payload = {'checkpoint': checkpoint, 'proof': proof}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            payload = {'failure_type': type(exc).__name__, 'reason': str(exc)[:300]}
        path = Path(output) / 'source_checkpoints' / (row['local_test_id'] + '.json')
        write_private(path, payload)
        bindings[row['local_test_id']] = {'path': str(path), 'sha256': sha(path)}
    if not unchanged(protected):
        raise ValueError('source changed before native execution began')
    return bindings, protected


class SourceOperationReplay(PaperLocalSafety):
    def __init__(self, output, run_id, mapping, diagnosis_mapping, *, credentials_path=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_id, self.mapping, self.diagnosis_mapping = run_id, mapping, diagnosis_mapping
        self.credentials_path = credentials_path
        self.clients = None  # No authentication or native writes before binding checks.
        self.checkpoint = None
        self.source_native_ids = set()

    def perform(self, p, ctx, op, payload, role='doctor', key=None):
        if any(str(ctx.get(k, '')) in self.source_native_ids for k in ('patient_id', 'encounter_id')):
            raise NativeBlocked('replay may not target an original source business identity')
        result = super().perform(p, ctx, op, payload, role, key)
        if op == 'registration.create' and any(str(result['data'].get(k, '')) in self.source_native_ids
                                               for k in ('patient_id', 'encounter_id')):
            raise NativeBlocked('replay registration returned an original source business identity')
        return result

    def fixture(self, test, suffix='', *, enter_queue=True):
        cid = test['test_id'] + suffix
        native = NativeOpenHISPort(self.output / 'units' / cid, catalog_mapping=self.mapping,
                                  diagnosis_mapping=self.diagnosis_mapping, clients=self.clients,
                                  credentials_path=self.credentials_path,
                                  base_url=f'http://127.0.0.1:{isolated_port()}/api/app')
        if self.clients is None:
            self.clients = {r: native._client(r) for r in ('doctor', 'nurse', 'cashier')}
            native.clients = self.clients
        p = PaperProtocolPort(native)
        ctx = {'case_id': cid, 'run_id': self.run_id}
        # New business identity; original patient and encounter are never edited.
        reg = self.perform(p, ctx, 'registration.create', {'patient_key': cid,
            'demographics': deepcopy(self.checkpoint['demographics'])}, 'administrator')['data']
        ctx.update(reg)
        self.perform(p, ctx, 'personnel.registry', {})
        if enter_queue:
            self.perform(p, ctx, 'queue.enter', {'encounter_id': ctx['encounter_id']}, 'administrator')
            self.perform(p, ctx, 'encounter.begin_consultation', {})
        return p, ctx

    def order(self, p, ctx, test, sign=True):
        items = deepcopy(self.checkpoint['prescription']['items'])
        if any(x.get('drug_code') not in self.mapping for x in items):
            raise ValueError('actual source product missing from the frozen replay catalogue')
        self.perform(p, ctx, 'order.save', {'order_id': 'rx', 'items': items})
        if sign:
            signer = self.perform(p, ctx, 'personnel.registry', {})['data']['signatory_id']
            self.perform(p, ctx, 'order.attest', {'order_id': 'rx', 'signatory_id': signer})

    def diagnosis(self, p, ctx, test):
        self.perform(p, ctx, 'consultation.save', {'content': deepcopy(self.checkpoint['consultation']), 'transcript': []})

    def note_content(self, test, *, replacement=False):
        value = deepcopy(self.checkpoint['note'])
        if replacement:
            # Identical payload in faulty and valid conditions; only lock state differs.
            # This tests persistence, not clinical revision quality.
            value['operation_replay_revision'] = 2
        return value

    def run_bound(self, row, binding):
        try:
            if sha(binding['path']) != binding['sha256']:
                raise ValueError('frozen replay checkpoint changed')
            saved = read(binding['path'])
            if 'failure_type' in saved:
                raise ValueError('source unavailable at freeze: ' + saved['reason'])
            checkpoint, proof = saved['checkpoint'], saved['proof']
            if digest(checkpoint) != proof['checkpoint_sha256'] or not unchanged(proof['source_files_sha256']):
                raise ValueError('bound source changed after replay planning')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'test_id': row['local_test_id'], 'group': row['group'],
                    'operation_category': row['operation_category'], 'request_condition': row['request_condition'],
                    'outcome': 'unresolved_source', 'failure_type': type(exc).__name__,
                    'reason': str(exc)[:300], 'model_calls': 0, 'native_effects_started': False,
                    'source_replaced': False, 'source_data_kind': row['data_kind']}
        self.checkpoint = checkpoint
        test = {'test_id': row['local_test_id'], 'category': CATEGORIES[row['operation_category']],
                'is_error': row['request_condition'] == 'single_fault', 'fixture_index': GROUPS.index(row['group'])}
        result = super().run_one(test)
        result.update(group=row['group'], operation_category=row['operation_category'],
                      request_condition=row['request_condition'], source_binding=proof,
                      data_kind='source_derived_operation_replay', source_data_kind=row['data_kind'],
                      replay_scope='fresh_objects_from_verified_source_artifacts',
                      source_native_objects_used_as_write_targets=False,
                      source_files_unchanged=unchanged(proof['source_files_sha256']),
                      clinical_validity_assessed=False)
        if not result['source_files_unchanged']:
            result.update(outcome='unresolved_source_changed', original_outcome=result['outcome'])
        self.checkpoint = None
        return result


def run(schedule_path, cohort_path, source_batch, output, run_id, catalog_path, diagnosis_path,
        *, engineering=False, deployment_dir=None):
    source_batch = Path(source_batch).resolve()
    with (source_batch / 'batch.lock').open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return _run_closed_batch(schedule_path, cohort_path, source_batch, output, run_id,
                                 catalog_path, diagnosis_path, engineering, deployment_dir)


def _run_closed_batch(schedule_path, cohort_path, source_batch, output, run_id,
                      catalog_path, diagnosis_path, engineering, deployment_dir):
    os.umask(0o077)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,70}', run_id):
        raise ValueError('use a safe unique run ID')
    schedule_bytes = Path(schedule_path).read_bytes()
    cohort_bytes = Path(cohort_path).read_bytes()
    schedule, cohort = json.loads(schedule_bytes), json.loads(cohort_bytes)
    rows = validate_schedule(schedule, cohort, engineering=engineering)
    output = Path(output).resolve()
    spec = read(source_batch / 'specification.json')
    summary_path = source_batch / 'batch_summary.json'
    source_summary = read(summary_path)
    if source_summary.get('batch') != spec.get('batch') or not source_summary.get('session_finished_at'):
        raise ValueError('source batch must be closed before operation replay')
    design = spec.get('linked_acceptance') or {}
    heldout = (spec.get('mode') == 'linked-acceptance' and design.get('independent_crossed') is True
               and design.get('evaluation_partition') == 'test')
    cohort_hash = hashlib.sha256(cohort_bytes).hexdigest()
    schedule_hash = hashlib.sha256(schedule_bytes).hexdigest()
    code_hashes = replay_code_hashes()
    if not engineering:
        if not heldout and spec.get('formal_experiment') is not True:
            raise ValueError('formal replay requires a frozen test cohort and source batch')
        bound = spec.get('heldout_operations' if heldout else 'formal_inputs') or {}
        if cohort_hash != bound.get('cohort_sha256') or schedule_hash != bound.get('operation_schedule_sha256'):
            raise ValueError('formal replay cohort or schedule differs from the source batch input freeze')
        if heldout and (bound.get('source_profile') != BASELINE_LABEL or bound.get('planned_units') != len(rows)):
            raise ValueError('held-out replay source profile or planned operation count differs from the freeze')
        if heldout and bound.get('code_sha256') != code_hashes:
            raise ValueError('held-out replay implementation differs from the freeze')
    capture = source_summary.get('native_observation')
    if (not engineering or capture) and (capture or {}).get('status') != 'closed':
        raise ValueError('source native observation must be closed before operation replay')
    if not engineering and deployment_dir is None:
        raise ValueError('formal replay requires the explicit source deployment directory')
    credentials_path = None
    deployment_hashes = None
    if deployment_dir is not None:
        deployment_dir = Path(deployment_dir).resolve()
        deployment_hashes = deployment_metadata(deployment_dir)['source_hashes']
        recorded_hashes = (spec.get('native_observation') or {}).get('source_hashes')
        if recorded_hashes != deployment_hashes:
            raise ValueError('replay deployment inputs differ from the observed source batch')
        credentials_path = deployment_dir / 'runtime/accounts/credentials.json'
        if not credentials_path.is_file():
            raise ValueError('isolated replay deployment credentials are unavailable')
    mapping, diagnoses = read(catalog_path), read(diagnosis_path)
    if digest(mapping) != spec.get('catalog_hash') or digest(diagnoses) != spec.get('diagnosis_catalog_hash'):
        raise ValueError('replay mappings must match the actual source execution')
    if output.exists():
        raise FileExistsError('preserve every prior replay; use a new output directory')
    output.mkdir(parents=True, mode=0o700)
    (output / 'source_checkpoints').mkdir(mode=0o700)
    protected = {str(Path(p).resolve()): sha(p) for p in [schedule_path, cohort_path,
        source_batch / 'specification.json', summary_path, catalog_path, diagnosis_path]}
    if protected[str(Path(schedule_path).resolve())] != schedule_hash or protected[str(Path(cohort_path).resolve())] != cohort_hash:
        raise ValueError('replay cohort or schedule changed during preflight')
    if deployment_hashes:
        protected.update({str(deployment_dir / name): value for name, value in deployment_hashes.items()})
    bindings, source_files = prepare_bindings(rows, source_batch, spec, output)
    manifest = {'version': VERSION, 'run_id': run_id, 'formal_experiment': not engineering,
                'created_at': datetime.now(timezone.utc).isoformat(), 'planned_units': len(rows),
                'source_inputs_sha256': protected, 'definitions': DEFINITIONS,
                'bound_source_files_sha256': source_files, 'source_bindings': bindings,
                'model_calls': 0, 'source_replaced': False,
                'replay_scope': 'fresh_objects_from_verified_source_artifacts',
                'entire_MAS_rerun': False, 'source_object_ids_remapped': True,
                'missing_source_policy': 'retain unresolved cell; never select another successful patient',
                'note_edit_policy': 'preserve actual clinical text and add a nonclinical revision marker; vary only lock precondition',
                'code_sha256': code_hashes}
    if not engineering and heldout:
        manifest['heldout_operations'] = bound
    write_private(output / 'manifest.json', manifest)
    executor = SourceOperationReplay(output, run_id, mapping, diagnoses, credentials_path=credentials_path)
    for binding in bindings.values():
        proof = read(binding['path']).get('proof', {})
        executor.source_native_ids.update(str(proof[k]) for k in
            ('source_native_patient_id', 'source_native_encounter_id') if proof.get(k))
    results = []
    for i, row in enumerate(rows, 1):
        if not unchanged(protected):
            raise ValueError('frozen source plan or mapping changed during execution')
        if not unchanged({str(Path(__file__).with_name(k)): v for k, v in manifest['code_sha256'].items()}):
            raise ValueError('frozen replay implementation changed during execution')
        result = executor.run_bound(row, bindings[row['local_test_id']])
        append(output / 'outcomes.jsonl', result)
        results.append(result)
        print(json.dumps({'completed_units': i, 'planned_units': len(rows), 'group': row['group'],
                          'operation_category': row['operation_category'], 'outcome': result['outcome']}), flush=True)
    summary = {**manifest, 'recorded_units': len(results), 'outcomes': dict(Counter(x['outcome'] for x in results)),
               'by_cell': [{'disease': g, 'operation': c, 'condition': condition,
                    'counts': dict(Counter(x['outcome'] for x in results if x['group'] == g
                        and x['operation_category'] == c and x['request_condition'] == condition))}
                    for g in GROUPS for c in CATEGORIES for condition in ['single_fault', 'compliant_control']],
               'source_inputs_unchanged': unchanged({**protected, **source_files}),
               'is_full_formal_schedule': not engineering and len(rows) == 500}
    write_private(output / 'summary.json', summary)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['schedule', 'cohort', 'source-batch', 'output', 'catalog', 'diagnosis-catalog']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--engineering-only', action='store_true')
    p.add_argument('--deployment-dir', type=Path, help='Isolated deployment of the source batch')
    a = p.parse_args()
    run(a.schedule, a.cohort, a.source_batch, a.output, a.run_id, a.catalog,
        a.diagnosis_catalog, engineering=a.engineering_only, deployment_dir=a.deployment_dir)


if __name__ == '__main__':
    main()
