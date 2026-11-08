#!/usr/bin/env python3
"""Read-only supplemental payment-ledger check; never modifies original results."""
import argparse,datetime,hashlib,importlib.util,json,re,sys
from decimal import Decimal
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.native_his import write_private

def main():
    ap=argparse.ArgumentParser();ap.add_argument('run_directory');a=ap.parse_args();run=Path(a.run_directory).resolve()
    if not (run/'summary.json').exists():raise RuntimeError('Original experiment must finish before supplemental readback')
    source=run/'outcomes.jsonl';rows=[json.loads(x) for x in source.read_text().splitlines()];targets=[r for r in rows if r['category']=='duplicate_payment' and r['is_error']]
    ids=[r['after_state'].get('native',r['after_state'])['encounter_id'] for r in targets];assert ids and all(re.fullmatch(r'[0-9]{1,19}',x) for x in ids)
    spec=importlib.util.spec_from_file_location('isolated_manage',HERE/'manage.py');manage=importlib.util.module_from_spec(spec);spec.loader.exec_module(manage)
    query="""BEGIN READ ONLY;
    SELECT coalesce(jsonb_agg(jsonb_build_object(
      'id',p.id::text,'patient_id',p.patient_id::text,'encounter_id',p.encounter_id::text,
      'kind',p.kind_enum,'status',p.status_enum,'outcome',p.outcome_enum,
      'amount',p.display_amount::text,'tendered_amount',p.tendered_amount::text,
      'charge_item_ids',p.charge_item_ids,
      'details',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',d.id::text,'reconciliation_id',d.reconciliation_id::text,'pay_enum',d.pay_enum,'result_enum',d.result_enum,'amount',d.amount::text)), '[]'::jsonb)
       FROM fin_payment_rec_detail d WHERE d.reconciliation_id=p.id AND NOT d.is_deleted))), '[]'::jsonb)
    FROM fin_payment_reconciliation p WHERE NOT p.is_deleted AND p.encounter_id IN ("""+','.join(ids)+"); COMMIT;"
    raw=manage.sql(args=['-A','-t'],text=query).stdout.strip();payments=json.loads(raw)
    results=[]
    for r in targets:
        state=json.loads((run/'units'/r['test_id']/'native_state.json').read_text());binding=state['bindings'][r['run_id']+':'+r['test_id']];bill=next(iter(binding['bills'].values()));wanted=set(bill['charge_item_ids'])
        own=[p for p in payments if p['encounter_id']==binding['encounter_id']]
        relevant=[p for p in own if wanted.intersection(re.findall(r'\d+',p['charge_item_ids'] or ''))]
        success=[p for p in relevant if p['status']=='SUCCESS'];cash=[d for p in success for d in p['details'] if d['pay_enum']=='CASH' and d['result_enum']=='SUCCESS']
        gaps=[]
        if len(success)!=1:gaps.append('expected_exactly_one_successful_payment_for_target_charges')
        if len(cash)!=1:gaps.append('expected_exactly_one_successful_cash_detail')
        if any(p['patient_id']!=binding['patient_id'] for p in relevant):gaps.append('patient_binding_mismatch')
        if success and (success[0]['id']!=bill['payment_id'] or Decimal(success[0]['amount'])!=Decimal(bill['amount'])):gaps.append('payment_identity_or_amount_mismatch')
        if sum((Decimal(d['amount']) for d in cash),Decimal(0))!=Decimal(bill['amount']):gaps.append('cash_amount_mismatch')
        results.append({'test_id':r['test_id'],'original_outcome':r['outcome'],'original_result_replaced':False,'observation_stage':'post_run_database_read_only','expected_payment_id':bill['payment_id'],'expected_amount':bill['amount'],'observed_payments':relevant,'successful_cash_details':len(cash),'verification':'confirmed_one_payment_one_cash_detail' if not gaps else 'unknown_or_mismatch','gaps':gaps})
    report={'schema_version':'native-payment-supplemental-1','recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'read_only_transaction':True,'original_results_changed':False,'original_observation_window_established':False,'n_units':len(results),'confirmed':sum(not x['gaps'] for x in results),'unknown_or_mismatch':sum(bool(x['gaps']) for x in results),'units':results}
    output=run/'supplemental_payment_readback.json'
    if output.exists():raise RuntimeError('Supplemental artifact exists; do not overwrite')
    write_private(output,report);print(json.dumps({k:report[k] for k in ('n_units','confirmed','unknown_or_mismatch','read_only_transaction','original_results_changed')},ensure_ascii=False))
if __name__=='__main__':main()
