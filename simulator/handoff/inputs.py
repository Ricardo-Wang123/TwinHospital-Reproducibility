"""Pack frozen case inputs and configure a fresh TwinHospital run."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil

from sandbox.experiments.linked.campaign import validate_window
from sandbox.experiments.linked.cases import READABLE_CASE_VERSIONS, load_case
from sandbox.experiments.linked.cases import VERSION as NATIVE_CASE_VERSION
from sandbox.experiments.linked.provider import check_data_policy
from sandbox.experiments.linked.run_batch import select_cases

from .run import ROOT, RunConfig, read


BUNDLE_VERSION = 'twinhospital.inputs/1'
CASE_KEYS = ('case_id', 'patient_key', 'group', 'split', 'data_kind')
CROSSED_MODES = {'comparison', 'factorial', 'heldout', 'repeat'}
OPERATION_SCHEDULE = 'operation_schedule.json'
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
        if (case.get('schema_version', NATIVE_CASE_VERSION) not in READABLE_CASE_VERSIONS
                or not (case.get('formulary') or case.get('formulary_directory'))):
            raise ValueError('Each case needs a supported version and a medication catalogue.')
        if (case['data_kind'] == 'mimic_derived' and case.get('encounter_setting') == 'emergency'
                and (not case.get('formulary_directory') or case.get('formulary'))):
            raise ValueError('MIMIC ED cases must use the shared source medication directory.')
    return references


def pack(args):
    target = args.output.resolve()
    if target.is_relative_to(ROOT):
        raise ValueError('Keep case bundles outside the source checkout.')
    manifest = args.manifest
    if manifest is None and not args.case:
        data = ROOT.parent / 'data'
        dataset = data / 'ED-100-aligned'
        if not dataset.is_dir():
            dataset = data / 'ED-100'
        manifest = dataset / 'manifest.json'
        if not args.inherit_native:
            deploy = ROOT / 'deploy' / 'openhis-experiment'
            args.diagnosis_catalog = args.diagnosis_catalog or deploy / 'diagnosis_mapping.json'
    references = inputs(manifest, args.case)
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
    if any('formulary_directory' in ref for ref in references):
        from sandbox.experiments.linked.formulary_search import FILENAME, directory_for_case
        from sandbox.experiments.linked.contracts import digest as content_hash
        directories = [directory_for_case(ref, ref['path']) for ref in references]
        directory = directories[0]
        if not directory or any(not d or d.descriptor != directory.descriptor for d in directories):
            raise ValueError('A bundle must share one frozen medication directory')
        parent = Path(references[0]['path']).parent.parent
        sources[FILENAME] = parent / FILENAME
        sources.setdefault('catalog.json', parent / 'catalog.json')
        mapping = read(sources['catalog.json'])
        if content_hash(mapping) != directory.mapping_hash or set(mapping) != set(directory.products):
            raise ValueError('The native product map does not match the medication directory')
        if any(ref['data_kind'] == 'mimic_derived' and ref['encounter_setting'] == 'emergency' for ref in references):
            from .mimic_formulary import fixture

            directory.require_mimic_products()
            if not fixtures:
                fixtures = ['fixtures/001.sql']
                sources[fixtures[0]] = parent / 'products.sql'
            if (len(fixtures) != 1 or not sources[fixtures[0]].is_file()
                    or sources[fixtures[0]].read_text() != fixture(directory.products.values(), mapping)):
                raise ValueError('MIMIC ED bundles require the product fixture from their medication directory.')
    if any(not source.is_file() for source in sources.values()):
        raise ValueError('A catalogue, workflow template, or native fixture file is missing.')

    frozen = []
    for i, row in enumerate(references, 1):
        name = f'cases/{i:04}.json'
        sources[name] = Path(row['path'])
        frozen.append({**row, 'path': name})
    target.mkdir(parents=True, mode=0o700)
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
    scenario_design = args.scenario_design
    bundle = args.bundle.resolve()
    receipt = verify_bundle(bundle, release_hashes(args.assets))
    manifest = read(bundle / 'manifest.json')
    if manifest['case_count'] != receipt['case_count'] or len(manifest['cases']) != receipt['case_count']:
        raise ValueError('Input bundle case count mismatch.')
    for row in manifest['cases']:
        name = row['path']
        if not name.startswith('cases/') or receipt['files'].get(name) != row['file_sha256']:
            raise ValueError('A case is not covered by the input bundle.')
    from sandbox.experiments.linked.scenario_design import design_for
    if scenario_design is None and manifest['cases']:
        scenario_design = design_for(manifest['cases'][0])
    if any(design_for(row) != scenario_design for row in manifest['cases']):
        raise ValueError('The selected design must match the setting of all bundled cases.')
    portable = manifest['cases']
    manifest['cases'] = [{**row, 'path': str((bundle / row['path']).resolve())} for row in portable]
    select_cases(manifest)
    if args.mode == 'heldout' and not args.development_manifest:
        raise ValueError('The held-out run needs a development manifest in the same identity namespace.')
    if args.development_manifest and args.mode != 'heldout':
        raise ValueError('Development exclusions apply to held-out runs.')
    if args.quick and args.mode != 'comparison':
        raise ValueError('--quick applies to reader comparisons.')
    assignment_plan = getattr(args, 'assignment_plan', None)
    if (args.mode == 'repeat') != (assignment_plan is not None):
        raise ValueError('--mode repeat requires --assignment-plan; other modes do not use it.')
    if OPERATION_SCHEDULE in receipt['files']:
        raise ValueError('An outpatient case bundle contains cases, catalogues and fixtures only.')

    provider = read(args.provider)
    provider.pop('key_file', None)
    if args.data_policy:
        provider['data_policy'] = read(args.data_policy)
    for kind in {row['data_kind'] for row in manifest['cases']}:
        check_data_policy(kind, provider.get('data_policy', {}), provider['endpoint'], formal=True)
    if assignment_plan is not None:
        from sandbox.experiments.linked.assignment_repeat import from_file
        from sandbox.experiments.linked.linked_acceptance import freeze_design, linked_profiles

        profiles, _ = linked_profiles(provider['tool_models'], factorial=True, scenario_design=scenario_design)
        design = freeze_design(manifest['cases'], provider['tool_models'],
                               patients_per_group=args.patients_per_disease,
                               workers=args.workers or 1, max_attempts=1, factorial=True, scenario_design=scenario_design)
        from_file(design, assignment_plan, manifest['cases'], profiles)

    target = args.output.resolve()
    fields = {
        'mode': args.mode, 'batch': args.batch, 'scenario_design': scenario_design,
        'his_interface': args.his_interface, 'manifest': 'manifest.local.json',
        'provider_config': 'provider.local.json', 'key_file': 'api.key',
        'openhis_assets': str(args.assets.resolve()), 'deploy': str(ROOT / 'deploy/openhis-experiment'),
        'batches': 'batches',
        'model_evidence': 'model_calls', 'ledger': 'budget.sqlite', 'workers': args.workers or 1,
        'native_fixtures': [str(bundle / name) for name in receipt['fixtures']],
    }
    if args.development_manifest:
        fields['development_manifest'] = str(args.development_manifest.resolve())
    if args.quick:
        fields['quick'] = True
    for field, name in CATALOGUES.items():
        if name in receipt['files']:
            fields[field] = str(bundle / name)
        elif field == 'clinical_catalogue':
            continue
        elif args.mode in {'factorial', 'heldout', 'repeat'}:
            raise ValueError(f'The crossed design requires {name}.')
    window = None
    if args.mode in CROSSED_MODES:
        template = bundle / 'workflow_template.json'
        window = read(template if template.name in receipt['files'] else ROOT / 'config/workflow.template.json')
        if scenario_design == 'emergency-v1' and template.name not in receipt['files']:
            from sandbox.experiments.linked.emergency import WINDOW_END, WINDOW_CONSTRAINTS
            window.update(scenario_design=scenario_design, end_boundary=WINDOW_END, constraints=list(WINDOW_CONSTRAINTS))
            window['resources'].pop('cash_settlement', None)
        if window.get('scenario_design', scenario_design) != scenario_design:
            raise ValueError('Observation window and case setting differ.')
        if scenario_design == 'emergency-v1':
            window['scenario_design'] = scenario_design
            window.setdefault('ed_prescribing_task', 'select_and_sign_regimen')
            window['resources']['model_endpoint'] = provider['endpoint'].rstrip('/')
        now = datetime.now(timezone.utc)
        window['frozen_at'] = now.isoformat()
        window['submission_window'].update(opens_at=now.isoformat(),
                                           closes_at=(now + timedelta(hours=args.dispatch_hours)).isoformat())
        window['workers'] = args.workers or (1 if args.mode == 'comparison' else window['workers'])
        if window['workers'] > 5:
            provider.setdefault('minimum_request_interval_seconds', 1.0)
        validate_window(window, workers=window['workers'], now=now, scenario_design=scenario_design)
        fields.update(workflow_policy='workflow.local.json', patients_per_disease=args.patients_per_disease,
                      workers=window['workers'])
    target.mkdir(parents=True, mode=0o700)
    write(target / 'manifest.local.json', manifest)
    write(target / 'provider.local.json', provider)
    if assignment_plan is not None:
        fields['assignment_plan'] = 'assignment_plan.json'
        shutil.copyfile(assignment_plan, target / fields['assignment_plan'])
    if window is not None:
        write(target / 'workflow.local.json', window)
    config = target / 'run.local.json'
    write(config, fields)
    RunConfig(config)
    print(f'Prepared {receipt["case_count"]} cases. Run configuration: {config}')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    make = actions.add_parser('pack', help='Freeze a portable, private case bundle.')
    selection = make.add_mutually_exclusive_group()
    selection.add_argument('--manifest', type=Path, help='Cohort manifest (default: data/ED-100/manifest.json).')
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
    setup.add_argument('--mode', choices=('linked', 'comparison', 'factorial', 'heldout', 'repeat'), default='linked')
    setup.add_argument('--scenario-design', choices=('outpatient-v2', 'emergency-v1'), help='Defaults to the setting of the frozen cases.')
    setup.add_argument('--assignment-plan', type=Path,
                       help='Repeat the exact case/configuration pairs of a prior audited assignment plan.')
    setup.add_argument('--quick', action='store_true', help='Run three predefined whole-visit configurations.')
    setup.add_argument('--provider', type=Path, default=ROOT / 'config/provider.example.json')
    setup.add_argument('--data-policy', type=Path)
    setup.add_argument('--his-interface', choices=('native', 'fhir-r4', 'wst-846'), default='fhir-r4')
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
