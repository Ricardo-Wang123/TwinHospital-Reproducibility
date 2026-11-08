"""Bounded, budgeted nonclinical health check using the actual configured routes."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.provider import BudgetLedger, BudgetedTransport, private_json, generation_settings
from sandbox.experiments.linked.json_boundary import parse_json_response


def validate_probe_response(response, cfg, role, model):
    """Use the frozen actor parser, including its existing whole-JSON fence rule."""
    choice=response['choices'][0];message=choice['message']
    policy=generation_settings(cfg,role,model).get('response_parsing')
    if policy:
        value,parsing=parse_json_response(message['content'],finish_reason=choice.get('finish_reason'),policy=policy)
    else:
        value=json.loads(message['content']);parsing=None
    valid=value=={'ok':True} and response.get('model')==model
    if role=='doctor' and generation_settings(cfg,role,model)['thinking'] is False:
        valid=valid and not message.get('reasoning_content') and response['usage'].get('completion_tokens_details',{}).get('reasoning_tokens',0)==0
    return valid,parsing


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','ledger','model-evidence','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--uncapped-budget',action='store_true',
        help='Use the existing ledger explicitly changed by recorded user authorization')
    a=p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,90}',a.run_id):raise ValueError('Invalid run ID')
    a.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    cfg=json.loads(a.config.read_text());ledger=BudgetLedger(a.ledger,cap_cny=None if a.uncapped_budget else 1000)
    before=ledger.summary();transport=BudgetedTransport(cfg,ledger,a.model_evidence)
    rows=[]
    routes=[('role','doctor',cfg['role_model'])]+[('tool_'+label,'scribe',model) for label,model in cfg['tool_models'].items()]
    expected_routes=1+len(cfg['tool_models'])
    if expected_routes not in (2,3):raise ValueError('One role backbone and one fixed AI tool, or a legacy pair, are required')
    for label,role,model in routes:
        try:
            response,request_id=transport.complete([{'role':'user','content':'Nonclinical API health probe. Return exactly {"ok":true}.'}],
                model=model,case_id='independent_nonclinical_health_probe',role=role,data_kind='synthetic_independent',
                config_id='health:'+label,run_id=a.run_id)
            usage=response['usage'];valid,parsing=validate_probe_response(response,cfg,role,model)
            rows.append({'route':label,'model':model,'returned_model':response.get('model'),
                'status':'completed' if valid else 'contract_mismatch','usage':usage,
                'request_id':request_id,'response_parse':parsing,
                'role_thinking_requested':generation_settings(cfg,role,model)['thinking'] if role=='doctor' else None})
        except Exception as e:
            rows.append({'route':label,'model':model,'status':'failed','error_type':type(e).__name__})
        if rows[-1]['status']!='completed':break
    after=ledger.summary()
    result={'run_id':a.run_id,'observed_at':datetime.now(timezone.utc).isoformat(),
        'config_sha256':hashlib.sha256(a.config.read_bytes()).hexdigest(),
        'nonclinical_probe':True,'endpoint':cfg['endpoint'],'api_protocol':cfg.get('api_protocol'),
        'tls_verified':True,'all_routes_pass':len(rows)==expected_routes and all(x['status']=='completed' for x in rows),
        'rows':rows,'provider_source_sha256':hashlib.sha256((Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/provider.py').read_bytes()).hexdigest(),
        'new_http_attempts':after['requests']-before['requests'],
        'conservative_cost_increment_cny':round(after['accounted_upper_cny']-before['accounted_upper_cny'],6),
        'budget':after}
    private_json(a.output/'summary.json',result)
    private_json(a.output/'driver_integrity.json',{'path':str(Path(__file__).resolve()),'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    print(json.dumps(result))
    return 0 if result['all_routes_pass'] else 1

if __name__=='__main__':sys.exit(main())
