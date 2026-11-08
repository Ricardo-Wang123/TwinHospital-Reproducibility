#!/usr/bin/env python3
"""No-model real native API engineering smoke: independent fictional inputs only."""
import argparse,datetime,json,re,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1];sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private,NativeBlocked
from sandbox.experiments.linked.cases import FORMULARY

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',default='native-smoke-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'));ap.add_argument('--count',type=int,choices=range(1,6),default=5)
    ap.add_argument('--his-url',default='http://127.0.0.1:28081/api/app');ap.add_argument('--credentials',type=Path);a=ap.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', a.run_id): ap.error('Invalid run ID')
    folder=HERE/'runtime'/a.run_id;results=[]
    mapping=json.loads((HERE/'catalog_mapping.json').read_text())
    for i,med in enumerate(FORMULARY[:a.count]):
        case_id=f'fictional-engineering-{i+1}';p=NativeOpenHISPort(folder/case_id,catalog_mapping=mapping,
            base_url=a.his_url,credentials_path=a.credentials)
        ctx={'case_id':case_id,'run_id':a.run_id,'role':'administrative'};steps=[]
        def go(op,payload,role):
            ctx['role']=role;r=p.execute(op,payload,ctx);steps.append({'operation':op,'status':r['status'],'native_effect':r['native_effect'],'readback_verified':r['readback_verified'],'data':r['data']});return r['data']
        try:
            reg=go('registration.create',{'patient_key':case_id,'demographics':{'age':55+i,'sex':'female' if i%2==0 else 'male'}},'administrative');ctx.update(reg)
            go('history.import',{'history':{'provenance':'independent_fictional','past_conditions':['Software engineering fixture, no real patient']}},'administrative')
            go('history.read',{},'doctor')
            go('handoff.save',{'artifact_id':'handoff','content':{'provenance':'independent_fictional','complaint':'虚构接口测试'},'version':1},'nurse')
            go('handoff.read',{'artifact_id':'handoff'},'doctor')
            catalogue=go('diagnosis.catalogue',{},'doctor')['items']
            chosen=catalogue[i]
            go('consultation.save',{'artifact_id':'consult','content':{'purpose':'interface engineering only','diagnoses':[{**chosen,'primary':True,'certainty':'working'}]},'transcript':[]},'doctor')
            item={**med,'quantity':1 if med['quantity_unit']=='inhaler' else 14,'duration_days':7}
            go('order.save',{'order_id':'rx-v1','items':[item]},'doctor')
            go('order.attest',{'order_id':'rx-v1'},'doctor')
            note=go('note.save',{'note_id':'note-v1','content':{'provenance':'independent_fictional','text':'虚构患者，接口写入与读回；不作真实医疗用途'}},'doctor')
            note=go('note.revise',{'note_id':'note-v1','expected_version':note['version'],'content':{'provenance':'independent_fictional','text':'虚构患者，已复核版本，准备锁定'}},'doctor')
            go('note.attest',{'note_id':'note-v1','version':note['version']},'doctor')
            bill=go('payment.prepare',{'order_id':'rx-v1'},'cashier')
            paid=go('payment.commit',{'bill_id':bill['bill_id'],'amount':bill['amount'],'currency':bill['currency'],'channel':'CASH','transaction_id':a.run_id+case_id},'cashier')
            results.append({'case_id':case_id,'drug_code':med['drug_code'],'complete':True,'steps':steps})
            print(case_id,'native_complete',paid['status'],flush=True)
        except Exception as e:
            results.append({'case_id':case_id,'drug_code':med['drug_code'],'complete':False,'error_type':type(e).__name__,'error':str(e),'steps':steps});print(case_id,'failed',type(e).__name__,str(e),flush=True)
        write_private(folder/'summary.json',{'run_id':a.run_id,'data_kind':'independent_fictional','model_calls':0,'clinical_validation':False,'cases':results})
    raise SystemExit(0 if all(r['complete'] for r in results) else 1)
if __name__=='__main__':main()
