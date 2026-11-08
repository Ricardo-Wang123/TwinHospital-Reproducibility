"""Explicit new infection presentations over preserved historical material.

Development scenario authoring, not a reconstruction of observed outpatient
visits. No chosen prescription, antimicrobial duration or approval is supplied.
The previous post-discharge followup remains an immutable different version.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
import os
from pathlib import Path

from .formulary_overlay import file_hash, write
from .current_episode_v4 import historical_patient_memory
from ..cases import validate_case
from ..chart import HistoryChart
from ..chart_projection import project_history_page, verify_projection
from ..contracts import digest,runtime_case

VERSION='constructed_new_infection_presentation_v11_specimen_checked'
VISIT_HOURS=90*24
POLICY={
    'version':VERSION,'formal_eligible':False,'development_feedback_informed_change':True,
    'case_selection_uses_model_outcomes':False,'all_original_patients_retained':True,
    'reason':'A post-discharge patient with remaining antibiotics may have no indication for a new dispensing. That is retained as an incomplete medication workflow, not converted to success. This version defines a distinct new symptomatic presentation for testing the complete workflow.',
    'time':'Hypothetical visit 90 days after the historical cutoff. This is not an observed visit or a claim about the source patient surviving, recovering or following treatment during that interval.',
    'source_history':'All historical encounters, orders, administrations, tests and source allergy facts remain unchanged at original relative times. No future source outcome is read.',
    'symptoms':'Explicit fictional symptoms of a new episode; source disease category is not a confirmed current diagnosis.',
    'current_medicines':'Explicit constructed patient report of no medicines taken in the last 72 hours. Historical prescriptions do not establish a current regimen; absence of treatment is not a claim that historical chronic medicines were unnecessary.',
    'current_tests':'New simulated examination/imaging/urine findings are marked constructed. Creatinine and potassium use a declared value-persistence assumption from the latest time-eligible historical numeric Blood result with the same unit; missing specimen or result remains missing, abnormal values are never normalized. These new scenario observations are not MIMIC measurements at the new time.',
    'separation':'Patients know symptoms and reported use only; current test findings are supplied to the hospital physician view. No current diagnosis, prescription, approval or human label is injected.',
    'quality':'Clinical plausibility and task quality require independent review. This engineering fixture is not a representative natural-history cohort.',
    'references':['https://www.nice.org.uk/guidance/ng250/chapter/Recommendations',
        'https://www.nice.org.uk/guidance/ng109/chapter/Recommendations',
        'https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html',
        'https://mimic.mit.edu/docs/iv/modules/hosp/d_labitems.html'],
}


def last_numeric(his,labels,unit,fluid='Blood'):
    candidates=[]
    for r in his.get('historical_observations',[]):
        if r.get('display') not in labels or r.get('unit_raw')!=unit or r.get('fluid')!=fluid:continue
        t=r.get('time',{});event=t.get('event_offset_hours');recorded=t.get('recorded_offset_hours')
        if (type(event) not in (int,float) or type(recorded) not in (int,float)
                or max(event,recorded)>0 or t.get('available_by_source_cutoff') is not True):continue
        try:value=float(r.get('numeric_value_raw'))
        except (TypeError,ValueError):continue
        if not math.isfinite(value) or value<=0:continue
        candidates.append((event,recorded,str(r.get('record_id','')),value,r))
    return max(candidates,key=lambda x:x[:3])[-1] if candidates else None


def build(source):
    if source['group'] not in {'pneumonia','uti'}:raise ValueError('An infection stratum is required')
    case=deepcopy(source);h=case['his_initial'];facts=[];observations=[]
    def fact(topic,text,*,status='reported',refs=()):
        facts.append({'fact_id':'episode11-'+str(len(facts)+1).zfill(3),'section':'current_episode',
            'topic':topic,'field':topic,'value':text,'status':status,'source_kind':'constructed',
            'source_refs':list(refs),'knowledge_state':'known_self_experience',
            'statement_as_of_offset_hours':VISIT_HOURS,'construction_version':VERSION})
    def measured(name,value,unit=None,*,basis='independently_authored_scenario_measurement',source_ref=None,fluid=None):
        row={'record_id':'obs_'+digest({'case':source['case_id'],'v':VERSION,'name':name})[:24],
            'name':name,'value':value,'unit':unit,'source_kind':'constructed','status':'simulated_new_visit_observation',
            'available_at':'physician_consultation','construction_basis':basis,
            'time':{'basis':'source_relative_to_anchor_discharge','event_offset_hours':VISIT_HOURS,
                'recorded_offset_hours':VISIT_HOURS,'event_time_known':True,'available_by_source_cutoff':False,
                'availability_basis':'constructed_new_episode_not_a_source_measurement'}}
        if source_ref:row['source_ref']=source_ref
        if fluid is not None:row['fluid']=fluid
        observations.append(row)
    if case['group']=='pneumonia':
        fact('chief_complaint','这三天新出现咳嗽、有黄痰、发热，咳嗽时右胸有点疼，想请医生检查。')
        fact('timeline','大约三天前开始，这两天咳嗽和痰比刚开始多；不是上次住院症状一直延续。')
        fact('severity','活动时比平时容易累，坐着休息能完整说话；没有咳血、晕倒、突然意识混乱或持续压榨样胸痛。')
        fact('oxygen_support','这三天没有使用吸氧设备。')
        measured('temperature',38.0,'degC');measured('respiratory_rate',20,'/min')
        measured('pulse',90,'/min');measured('SpO2',96,'%')
        measured('chest_examination','右下肺可闻局灶湿啰音；没有记录到明显呼吸窘迫。')
        measured('chest_radiograph','本次影像见右下肺局灶性浸润影；未见胸腔积液。须结合当前症状和病史判断。')
    else:
        fact('chief_complaint','这两天新出现尿痛、尿频，尿完后下腹仍有一点不舒服，想请医生检查。')
        fact('timeline','大约两天前开始，症状持续；不是上次住院症状一直延续。')
        fact('upper_tract_symptoms','这两天没有发热、寒战、腰背或侧腹痛，也没有反复呕吐。')
        fact('urinary_background','目前没有留置尿管；能自行排尿，没有尿不出来或肉眼血尿。')
        if h['demographics'].get('sex')=='M':fact('prostate_symptoms','这两天没有会阴痛、睾丸疼痛、尿潴留或发热。')
        measured('temperature',36.8,'degC');measured('respiratory_rate',16,'/min')
        measured('pulse',78,'/min');measured('SpO2',97,'%')
        measured('urine_nitrite','positive');measured('urine_leukocyte_esterase','positive')
        measured('urine_leukocytes',25,'cells/HPF')
    measured('blood_pressure','122/76','mmHg')
    measured('oxygen_support','room_air; no supplemental oxygen during this simulated measurement')
    measured('mental_status','当前清醒，能正确回答简单定向问题。')
    renal_sources={}
    for name,labels,unit in (('creatinine',{'Creatinine','Creatinine, Whole Blood'},'mg/dL'),
                            ('potassium',{'Potassium','Potassium, Whole Blood'},'mEq/L')):
        r=last_numeric(h,labels,unit)
        if r:
            measured(name,float(r['numeric_value_raw']),unit,basis='explicit_value_persistence_assumption_not_observed_MIMIC_current_result',source_ref=r['record_id'],fluid=r['fluid'])
            renal_sources[name]={'historical_record_ref':r['record_id'],'historical_time':deepcopy(r['time']),
                'source_fluid':r['fluid'],'source_value_unchanged':True,'new_measurement_source_kind':'constructed'}
    fact('oral_intake','这几天能吃饭、喝水和吞咽药物，没有反复呕吐。')
    fact('current_medications','这三天没有服用或使用任何药物，也没有自行买抗生素、止痛药、草药或保健品；现在没有待继续服用的抗生素或剩余疗程。')
    fact('history_material','以前住院的资料在医院档案中；旧药单不是我这次正在使用的药，请医生一起核对。')
    for item in historical_patient_memory(source):
        fact('past_medical_history','以前医生告诉过我有'+item['lay_name']+'。',refs=item['source_refs'])
    original_allergy=deepcopy(h.get('allergy_record',{'status':'unknown','items':[]}))
    # Do not assign a negative allergy report from an empty MIMIC table.
    fact('allergy_history','以前药物过敏的细节我不能确定，请帮我查已有档案；这三天没有用药后起风团或嘴唇舌头肿胀的经历。')
    if h['demographics'].get('sex')=='F':fact('pregnancy_status','我目前没有怀孕，也没有哺乳。')
    fact('function','这几天能清楚交流；没有新发生的晕倒或意识改变。日常活动限制请结合既往病史判断。')
    fact('followup_questions','想请医生判断原因、是否需要用药，以及后面如何观察和复查。')
    h['time_basis']={'history_offset_origin':'source_anchor_discharge','new_visit_offset_hours':VISIT_HOURS,
        'source_mode':'historical_material_plus_explicit_counterfactual_episode',
        'meaning':'A hypothetical new presentation; not an observed post-discharge course or a survival/outcome claim.'}
    h['current_observations']=observations
    h['current_use_record']={'status':'reported_no_use_in_defined_window','source_kind':'constructed',
        'as_of_offset_hours':VISIT_HOURS,'items':[],'current_plan_item_count':0,
        'reconciliation_status':'current_team_verification_pending',
        'scope_status':'complete_patient_report_for_last_72_hours_not_a_source_longitudinal_claim',
        'nonprescription_report':{'status':'reported_none','source_kind':'constructed','value':'最近72小时无药物使用。'},
        'coverage_caveat':'The explicit fictional no-use report does not imply historical chronic treatment was unnecessary; assess current needs independently.'}
    h['allergy_record']=deepcopy(original_allergy)
    h['allergy_record']['patient_report']={'status':'unable_to_ascertain','source_kind':'constructed',
        'as_of_offset_hours':VISIT_HOURS,'value':'患者不能确认既往过敏细节；查阅原历史记录。'}
    for d in (h['demographics'],case['patient_initial']['demographics']):
        d['age_time_basis']='Approximate source-derived age for hypothetical near-future episode; no exact birthday or survival claim; source90+ remains censored.'
    case['patient_initial']['patient_facts']=facts
    case['patient_initial']['communication_profile']={'style':'自然口语；不熟悉术语请对方解释',
        'intentional_misreport':False,'automatic_age_memory_penalty':False,
        'unlisted_fact_policy':'Do not invent an unlisted fact. Current test results are not patient memory. The declared no-use window concerns medicines, not absence of historical diseases.'}
    case['case_id']=source['case_id']+'_infection11s'
    case['provenance']={'source_dataset':'MIMIC-IV 2.2','uses_patient_records':True,
        'construction_version':VERSION,'parent_case_id':source['case_id'],'parent_case_hash':digest(source),
        'parent_provenance':deepcopy(source.get('provenance')),'construction_policy':deepcopy(POLICY),
        'current_lab_persistence_assumptions':renal_sources,
        'previous_current_episode_retained_in_parent':True,'source_allergy_record':original_allergy}
    case['evaluation'].update(reference_facts=deepcopy(facts),required_facts=[],prescription_labels=None,risk_labels=None,
        clinical_reference_status='pending_independent_review',prescribing_required=False,
        input_qualification={'structural':'checked','clinical_sufficiency':'pending_independent_review'})
    case['runtime_overlay_provenance']={'episode_version':VERSION,'new_run_required':True}
    for key,value in source['his_initial'].items():
        if key.startswith('historical_') and h[key]!=value:raise ValueError('Historical source content changed')
    if len(observations)>20:raise ValueError('Current measurement panel exceeds controller limit')
    validate_case(case);chart=HistoryChart(h)
    page=chart.query('current_observations',limit=20)
    if not verify_projection(page,project_history_page(page)):raise ValueError('Measurement projection lost data')
    return case,{'source_history_unchanged':True,'current_observations':len(observations),
        'constructed_lab_persistence_count':len(renal_sources),'current_tests_are_source_observations':False,
        'prescription_preassigned':False,'clinical_qualified':None}


def prepare(manifest_path,output):
    manifest_path,output=Path(manifest_path),Path(output)
    if output.exists():raise FileExistsError('Retain every previous input version')
    output.mkdir(parents=True,mode=0o700);m=json.loads(manifest_path.read_text());rows=[];audits=[]
    protected={str(manifest_path):file_hash(manifest_path),str(Path(__file__)):file_hash(__file__)}
    write(output/'policy_frozen_before_cases.json',POLICY)
    for e in m['cases']:
        path=Path(e['path']);protected[str(path)]=file_hash(path)
        if protected[str(path)]!=e['file_sha256']:raise ValueError('Input bytes differ')
        source=json.loads(path.read_text())
        if source['group'] in {'pneumonia','uti'}:
            case,audit=build(source)
            target=output/'cases'/(case['case_id']+'.json');write(target,case)
            row={**e,'case_id':case['case_id'],'path':str(target.resolve()),'file_sha256':file_hash(target),
                'case_hash':digest(case),'runtime_input_hash':digest(runtime_case(case))}
            audits.append({'case_id':case['case_id'],'group':case['group'],**audit})
        else:row=deepcopy(e)
        rows.append(row)
    if any(file_hash(p)!=v for p,v in protected.items()):raise ValueError('Input changed during construction')
    write(output/'manifest.json',{'version':VERSION,'cases':rows,'case_count':len(rows),'formal_eligible':False,
        'policy':POLICY,'all_original_patients_retained':True,'source_manifest':str(manifest_path.resolve())})
    write(output/'qualification_private.json',{'cases':audits})
    write(output/'input_freeze.json',{'source_files':protected,'files':[{'path':r['path'],'sha256':r['file_sha256']} for r in rows]})
    summary={'version':VERSION,'cases':len(rows),'new_infection_episodes':len(audits),'unchanged_chronic_cases':len(rows)-len(audits),
        'groups':dict(Counter(r['group'] for r in rows)),'clinical_qualified':None,'new_model_calls':0}
    write(output/'summary.json',summary);return summary


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(prepare(a.manifest,a.output)))
