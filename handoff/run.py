"""Check, start, resume, or audit a frozen TwinHospital experiment."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
from getpass import GetPassWarning, getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import warnings
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from sandbox.experiments.linked.native_his import isolated_port

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_bytes())


class RunConfig:
    def __init__(self, path):
        self.base = path.resolve().parent
        self.fields = read(path)
        self.mode = self.fields['mode']
        if self.mode not in {'linked', 'factorial', 'heldout', 'formal', 'recover'}:
            raise ValueError('Unsupported run mode.')
        self.name = self.fields['batch']
        if not re.fullmatch('[A-Za-z0-9_-]{1,90}', self.name):
            raise ValueError('Invalid batch name.')
        port = isolated_port()
        self.his_url = self.fields.get('his_url', f'http://127.0.0.1:{port}/api/app')
        his = urlsplit(self.his_url)
        if (his.scheme != 'http' or his.hostname != '127.0.0.1' or his.path != '/api/app'
                or his.username or his.query or his.fragment or his.port != port):
            raise ValueError(f'The HIS URL must be http://127.0.0.1:{port}/api/app.')

    def path(self, key, default=None):
        value = self.fields.get(key, default)
        if value is None:
            raise ValueError(f'Missing private configuration field: {key}')
        return (self.base / Path(value).expanduser()).resolve()

    @property
    def batch(self):
        return self.path('batches') / self.name

    @property
    def deploy(self):
        return self.path('deploy', ROOT / 'deploy/openhis-experiment')

    @property
    def runtime(self):
        return self.path('frozen_runtime', ROOT)

    @property
    def credentials(self):
        return self.path('his_credentials', self.deploy / 'runtime/accounts/credentials.json')

    def command(self, provider):
        command = [sys.executable, '-m', 'sandbox.experiments.linked.run_batch',
                   '--manifest', self.path('manifest'), '--provider-config', provider,
                   '--budget-ledger', self.path('ledger'), '--model-evidence', self.path('model_evidence'),
                   '--batch', self.name, '--output', self.path('batches'),
                   '--catalog', self.path('catalog', self.deploy / 'catalog_mapping.json'),
                   '--diagnosis-catalog', self.path('diagnosis_catalog', self.deploy / 'diagnosis_mapping.json'),
                   '--his-credentials', self.credentials, '--his-url', self.his_url,
                   '--workers', str(self.fields.get('workers', 1)), '--close-prescription-loop']
        if self.fields.get('uncapped') is True:
            command += ['--uncapped-budget']
        if self.mode in {'factorial', 'heldout', 'recover', 'formal'}:
            command += ['--capture-native', '--observation-deploy', self.deploy,
                        '--workflow-policy', self.path('workflow_policy'),
                        '--rule-engine', 'documented-candidate',
                        '--clinical-catalogue', self.path('clinical_catalogue'),
                        '--acceptance-patients-per-disease', str(self.fields.get('patients_per_disease', 2)),
                        '--acceptance-factorial']
        if self.mode in {'factorial', 'recover'}:
            command += ['--mode', 'linked-acceptance', '--engineering-only', '--acceptance-attempts',
                        str(self.fields.get('acceptance_attempts', 3))]
        elif self.mode == 'heldout':
            command += ['--mode', 'linked-acceptance', '--engineering-only',
                        '--independent-crossed', '--acceptance-attempts', '1',
                        '--exclude-development-manifest', self.path('development_manifest')]
        elif self.mode == 'formal':
            command += ['--mode', 'matrix', '--formal-experiment', '--analysis-plan', self.path('analysis_plan'),
                        '--acceptance-batch', self.path('acceptance_batch'), '--acceptance-audit', self.path('acceptance_audit')]
        else:
            command += ['--mode', 'linked', '--engineering-only']
        if self.mode == 'recover':
            command += ['--acceptance-recovery-plan', self.path('recovery_plan')]
        return [str(x) for x in command]


def save_key(path):
    if path.is_relative_to(ROOT):
        ignored = subprocess.run(['git', '-C', ROOT, 'check-ignore', '--quiet', '--', path])
        if ignored.returncode != 0:
            raise ValueError('Store the API key outside the checkout or in a Git-ignored path.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', GetPassWarning)
        try:
            key = getpass('API key（输入不会显示）: ').strip()
        except GetPassWarning:
            raise ValueError('Use an interactive terminal for hidden API key entry.') from None
    if not key or any(c.isspace() for c in key):
        raise ValueError('Enter a single nonempty API key.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.key-', suffix='.key', delete=False, mode='w') as stream:
        tmp = Path(stream.name)
        stream.write(key + '\n')
    os.replace(tmp, path)
    print('API key saved locally with mode 0600.')


def require_private_file(path):
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError(f'A local file with mode 0600 is required: {path.name}')


def preflight(config, command, log_path):
    pinned = None
    if 'frozen_runtime' in config.fields:
        pinned = read(ROOT / 'results/cohort_170_execution.json')
        revision = subprocess.check_output(
            ['git', '-C', str(config.runtime), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(
            ['git', '-C', str(config.runtime), 'status', '--porcelain'], text=True)
        if revision != pinned['runtime_commit'] or dirty:
            raise ValueError('Check out a clean cohort-170-runtime worktree.')
    output = subprocess.run([*command, '--dry-run'], cwd=config.runtime, text=True, capture_output=True)
    if output.returncode:
        log_path.write_text(output.stderr, encoding='utf-8')
        raise ValueError(f'Runner preflight failed; see private log {log_path}.')
    receipt = json.loads(output.stdout)
    spec = receipt['specification']
    if pinned:
        case_digest = sha256('\n'.join(row['file_sha256'] for row in spec['cases']).encode()).hexdigest()
        for observed, expected in (
            (case_digest, pinned['ordered_case_files_sha256']),
            (spec['code_hashes'], pinned['linked_code_sha256']),
            (spec['catalog_hash'], pinned['catalog_hash']),
            (spec['diagnosis_catalog_hash'], pinned['diagnosis_catalog_hash']),
            (spec['rules']['source_catalogue_file_sha256'], pinned['clinical_catalogue_file_sha256']),
        ):
            if observed != expected:
                raise ValueError('The selected case files, runtime or dictionaries differ from the frozen cohort.')
        assignment = {key: spec['models'][key] for key in pinned['model_assignment']}
        generation = spec['generation']
        if (spec['generation_policy'] != pinned['generation_policy']
                or assignment != pinned['model_assignment']
                or generation['max_attempts'] != pinned['max_transport_attempts']
                or generation['max_calls_per_run'] != pinned['max_model_calls_per_run']):
            raise ValueError('The model assignment or wire settings differ from the frozen cohort.')
        transport = {key: spec['transport_policy'][key] for key in pinned['transport_settings']}
        workflow = {key: spec['workflow_observation_policy'][key] for key in pinned['workflow_settings']}
        if transport != pinned['transport_settings'] or workflow != pinned['workflow_settings']:
            raise ValueError('The transport or observation settings differ from the frozen cohort.')
    recovery = spec.get('engineering_recovery')
    result = {'batch': config.name, 'mode': config.mode, 'input_cases': len(spec['cases']),
              'role_model': spec['models']['role_model'], 'tool_models': spec['models']['tool_models'],
              'new_model_calls': 0, 'new_his_writes': 0}
    if recovery:
        result['retained_paid'] = recovery['retained_verified_paid']
        result['unpaid_to_dispatch'] = recovery['new_assignment_count']
    if spec.get('linked_acceptance'):
        result['planned_encounters'] = spec['linked_acceptance']['planned_unique_encounters']
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return spec


def verify_his(config):
    require_private_file(config.credentials)
    health = config.his_url.removesuffix('/api/app') + '/actuator/health'
    try:
        with build_opener(ProxyHandler({})).open(health, timeout=10) as response:
            ok = response.status == 200 and json.load(response).get('status') == 'UP'
    except Exception:
        ok = False
    if not ok:
        raise ValueError('The isolated OpenHIS health check did not return UP.')


def provision_his(config):
    manage = config.deploy / 'manage.py'
    fixtures = [config.base / path for path in config.fields.get('native_fixtures', [])]
    if any(not path.is_file() for path in fixtures):
        raise ValueError('A configured native fixture is missing.')
    subprocess.run([sys.executable, manage, 'up'], check=True)
    command = [sys.executable, manage, 'provision']
    for fixture in fixtures:
        command.extend(('--fixture', fixture))
    subprocess.run(command, check=True)
    verify_his(config)


def verify_routes(config, provider, provider_file, receipt, *, recover=True):
    from sandbox.experiments.linked.provider import BudgetLedger

    models = list(dict.fromkeys([provider['role_model'], *provider['tool_models'].values()]))
    ledger = BudgetLedger(config.path('ledger'), cap_cny=None if config.fields.get('uncapped') is True else 1000)
    if ledger.summary()['new_requests_stopped'] and not ledger.summary()['provider_unavailable']:
        raise ValueError('The cost ledger has a non-provider stop. Inspect it before launching.')
    command = [sys.executable, 'tools/probe_linked_provider_recovery.py', '--config', str(provider_file),
               '--ledger', str(config.path('ledger')), '--model-evidence', str(config.path('model_evidence')),
               '--output', str(receipt), '--run-id', config.name + '-health']
    if config.fields.get('uncapped') is True:
        command += ['--uncapped-budget']
    for model in models:
        command += ['--model', model]
    output = subprocess.run(command, cwd=config.runtime, capture_output=True, text=True)
    path = receipt / 'summary.json'
    if not path.is_file():
        (receipt.parent / 'probe-error.log').write_text(output.stderr, encoding='utf-8')
        raise ValueError('Provider probe did not finish. Inspect its private receipt folder.')
    report = read(path)
    rows = report['rows']
    print(json.dumps({'nonclinical_probes': [{k: row[k] for k in ('model','status','http_status') if k in row}
                                           for row in rows]}, ensure_ascii=False), flush=True)
    if output.returncode or len(rows) != len(models) or any(row['status'] != 'completed' for row in rows):
        if any(row.get('http_status') == 401 for row in rows):
            raise ValueError('The provider rejected the API credential (HTTP 401). Dispatch is blocked.')
        raise ValueError('At least one configured model route failed. No cases were dispatched.')
    if recover and ledger.summary()['provider_unavailable']:
        ledger.recover_provider(recovery_id=receipt.parent.name, config_sha256=sha256(provider_file.read_bytes()).hexdigest(),
                                probes_by_model={row['model']: row['request_id'] for row in rows},
                                evidence={'probe_receipt_sha256': sha256(path.read_bytes()).hexdigest()}, config=provider)


def audit(config, observe):
    output = config.path('audit_output', config.path('batches') / (config.name + '_audit'))
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    command = [sys.executable, 'tools/audit_linked_workflows.py', '--batch-dir', str(config.batch),
               '--public-dir', str(output / 'content_minimized'), '--private-path', str(output / 'audit.private.json'),
               '--deployment-dir', str(config.deploy)]
    if observe:
        verify_his(config)
        command += ['--observe-native']
    with (output/'audit.log').open('x') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    print(f'Audit stored locally: {output}')


def status(config):
    path = config.batch / 'batch_summary.json'
    if not path.is_file():
        print(json.dumps({'batch': config.name, 'status': 'no_closed_summary'}))
        return
    report = read(path)
    rows = [r for case in report['cases'] for r in case.get('attempts', {}).values()]
    print(json.dumps({'batch': config.name, 'assignment_statuses': dict(Counter(row['status'] for row in rows)),
                      'runtime_native_complete': sum(row.get('native_complete') is True for row in rows),
                      'stopped_provider': report.get('stopped_provider'),
                      'native_observation_status': (report.get('native_observation') or {}).get('status'),
                      'note': 'Verify paid outcomes using the native audit.'}, ensure_ascii=False))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('check', help='Validate inputs and recoverable assignments without case dispatch.')
    actions.add_parser('set-key', help='Save a key in the local configured 0600 file.')
    actions.add_parser('probe', help='Send nonclinical model health probes without provisioning or dispatch.')
    actions.add_parser('provision', help='Start isolated OpenHIS and apply this input bundle’s fixtures.')
    actions.add_parser('status', help='Show closed-batch counts without printing case data.')
    launch = actions.add_parser('run', help='Preflight by default; use --execute to launch.')
    launch.add_argument('--execute', action='store_true')
    audit_parser = actions.add_parser('audit', help='Analyze the closed batch; output stays local.')
    audit_parser.add_argument('--observe-native', action='store_true')
    args = parser.parse_args()
    cfg = RunConfig(args.config)
    if 'openhis_assets' in cfg.fields:
        os.environ['TH_OPENHIS_ASSETS'] = str(cfg.path('openhis_assets'))
    if args.action == 'provision':
        return provision_his(cfg)
    if args.action == 'set-key':
        return save_key(cfg.path('key_file'))
    if args.action == 'audit':
        return audit(cfg, args.observe_native)
    if args.action == 'status':
        return status(cfg)
    live = args.action == 'run' and args.execute
    state = cfg.path('launcher_state', cfg.path('ledger').parent / 'handoff_runs')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    session = state / f'{cfg.name}_{stamp}'
    session.mkdir(parents=True, exist_ok=False, mode=0o700)
    provider = read(cfg.path('provider_config'))
    provider['key_file'] = str(cfg.path('key_file'))
    provider_file = session / 'provider.local.json'
    provider_file.write_text(json.dumps(provider, ensure_ascii=False) + '\n', encoding='utf-8')
    command = cfg.command(provider_file)
    if not live and args.action != 'probe':
        preflight(cfg, command, session/'preflight.log')
        return

    with (state / 'dispatch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'probe':
            require_private_file(cfg.path('key_file'))
            return verify_routes(cfg, provider, provider_file, session/'probe', recover=False)
        if cfg.batch.exists():
            raise ValueError('Destination batch exists. Use a new audited recovery batch; never overwrite existing runs.')
        preflight(cfg, command, session/'preflight.log')
        require_private_file(cfg.path('key_file'))
        verify_his(cfg)
        if cfg.mode in {'recover','factorial','heldout','formal'}:
            from sandbox.experiments.linked.campaign import admission_status
            from sandbox.experiments.linked.native_observation import IsolatedObservationDB

            if admission_status(read(cfg.path('workflow_policy'))) != 'open':
                raise ValueError('The configured dispatch window is not open.')
            db = IsolatedObservationDB(cfg.deploy)
            if db.sql("SELECT to_regclass('th_observation_v1.control') IS NOT NULL;") == 't':
                control = db.control()
                if control.get('session_id') or control.get('quarantined'):
                    raise ValueError('Another observation session owns the instance, or it is quarantined.')
        verify_routes(cfg, provider, provider_file, session/'probe')
        print(f'Running the frozen batch. Private log: {session / "execution.log"}', flush=True)
        with (session / 'execution.log').open('x') as log:
            subprocess.run(command, cwd=cfg.runtime, stdout=log, stderr=subprocess.STDOUT, check=True)
        if not (cfg.batch/'batch_summary.json').is_file():
            raise ValueError('Batch closed without a summary; inspect its private log.')
        print(f'Batch summary: {cfg.batch / "batch_summary.json"}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
