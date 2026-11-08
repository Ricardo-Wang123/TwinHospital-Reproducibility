#!/usr/bin/env python3
"""Manage the isolated experiment project without touching another deployment."""
import argparse, importlib.util, subprocess, sys, os, re, secrets
from functools import cache
from pathlib import Path
HERE=Path(__file__).resolve().parent
PROJECT=os.environ.get('TH_OPENHIS_PROJECT','twinhospital-exp')
if not re.fullmatch(r'twinhospital-exp(?:-smoke-[a-z0-9-]+)?',PROJECT):
    raise ValueError('Only the isolated experiment or a dedicated smoke project is permitted')
COMPOSE=['docker','compose','-p',PROJECT,'--env-file',str(HERE/'runtime/secrets.env'),'-f',str(HERE/'compose.yaml')]

def assets():
    value=os.environ.get('TH_OPENHIS_ASSETS')
    if not value:
        raise ValueError('Set TH_OPENHIS_ASSETS to the authorized OpenHIS release directory')
    path=Path(value).expanduser().resolve()
    for name in ('openhis-seed/openhis_300_seed.dump','openhis-seed/run-upgrades.sh',
                 'openhis-seed/20-restore-openhis-seed.sh','release/health-app.jar','tools/runtime/provision.py'):
        if not (path/name).is_file():raise FileNotFoundError('Missing OpenHIS release asset: '+name)
    os.environ['TH_OPENHIS_ASSETS']=str(path)
    return path

@cache
def check_project_ownership():
    ids=subprocess.check_output(['docker','ps','-aq','--filter',f'label=com.docker.compose.project={PROJECT}'],text=True).split()
    if ids:
        current=subprocess.check_output(['docker','inspect','--format',
            '{{ index .Config.Labels "com.docker.compose.project.config_files" }}',*ids],text=True).splitlines()
        if any(Path(name).resolve()!=HERE/'compose.yaml' for name in current):
            raise RuntimeError('The experiment project name belongs to another deployment. Use its original directory.')

def ensure_secrets():
    runtime=HERE/'runtime'; runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=runtime/'secrets.env'
    if target.exists():
        if target.stat().st_mode & 0o077:raise RuntimeError('Private secrets.env must have mode 0600')
        return
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:f.write('DB_PASSWORD='+secrets.token_urlsafe(32)+'\nJWT_SECRET='+secrets.token_urlsafe(64)+'\n')
def sql(args=None, text=None):
    check_project_ownership()
    return subprocess.run(COMPOSE+['exec','-T','postgres','psql','-X','-q','-U','postgres','-d','openhis_205_local','-v','ON_ERROR_STOP=1']+(args or []), input=text,text=True,capture_output=True,check=True)
def provision():
    source=assets()
    sys.path.insert(0,str(source/'tools/runtime'))
    sp=importlib.util.spec_from_file_location('isolated_provision',source/'tools/runtime/provision.py'); mod=importlib.util.module_from_spec(sp); sp.loader.exec_module(mod)
    mod.ROOT=HERE
    mod.sql_command=lambda: COMPOSE+['exec','-T','postgres','psql','-X','-U','postgres','-d','openhis_205_local','-v','ON_ERROR_STOP=1']
    sys.argv=['provision','--daily']; mod.main()
    for name in ['90-live-demo.sql','94-realistic-organizations.sql','95-realistic-personas.sql','96-natural-logins.sql','97-record-display-details.sql','98-workbench-display-details.sql','99-catalog-display-details.sql','99-curated-business-scenarios.sql','99-data-integrity.sql','100-hospital-departments.sql']:
        sql(text=(source/'openhis-seed'/name).read_text())
    sql(text=(HERE/'experiment-fixtures.sql').read_text())
    print('Isolated role identities and daily synthetic schedule ready; existing desktop runtime untouched.')
def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['up','status','provision','catalog-v2','stop']); a=p.parse_args()
    assets(); check_project_ownership(); ensure_secrets()
    if a.action=='up':subprocess.run(COMPOSE+['up','-d','--wait','postgres','backend','gateway'],check=True)
    elif a.action=='status':subprocess.run(COMPOSE+['ps'],check=True)
    elif a.action=='provision':provision()
    elif a.action=='catalog-v2':
        sql(text=(HERE/'experiment-fixtures-v2.sql').read_text())
        print('Versioned SIM catalogue v2 added only to twinhospital-exp; v1 rows unchanged.')
    elif a.action=='stop':subprocess.run(COMPOSE+['stop'],check=True)
if __name__=='__main__':main()
