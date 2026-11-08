"""Prepare versioned local inputs for the frozen 1000-patient study.

Reuse the prospective scenario constructors without their development selection
steps. Rebind the preassigned APP III tests to the new input hashes. This tool
reads source inputs only and makes no model calls or HIS writes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.experiments.linked.case_preparation import current_panel_v14, current_plan_v15, reported_allergy_episode_v12
from sandbox.experiments.linked.case_preparation.formulary_overlay import validate_formulary
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.provider import private_json
from sandbox.experiments.linked.run_batch import file_sha256
from sandbox.experiments.linked.source_operation_replay import read, validate_schedule
from tools import prepare_engineering_readiness_v13 as readiness


VERSION = 'formal_runtime_inputs_candidate_v1'
IDENTITY = ('case_id', 'patient_key', 'group', 'split', 'data_kind')
LOCAL_ASSIGNMENT = ('local_test_id', 'patient_key', 'group', 'operation_category', 'request_condition')


def prepare_case(source, formulary, private_data):
    case = readiness.transform(source, formulary)
    case, panel_audit = current_panel_v14.build(case)
    patterns = Counter(
        (str(item.get('name') or '').strip().casefold(), str(item.get('route') or '').strip().casefold())
        for item in case['his_initial'].get('current_use_record', {}).get('items', [])
        if item.get('plan')
    )
    links, source_hashes = {}, {}
    if any(n > 1 for n in patterns.values()):
        links, source_hashes = current_plan_v15.load_private_order_links(source, private_data)
    case, plan_audit = current_plan_v15.build(case, links)
    if any(case[k] != source[k] for k in IDENTITY[1:]):
        raise ValueError('Scenario construction changed patient identity, disease, or partition')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', case['case_id']):
        raise ValueError('Derived case identity is not safe for runtime paths')
    chart = HistoryChart(case['his_initial']).initial_view()
    audit = {
        'adapted': 1,
        'new_current_panel_values': panel_audit['added_count'],
        'duplicate_plan_patterns_suppressed': plan_audit['nonadopted_pattern_count'],
        'unresolved_plan_groups': len(plan_audit['unresolved_groups']),
        'last_dose_reports_visible': len(chart['recent_reported_dose_timing']['records']),
        'missing_prior_regimen': int(not readiness.screen(case)['eligible']),
    }
    return case, source_hashes, audit


def prepare(cohort_path, schedule_path, analysis_path, formulary_path, private_data, output):
    output = output.resolve()
    if output.exists():
        raise FileExistsError('Use a new directory for each prepared input version')
    inputs = (cohort_path, schedule_path, analysis_path, formulary_path)
    implementations = (Path(__file__), Path(readiness.__file__), Path(reported_allergy_episode_v12.__file__),
                       Path(current_panel_v14.__file__), Path(current_plan_v15.__file__))
    protected = {str(p.resolve()): file_sha256(p) for p in (*inputs, *implementations)}
    cohort, schedule, analysis, formulary = map(read, inputs)
    if (protected[str(cohort_path.resolve())] != analysis['source_selected_cohort_sha256']
            or protected[str(schedule_path.resolve())] != analysis['source_local_schedule_sha256']):
        raise ValueError('Selected patient or operation manifest differs from the frozen analysis plan')
    validate_schedule(schedule, cohort)
    if isinstance(formulary, dict):
        formulary = formulary.get('formulary', formulary.get('items'))
    validate_formulary(formulary)

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix=f'.{output.name}.', dir=output.parent) as staging_name:
        staging = Path(staging_name)
        prepared = {}
        aggregates = defaultdict(Counter)
        for index, row in enumerate(cohort['cases'], 1):
            source_path = Path(row['path']).resolve()
            raw = source_path.read_bytes()
            source_hash = sha256(raw).hexdigest()
            if source_hash != row['file_sha256']:
                raise ValueError('Selected source file differs from its frozen hash')
            source = json.loads(raw)
            if (any(source[k] != row[k] for k in IDENTITY)
                    or digest(runtime_case(source)) != row['runtime_input_hash']):
                raise ValueError('Selected source identity or runtime input differs from the frozen manifest')
            protected[str(source_path)] = source_hash
            case, private_hashes, audit = prepare_case(source, formulary, private_data)
            protected.update(private_hashes)
            relative = Path('cases') / f"{case['case_id']}.json"
            target = staging / relative
            private_json(target, case)
            prepared[row['case_id']] = {
                **row, 'case_id': case['case_id'], 'path': str(output / relative),
                'file_sha256': file_sha256(target), 'case_hash': digest(case),
                'runtime_input_hash': digest(runtime_case(case)),
                'source_case_id': row['case_id'], 'source_file_sha256': source_hash,
            }
            aggregates[row['group']].update(audit)
            if index % 100 == 0:
                print(json.dumps({'prepared': index, 'planned': len(cohort['cases'])}), file=sys.stderr, flush=True)

        new_cohort = {**cohort, 'cases': list(prepared.values()), 'version': VERSION,
                      'purpose': 'Local prepared inputs for the same frozen held-out patients; execution freeze pending'}
        new_schedule = {**schedule, 'version': VERSION,
                        'cases': [{**row, **prepared[row['case_id']]} for row in schedule['cases']]}
        validate_schedule(new_schedule, new_cohort)
        if any(before[k] != after[k] for before, after in zip(schedule['cases'], new_schedule['cases'])
               for k in LOCAL_ASSIGNMENT):
            raise ValueError('A local operation assignment changed during rebinding')
        if any(file_sha256(path) != expected for path, expected in protected.items()):
            raise ValueError('An input file changed during preparation')
        cohort_name = 'formal_test1000_manifest.json'
        schedule_name = 'app3_local500_manifest.json'
        private_json(staging / cohort_name, new_cohort)
        private_json(staging / schedule_name, new_schedule)
        receipt = {
            'version': VERSION, 'prepared_at': datetime.now(timezone.utc).isoformat(),
            'source_plan_and_implementation_hashes': {str(p.resolve()): protected[str(p.resolve())]
                                                     for p in (*inputs, *implementations)},
            'prepared_manifest_sha256': file_sha256(staging / cohort_name),
            'prepared_operation_schedule_sha256': file_sha256(staging / schedule_name),
            'source_patients': len(prepared), 'per_disease': dict(aggregates),
            'local_tests': dict(Counter(row['request_condition'] for row in new_schedule['cases'])),
            'patient_or_assignment_replacements': 0, 'execution_outcomes_read': False,
            'model_calls': 0, 'his_mutations': 0, 'formal_runtime_freeze_pending': True,
        }
        private_json(staging / 'preparation_receipt.json', receipt)
        if output.exists():
            raise FileExistsError('A prepared version already exists')
        staging.rename(output)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cohort-path', 'schedule-path', 'analysis-path', 'formulary-path', 'private-data', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    result = prepare(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('version', 'source_patients', 'local_tests', 'per_disease')}))
