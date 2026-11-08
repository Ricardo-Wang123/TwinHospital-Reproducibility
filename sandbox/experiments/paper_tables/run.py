"""Execute actual table experiments; no test-suite or smoke-test entry point."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..actors import ACTOR_IDS, ScriptedSession
from .model_api import HTTPSession, ModelAPI

PROJECT=Path(__file__).resolve().parents[3]
WORKSPACE=PROJECT.parent


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def model_spec():
    from dotenv import dotenv_values
    values={**dotenv_values(PROJECT/'.env'),**os.environ}
    return {'model':values.get('MODEL_TYPE','gpt-4.1-mini'),
            'endpoint':values.get('MODEL_BASE_URL','https://model-provider.example/v1'),
            'api_key_env':'TWINHOSPITAL_PROVIDED_API_KEY' if values.get('TWINHOSPITAL_PROVIDED_API_KEY') else 'API_KEY',
            'env_file':str(PROJECT/'.env'),'temperature':0.2,'max_tokens':1800,
            'timeout_seconds':90,'max_calls':20000,'role':'foundation',
            'revision':'provider-returned-model-recorded-per-call'}


class ExperimentFactory:
    def __init__(self,api,config_id,*,all_clinical_roles=True,medical_api=None):
        self.api,self.config_id=api,config_id
        self.medical_api=medical_api
        self.all_clinical_roles=all_clinical_roles
        self.sessions=[]

    def actor_id(self,role): return ACTOR_IDS.get(role,role+'-1')

    def session(self,case_id,role,model_family=None):
        if role=='patient' or (model_family is None and not self.all_clinical_roles):
            result=ScriptedSession(case_id,role)
        else:
            if model_family=='medical' and self.medical_api is None:
                raise ValueError('medical intervention requires a separately configured medical model API')
            api=self.medical_api if model_family=='medical' else self.api
            result=HTTPSession(api,case_id,role,self.config_id)
        self.sessions.append(result)
        return result

    def evidence_refs(self):
        return [p for session in self.sessions for p in getattr(session,'evidence_refs',[])]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['clinical-batch'])
    p.add_argument('--output',required=True)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--limit',type=int)
    p.add_argument('--model-config',help='JSON with foundation/medical model specifications; credential references only')
    p.add_argument('--dataset',help='Existing frozen CaseProvider directory; default constructs the documented inputs')
    p.add_argument('--selected-configurations',nargs='+',help='Explicit configuration IDs, on a new output directory')
    p.add_argument('--constructed-survey-example',action='store_true',help='Explicitly request a separate constructed E8/E9 import example')
    args=p.parse_args(argv)
    if args.command=='clinical-batch':
        from .clinical_batch import run_batch
        run_batch(args.output,workers=args.workers,limit=args.limit,
            model_config=args.model_config,dataset=args.dataset,selected=args.selected_configurations,
            survey_example=args.constructed_survey_example)


if __name__=='__main__': main()
