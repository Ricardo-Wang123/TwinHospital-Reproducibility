"""Write editable fictional visits for the included OpenHIS formulary."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

from sandbox.experiments.linked.case_preparation.synthetic_execution_v2 import PRIOR_PLANS, build_execution_case
from sandbox.experiments.linked.case_preparation.anthropometry import author
from sandbox.experiments.linked.cases import FORMULARY, GROUPS
from sandbox.experiments.linked.contracts import digest, runtime_case


EXAMPLE_PRIOR_PLANS = {
    **PRIOR_PLANS,
    'copd': {'drug_code': 'SIM-SALBUTAMOL-100', 'dose': 2, 'dose_unit': 'puff', 'route': 'inhaled', 'frequency': 'QID'},
}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--all-groups', action='store_true', help='Write one fictional case per diagnosis stratum.')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    groups = GROUPS if args.all_groups else ('type2_diabetes',)
    if args.all_groups:
        (output / 'cases').mkdir()
    rows = []
    for group in groups:
        case = build_execution_case(group, 100101, FORMULARY, prior_plans=EXAMPLE_PRIOR_PLANS)
        case, _ = author(case, allow_reference_scenario=True)
        path = output / (f'cases/{group}.json' if args.all_groups else 'case.json')
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        row = {key: case[key] for key in ('case_id', 'patient_key', 'data_kind', 'group', 'split')}
        row.update(path=path.relative_to(output).as_posix(), file_sha256=sha256(path.read_bytes()).hexdigest(),
                   case_hash=digest(case), runtime_input_hash=digest(runtime_case(case)))
        rows.append(row)
    (output / 'manifest.json').write_text(json.dumps({'cases': rows}, indent=2) + '\n', encoding='utf-8')
    print(f'Prepared {len(rows)} independently constructed cases.')


if __name__ == '__main__':
    main()
