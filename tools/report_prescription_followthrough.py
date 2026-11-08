#!/usr/bin/env python3
"""Report original review separately from actual downstream care outcomes."""
import argparse
from collections import Counter
import csv
import hashlib
import html
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.contracts import digest


def summarize_run(run):
    a=run.get('artifacts',{});review=a.get('original_prescription_review') or {}
    plan=a.get('final_care_plan');plan_state=a.get('final_care_plan_outcome') or {}
    note=a.get('attested_note') or {};payment=(a.get('settlement') or {}).get('payment') or {}
    state=a.get('prescription_business_state') or {}
    def observed(op):
        return any(e.get('event')=='business_operation' and e.get('operation')==op
            and all(e.get(k)==run.get(k) for k in ('run_id','case_id','patient_id','encounter_id'))
            and e.get('response',{}).get('status')=='executed'
            and e.get('response',{}).get('readback_verified') is True
            for e in run.get('events',[]))
    signed = state.get('status')=='signed' and state.get('readback_verified') is True and observed('order.attest')
    locked = note.get('status')=='locked' and observed('note.attest')
    paid = payment.get('status')=='paid' and observed('payment.commit')
    if signed and locked and paid:
        outcome='approved_documented_and_paid'
    elif plan_state.get('status')=='recorded' and isinstance(plan,dict) and locked and not paid and not signed:
        outcome='non_dispensing_plan_documented'
    else:
        outcome='not_closed'
    versions=a.get('prescription_versions',[])
    source=run.get('stage_inputs',{}).get('prescription')
    if not isinstance(source,dict) or digest(source)!=run.get('stage_input_hash'):
        raise ValueError('Frozen original prescription input is not intact')
    if 'original_prescription' in a and a['original_prescription']!=source.get('prescription'):
        raise ValueError('Original prescription was replaced')
    if versions and review!=versions[0].get('review'):
        raise ValueError('Original review was replaced by a later disposition')
    return {'group':run.get('group'),'config_id':run.get('config_id'),'runtime_status':run.get('status'),
        'original_review_decision':review.get('decision'),'observed_terminal_outcome':outcome,
        'last_review_decision':versions[-1]['review']['decision'] if versions else None,
        'prescription_versions':len(versions),'revision_count':max(0,len(versions)-1),
        'physician_followup_questions':sum(e.get('event')=='prescription_followup_question' for e in run.get('events',[])),
        'explicit_plan_disposition':plan.get('disposition') if isinstance(plan,dict) else None,
        'planned_investigation_or_referral_executed':False if plan else None,
        'order_signature_observed':signed,'note_signature_observed':locked,'drug_payment_observed':paid,
        'role_calls':run.get('call_count'),'whole_encounter_completion_claimed':False,
        'clinical_correctness':None,'independent_reference_status':'pending'}


def export(batch,output):
    batch,output=Path(batch),Path(output)
    if not (batch/'batch_summary.json').is_file():raise ValueError('Batch not finished')
    spec=json.loads((batch/'specification.json').read_text());conditions=spec['conditions']
    if not spec.get('prescription_followthrough_policy'):raise ValueError('No declared prescription followthrough policy')
    rows=[];hashes={}
    for n,case in enumerate(spec['cases'],1):
        index=json.loads((batch/case['case_id']/'attempts.json').read_text())
        for condition in conditions:
            if condition['task']!='prescription':continue
            label=condition['config_id'].replace(':','__');record=index['attempts'].get(label,{})
            path=record.get('result_path')
            identity={'case_label':f'C{n:03d}','group':case['group'],'config_id':condition['config_id']}
            if not path:
                rows.append({**identity,'runtime_status':record.get('status','missing'),
                    'observed_terminal_outcome':'not_closed','clinical_correctness':None});continue
            p=Path(path).resolve()
            if batch.resolve() not in p.parents:raise ValueError('Result escapes batch')
            run=json.loads(p.read_text())
            if run['case_id']!=case['case_id'] or run['config_id']!=condition['config_id']:
                raise ValueError('Result identity mismatch')
            rows.append({**identity,**summarize_run(run)})
            hashes[identity['case_label']+'/'+condition['config_id']]=hashlib.sha256(p.read_bytes()).hexdigest()
    summary={'batch':spec['batch'],'planned_prescription_units':len(rows),
        'original_review_counts':dict(Counter(r.get('original_review_decision') or 'unobserved' for r in rows)),
        'terminal_outcome_counts':dict(Counter(r['observed_terminal_outcome'] for r in rows)),
        'initial_defers_later_paid':sum(r.get('original_review_decision')=='defer' and r['observed_terminal_outcome']=='approved_documented_and_paid' for r in rows),
        'clinical_scores':None,'new_full_encounters':0,
        'formal_results':False,'source_runs_sha256':hashes,
        'evidence_scope':'Actual bound business-operation readbacks; separate fresh native audit remains required',
        'planned_tests_and_referrals_are_not_executed_results':True}
    if output.exists():raise FileExistsError('Use a new report directory')
    output.mkdir(parents=True)
    (output/'report.json').write_text(json.dumps({'summary':summary,'rows':rows},ensure_ascii=False,indent=2)+'\n')
    keys=sorted({k for row in rows for k in row})
    with (output/'outcomes.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)
    body=''.join('<tr>'+''.join('<td>'+html.escape(str(row.get(k,'')))+'</td>' for k in
        ('case_label','group','config_id','original_review_decision','observed_terminal_outcome','revision_count','explicit_plan_disposition'))+'</tr>' for row in rows)
    (output/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>审方闭环实际结果</title><style>body{font:15px/1.7 system-ui;margin:36px}td,th{padding:8px;border-bottom:1px solid #ddd}table{border-collapse:collapse}th{text-align:left}</style><h1>首次审方与后续处置分开记录</h1><p>原始处方和首次审方结果保持冻结；修改、再次审核、病历签署和模拟缴费来自实际运行。未开药计划不算开药缴费完成，也不声称检查或转诊已经执行。临床正确性待独立评价。</p><p>状态说明：approved_documented_and_paid＝审核通过、签署病历并模拟缴费；non_dispensing_plan_documented＝明确未开药处置并签署病历；not_closed＝尚未闭环。</p><table><tr><th>病例</th><th>病种</th><th>配置</th><th>首次审方</th><th>实际结尾</th><th>修改次数</th><th>未开药处置</th></tr>'+body+'</table>')
    return {k:v for k,v in summary.items() if k!='source_runs_sha256'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(export(a.batch,a.output),ensure_ascii=False))
