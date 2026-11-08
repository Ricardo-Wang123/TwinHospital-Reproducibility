"""Pack and recompute completed encounter evidence on an authorized local host."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from results.render_linked_manuscript import render
from results.summarize_paid_runs import summarize

from .inputs import digest, write
from .run import ROOT, read


VERSION = 'twinhospital.paid-evidence/1'
SIDECARS = ('audit.json', 'human_review.json', 'operation_replays.json')


def private_output(path):
    path = path.resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('Keep encounter evidence and its outputs outside the source checkout.')
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def contained_file(root, value, *, portable=False):
    name = Path(value)
    if portable and name.is_absolute():
        raise ValueError('Evidence index paths must be relative to the bundle.')
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('An indexed file is missing or outside its evidence directory.')
    return path


def paid_index(batch):
    for case in read(batch / 'batch_summary.json')['cases']:
        for item in case['attempts'].values():
            if item['status'] == 'completed':
                yield case['case_id'], item


def calculate(bundle):
    audit, human_review, probes = (read(bundle / name) for name in SIDECARS)
    spec_hash = digest(bundle / 'batch/specification.json')
    if audit.get('audit', audit).get('source_specification_sha256') != spec_hash:
        raise ValueError('The evidence requires a native audit bound to the batch specification.')
    manifest = bundle / 'inputs/manifest.json'
    for row in read(manifest)['cases']:
        contained_file(manifest.parent, row['path'], portable=True)
    batch = bundle / 'batch'
    for _, row in paid_index(batch):
        contained_file(batch, row['result_path'], portable=True)
    metrics = summarize(batch, bundle / 'audit.json', manifest,
                        bundle / 'inputs/clinical_catalogue.json')
    data = (json.dumps(metrics, indent=2) + '\n').encode()
    tables = render(metrics, audit, human_review, probes).encode()
    return metrics, data, tables


def output_checks(metrics, data, tables):
    return {'verified_paid': metrics['verified_paid'],
            'paid_cohort_sha256': metrics['paid_cohort_sha256'],
            'metrics_sha256': sha256(data).hexdigest(),
            'tables_sha256': sha256(tables).hexdigest()}


def pack(args):
    target = private_output(args.output)
    batch, inputs = args.batch.resolve(), args.inputs.resolve()
    with TemporaryDirectory(dir=target.parent, prefix='.evidence-') as staging:
        bundle = Path(staging) / 'bundle'
        (bundle / 'batch/runs').mkdir(parents=True, mode=0o700)
        (bundle / 'inputs/cases').mkdir(parents=True, mode=0o700)
        copyfile(batch / 'specification.json', bundle / 'batch/specification.json')
        copyfile(inputs / 'clinical_catalogue.json', bundle / 'inputs/clinical_catalogue.json')
        for name, source in zip(SIDECARS, (args.audit, args.human_review, args.probes), strict=True):
            copyfile(source, bundle / name)

        index = {}
        for number, (case_id, item) in enumerate(paid_index(batch), 1):
            name = f'runs/{number:04}.json'
            copyfile(contained_file(batch, item['result_path']), bundle / 'batch' / name)
            index.setdefault(case_id, {})[str(number)] = {'status': 'completed', 'result_path': name}
        write(bundle / 'batch/batch_summary.json', {
            'cases': [{'case_id': case, 'attempts': attempts} for case, attempts in index.items()],
        })

        manifest = read(inputs / 'manifest.json')
        for number, row in enumerate(manifest['cases'], 1):
            source = contained_file(inputs, row['path'], portable=True)
            row['path'] = f'cases/{number:04}.json'
            copyfile(source, bundle / 'inputs' / row['path'])
        write(bundle / 'inputs/manifest.json', manifest)

        checks = output_checks(*calculate(bundle))
        files = {p.relative_to(bundle).as_posix(): digest(p)
                 for p in sorted(bundle.rglob('*')) if p.is_file()}
        write(bundle / 'bundle.json', {'version': VERSION, 'files': files, **checks})
        bundle.rename(target)
    print(f"Packed evidence for {checks['verified_paid']} verified paid encounters.")


def recompute(args):
    bundle = args.bundle.resolve()
    receipt = read(bundle / 'bundle.json')
    if receipt['version'] != VERSION:
        raise ValueError('Unsupported evidence bundle.')
    for name, expected in receipt['files'].items():
        if digest(contained_file(bundle, name, portable=True)) != expected:
            raise ValueError('An evidence file differs from the bundle checksum.')
    metrics, data, tables = calculate(bundle)
    checks = output_checks(metrics, data, tables)
    if any(receipt.get(name) != value for name, value in checks.items()):
        raise ValueError('Recomputed outputs differ from the frozen evidence receipt.')
    target = private_output(args.output)
    target.mkdir(mode=0o700)
    (target / 'verified_paid_results.json').write_bytes(data)
    (target / 'acl_results.tex').write_bytes(tables)
    print(f"Recomputed {metrics['verified_paid']} verified paid encounters and their paper tables.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    producer = commands.add_parser('pack', help='Create a private portable evidence package.')
    for name in ('batch', 'audit', 'inputs', 'human-review', 'probes', 'output'):
        producer.add_argument('--' + name, type=Path, required=True)
    producer.set_defaults(action_function=pack)
    receiver = commands.add_parser('recompute', help='Verify a received bundle and regenerate the results.')
    receiver.add_argument('--bundle', type=Path, required=True)
    receiver.add_argument('--output', type=Path, required=True)
    receiver.set_defaults(action_function=recompute)
    args = parser.parse_args()
    try:
        args.action_function(args)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()
