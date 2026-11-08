"""Independent fictional inputs for execution testing, never a clinical gold set.

All five profiles use one schema. This constructor reads no MIMIC material or
model outcomes. It supplements observable current-visit information, not the
prescription, approval, signed note or payment that the agents must generate.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
from ..cases import build_synthetic_case, validate_case
from ..contracts import digest

VERSION='independent-execution-input-v2'
# Explicit authoring choices for independent fictional prior plans, never a
# nearest-product conversion of a real patient's medication or a current Rx key.
PRIOR_PLANS={
    'type2_diabetes':{'drug_code':'SIM-METFORMIN-500','dose':500,'dose_unit':'mg','route':'oral','frequency':'BID'},
    'heart_failure':{'drug_code':'SIM-FUROSEMIDE-20','dose':20,'dose_unit':'mg','route':'oral','frequency':'QD'},
    'copd':{'drug_code':'SIM-TIOTROPIUM-18','dose':1,'dose_unit':'capsule','route':'inhaled','frequency':'QD'},
}
REFERENCES={
    'source_separation':'https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html',
    'medication_reconciliation':'https://www.nice.org.uk/guidance/ng5/chapter/Recommendations',
    'pneumonia':'https://www.nice.org.uk/guidance/ng250/chapter/recommendations',
    'uti':'https://www.nice.org.uk/guidance/ng109/chapter/recommendations',
}
POLICY={'version':VERSION,'uses_patient_records':False,'uses_model_outcomes':False,
    'purpose':'Business workflow execution and input-interface testing; no clinical reference label is implied.',
    'population':'Explicit fictional adult outpatient profiles; five scenario templates are not 1000 independent clinical disease patterns.',
    'clinical_reference_status':'pending_independent_review',
    'current_measurements':'Explicit constructed observations available to the physician, never copied from historical measurements.',
    'decisions':'The physician chooses the diagnosis, prescription, alteration, deferral or referral. No prescription or approval is forced.',
    'failure_policy':'All planned cases and clinical dispositions remain in the assigned denominator.',
    'references':REFERENCES}


def build_execution_case(group,index,formulary,*,split='engineering'):
    case=build_synthetic_case(group,index,split=split)
    base_hash=digest(case)
    case['case_id']='exec2_'+case['case_id'];case['patient_key']='exec2_'+case['patient_key']
    case['formulary']=deepcopy(formulary)
    h=case['his_initial'];facts=case['patient_initial']['patient_facts']
    for demographics in (h['demographics'],case['patient_initial']['demographics']):
        demographics.update(age_status='exact',age_time_basis='constructed_current_encounter')
    h['time_basis']={'history_offset_origin':'constructed_current_encounter','new_visit_offset_hours':0,
                'source_mode':'independently_constructed','meaning':'Relative time in this fictional encounter only.'}
    # A current measurement is an authored scenario fact, not a normal value
    # inferred from an absent MIMIC observation or an automatic safety clearance.
    measurements={item['name']:deepcopy(item) for item in h['current_observations']}
    defaults={'heart_rate':(76,'bpm'),'respiratory_rate':(18,'breaths/min'),
        'systolic_blood_pressure':(124,'mmHg'),'diastolic_blood_pressure':(76,'mmHg'),
        'temperature':(36.8,'degC'),'SpO2':(97,'%'),'creatinine':(0.9,'mg/dL'),
        'potassium':(4.1,'mmol/L'),'weight':(68,'kg')}
    for name,(value,unit) in defaults.items():measurements.setdefault(name,{'name':name,'value':value,'unit':unit})
    if group=='pneumonia':
        measurements['chest_radiograph']={'name':'chest_radiograph','value':
            '新出现右下肺局灶性浸润影；未见胸腔积液。需结合本次症状和体征判断。','unit':None}
    if group=='uti':
        measurements.update({
            'urine_nitrite':{'name':'urine_nitrite','value':'positive','unit':None},
            'urine_leukocyte_esterase':{'name':'urine_leukocyte_esterase','value':'positive','unit':None},
            'urine_leukocytes':{'name':'urine_leukocytes','value':25,'unit':'cells/HPF'}})
    h['current_observations']=[{**item,'source_kind':'constructed','available_at':'physician_consultation',
        'time':{'basis':'constructed_current_encounter','event_offset_hours':0,'event_time_known':True}}
        for item in measurements.values()]

    def add(topic,text):
        facts.append({'fact_id':'exec2-'+str(len(facts)+1),'topic':topic,'field':topic,
            'value':text,'status':'reported','source_kind':'constructed','source_refs':[],
            'construction_version':VERSION})
    add('communication','能描述自己的日常症状和用药，不熟悉医学术语时会请对方解释。')
    add('recent_medication_changes','近三个月没有自行加药，也没有服用抗生素、保健品或中草药；实际用药以我带来的药袋和问诊核对为准。')
    if group in {'pneumonia','uti'}:
        add('current_treatment','这次出现症状后还没有使用抗感染药物，希望医生检查后决定怎么治疗。')
        add('relevant_background','没有被诊断过肾病或免疫系统疾病，近期没有住院或插过导尿管。')
    if h['demographics']['sex']=='F':add('pregnancy_status','目前没有怀孕，也没有哺乳。')
    if group in {'type2_diabetes','heart_failure','copd'}:
        med=deepcopy(PRIOR_PLANS[group])
        product=next((p for p in formulary if p['drug_code']==med['drug_code']),None)
        if product is None:raise ValueError('Declared prior product is absent from the shared hospital formulary')
        h['historical_orders']=[{**deepcopy(med),'record_id':'rx_'+digest(case['case_id'])[:24],
            'drug_name':product['name'],'evidence_type':'historical_order','source_kind':'constructed',
            'current_use_inferred':False}]
        for fact in facts:
            if fact['topic']=='medications':
                fact['value']=f"自述目前使用{product['display_name_zh']}，每次{med['dose']} {med['dose_unit']}，频次{med['frequency']}；药名和用法记在药袋上。"
        daily={'BID':2,'QD':1,'QID':4}[med['frequency']]
        h['current_use_record']={'status':'patient_report_with_prior_plan','source_kind':'constructed',
            'reconciliation_status':'not_yet_verified_by_current_visit_team',
            'prior_visit':{'source_kind':'constructed','event_offset_hours':-144,
                'reason':'已知慢性病的日常治疗和用药复核','current_prescribing_decision':'not_preassigned'},
            'as_of_offset_hours':0,'current_plan_item_count':1,
            'items':[{'record_id':'rx_'+digest({'case':case['case_id'],'current_item':1})[:24],
                'source_kind':'constructed','name':product['name'],
                'dose':med['dose'],'dose_unit':med['dose_unit'],'route':med['route'],'frequency':med['frequency'],
                'current_use_status':'reported_taken_in_defined_episode','status':'patient_report_with_prior_plan',
                'current_product':{**deepcopy(product),'source_kind':'constructed',
                    'identity_basis':'explicit_independent_scenario_product_assignment'},
                'plan':{'source_kind':'constructed','issued_offset_hours':-144,'start_offset_hours':-144,
                    'review_due_offset_hours':0,'purpose':'Medication and symptom review after a short initial supply; no current renewal predetermined.'},
                'actual_use':{'source_kind':'constructed','window_start_offset_hours':-144,'window_end_offset_hours':0,
                    'planned_doses':6*daily,'reported_taken_doses':6*daily,'missed_doses':0,'additional_doses':0,
                    'last_taken_offset_hours':-8},
                'supply':{'source_kind':'constructed','initial_dose_equivalents':7*daily,
                    'used_dose_equivalents':6*daily,'remaining_dose_equivalents':daily,
                    'meaning':'One dose equivalent is one administration of the stated dose; supply is not intended treatment duration.'},
                'indication_status':'requires_current_review'}],
            'coverage_caveat':'Patient-reported current use; current clinician reconciliation is not already completed.'}
        add('current_product_label','药袋上写的是'+product['display_name_zh']+'，目前按原来的用法使用，余量约够一天。今天想请医生看看后面是否需要续方或调整。')
    else:
        h['current_use_record']={'status':'patient_reported_none_current','source_kind':'constructed','items':[],
            'patient_report':{'status':'reported','value':'这次出现症状后尚未使用药物，近三个月未用抗生素。',
                'source_kind':'constructed','as_of_offset_hours':0},
            'reconciliation_status':'not_yet_verified_by_current_visit_team'}
    h['allergy_record']['patient_report']={'status':'unknown' if index%4==0 else 'reported_none_known',
        'source_kind':'constructed','verification_status':'not_independently_verified','as_of_offset_hours':0}
    case['evaluation'].update(reference_facts=deepcopy(facts),required_facts=deepcopy(facts),
        required_fact_ids=[f['fact_id'] for f in facts],clinical_reference_status='pending_independent_review',
        prescription_labels=None,risk_labels=None,required_fact_selection_status='candidate_items_require_independent_applicability_check')
    case['provenance'].update(construction_version=VERSION,parent_independent_case_hash=base_hash,
        construction_policy=deepcopy(POLICY),uses_patient_records=False,source_dataset=None,
        source_kind='independently_constructed')
    validate_case(case)
    return case


def prepare(output,formulary_path,*,count=1,start_index=100101,split='engineering'):
    """Freeze independently authored inputs before any model result is read."""
    from ..cases import GROUPS,write_case,validate_patient_splits
    from ..chart import HistoryChart
    from ..provider import private_json
    registry_path=Path(__file__).with_name('classification_v1.json')
    subtype_registry=json.loads(registry_path.read_text())['source_subtypes']
    if count<1:raise ValueError('Positive cases per disease required')
    output=Path(output).resolve();formulary_path=Path(formulary_path).resolve()
    if output.exists():raise FileExistsError('Use a new input version directory')
    formulary=json.loads(formulary_path.read_text());rows=[];cases=[]
    output.mkdir(parents=True,mode=0o700)
    for index in range(start_index,start_index+count):
        for group in GROUPS:
            case=build_execution_case(group,index,formulary,split=split)
            HistoryChart(case['his_initial'])  # no clinical text or raw IDs lost at the boundary
            p=output/'cases'/(case['case_id']+'.json');write_case(p,case);cases.append(case)
            rows.append({**{k:case[k] for k in ('case_id','patient_key','group','data_kind','split')},
                'system':subtype_registry[group]['system'],'path':str(p),'file_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                'clinical_reference_status':'pending_independent_review','formal_eligible':False})
    validate_patient_splits(cases)
    manifest={'version':VERSION,'cases':rows,'case_count':len(rows),'counts_per_disease':count,
        'start_index':start_index,'formal_eligible':False,'policy':POLICY,
        'prior_plan_authoring_choices':PRIOR_PLANS,'independent_scenario_templates':5,
        'execution_order':'index then fixed disease order','model_outcomes_read':False}
    private_json(output/'manifest.json',manifest)
    sources=[Path(__file__),Path(__file__).parent.parent/'cases.py',formulary_path,registry_path]
    private_json(output/'input_freeze.json',{'source_files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'manifest_sha256':hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest(),
        'files':[{'path':r['path'],'sha256':r['file_sha256']} for r in rows]})
    return {'case_count':len(rows),'counts_per_disease':count,'output':str(output),'uses_patient_records':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--formulary',type=Path,required=True);p.add_argument('--count',type=int,default=1)
    p.add_argument('--start-index',type=int,default=100101);p.add_argument('--split',default='engineering')
    a=p.parse_args();os.umask(0o077)
    print(json.dumps(prepare(a.output,a.formulary,count=a.count,start_index=a.start_index,split=a.split)))
