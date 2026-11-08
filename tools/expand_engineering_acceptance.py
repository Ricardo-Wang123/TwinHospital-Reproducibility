"""Expand the source-locked engineering cohort without reading run outcomes."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.case_preparation.formulary_overlay import validate_formulary
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.linked_acceptance import SUPPORTED_PATIENTS_PER_GROUP
from sandbox.experiments.linked.provider import private_json
from sandbox.experiments.linked.run_batch import file_sha256, load_verified_reference, select_cases
from tools.prepare_engineering_readiness_v13 import screen
from tools.prepare_formal_runtime_inputs import IDENTITY, prepare_case


SALT = 'TwinHospital-engineering-20-per-disease-20261108'


def prepare(base, reserve, heldout, formulary_path, output, target):
    output = output.resolve()
    if output.exists():
        raise FileExistsError('Use a new output directory')
    if target not in SUPPORTED_PATIENTS_PER_GROUP:
        raise ValueError('Unsupported engineering target')
    inputs = (base, reserve, heldout, formulary_path)
    protected = {str(path.resolve()): file_sha256(path) for path in inputs}
    prior, pool, formal, formulary = (json.loads(p.read_text()) for p in inputs)
    if isinstance(formulary, dict):
        formulary = formulary.get('formulary', formulary.get('items'))
    validate_formulary(formulary)

    retained = select_cases(prior)
    excluded = {row['patient_key'] for row in formal['cases']}
    selected = {row['patient_key'] for row in retained}
    counts = Counter(row['group'] for row in retained)
    if (len(selected) != len(retained) or selected & excluded
            or any(row['split'] != 'development' or row['data_kind'] != 'mimic_derived' for row in retained)
            or any(counts[g] >= target for g in GROUPS)):
        raise ValueError('Retained inputs must be distinct development patients below the new target')
    protected.update({row['path']: row['file_sha256'] for row in retained})
    candidates = sorted(pool['cases'],
        key=lambda row: sha256((SALT + '|' + row['patient_key']).encode()).hexdigest())
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix=f'.{output.name}.', dir=output.parent) as staging_name:
        staging = Path(staging_name)
        added = []
        screened = Counter()
        for row in candidates:
            group = row['group']
            if counts[group] >= target or row['patient_key'] in selected | excluded:
                continue
            if row['split'] != 'development' or row['data_kind'] != 'mimic_derived':
                raise ValueError('Reserve input has an unexpected origin or partition')
            source = load_verified_reference(row)
            if not screen(source)['eligible']:
                screened[group] += 1
                continue
            case, source_hashes, _ = prepare_case(source, formulary, reserve.parent.parent)
            if not screen(case)['eligible']:
                screened[group] += 1
                continue
            if case['case_id'] in {r['case_id'] for r in (*retained, *added)}:
                raise ValueError('Duplicate prepared case identity')
            protected.update(source_hashes)
            protected[str(Path(row['path']).resolve())] = row['file_sha256']
            relative = Path('cases') / f"{case['case_id']}.json"
            private_json(staging / relative, case)
            added.append({**{key: case[key] for key in IDENTITY}, 'path': str(output / relative),
                          'file_sha256': file_sha256(staging / relative), 'case_hash': digest(case),
                          'runtime_input_hash': digest(runtime_case(case)), 'formal_eligible': False,
                          'source_case_id': row['case_id'], 'source_file_sha256': row['file_sha256']})
            counts[group] += 1
            selected.add(row['patient_key'])
            if counts == Counter({g: target for g in GROUPS}):
                break
        if counts != Counter({g: target for g in GROUPS}):
            raise ValueError(f'Insufficient input-ready development patients: {dict(counts)}')
        if any(file_sha256(path) != expected for path, expected in protected.items()):
            raise ValueError('A pinned input changed during preparation')
        rows = retained + added
        version = 'engineering_acceptance_overseas_20_v1'
        private_json(staging / 'manifest.json', {'version': version, 'cases': rows,
            'case_count': len(rows), 'formal_eligible': False})
        summary = {'version': version, 'retained': len(retained), 'added': len(added),
                   'per_disease': dict(counts), 'heldout_overlap': 0, 'execution_outcomes_read': False,
                   'source_readiness_exclusions': dict(screened), 'model_calls': 0}
        private_json(staging / 'preparation_receipt.json', {**summary, 'selection_salt': SALT,
            'source_sha256': protected, 'manifest_sha256': file_sha256(staging / 'manifest.json')})
        staging.rename(output)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'reserve', 'heldout', 'formulary-path', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--target', type=int, choices=SUPPORTED_PATIENTS_PER_GROUP, required=True)
    print(json.dumps(prepare(**vars(parser.parse_args()))))
