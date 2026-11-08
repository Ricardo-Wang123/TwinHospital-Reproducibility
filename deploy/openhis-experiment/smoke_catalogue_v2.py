#!/usr/bin/env python3
"""Native engineering checks, NOT clinical prescriptions or model experiments."""
import argparse,datetime,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private
from sandbox.experiments.linked.medication_catalogue import get_catalogue,VERSION

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--run-id',default='catalogue-v2-smoke-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'));a=ap.parse_args()
 folder=HERE/'runtime'/a.run_id;mapping=json.loads((HERE/'catalog_mapping_v2.json').read_text());results=[]
 for i,product in enumerate(get_catalogue()):
  case_id='fictional-catalogue-'+str(i+1);p=NativeOpenHISPort(folder/case_id,catalog_mapping=mapping)
  ctx={'case_id':case_id,'run_id':a.run_id,'role':'administrative'};steps=[]
  def go(op,payload,role):
   ctx['role']=role;r=p.execute(op,payload,ctx);steps.append({'operation':op,'status':r['status'],'native_effect':r['native_effect'],'readback_verified':r['readback_verified']});return r['data']
  try:
   reg=go('registration.create',{'patient_key':case_id,'demographics':{'age':45,'sex':'F'}},'administrative');ctx.update(reg)
   go('history.import',{'history':{'demographics':{'age':45,'sex':'F','source_kind':'constructed'}}},'administrator')
   go('history.index',{},'doctor');go('history.query',{'section':'demographics'},'doctor')
   diagnoses=go('diagnosis.catalogue',{},'doctor')['items']
   go('consultation.save',{'content':{'purpose':'Independent interface fixture; no diagnostic or treatment validation','diagnoses':[dict(diagnoses[0],primary=True,certainty='working')]},'transcript':[]},'doctor')
   # Deliberately an interface number, not a recommended dose or treatment plan.
   item={k:product[k] for k in ('drug_code','dose_unit','quantity_unit','route')}
   item.update(dose=1,quantity=1,frequency='ST',duration_days=1)
   go('order.save',{'order_id':'interface-rx','items':[item]},'doctor')
   go('order.attest',{'order_id':'interface-rx'},'doctor')
   note=go('note.save',{'note_id':'interface-note','content':{'text':'独立虚构药品目录接口测试；剂量为接口占位数字，不代表临床方案。'}},'doctor')
   go('note.attest',{'note_id':'interface-note','version':note['version']},'doctor')
   bill=go('payment.prepare',{'order_id':'interface-rx'},'cashier')
   paid=go('payment.commit',{'bill_id':bill['bill_id'],'amount':bill['amount'],'currency':'CNY','channel':'CASH','transaction_id':a.run_id+case_id},'cashier')
   results.append({'drug_code':product['drug_code'],'complete':True,'status':paid['status'],'steps':steps})
   print(product['drug_code'],'native_readback_paid',flush=True)
  except Exception as e:
   results.append({'drug_code':product['drug_code'],'complete':False,'error_type':type(e).__name__,'error':str(e),'steps':steps});print(product['drug_code'],'failed',type(e).__name__,str(e),flush=True)
  write_private(folder/'summary.json',{'run_id':a.run_id,'catalogue_version':VERSION,'data_kind':'independent_fictional','model_calls':0,'clinical_validation':False,'n_products':len(results),'products':results})
 raise SystemExit(0 if all(r['complete'] for r in results) else 1)
if __name__=='__main__':main()
