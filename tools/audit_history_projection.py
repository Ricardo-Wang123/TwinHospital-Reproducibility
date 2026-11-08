#!/usr/bin/env python3
"""Read-only source/query/context audit; output clinical details locally only."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.chart import HistoryChart,json_bytes
from sandbox.experiments.linked.chart_projection import project_history_page,verify_projection,sha256_json,deduplicate_projected_page,PROJECTION_POLICY

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,data):
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)

def audit(batch,manifest,out):
    batch,manifest,out=map(Path,(batch,manifest,out));m=json.loads(manifest.read_text())
    lookup={e['case_id']:e for e in m['cases']}
    specification=json.loads((batch/'specification.json').read_text())
    source_spec={e['case_id']:e for e in specification['cases']}
    chart_path=Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/chart.py'
    expected_chart=specification['code_hashes']['chart.py']
    if sha(chart_path)!=expected_chart:raise ValueError('Readback implementation differs from frozen run')
    protected={str(p):sha(p) for p in batch.rglob('*') if p.is_file()}
    protected[str(manifest)]=sha(manifest)
    summaries=[];private=[]
    for p in sorted(batch.glob('*/linked_baseline_anchor/runs/*/run.json')):
        run=json.loads(p.read_text());entry=lookup[run['case_id']];case=json.loads(Path(entry['path']).read_text())
        if sha(entry['path'])!=source_spec[run['case_id']]['file_sha256']:raise ValueError('Source case differs from frozen run')
        protected[entry['path']]=sha(entry['path']);chart=HistoryChart(case['his_initial']);h=run['artifacts']['visible_history']
        projected_history={**deepcopy(h),'queried_pages':[],'last_query_feedback':None}
        rows=[];compact_sim_accepts=0
        for event in run['events']:
            if event['event']!='history_lookup' or not event.get('request'):continue
            request=event['request'];raw=chart.query(**request);compact=project_history_page(raw)
            if not verify_projection(raw,compact):raise ValueError('Clinical projection lost a source field')
            refs=[x['record_ref'] for x in raw['records']]
            if refs!=event['returned_refs']:raise ValueError('Replay does not match actual readback')
            match=next((q for q in h['queried_pages'] if sha256_json(q['page'])==sha256_json(raw)),None)
            trial=deepcopy(projected_history);trial['last_query_feedback']=None
            unique=deduplicate_projected_page(compact,trial['queried_pages'])
            if not verify_projection(raw,unique['page'],trial['queried_pages']):raise ValueError('Deduplication loses evidence')
            if not unique['all_records_already_visible'] and not any(q['page']['source_page_sha256']==compact['source_page_sha256'] for q in trial['queried_pages']):
                trial['queried_pages'].append({'page':unique['page'],'phase':event['phase'],'queried_after_sequence':match['queried_after_sequence'] if match else 0})
            accepted=json_bytes(trial)<=60000-1024
            if accepted:projected_history=trial;compact_sim_accepts+=1
            projected_history['last_query_feedback']={'status':'visible' if accepted else 'requires_narrower_query_for_visible_context_budget',
                'section':request['section'],'returned_count':len(refs),'remaining_queries_this_phase':0,
                'already_visible_record_refs':unique['already_visible_record_refs'],
                'matching_count':raw['matching_count'],'next_offset':raw['next_offset'],
                'instruction':'Unknown/unshown is not absent. Use a narrower section, term or limit if this page could not fit.'}
            row={'phase':event['phase'],'section':request['section'],'term':request.get('term'),
                'offset':request.get('offset',0),'limit':request.get('limit',5),
                'raw_page_utf8_bytes':json_bytes(raw),'projected_page_utf8_bytes':json_bytes(compact),
                'returned_count':len(refs),'actual_accepted':event['accepted_into_context'],
                'actual_retained_context_bytes':event.get('retained_context_bytes'),
                'simulated_projected_accept':accepted,'simulated_projected_context_bytes':json_bytes(projected_history),
                'raw_page_sha256':sha256_json(raw),'projected_page_sha256':sha256_json(compact),
                'already_visible_records_shared':len(unique['already_visible_record_refs']),
                'all_clinical_fields_reconstruct_exactly':True}
            rows.append(row)
        accepted_rows=[x for q in h['queried_pages'] for x in q['page']['records']]
        visible_cr=[x['record'] for q in h['queried_pages'] if q['page']['section']=='historical_observations'
            for x in q['page']['records'] if 'creatinine' in x['record'].get('display','').lower()]
        source_cr=[x for x in case['his_initial']['historical_observations'] if 'creatinine' in x.get('display','').lower()]
        source_gfr=[x for x in case['his_initial']['historical_observations'] if any(t in x.get('display','').lower() for t in ('gfr','glomerular'))]
        final_calls=[x for x in run['calls'] if x.get('role')=='doctor' and x.get('observation',{}).get('hospital_history')]
        final_h=final_calls[-1]['observation']['hospital_history'] if final_calls else {}
        final_cr=[x['record'] for q in final_h.get('queried_pages',[]) if q['page']['section']=='historical_observations'
            for x in q['page']['records'] if 'creatinine' in x['record'].get('display','').lower()]
        summary={'group':run['group'],'actual_context_utf8_bytes':json_bytes(h),
            'projected_replay_context_utf8_bytes':json_bytes(projected_history),
            'actual_budget_rejections':sum(x['result_status']=='requires_narrower_query_for_visible_context_budget' for x in run['events'] if x['event']=='history_lookup'),
            'projected_replay_budget_rejections':sum(not x['simulated_projected_accept'] for x in rows),
            'actual_query_records_retained':len(accepted_rows),'actual_unique_record_refs':len({x['record_ref'] for x in accepted_rows}),
            'source_creatinine_count':len(source_cr),'source_gfr_label_count':len(source_gfr),
            'final_doctor_visible_creatinine_count':len(final_cr),
            'final_doctor_visible_blood_creatinine_count':sum(x.get('fluid')=='Blood' for x in final_cr),
            'final_doctor_visible_cr_numeric_count':sum(x.get('numeric_value_raw') not in (None,'') for x in final_cr),
            'final_doctor_visible_cr_time_known_count':sum(x.get('time',{}).get('event_time_known') is True for x in final_cr),
            'all_replayed_reads_match_actual_refs':True,'all_projected_clinical_fields_verified':True,
            'queries':rows,'simulation_only_no_model_rerun':True}
        summaries.append(summary)
        private.append({'group':run['group'],'run_file':str(p),'source_case':entry['path'],
            'source_case_sha256':sha(entry['path']),'source_manifest_sha256':sha(manifest),
            'actual_final_doctor_creatinine_records':final_cr,
            'source_creatinine_records':source_cr,'source_gfr_records':source_gfr,
            'final_doctor_response':final_calls[-1]['response'] if final_calls else None,
            'prescription_eligibility':run['artifacts'].get('prescription_eligibility'),
            'projection_replay':projected_history,'summary':summary})
    unchanged=all(sha(p)==h for p,h in protected.items())
    if not unchanged:raise RuntimeError('Frozen source/run changed')
    result={'projection_policy':PROJECTION_POLICY,'source_run_count':len(summaries),'model_calls':0,'input_mutations':0,'protected_files':len(protected),
        'all_protected_hashes_unchanged':unchanged,'context_budget_unchanged':60000,
        'clinical_appropriateness_not_assessed':True,'cases':summaries}
    save(out/'history_projection_audit_summary.json',result)
    save(out/'history_projection_audit_private.json',{'cases':private,'protected_hashes':protected})
    print(json.dumps({'cases':[{'group':s['group'],'actual_context_bytes':s['actual_context_utf8_bytes'],
        'projected_context_bytes':s['projected_replay_context_utf8_bytes'],'budget_rejections':s['actual_budget_rejections'],
        'projected_rejections':s['projected_replay_budget_rejections'],'queries':[{k:q[k] for k in ('section','term','limit','raw_page_utf8_bytes','projected_page_utf8_bytes','actual_accepted')} for q in s['queries']]} for s in summaries],
        'source_hashes_unchanged':unchanged,'model_calls':0},ensure_ascii=False))


def page_scan(manifest,out):
    manifest,out=Path(manifest),Path(out);m=json.loads(manifest.read_text());counts=Counter();private=[]
    for entry in m['cases']:
        before=sha(entry['path']);case=json.loads(Path(entry['path']).read_text());chart=HistoryChart(case['his_initial'])
        pages=0;maxraw=0;maxcompact=0
        for section in chart.initial_view()['sections']:
            offset=0
            while True:
                raw=chart.query(section,offset=offset,limit=20);p=project_history_page(raw)
                if not verify_projection(raw,p):raise ValueError('Clinical field roundtrip differs')
                pages+=1;counts['pages']+=1;counts['records']+=len(raw['records'])
                counts['explicit_expansions']+=len(raw.get('requires_expansion',[]))
                counts['raw_page_bytes']+=json_bytes(raw);counts['projected_page_bytes']+=json_bytes(p)
                maxraw=max(maxraw,json_bytes(raw));maxcompact=max(maxcompact,json_bytes(p))
                if raw['next_offset'] is None:break
                if raw['next_offset']<=offset:raise ValueError('Non-progressing pagination')
                offset=raw['next_offset']
        if sha(entry['path'])!=before:raise RuntimeError('Frozen case changed')
        counts['cases']+=1;counts['max_raw_page_bytes']=max(counts['max_raw_page_bytes'],maxraw)
        counts['max_projected_page_bytes']=max(counts['max_projected_page_bytes'],maxcompact)
        private.append({'path':entry['path'],'sha256':before,'pages':pages,'max_raw_page_bytes':maxraw,'max_projected_page_bytes':maxcompact})
    result={'counts':dict(counts),'all_clinical_fields_roundtrip_exact':True,'source_mutations':0,
        'manifest_sha256':sha(manifest),'projection_policy':PROJECTION_POLICY}
    save(out/'all50_projection_scan_summary.json',result);save(out/'all50_projection_scan_private.json',{'cases':private})
    print(json.dumps({'counts':dict(counts),'all_clinical_fields_roundtrip_exact':True,'source_mutations':0}))

if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('--batch');p.add_argument('--manifest',required=True);p.add_argument('--output',required=True);p.add_argument('--page-scan',action='store_true');a=p.parse_args()
    if a.page_scan:page_scan(a.manifest,a.output)
    else:audit(a.batch,a.manifest,a.output)
