"""Inspect the MIMIC-IV source layout or extract selected history locally."""

import argparse
import json
import os
from pathlib import Path
from secrets import token_bytes

from sandbox.experiments.linked.case_preparation.case_contract import build_case, prepare_views
from sandbox.experiments.linked.case_preparation.mimic_reader import extract_seeds, inspect_source

from .inputs import write
from .run import ROOT


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    inspect = commands.add_parser('inspect', help='List tables and columns without reading patient records.')
    extract = commands.add_parser('extract', help='Prepare private historical material for selected admissions.')
    for command in (inspect, extract):
        command.add_argument('--source', required=True, type=Path)
    extract.add_argument('--selection', required=True, type=Path)
    extract.add_argument('--identity-key', required=True, type=Path)
    extract.add_argument('--output', required=True, type=Path)
    extract.add_argument('--without-labs', action='store_true')
    args = parser.parse_args()
    if args.action == 'inspect':
        print(json.dumps(inspect_source(args.source), indent=2))
        return
    output, key = args.output.resolve(), args.identity_key.resolve()
    if output.is_relative_to(ROOT) or key.is_relative_to(ROOT):
        parser.error('Keep patient-derived files and the identity key outside the checkout.')
    if output.exists():
        parser.error('Choose a new historical-starter output directory.')
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with key.open('xb') as stream:
            stream.write(token_bytes(32))
    if key.stat().st_mode & 0o077:
        parser.error('The identity key must be private (mode 0600).')
    secret = key.read_bytes()
    if len(secret) < 32:
        parser.error('The identity key must contain at least 32 bytes.')
    selections = json.loads(args.selection.read_bytes())
    seeds = extract_seeds(args.source, selections, include_labs=not args.without_labs,
                          skip_invalid_history=True)
    output.mkdir(parents=True, mode=0o700)
    for index, (seed, selection) in enumerate(zip(seeds, selections, strict=True), 1):
        folder = output / f'{index:04}'
        folder.mkdir(mode=0o700)
        package = build_case(seed, selection['group'], secret)
        write(folder / 'source_package.json', package)
        write(folder / 'views.json', prepare_views(package))
    print(json.dumps({'historical_packages': len(seeds), 'model_calls': 0,
                      'next_step': 'author the follow-up visit in the runtime case format'}))


if __name__ == '__main__':
    main()
