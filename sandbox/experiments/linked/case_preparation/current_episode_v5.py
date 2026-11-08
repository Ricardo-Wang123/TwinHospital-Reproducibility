"""Versioned prior-visit context over v4.2.2 development episodes.

No outcomes are read, no source record is overwritten, and no clinical approval
or reference prescription is supplied. This is explicit scenario authoring.
"""
from copy import deepcopy

from ..cases import validate_case
from ..chart import HistoryChart
from ..contracts import digest

VERSION = 'constructed-prior-visit-context-v5.0'
PROFILES = {
    'type2_diabetes': {
        'name': '二型糖尿病', 'drug_prefix': 'metformin xr', 'patient_purpose':'控制血糖',
        'indication': '既往二型糖尿病的血糖管理',
        'reference': 'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=ef77c7dd-a65f-4adc-a0d9-a93779b459a0'},
    'heart_failure': {
        'name': '心力衰竭', 'drug_prefix': 'metoprolol succinate', 'patient_purpose':'心力衰竭的长期治疗',
        'indication': '既往心力衰竭的长期药物管理；不据此推定当前射血分数或治疗充分性',
        'reference': 'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=58441cd3-6043-4bb8-adab-1b32a203bfb2'},
    'copd': {
        'name': '慢阻肺', 'drug_prefix': 'fluticasone-salmeterol diskus (250/50)', 'patient_purpose':'慢阻肺的日常维持治疗，不是临时救急用',
        'indication': '既往慢阻肺的维持治疗；不是急性发作时的救急药',
        'reference': 'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=4eeb5f6a-593f-4a9e-9692-adefa2caf8fc'},
}
POLICY = {
    'version': VERSION, 'model_outputs_read': False, 'clinical_reference_status': 'independent_review_pending',
    'scope': 'Three fixed development sources; not an infection or whole-cohort construction policy.',
    'prior_visit': 'Explicit fictional follow-up at the existing plan time, using a source-supported historical disease as its reason. Neither source-database outpatient history nor a confirmed current diagnosis is claimed.',
    'indication': 'An authored prior-prescriber intention for the listed core medicine only; label indication does not establish patient-specific suitability, exact historical product identity, dose correctness or complete treatment.',
    'unchanged': ['historical records', 'source allergy status', 'allergy patient report', 'medication amounts and schedules',
                  'current vital values', 'lack of new laboratory testing', 'source patient and partition'],
    'current_decision': 'Independent agent may prescribe, change, stop, defer or refer. No normal-control or success label is assigned.',
    'references': ['https://www.nice.org.uk/guidance/ng5/chapter/Recommendations',
                   'https://www.nice.org.uk/guidance/cg183/chapter/Recommendations'] + [p['reference'] for p in PROFILES.values()],
}


def _matches(group, coding):
    code=str(coding.get('code','')).upper().replace('.',''); version=str(coding.get('version',''))
    if version=='10':
        return code.startswith({'type2_diabetes':'E11','heart_failure':'I50','copd':'J44'}[group])
    if version=='9':
        if group=='type2_diabetes': return code.startswith('250') and len(code)>=5 and code[4] in '02'
        return code.startswith(('428',) if group=='heart_failure' else ('491','492','496'))
    return False


