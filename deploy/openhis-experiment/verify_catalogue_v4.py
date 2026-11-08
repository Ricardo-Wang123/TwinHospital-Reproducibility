#!/usr/bin/env python3
"""Read native catalogue with a prior independently fictional encounter only.

No prescriptions or payments are created. Native IDs stay in private evidence.
"""
import argparse, datetime, hashlib, json, sys
from decimal import Decimal
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private
from sandbox.experiments.linked.medication_catalogue_v4 import VERSION, additions
import manage

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--first5',action='store_true');a=ap.parse_args()
    suffix='_first5' if a.first5 else ''
    mapping_path=HERE/f'catalog_mapping_v4{suffix}.json';mapping=json.loads(mapping_path.read_text())
    states=sorted((HERE/'runtime').glob('catalogue-v2-smoke*/fictional-catalogue-*/native_state.json'))
    if not states:raise RuntimeError('Prior independent fictional encounter required; never borrow a patient case')
    port=NativeOpenHISPort(states[0].parent);b=next(iter(port.state['bindings'].values()));c=port._client('doctor')
    rb=c.post('/doctorStationAdvice/pageDoctorStationAdviceCatalog',{
      'patientId':b['patient_id'],'encounterId':b['encounter_id'],'keyword':'V4-',
      'pageNo':1,'pageSize':100,'productTableNames':['med_medication_definition']})
    by_id={str(x['productDefinitionId']):x for x in rb['content']};summary=[]
    for p in additions(first5=a.first5):
        m=mapping[p['drug_code']];r=by_id.get(m['medicationDefinitionId'],{})
        bands={x['bandKind']:x for x in r.get('unitBands',[])}
        checks={'unique_native_product':sum(str(x['productDefinitionId'])==m['medicationDefinitionId'] for x in rb['content'])==1,
          'bus_no':r.get('busNo')==m['catalog_bus_no'],'strength':r.get('totalVolume')==p['strength'],
          'dose_unit':bands.get('DOSE',{}).get('unitCode')==m['dose_unit_code'],
          'dispense_unit':r.get('unitCode')==m['quantity_unit_code'],
          'route':r.get('methodCode')==m['route_codes'][p['route']],
          'positive_inventory':Decimal(str(r.get('availableQuantity') or 0))>0,
          'simulated_price':Decimal(str(r.get('unitPrice') or 0))==1,
          'no_default_regimen':r.get('dose') is None and r.get('rateCode') is None,
          'dose_quantity_ratio_not_invented':bands.get('DOSE',{}).get('ratioToSplit') is None}
        summary.append({'drug_code':p['drug_code'],'checks':checks,'verified':all(checks.values())})
    ids=','.join(m['medicationDefinitionId'] for m in mapping.values())
    db=manage.sql(['-At'],text='BEGIN READ ONLY; SELECT json_agg(m ORDER BY m.id) FROM med_medication_definition m WHERE id IN ('+ids+'); COMMIT;').stdout
    dbrows=json.loads(db.strip());db_by_id={str(r['id']):r for r in dbrows}
    for row in summary:
        m=mapping[row['drug_code']];r=db_by_id.get(m['medicationDefinitionId'],{})
        row['checks']['db_ingredient']=r.get('ingredient')==m['ingredient']
        row['checks']['db_no_official_sku_claim']=r.get('official_verified_flag') is False
        row['verified']=all(row['checks'].values())
    stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
    private=HERE/f'runtime/catalogue-v4{suffix}-verification.json'
    write_private(private,{'observed_at':stamp,'native_response':rb,'db_product_rows':dbrows,'checks':summary})
    result={'version':VERSION,'observed_at':stamp,'scope':'native_catalogue_identity_units_inventory_price_readback; no treatment validation',
      'native_api':'doctorStationAdvice/pageDoctorStationAdviceCatalog','independent_fictional_encounter':True,
      'new_products':len(summary),'all_verified':all(r['verified'] for r in summary),'products':summary,
      'mapping_sha256':sha(mapping_path),'formulary_sha256':sha(HERE/f'formulary_v4{suffix}.json'),
      'metadata_sha256':sha(HERE/f'catalogue_metadata_v3{suffix}.json'),
      'patient_ndc_verified':False,'new_clinical_orders_created':0,'model_calls':0,
      'private_native_evidence_sha256':sha(private)}
    (HERE/f'catalogue_v4{suffix}_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('version','new_products','all_verified','model_calls')}))
    if not result['all_verified']:raise SystemExit(1)
if __name__=='__main__':main()
