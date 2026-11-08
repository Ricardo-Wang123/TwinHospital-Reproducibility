"""Generate portable table descriptions and explicit execution templates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import default_config
from .table_specs import spec

EXPERIMENTS = {1: 'materials', 2: 'operation_materials', 3: 'participants', 4: 'E1',
    5: 'E2', 6: 'E2_hospital', 7: 'E2', 8: 'E3', 9: 'E3_hospital',
    10: 'E4_reuses_E2', 11: 'E4_hospital', 12: 'E5_reuses_E3',
    13: 'E5_hospital', 14: 'E6', 15: 'E7', 16: 'E8', 17: 'E9'}
COHORTS = {1: 900, 2: 2000, 3: 200, 4: 200, 5: 200, 6: 200, 7: 200,
    8: 200, 9: 200, 10: 200, 11: 200, 12: 200, 13: 200, 14: 1000,
    15: 1000, 16: 100, 17: 100}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def write_all(run, output):
    run, output = Path(run).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    model = {'model': 'replace-with-model-name', 'endpoint': 'https://model-provider.example/v1',
        'api_key_env': 'API_KEY', 'env_file': '.env', 'temperature': 0.2,
        'max_tokens': 1800, 'timeout_seconds': 90, 'max_calls': 20000}
    write_json(output / 'clinical-models.json', {'foundation': model, 'medical': None})
    native = default_config()
    native.update(purpose='engineering_demo', transport='paper_tables_http',
        his_backend='native_http', dataset=str(run / 'dataset'), model_api=model,
        max_interactions=12, case_concurrency=4, submission_window_s=10,
        model_timeout_s=90, case_timeout_s=600, observation_window_s=0,
        budget={'max_calls': 20000, 'max_tokens': 40000000},
        actor_ids={role: role + '-1' for role in
            ('doctor', 'nurse', 'patient', 'pharmacist', 'senior_pharmacist', 'scribe')},
        native_http={'base_url': 'http://localhost:18080/openhis',
            'token_env': 'OPENHIS_TOKEN', 'env_file': '.env',
            'timeout_s': 30, 'allow_writes': False})
    write_json(output / 'native-execution.json', native)
    entries = []
    for number in range(1, 18):
        header, rows, columns = spec(number)
        if number in (4, 5, 7, 8, 10, 12):
            inputs = ['dataset/cases.jsonl', 'dataset/inputs.jsonl', 'dataset/reference.jsonl']
            command, prerequisite = 'clinical', 'Explicit model configuration and input cases; E4/E5 reuse E2/E3.'
        elif number in (6, 9, 11, 13):
            inputs = ['hospital-return.json']
            command, prerequisite = 'import', 'Independent hospital artifacts, annotations and active-work records.'
        elif number in (3, 16, 17):
            inputs = ['questionnaire.jsonl']
            command, prerequisite = 'import', 'Real participant-level integer ratings; constructed examples stay labelled.'
        elif number in (14, 15):
            inputs = ['dataset/end_to_end_cases.jsonl' if number == 14 else 'dataset/safety_tests.jsonl',
                'native-material-manifest.json']
            command, prerequisite = 'native', 'Dedicated native objects, verified HTTP evidence, local cash-only channels, explicit write enablement.'
        else:
            inputs = ['table-context.json']
            command, prerequisite = 'export', 'Source-labelled material inventory; no fabricated real counts.'
        value = {'table': number, 'experiment': EXPERIMENTS[number],
            'target_unique_cases_or_people': COHORTS[number], 'row_header': header,
            'rows': [{'id': key, 'meaning': label} for key, label in rows],
            'columns': [{'id': key, 'meaning': label} for key, label in columns],
            'input_files': inputs, 'source_run': str(run), 'command': command,
            'prerequisite': prerequisite,
            'implementation': 'sandbox.experiments.paper_tables.table_export',
            'code_areas': {'experiment': 'sandbox/experiments: execution, HTTP adapter, calculation and export',
                'public_MedicalAgent': 'no source changes required by this entry',
                'OpenHIS': 'existing native API; hold requirements that actually require native source changes'},
            'native_boundary': {'experiment_ledger': ['queue', 'versions', 'trusted doctor confirmation'],
                'native_http': ['patient/encounter ownership', 'EMR body', 'order', 'payment effects']} if number in (14, 15) else None,
            'missing_conventions': {'0': 'observed zero', 'empty': 'missing input',
                'UNDEFINED': 'zero denominator', 'N/A': 'not applicable'}}
        write_json(output / f'table_{number:02d}.json', value)
        entries.append({'table': number, 'file': f'table_{number:02d}.json', 'experiment': EXPERIMENTS[number]})
    write_json(output / 'index.json', entries)
    print(json.dumps({'table_configs': len(entries), 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, help='Planned or existing clinical output directory')
    parser.add_argument('--output', required=True, help='New directory for 17 descriptions and templates')
    options = parser.parse_args()
    write_all(options.run, options.output)
