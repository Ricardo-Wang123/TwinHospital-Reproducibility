"""Freeze one independent fictional visit for the installed demo formulary."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

from sandbox.experiments.linked.case_preparation.synthetic_execution_v2 import build_execution_case
from sandbox.experiments.linked.cases import FORMULARY
from sandbox.experiments.linked.contracts import digest, runtime_case


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    case = build_execution_case('type2_diabetes', 100101, FORMULARY)
    path = output / 'case.json'
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    row = {key: case[key] for key in ('case_id','patient_key','data_kind','group','split')}
    row.update(path=str(path), file_sha256=sha256(path.read_bytes()).hexdigest(),
               case_hash=digest(case), runtime_input_hash=digest(runtime_case(case)))
    (output/'manifest.json').write_text(json.dumps({'cases':[row]}, indent=2)+'\n', encoding='utf-8')
    print('Prepared one independent fictional case. No patient data or model calls.')


if __name__ == '__main__':
    main()
