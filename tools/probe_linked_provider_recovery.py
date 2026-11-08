"""One budgeted, fixed nonclinical request per explicitly selected model.

May observe service health despite a provider-freeze gate. Never clears that
gate, retries a clinical case, changes models during a run, or prints content.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.provider import BudgetLedger, BudgetedTransport, private_json


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config','ledger','model-evidence','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--model', action='append', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--uncapped-budget', action='store_true')
    args=parser.parse_args()
    if not 1<=len(args.model)<=12 or len(set(args.model))!=len(args.model):
        raise ValueError('Choose 1 to 12 distinct models')
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    cfg=json.loads(args.config.read_text());cfg['max_attempts']=1
    ledger=BudgetLedger(args.ledger, cap_cny=None if args.uncapped_budget else 1000)
    before=ledger.summary();transport=BudgetedTransport(cfg,ledger,args.model_evidence)
    rows=[]
    for model in args.model:
        try:
            response,rid=transport.complete([{'role':'user','content':'Return the word OK.'}],
                model=model,case_id='provider_health_probe',role='provider_probe',
                data_kind='synthetic_independent',run_id=args.run_id,provider_probe=True)
            row={'model':model,'status':'completed' if response.get('model')==model else 'model_mismatch',
                'request_id':rid,'returned_model':response.get('model'),'usage':response.get('usage')}
        except Exception as error:
            rid=getattr(error,'request_id',None)
            row={'model':model,'status':'failed','error_type':type(error).__name__,'request_id':rid}
        if row.get('request_id'):
            path=args.model_evidence/(row['request_id']+'.json')
            if path.exists():
                evidence=json.loads(path.read_text())
                row.update(http_status=evidence.get('http_status'),provider_error_code=evidence.get('provider_error_code'))
        rows.append(row)
        print(json.dumps(row),flush=True)
    after=ledger.summary()
    private_json(args.output/'summary.json',{'observed_at':datetime.now(timezone.utc).isoformat(),
        'run_id':args.run_id,'nonclinical_probe':True,'endpoint':cfg['endpoint'],'rows':rows,
        'config_sha256':hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'new_http_attempts':after['requests']-before['requests'],
        'conservative_cost_increment_cny':round(after['accounted_upper_cny']-before['accounted_upper_cny'],6),
        'before':before,'after':after})
    return 0 if all(row['status']=='completed' for row in rows) else 1


if __name__=='__main__':raise SystemExit(main())
