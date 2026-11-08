"""Constructed, explicitly nonclinical input packages for actual API execution."""
from __future__ import annotations
import json
from copy import deepcopy
from pathlib import Path
from ..contracts import digest
from ..data import generate_demo, read_jsonl
from ..store import file_hash
from .table_specs import PATIENT_ITEMS, CLINICIAN_ITEMS

MODULE_BY_FACT={'chief_complaint':'chief_complaint','onset':'symptom_progression',
    'symptom_progression':'symptom_progression','past_medical_history':'past_medical_history',
    'medications':'medication_records','allergy_history':'medication_records',
    'risk_signal':'symptom_progression'}


def _write_rows(path,rows):
    Path(path).write_text(''.join(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n' for row in rows),encoding='utf-8')


def prepare(root,seed=20261025):
    root=Path(root)
    if (root/'data_catalog.json').exists():
        return root
    catalog=generate_demo(root,scale='paper',seed=seed)
    cases=read_jsonl(root/'cases.jsonl');inputs=read_jsonl(root/'inputs.jsonl')
    refs=read_jsonl(root/'reference.jsonl');by_id={r['case_id']:r for r in refs}
    facts_rows=[];transcript_rows=[]
    complaints=['咳嗽','头痛','腹痛','咽痛','腰部疼痛','皮疹','乏力','鼻塞','膝部疼痛','恶心']
    for row in inputs:
        cid=row['case_id'];i=int(cid.rsplit('-',1)[1])-1
        values={'chief_complaint':complaints[i%len(complaints)]+'，持续'+str(i%7+1)+'天',
                'onset':str(i%7+1)+'天前开始',
                'symptom_progression':['逐渐加重','与开始时相近','较前缓解'][i%3],
                'past_medical_history':['无已知慢性病','既往高血压，自述规律随诊','既往哮喘，自述近期无发作'][i%3],
                'medications':['目前未服药','患者自述服用降压药，药名不记得','患者自述按既往医嘱使用吸入药，具体名称不记得'][i%3],
                'allergy_history':'患者否认已知药物过敏' if i%4 else '患者报告曾有青霉素过敏',
                'risk_signal':'患者报告出现呼吸困难' if i%5==0 else '患者否认呼吸困难、晕厥或意识改变'}
        facts=[{'fact_id':k,'value':v,'status':'known','release_on':[k]} for k,v in values.items()]
        transcript=[]
        # Four release points correspond to the four required modules, rather than seven duplicate drafts.
        for seq,module in enumerate(('chief_complaint','symptom_progression','past_medical_history','medication_records')):
            included=[f for f in facts if MODULE_BY_FACT[f['fact_id']]==module]
            transcript.append({'segment_id':cid+'-s'+str(seq),'sequence':seq,'speaker_role':'patient',
                'start_ms':seq*30000,'end_ms':seq*30000+20000,'available_at_ms':(seq+1)*30000,
                'text':'；'.join(f['fact_id']+'：'+f['value'] for f in included),
                'verified':False,'source_kind':'engineering_example'})
        value=row['input']
        value.update(initial_visible_facts=[deepcopy(facts[0])],patient_known_facts=facts,
                     required_topics=list(values),transcript=transcript,encounter_end_ms=120000)
        reference=by_id[cid]
        reference.update(required_items=[{'item_id':f['fact_id'],'fact_id':f['fact_id'],
            'module':MODULE_BY_FACT[f['fact_id']],'value':f['value'],'applicable':True} for f in facts],
            risk_signals=[{'signal_id':'risk_signal','description':values['risk_signal']}] if i%5==0 else [],
            reference_version='constructed-four-module-20261025-1',clinical_verification=False)
        facts_rows.extend({'case_id':cid,**f} for f in facts)
        transcript_rows.extend({'case_id':cid,**segment} for segment in transcript)
    input_by_id={r['case_id']:r['input'] for r in inputs}
    for row in cases: row['input_sha256']=digest(input_by_id[row['case_id']])
    for name,rows in [('cases.jsonl',cases),('inputs.jsonl',inputs),('reference.jsonl',refs),
                      ('patient_facts.jsonl',facts_rows),('transcripts.jsonl',transcript_rows)]:
        _write_rows(root/name,rows)
    # Every software operation has a concrete input object; actual native references remain required externally.
    e2e=read_jsonl(root/'end_to_end_cases.jsonl')
    clinical_pre=[r for r in inputs if r['case_id'].startswith('preconsult-')]
    for i,row in enumerate(e2e): row['input']=deepcopy(clinical_pre[i%len(clinical_pre)]['input'])
    _write_rows(root/'end_to_end_cases.jsonl',e2e)
    for artifact in catalog['artifacts']:
        artifact['sha256']=file_hash(root/artifact['path'])
        if artifact['path'].endswith('.jsonl'): artifact['record_count']=len(read_jsonl(root/artifact['path']))
    catalog.update(generator_version='constructed-four-module-20261025-1',
        generation_method='deterministic, explicitly unverified outpatient scenarios',
        note='实际API输入；每类200份。无真实患者、无临床核验；处方药品为DEMO规则标记，不是临床处方。')
    (root/'data_catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    return root


def constructed_survey(root):
    """Full raw-row example for imports; never a human-participant survey."""
    root=Path(root);rows=[]
    for group,items in [('patient',PATIENT_ITEMS),('clinician',CLINICIAN_ITEMS)]:
        for person in range(100):
            for item_index,(dimension,_) in enumerate(items):
                for mode_index,workflow in enumerate(('existing','human_ai')):
                    rows.append({'participant_id':f'constructed-{group}-{person+1:03d}',
                        'participant_group':group,'workflow':workflow,'sequence':mode_index+1,
                        'dimension':dimension,'item_id':dimension,'score':1+(person+item_index+mode_index)%5,
                        'source_kind':'constructed','valid':True,'is_final':True,
                        'evidence_refs':[str((root/'constructed-survey.jsonl').resolve())]})
    _write_rows(root/'constructed-survey.jsonl',rows)
    return rows
