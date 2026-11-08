"""Repair the disease-focused prior-plan context without changing source history.

This development revision is informed by input failures, not a new measured
outcome. All fifty source patients remain. No failed patient is replaced and no
prescription, approval, negative allergy or current test result is supplied.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path

from .current_episode_v4 import build_episode, candidate_plan, regimen_candidate, HISTORY_FIELDS, SYMPTOMS
from .current_episode_v5 import _matches
from .v4_source_qualification import load_frequency_context
from .adult_population import add_adult_population
from .formulary_overlay import file_hash, write
from ..cases import validate_case
from ..chart import HistoryChart
from ..chart_projection import project_history_page, verify_projection
from ..contracts import digest, runtime_case

VERSION='disease_focused_prior_plan_v10'
NAMES={'type2_diabetes':'二型糖尿病','heart_failure':'心力衰竭','copd':'慢阻肺'}
# Exact finite authoring rules, not source-NDC equivalence. Form and release
# distinctions are explicit; an unlisted pattern is retained as unresolved.
PRODUCTS={
    ('metformin xr (glucophage xr)','500mg tab'):'SIM-METFORMIN-ER-500',
    ('metformin (glucophage)','500mg tablet'):'SIM-METFORMIN-500',
    ('glipizide xl','5mg cr tablet'):'SIM-GLIPIZIDE-ER-5',
    ('glipizide xl','10mg cr tab'):'SIM-GLIPIZIDE-ER-10',
    ('glipizide','5 mg tab'):'SIM-GLIPIZIDE-IR-5',
    ('metoprolol succinate xl','25mg xl tab'):'SIM-METOPROLOL-SUCCINATE-ER-25',
    ('metoprolol succinate xl','50mg xl tab'):'SIM-METOPROLOL-SUCCINATE-ER-50',
    ('metoprolol succinate xl','100mg xl tab'):'SIM-METOPROLOL-SUCCINATE-ER-100',
    ('metoprolol tartrate','25mg tablet'):'SIM-METOPROLOL-TARTRATE-25',
    ('lisinopril','5mg tablet'):'SIM-LISINOPRIL-5',
    ('losartan potassium','50mg tablet'):'SIM-LOSARTAN-POTASSIUM-50',
    ('fluticasone-salmeterol diskus (250/50)','250/50mcg diskus'):'SIM-FLUTICASONE-SALMETEROL-DISKUS-250-50',
    ('tiotropium bromide','18mcg capsule-inhalation device'):'SIM-TIOTROPIUM-18',
    ('ipratropium-albuterol inhalation spray','20-100 mcg - 4 g inhaler'):'SIM-IPRATROPIUM-ALBUTEROL-RESPIMAT-20-100',
    ('ipratropium-albuterol neb','(0.5mg - 3 mg) - 3 ml neb'):'SIM-IPRATROPIUM-ALBUTEROL-NEB-05-3-3ML',
    # A source 2.5mL vial without concentration is intentionally not mapped.
}
POLICY={
    'version':VERSION,'formal_eligible':False,'development_feedback_informed_change':True,
    'selection_uses_model_outcomes':False,'all_source_patients_retained':True,
    'source_and_current':'Immutable real history plus an explicitly authored hypothetical outpatient episode. No real discharge continuation or source NDC equivalence is claimed.',
    'repair':'The previously chosen disease-related historical material is not silently lost because a second retrieval window excludes it. A fully specified scheduled pattern may be explicitly adopted into the fictional prior plan; source stop times remain unchanged and visible.',
    'existing_window_candidates':'Retain the other v4 candidates and their uncertainties; do not erase polypharmacy or rename unresolved medicines.',
    'unresolved_patterns':'Single-dose/PRN/unknown/conflicting frequency, missing concentration and ambiguous current products remain unresolved; never turn ONCE into daily or a vial volume into a concentration.',
    'prior_intent':'The prior fictional clinician considered the historical disease and selected material. This is not a source-database fact, current diagnosis, appropriate-regimen label or order to renew.',
    'infection':'Preserve the existing infection episode; no antibiotic course extension is invented.',
    'measurements':'No current laboratory/imaging results authored, historical measurements never retimed.',
    'new_execution':'All new results belong to this new development input version. Previous unfinished runs stay in their original denominator and evidence.',
    'references':['https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html',
        'https://mimic.mit.edu/docs/iv/modules/hosp/pharmacy.html',
        'https://www.nice.org.uk/guidance/ng5/chapter/Recommendations'],
}


def build(case, frequency_context):
    if case['group'] not in NAMES:raise ValueError('This repair covers chronic prior-plan context only')
    source=deepcopy(case);selected=source['provenance']['selected_historical_order']
    actual=next((o for o in source['his_initial']['historical_orders'] if o['record_id']==selected['record_id']),None)
    if actual!=selected:raise ValueError('Chosen material must be an unchanged actual historical row')
    entries,exclusions=candidate_plan(source,frequency_context)
    existing=next((e for e in entries if e['source_order']['record_id']==selected['record_id']),None)
    inserted=existing is None
    if inserted:
        existing=regimen_candidate([selected],frequency_context)
        key=lambda o:(o['drug_name_raw'].strip().casefold(),o['regimen_raw'].get('prod_strength'),o['regimen_raw'].get('route'))
        if any(key(e['source_order'])==key(selected) for e in entries):
            existing['reasons'].append('another_current_candidate_for_same_product_requires_reconciliation')
            existing['candidate_state']='unresolved_not_assumed_taken'
        existing['construction_adoption_basis']='explicit_fictional_prior_plan_choice_outside_candidate_window; not_actual_discharge_use'
        entries.append(existing)
    result,audit=build_episode(source,frequency_context,candidate_override=(entries,exclusions))
    core=next(i for i in result['his_initial']['current_use_record']['items'] if i['source_ref']==selected['record_id'])
    dx=next((d for e in source['his_initial']['historical_encounters'] for d in e.get('diagnoses',[])
        if _matches(source['group'],d.get('coding',{}))),None)
    if dx is None:raise ValueError('Historical disease evidence required; group label alone is insufficient')
    current=result['his_initial']['current_use_record'];disease=NAMES[source['group']]
    current['prior_visit']={'source_kind':'constructed','event_offset_hours':24,
        'document_kind':'constructed_prior_outpatient_followup_context',
        'reason':disease+'复诊及既往用药核对',
        'diagnoses':[{'display':dx.get('display'),'coding':deepcopy(dx.get('coding')),
            'source_ref':dx['record_id'],'source_kind':'constructed',
            'status':'fictional_prior_working_diagnosis_not_current_confirmation'}],
        'assessment_scope':'这份新门诊背景单说明复诊来由与先前药物意图；不表示已验证所有合并症、既往停药原因或当前处方。',
        'followup_plan':'六天后复核用药、症状、耐受情况和剩余药量，决定后续治疗；不预定续方。',
        'current_prescribing_decision':'not_preassigned'}
    raw=selected['regimen_raw'];key=(selected['drug_name_raw'].strip().casefold(),str(raw.get('prod_strength') or '').strip().casefold())
    code=PRODUCTS.get(key)
    product=next((p for p in result['formulary'] if p['drug_code']==code),None)
    if code and product is None:raise ValueError('Authored product absent from common formulary')
    if core.get('plan'):
        core['plan'].update(indication='先前虚构门诊针对'+disease+'所选择的药物模式；具体适应证与继续使用均须当前医生判断',
            indication_status='constructed_prior_prescriber_intent_not_source_database_fact',diagnosis_source_ref=dx['record_id'])
        core['indication_status']='constructed_prior_intent_supplied; current_suitability_requires_assessment'
        if product:
            core['current_product']={k:deepcopy(product[k]) for k in
                ('drug_code','name','ingredient','dosage_form','strength','route','dose_unit','quantity_unit')}
            core['current_product'].update(source_kind='constructed',
                identity_basis='explicit_new_episode_formulary_product_assignment_not_historical_NDC_match',
                historical_exact_identity='not_established')
            package=product.get('label_package') or {}
            device=package.get('device') or package.get('device_required')
            if code=='SIM-TIOTROPIUM-18':device='HandiHaler'
            if device:core['current_product']['device']=device
            core['product_identity_status']='constructed_current_product_identified; historical_manufacturer_NDC_unverified'
    facts=result['patient_initial']['patient_facts']
    for f in facts:
        if f['topic']=='chief_complaint':
            f['value']='上次为'+disease+'来复诊，今天按约定回来核对身体情况和药，请医生判断下一步怎么处理。'
    def add(topic,text,refs):
        facts.append({'fact_id':'episode10-'+str(len(facts)+1),'section':'current_episode','topic':topic,'field':topic,
            'value':text,'status':'reported','source_kind':'constructed','source_refs':refs,
            'knowledge_state':'known_from_brought_record','statement_as_of_offset_hours':168,
            'narrative_time_scope':'Fictional prior visit and current brought record, not an actual source outpatient encounter.',
            'construction_version':VERSION})
    add('prior_visit_reason','六天前的门诊单写的是'+disease+'复诊，要回来核对用药和症状。',[dx['record_id']])
    if core.get('plan'):
        add('prior_medication_purpose','上次门诊药单把'+core['name']+'列在'+disease+'的复诊方案中；具体是否继续或调整，请医生检查后判断。',[core['record_id']])
        if product:
            add('current_product_label','这项药袋上的名称和规格是'+product['display_name_zh']+'；可以查看药袋与用法记录。',[core['record_id']])
    result['case_id']=source['case_id']+'_episode10'
    result['provenance'].update(construction_version=VERSION,current_context_policy=deepcopy(POLICY),
        core_source_ref=selected['record_id'],core_added_outside_retrieval_window=inserted,
        new_current_product_code=code if core.get('plan') and product else None,
        historical_exact_product_mapping_claimed=False)
    result['runtime_overlay_provenance']['episode_version']=VERSION
    result['evaluation'].update(reference_facts=deepcopy(facts),clinical_reference_status='pending_independent_review')
    for key in HISTORY_FIELDS:
        if source['his_initial'][key]!=result['his_initial'][key]:raise ValueError('Source history changed')
    validate_case(result)
    chart=HistoryChart(result['his_initial'])
    for section in ('current_use_record','current_use_items'):
        offset=0
        while True:
            page=chart.query(section,offset=offset,limit=5)
            if not verify_projection(page,project_history_page(page)):raise ValueError('Current context projection lost facts')
            if page['next_offset'] is None:break
            offset=page['next_offset']
    return result,{**audit,'selected_core_present':True,'selected_core_scheduled':bool(core.get('plan')),
        'core_added_outside_retrieval_window':inserted,'core_unresolved_reasons':existing['reasons'],
        'constructed_core_product':core.get('current_product',{}).get('drug_code'),
        'source_history_unchanged':True,'clinical_qualified':None}


def prepare(manifest_path,history_manifest_path,private_data_root,output):
    manifest_path,history_manifest_path,private_data_root,output=map(Path,(manifest_path,history_manifest_path,private_data_root,output))
    if output.exists():raise FileExistsError('Retain prior input versions')
    m=json.loads(manifest_path.read_text());hm=json.loads(history_manifest_path.read_text())
    histories={r['patient_key']:r for r in hm['cases']}
    if len(histories)!=len(m['cases']) or set(histories)!={r['patient_key'] for r in m['cases']}:
        raise ValueError('Every original patient must be retained')
    output.mkdir(parents=True,mode=0o700)
    protected={str(p):file_hash(p) for p in (manifest_path,history_manifest_path,Path(__file__),Path(__file__).with_name('current_episode_v4.py'))}
    write(output/'policy_frozen_before_cases.json',{'policy':POLICY,'source_hashes':protected,'product_rules':[
        {'source_name':k[0],'source_strength':k[1],'constructed_product_code':v} for k,v in PRODUCTS.items()]})
    rows=[];audits=[]
    for e in m['cases']:
        path=Path(e['path']);protected[str(path)]=file_hash(path)
        if e.get('sha256',e.get('file_sha256'))!=protected[str(path)]:raise ValueError('Input hash changed')
        source=json.loads(path.read_text());hrow=histories[source['patient_key']];hp=Path(hrow['path'])
        protected[str(hp)]=file_hash(hp);historical=json.loads(hp.read_text())
        if source['group']!=historical['group'] or source['split']!=historical['split']:raise ValueError('History partition differs')
        if source['group'] in SYMPTOMS:
            context,hashes,_=load_frequency_context(source,private_data_root);protected.update(hashes)
            case,audit=build(source,context);case=add_adult_population(case)
            for section in ('historical_administrations','historical_microbiology','historical_outpatient_measurements'):
                if section in historical['his_initial']:case['his_initial'][section]=deepcopy(historical['his_initial'][section])
            case['provenance']['supplemental_history_parent']={'case_id':historical['case_id'],'sha256':protected[str(hp)]}
        else:
            case=deepcopy(historical);case['case_id']+='_retained10';audit={'infection_episode_unchanged':True,'clinical_qualified':None}
        validate_case(case)
        target=output/'cases'/(case['case_id']+'.json');write(target,case)
        rows.append({**{k:case[k] for k in ('case_id','patient_key','group','split','data_kind')},'path':str(target.resolve()),
            'file_sha256':file_hash(target),'case_hash':digest(case),'runtime_input_hash':digest(runtime_case(case)),
            'source_case_id':source['case_id'],'formal_eligible':False})
        audits.append({'case_id':case['case_id'],'group':case['group'],**audit})
    if any(file_hash(p)!=h for p,h in protected.items()):raise ValueError('A source changed during preparation')
    write(output/'manifest.json',{'version':VERSION,'cases':rows,'case_count':len(rows),'formal_eligible':False,
        'all_original_patients_retained':True,'selection_uses_model_outcomes':False,'policy':POLICY})
    write(output/'qualification_private.json',{'cases':audits})
    write(output/'input_freeze.json',{'source_files':protected,'files':[{'path':r['path'],'sha256':r['file_sha256']} for r in rows]})
    summary={'version':VERSION,'cases':len(rows),'groups':dict(Counter(r['group'] for r in rows)),
        'chronic_scheduled_core_cases':sum(a.get('selected_core_scheduled',False) for a in audits),
        'explicit_constructed_core_products':sum(bool(a.get('constructed_core_product')) for a in audits),
        'core_recovered_outside_secondary_window':sum(a.get('core_added_outside_retrieval_window',False) for a in audits),
        'clinical_qualified':None,'new_source_patients':0,'new_current_measurements':0}
    write(output/'summary.json',summary);return summary


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','history-manifest','private-data-root','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.manifest,a.history_manifest,a.private_data_root,a.output)))
