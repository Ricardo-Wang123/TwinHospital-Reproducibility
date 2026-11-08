"""Pack frozen case inputs and configure a fresh TwinHospital run."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil

from sandbox.experiments.linked.campaign import validate_window
from sandbox.experiments.linked.cases import VERSION, load_case
from sandbox.experiments.linked.provider import check_data_policy
from sandbox.experiments.linked.run_batch import select_cases

from .run import ROOT, RunConfig, read


BUNDLE_VERSION = 'twinhospital.inputs/1'
CASE_KEYS = ('case_id', 'patient_key', 'group', 'split', 'data_kind')
CROSSED_MODES = {'comparison', 'factorial', 'heldout'}
RELEASE_REQUIRED = ('release/health-app.jar', 'openhis-seed/openhis_300_seed.dump',
                    'openhis-seed/20-restore-openhis-seed.sh', 'openhis-seed/run-upgrades.sh',
                    'tools/runtime/provision.py')
CATALOGUES = {
    'catalog': 'catalog.json',
    'diagnosis_catalog': 'diagnosis_catalog.json',
    'clinical_catalogue': 'clinical_catalogue.json',
}


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def write(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def release_hashes(root):
    paths = {root / name for name in RELEASE_REQUIRED}
    paths.update(path for path in (root / 'openhis-seed').glob('*')
                 if path.is_file() and not path.name.startswith('.'))
    paths.update((root / 'tools/runtime').glob('*.py'))
    return {str(path.relative_to(root)): digest(path) for path in sorted(paths)}


def verify_bundle(bundle, release):
    receipt = read(bundle / 'bundle.json')
    if receipt['version'] != BUNDLE_VERSION:
        raise ValueError('Unsupported input bundle.')
    files = receipt['files']
    if 'manifest.json' not in files or any(
        not name.startswith('fixtures/') or name not in files for name in receipt['fixtures']
    ):
        raise ValueError('The input bundle is missing its manifest or a native fixture.')
    for name, expected in files.items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle) or digest(path) != expected:
            raise ValueError('The input bundle failed its file-integrity check.')
    if release != receipt['openhis_release']:
        raise ValueError('The OpenHIS release does not match the input bundle.')
    return receipt


def inputs(manifest, case_files):
    if manifest:
        rows = read(manifest)['cases']
        rows = [{**row, 'path': str(manifest.parent / row['path'])} for row in rows]
    else:
        rows = []
        for path in case_files:
            case = load_case(path)
            rows.append({**{key: case[key] for key in CASE_KEYS}, 'path': str(path)})
    references = select_cases({'cases': rows})
    if len({row['patient_key'] for row in references}) != len(references):
        raise ValueError('A bundle must use one case per source patient.')
    for row in references:
        case = load_case(row['path'])
        if case.get('schema_version') != VERSION or not case.get('formulary'):
            raise ValueError(f'Each case needs schema_version {VERSION} and a nonempty formulary.')
    return references


def pack(args):
    target = args.output.resolve()
    if target.is_relative_to(ROOT):
        raise ValueError('Keep case bundles outside the source checkout.')
    references = inputs(args.manifest, args.case)
    release = release_hashes(args.assets)
    sources = {name: getattr(args, key) for key, name in CATALOGUES.items()}
    sources = {name: path for name, path in sources.items() if path}
    fixtures = [f'fixtures/{i:03}.sql' for i in range(1, len(args.fixture) + 1)]
    sources.update(zip(fixtures, args.fixture))
    if args.workflow_policy:
        sources['workflow_template.json'] = args.workflow_policy
    if args.inherit_native:
        if sources:
            raise ValueError('Use inherited native settings or supply explicit catalogues and fixtures.')
        inherited = args.inherit_native.resolve()
        receipt = verify_bundle(inherited, release)
        fixtures = receipt['fixtures']
        names = [*CATALOGUES.values(), 'workflow_template.json', *fixtures]
        sources = {name: inherited / name for name in names if name in receipt['files']}
    if any(not source.is_file() for source in sources.values()):
        raise ValueError('A catalogue, workflow template, or native fixture file is missing.')

    target.mkdir(parents=True, mode=0o700)
    frozen = []
    for i, row in enumerate(references, 1):
        name = f'cases/{i:04}.json'
        sources[name] = Path(row['path'])
        frozen.append({**row, 'path': name})
    checksums = {}
    for name, source in sources.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        checksums[name] = digest(destination)
    if any(checksums[row['path']] != row['file_sha256'] for row in frozen):
        raise ValueError('A case file changed during bundle preparation.')
    write(target / 'manifest.json', {'cases': frozen, 'case_count': len(frozen)})
    checksums['manifest.json'] = digest(target / 'manifest.json')
    write(target / 'bundle.json', {
        'version': BUNDLE_VERSION, 'files': checksums, 'openhis_release': release,
        'fixtures': fixtures, 'case_count': len(frozen),
    })
    print(json.dumps({'cases': len(frozen), 'fixture_files': len(fixtures), 'model_calls': 0}))


def configure(args):
    bundle = args.bundle.resolve()
    receipt = verify_bundle(bundle, release_hashes(args.assets))
    manifest = read(bundle / 'manifest.json')
    if manifest['case_count'] != receipt['case_count'] or len(manifest['cases']) != receipt['case_count']:
        raise ValueError('Input bundle case count mismatch.')
    for row in manifest['cases']:
        name = row['path']
        if not name.startswith('cases/') or receipt['files'].get(name) != row['file_sha256']:
            raise ValueError('A case is not covered by the input bundle.')
        row['path'] = str((bundle / name).resolve())
    select_cases(manifest)
    if args.frozen_runtime and args.mode != 'factorial':
        raise ValueError('The historical runtime applies to the original factorial design.')
    if args.mode == 'heldout' and not args.development_manifest:
        raise ValueError('The held-out run needs a development manifest in the same identity namespace.')
    if args.development_manifest and args.mode != 'heldout':
        raise ValueError('Development exclusions apply to held-out runs.')
    if args.quick and args.mode != 'comparison':
        raise ValueError('--quick applies to reader comparisons.')

    provider = read(args.provider)
    provider.pop('key_file', None)
    if args.data_policy:
        provider['data_policy'] = read(args.data_policy)
    for kind in {row['data_kind'] for row in manifest['cases']}:
        check_data_policy(kind, provider.get('data_policy', {}), provider['endpoint'], formal=True)

    target = args.output.resolve()
    fields = {
        'mode': args.mode, 'batch': args.batch, 'manifest': 'manifest.local.json',
        'provider_config': 'provider.local.json', 'key_file': 'api.key',
        'openhis_assets': str(args.assets.resolve()), 'deploy': str(ROOT / 'deploy/openhis-experiment'),
        'batches': 'batches',
        'model_evidence': 'model_calls', 'ledger': 'budget.sqlite', 'workers': args.workers or 1,
        'native_fixtures': [str(bundle / name) for name in receipt['fixtures']],
    }
    if args.frozen_runtime:
        fields['frozen_runtime'] = str(args.frozen_runtime.resolve())
    if args.development_manifest:
        fields['development_manifest'] = str(args.development_manifest.resolve())
    if args.quick:
        fields['quick'] = True
    for field, name in CATALOGUES.items():
        if name in receipt['files']:
            fields[field] = str(bundle / name)
        elif args.mode == 'comparison' and field == 'clinical_catalogue':
            fields[field] = str(ROOT / 'deploy/openhis-experiment/catalogue_metadata_v2.json')
        elif args.mode in {'factorial', 'heldout'}:
            raise ValueError(f'The crossed design requires {name}.')
    window = None
    if args.mode in CROSSED_MODES:
        template = bundle / 'workflow_template.json'
        window = read(template if template.name in receipt['files'] else ROOT / 'config/workflow.template.json')
        now = datetime.now(timezone.utc)
        window['frozen_at'] = now.isoformat()
        window['submission_window'].update(opens_at=now.isoformat(),
                                           closes_at=(now + timedelta(hours=args.dispatch_hours)).isoformat())
        window['workers'] = args.workers or (1 if args.mode == 'comparison' else window['workers'])
        validate_window(window, workers=window['workers'], now=now)
        fields.update(workflow_policy='workflow.local.json', patients_per_disease=args.patients_per_disease,
                      workers=window['workers'])
    target.mkdir(parents=True, mode=0o700)
    write(target / 'manifest.local.json', manifest)
    write(target / 'provider.local.json', provider)
    if window is not None:
        write(target / 'workflow.local.json', window)
    if args.mode == 'comparison':
        from sandbox.experiments.linked.source_operation_replay import CATEGORIES

        fields['operation_schedule'] = 'operation_schedule.json'
        source = manifest['cases'][0]
        write(target / fields['operation_schedule'], {'cases': [
            {**source, 'local_test_id': f'{category}-{condition}',
             'operation_category': category, 'request_condition': condition}
            for category in CATEGORIES for condition in ('single_fault', 'compliant_control')
        ]})
    config = target / 'run.local.json'
    write(config, fields)
    RunConfig(config)
    print(f'Prepared {receipt["case_count"]} cases. Run configuration: {config}')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    make = actions.add_parser('pack', help='Freeze a portable, private case bundle.')
    selection = make.add_mutually_exclusive_group(required=True)
    selection.add_argument('--manifest', type=Path)
    selection.add_argument('--case', type=Path, action='append')
    for key in CATALOGUES:
        make.add_argument('--' + key.replace('_', '-'), type=Path)
    make.add_argument('--fixture', action='append', type=Path, default=[])
    make.add_argument('--workflow-policy', type=Path)
    make.add_argument('--inherit-native', type=Path,
                      help='Reuse a verified bundle’s catalogues, fixtures, and workflow policy.')
    make.set_defaults(action_function=pack)
    setup = actions.add_parser('configure', help='Verify a bundle and generate a machine-local configuration.')
    setup.add_argument('--bundle', required=True, type=Path)
    setup.add_argument('--batch', required=True)
    setup.add_argument('--mode', choices=('linked', 'comparison', 'factorial', 'heldout'), default='linked')
    setup.add_argument('--quick', action='store_true', help='Run three predefined whole-visit configurations.')
    setup.add_argument('--provider', type=Path, default=ROOT / 'config/provider.example.json')
    setup.add_argument('--data-policy', type=Path)
    setup.add_argument('--frozen-runtime', type=Path)
    setup.add_argument('--development-manifest', type=Path)
    setup.add_argument('--workers', type=int, choices=range(1, 33),
                       help='Use the input bundle’s recorded workers by default for crossed runs.')
    setup.add_argument('--patients-per-disease', type=int, default=2)
    setup.add_argument('--dispatch-hours', type=int, choices=range(1, 169), default=72)
    setup.set_defaults(action_function=configure)
    for command in (make, setup):
        command.add_argument('--assets', required=True, type=Path)
        command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        args.action_function(args)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()
