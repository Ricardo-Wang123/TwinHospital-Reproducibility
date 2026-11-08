#!/usr/bin/env python3
"""Independent fictional, no-model diagnosis write/readback in isolated OpenHIS."""
import json
from pathlib import Path
import sys
import uuid

HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private
from sandbox.experiments.linked.protocol_his import PaperProtocolPort


def main():
    run_id='diagnosis-v2-smoke-'+uuid.uuid4().hex
    root=HERE/'runtime'/run_id
    mapping=json.loads((HERE/'diagnosis_mapping_v2.json').read_text())
    port=PaperProtocolPort(NativeOpenHISPort(root,diagnosis_mapping=mapping))
    context={'case_id':'independent-fictional-diagnosis-fixture','run_id':run_id,'config_id':'dictionary-write-readback-only'}
    steps=[]
    def go(op,payload,role):
        ctx={**context,'role':role,'actor_id':role+'-1','idempotency_key':run_id+':'+op}
        r=port.execute(op,payload,ctx)
        if not r.get('readback_verified'):raise RuntimeError('Unverified operation')
        steps.append({'operation':op,'status':r['status'],'native_effect':r['native_effect'],
                      'readback_verified':r['readback_verified']})
        return r
    r=go('registration.create',{'patient_key':context['case_id'],'demographics':{'age':55,'sex':'M'}},'administrator')
    context.update({k:r['data'][k] for k in ('patient_id','encounter_id')})
    go('queue.enter',{'encounter_id':context['encounter_id']},'administrator')
    go('encounter.begin_consultation',{},'doctor')
    items=go('diagnosis.catalogue',{},'doctor')['data']['items']
    codes={x['code']:x for x in items}
    selected=[{**codes['Z76.0'],'primary':True,'certainty':'working'},
              {**codes['E11.65'],'primary':False,'certainty':'working'}]
    result=go('consultation.save',{'artifact_id':'fixture-consultation','content':{
        'purpose':'Independent fictional dictionary plumbing fixture, no diagnosis or treatment validation',
        'diagnoses':selected},'transcript':[]},'doctor')
    write_private(root/'private_evidence.json',{'data_kind':'independent_fictional','context':context,'native_response':result,'steps':steps})
    public={'version':'diagnosis-v2-write-readback-smoke-1','data_kind':'independent_fictional','model_calls':0,
        'native_catalogue_items':len(items),'selected_codes':['Z76.0','E11.65'],
        'system':'ICD-10-CM','diagnosis_write_readback_verified':result['readback_verified'],
        'steps':steps,'clinical_validation':False,'complete_workflow':False,
        'scope':'Registration, gateway queue, consultation start, vocabulary read and two diagnosis writes only; no medication or medication-payment action.'}
    (HERE/'diagnosis_v2_smoke.json').write_text(json.dumps(public,indent=2)+'\n')
    print(json.dumps({k:public[k] for k in ('native_catalogue_items','selected_codes','diagnosis_write_readback_verified','model_calls','complete_workflow')}))


if __name__=='__main__':main()
