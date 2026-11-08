"""Apply additive definitions outside observation sessions, then read them back."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('deployment-dir','finished-batch','output'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();dep=a.deployment_dir
    if a.output.exists():raise FileExistsError('Retain previous verification')
    for capture in a.finished_batch.parent.glob('*/native_observation/observation_session.json'):
        if json.loads(capture.read_text()).get('status')=='active':
            raise ValueError('An active native observation must finish first')
    spec=importlib.util.spec_from_file_location('isolated_manage',dep/'manage.py')
    manage=importlib.util.module_from_spec(spec);spec.loader.exec_module(manage)
    old_med=json.loads((dep/'catalog_mapping_v4.json').read_text())
    med=json.loads((dep/'catalog_mapping_v5.json').read_text())
    old_dx=json.loads((dep/'diagnosis_mapping_v2.json').read_text())
    dx=json.loads((dep/'diagnosis_mapping_v3.json').read_text())
    def rows(table,ids):
        sql='BEGIN READ ONLY; SELECT coalesce(json_agg(x ORDER BY x.id),\'[]\'::json) FROM '+table+' x WHERE id IN ('+','.join(ids)+'); COMMIT;'
        return json.loads(manage.sql(['-At'],text=sql).stdout.strip())
    med_ids=[v['medicationDefinitionId'] for v in old_med.values()]
    dx_ids=[v['conditionDefinitionId']for v in old_dx.values()]
    lock=os.open(dep/'runtime/observation-global-write.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        before={'medications':rows('med_medication_definition',med_ids),
                'diagnoses':rows('cln_condition_definition',dx_ids)}
        manage.sql(text=(dep/'experiment-fixtures-v5.sql').read_text())
        manage.sql(text=(dep/'experiment-diagnoses-v3.sql').read_text())
        after={'medications':rows('med_medication_definition',med_ids),
               'diagnoses':rows('cln_condition_definition',dx_ids)}
        if before!=after:raise ValueError('Existing native definitions changed')
    finally:
        os.close(lock)
    summary=json.loads((a.finished_batch/'batch_summary.json').read_text())
    completed=next(r for r in summary['cases'] if r['attempts']['linked_baseline_anchor']['status']=='completed')
    state=a.finished_batch/completed['case_id']/'linked_baseline_anchor/native'
    state_hash=digest(json.loads((state/'native_state.json').read_text()))
    port=NativeOpenHISPort(state,credentials_path=dep/'runtime/accounts/credentials.json',
                          catalog_mapping=med,diagnosis_mapping=dx)
    binding=next(iter(port.state['bindings'].values()))
    rb=port._client('doctor').post('/doctorStationAdvice/pageDoctorStationAdviceCatalog',{
        'patientId':binding['patient_id'],'encounterId':binding['encounter_id'],'keyword':'V5-',
        'pageNo':1,'pageSize':100,'productTableNames':['med_medication_definition']})
    code='SIM-FLUTICASONE-SALMETEROL-DISKUS-500-50';item=med[code]
    matches=[r for r in rb['content']if str(r['productDefinitionId'])==item['medicationDefinitionId']]
    if len(matches)!=1:raise ValueError('New product absent or duplicated in native API')
    native=matches[0];bands={b['bandKind']:b for b in native['unitBands']}
    checks={'old_native_definitions_unchanged':True,'product_code':native['busNo']==item['catalog_bus_no'],
        'strength':native['totalVolume']==item['strength'],'dose_unit':bands['DOSE']['unitCode']==item['dose_unit_code'],
        'dispense_unit':native['unitCode']==item['quantity_unit_code'],
        'positive_inventory':Decimal(str(native.get('availableQuantity') or 0))>0,
        'simulated_price':Decimal(str(native.get('unitPrice') or 0))==1,
        'no_default_regimen':native.get('dose') is None and native.get('rateCode') is None,
        'no_dose_quantity_ratio_invented':bands['DOSE'].get('ratioToSplit') is None,
        'original_completed_encounter_state_unchanged':state_hash==digest(json.loads((state/'native_state.json').read_text()))}
    new_dx={k:v for k,v in dx.items()if k not in old_dx}
    db_dx=rows('cln_condition_definition',[v['conditionDefinitionId']for v in new_dx.values()])
    checks['diagnosis_dictionary_exact_readback']=len(db_dx)==len(new_dx) and all(
        r['condition_code']in new_dx and r['name']==new_dx[r['condition_code']]['display'] for r in db_dx)
    record={'observed_at':datetime.now(timezone.utc).isoformat(),'checks':checks,'all_verified':all(checks.values()),
        'private_native_product_response':rb,'private_new_diagnosis_rows':db_dx,
        'before_definition_hash':digest(before),'after_definition_hash':digest(after),
        'new_medication_definitions':1,'new_diagnosis_definitions':len(new_dx),
        'readback_context':'existing_completed_MIMIC_derived_encounter_read_only',
        'new_patient_encounters':0,'new_prescriptions':0,'model_calls':0,
        'clinical_appropriateness_established':False}
    write_private(a.output,record)
    print(json.dumps({k:record[k]for k in ['all_verified','checks','new_medication_definitions','new_diagnosis_definitions','model_calls']}))
    if not record['all_verified']:raise SystemExit(1)


if __name__=='__main__':main()