def build_context(case, *, profile=None, version=VERSION, policy=None, suffix='_context5', fact_prefix='episode5'):
    if case.get('provenance',{}).get('construction_version')!='constructed-current-episode-v4-preview-2.2':
        raise ValueError('Requires a frozen v4.2.2 episode')
    if case['group'] not in PROFILES: raise ValueError('No reviewed construction policy for this stratum')
    profile=profile or PROFILES[case['group']]; policy=policy or POLICY; result=deepcopy(case)
    diagnosis=next((d for enc in case['his_initial']['historical_encounters'] for d in enc.get('diagnoses',[])
                    if _matches(case['group'],d.get('coding',{}))),None)
    if diagnosis is None: raise ValueError('Cannot author this prior-visit reason without a matching historical diagnosis')
    current=result['his_initial']['current_use_record']
    core=[item for item in current['items'] if item.get('plan') and
          item.get('name','').strip().casefold().startswith(profile['drug_prefix'])]
    if len(core)!=1: raise ValueError('Requires exactly one source-linked core medication plan')
    visit_time=core[0]['plan']['issued_offset_hours']
    prior_dx={k:deepcopy(diagnosis[k]) for k in ('display','coding') if k in diagnosis}
    prior_dx.update(source_ref=diagnosis['record_id'], source_kind='constructed',
                    status='constructed_prior_visit_working_diagnosis_not_current_confirmation')
    current['prior_visit']={
        'source_kind':'constructed', 'document_kind':'constructed_prior_outpatient_followup_context',
        'event_offset_hours':visit_time, 'reason':profile['name']+'复诊、症状和既往药物核对',
        'diagnoses':[prior_dx],
        'assessment_scope':'记录这次模拟门诊的来由和先前用药意图；不是已验证的最佳方案，也不代表全部合并症已处理。',
        'followup_plan':'六天后复核实际用药、症状、耐受情况和剩余药量，决定是否续方、调整或进一步检查。',
        'current_prescribing_decision':'not_preassigned'}
    core[0]['plan'].update(indication=profile['indication'],
        indication_status='constructed_prior_prescriber_intent_not_source_database_fact',
        diagnosis_source_ref=diagnosis['record_id'], reference_url=profile['reference'])
    core[0]['indication_status']='constructed_prior_intent_supplied; current_suitability_requires_assessment'
    facts=result['patient_initial']['patient_facts']
    for fact in facts:
        if fact['topic']=='chief_complaint':
            fact['value']=f"上次为{profile['name']}来复诊，今天按约定回来核对身体情况和药。药按原来的用法还够大约一天，想请医生看看后面怎么处理。"
            fact['construction_version']=version
    def add(topic,text,refs=()):
        facts.append({'fact_id':fact_prefix+'-'+str(len(facts)+1).zfill(3), 'section':'current_episode',
            'topic':topic,'field':topic,'value':text,'status':'reported', 'source_kind':'constructed',
            'source_refs':list(refs),'knowledge_state':'known_from_brought_record',
            'statement_as_of_offset_hours':168,
            'narrative_time_scope':'Recollection of the fictional clinic visit six days ago; not a new present-time diagnosis.',
            'construction_version':version})
    add('prior_visit_reason',f"六天前的门诊单写的是{profile['name']}复诊，要我回来核对用药、症状和剩余药量。",[diagnosis['record_id']])
    add('prior_medication_purpose',f"药单上说{core[0]['name']}这项是{profile['patient_purpose']}。具体要不要改，今天想请医生看看。",[core[0]['record_id']])
    result['case_id']=case['case_id']+suffix
    result['provenance'].update(construction_version=version,
        prior_episode_sha256=digest(case), prior_episode_case_id=case['case_id'],
        prior_episode_provenance=deepcopy(case['provenance']), current_context_policy=deepcopy(policy))
    result['provenance']['constructed_fields']+=['his_initial.current_use_record.prior_visit',
        'his_initial.current_use_record.items.core.plan.indication','patient_initial.patient_facts.prior_visit_context']
    result['runtime_overlay_provenance']['episode_version']=version
    result['evaluation'].update(reference_facts=deepcopy(facts), clinical_current_suitability='pending_independent_review')
    validate_case(result)
    for key in ('patient_key','group','split','data_kind','formulary'):
        if result[key]!=case[key]: raise ValueError('Identity/partition/formulary changed')
    for key in ('historical_encounters','historical_orders','historical_observations','allergy_record','current_observations'):
        if result['his_initial'][key]!=case['his_initial'][key]: raise ValueError('Source/measurement/allergy changed')
    for old,new in zip(case['his_initial']['current_use_record']['items'],current['items'],strict=True):
        for key in ('name','dose','dose_unit','frequency','route','actual_use','supply'):
            if old.get(key)!=new.get(key):raise ValueError('Medication schedule/use/supply changed')
    projected=HistoryChart(result['his_initial']).initial_view()
    # Small, structured prior context is visible without a hidden evaluator read.
    if projected['current_use_record'].get('prior_visit')!=current['prior_visit']:
        raise ValueError('Prior-visit context was dropped by chart projection')
    return result, {'version':version,'historical_data_unchanged':True,'measurements_unchanged':True,
        'medication_schedule_unchanged':True,'allergy_unchanged':True,'prior_visit_visible':True,
        'core_indication_count':len(core),'clinical_sufficiency':'pending','formal_eligible':False}
