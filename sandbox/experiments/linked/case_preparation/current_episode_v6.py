"""Source-only engineering selection and explicit current product construction.

This deliberately selected engineering subset cannot estimate cohort performance.
Prior failures, original cohort assignment and every historical record are retained.
"""
from copy import deepcopy
from decimal import Decimal

from ..cases import validate_case
from ..chart import HistoryChart
from ..contracts import digest
from .current_episode_v4 import build_episode, candidate_plan, HISTORY_FIELDS
from .current_episode_v5 import build_context

VERSION = 'constructed-basic-engineering-episode-v6.0'
PROFILES = {
    'type2_diabetes': {
        'name':'二型糖尿病','drug_prefix':'metformin (glucophage)',
        'dose':'500','dose_unit':'mg','frequency':'BID','route':'PO','source_strength':'500mg Tablet',
        'drug_code':'SIM-METFORMIN-500','patient_purpose':'控制血糖',
        'indication':'既往二型糖尿病的血糖管理',
        'reference':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=30658e19-8e6e-49aa-a007-e414224797c5'},
    'heart_failure': {
        'name':'心力衰竭','drug_prefix':'metoprolol succinate',
        'dose':'50','dose_unit':'mg','frequency':'DAILY','route':'PO','source_strength':'50mg XL Tab',
        'drug_code':'SIM-METOPROLOL-SUCCINATE-ER-50','patient_purpose':'心力衰竭的长期治疗',
        'indication':'既往心力衰竭的长期药物管理；不据此推定当前射血分数或治疗充分性',
        'reference':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=58441cd3-6043-4bb8-adab-1b32a203bfb2'},
    'copd': {
        'name':'慢阻肺','drug_prefix':'tiotropium bromide',
        'dose':'1','dose_unit':'CAP','frequency':'DAILY','route':'IH','source_strength':'18mcg Capsule-Inhalation Device',
        'drug_code':'SIM-TIOTROPIUM-18','patient_purpose':'慢阻肺的日常维持治疗，不是临时救急用',
        'indication':'既往慢阻肺的维持治疗；不是急性发作时的救急药',
        'device':'HandiHaler',
        'reference':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=c6343458-617b-41ed-8e96-763e91dc8e90'},
}
POLICY = {
    'version':VERSION,'purpose':'Three deliberately selected basic engineering probes; not a representative clinical evaluation.',
    'model_outputs_read':False,'selection_uses_model_outcomes':False,
    'selection':'Within the frozen development manifest, require one exactly parseable specified core pattern and no recorded source-risk cues. Rank by fewer scheduled candidates, fewer unresolved candidates, then manifest order. Absence of a coded cue is not absence of disease or risk.',
    'case_groups':'Preserve original ICD disease stratum, organ-system parent and patient partition. Never relabel failed runs as a new disease.',
    'prior_failures':'Retained in their original batches; these probes do not replace failed observations or change a denominator.',
    'current_product':'Explicitly author the core product of the separate fictional prior clinic plan from the experiment formulary. Raw historical product identity remains unverified; no exact NDC or manufacturer match is claimed.',
    'unchanged':['source history','source allergy status','medication doses and frequencies','current vital values','absence of current laboratory tests','other candidate medications and uncertainties'],
    'clinical_reference_status':'independent_review_pending','normal_control_label':None,
    'current_decision':'No required prescription or approval; physician and pharmacist may defer.',
    'references':[p['reference'] for p in PROFILES.values()] + [
        'https://www.nice.org.uk/guidance/ng5/chapter/Recommendations',
        'https://www.nice.org.uk/guidance/cg183/chapter/Recommendations'],
}


def core_match(entry, profile):
    if entry['reasons']: return False
    order=entry['source_order']; r=order['regimen_raw']
    return (order.get('drug_name_raw','').strip().casefold().startswith(profile['drug_prefix'])
        and Decimal(str(entry['dose']))==Decimal(profile['dose'])
        and r.get('dose_unit_rx')==profile['dose_unit'] and r.get('route')==profile['route']
        and entry['frequency_raw']==profile['frequency'] and r.get('prod_strength')==profile['source_strength'])


def source_gate(case, frequency_context):
    if case['group'] not in PROFILES:
        return {'eligible_for_this_probe':False,'reasons':['outside_three_chronic_probe_profiles']}
    entries,_=candidate_plan(case,frequency_context); profile=PROFILES[case['group']]
    core=[e for e in entries if core_match(e,profile)]
    reasons=[]
    if len(core)!=1: reasons.append('requires_one_exact_core_pattern')
    if case.get('evaluation',{}).get('source_risk_cues'): reasons.append('recorded_source_risk_cues_retained_for_complex_probe')
    product=next((p for p in case['formulary'] if p['drug_code']==profile['drug_code']),None)
    required=('drug_code','name','ingredient','dosage_form','strength','route','dose_unit','quantity_unit','display_name_zh')
    if product is None or any(not product.get(k) for k in required):reasons.append('target_product_metadata_incomplete')
    scheduled=sum(not e['reasons'] for e in entries)
    return {'eligible_for_this_probe':not reasons,'reasons':reasons,'scheduled_candidates':scheduled,
            'unresolved_candidates':len(entries)-scheduled,
            'clinical_eligibility':'not_assessed','source_risk_absence_claim':False}


def build_basic_episode(case, frequency_context):
    gate=source_gate(case,frequency_context)
    if not gate['eligible_for_this_probe']:raise ValueError('Source does not meet the frozen engineering profile')
    profile=PROFILES[case['group']]
    episode,episode_audit=build_episode(case,frequency_context)
    result,context_audit=build_context(episode,profile=profile,version=VERSION,policy=POLICY,
                                      suffix='_basic6',fact_prefix='episode6')
    core=next(i for i in result['his_initial']['current_use_record']['items']
              if i.get('plan') and i['name'].strip().casefold().startswith(profile['drug_prefix']))
    product=next(p for p in result['formulary'] if p['drug_code']==profile['drug_code'])
    old_product_statement={k:deepcopy(core.get(k)) for k in ('product_identity_status','name','strength','dosage_form','device')}
    core['current_product']={k:deepcopy(product[k]) for k in ('drug_code','name','ingredient','dosage_form',
        'strength','route','dose_unit','quantity_unit')}
    core['current_product'].update(source_kind='constructed',
        identity_basis='explicit_new_episode_product_from_experiment_formulary',
        historical_exact_identity='not_established')
    core['product_identity_status']='constructed_current_product_identified; historical_exact_manufacturer_NDC_identity_unverified'
    if profile.get('device'):core['current_product']['device']=profile['device']
    facts=result['patient_initial']['patient_facts']
    detail=f"这次药袋中这一项的标签是{product['display_name_zh']}，是上次模拟门诊新发的这一盒。"
    # Patient speech remains ordinary; provenance stays in structured fields.
    detail=detail.replace('模拟门诊','门诊')
    if profile.get('device'):detail+='配套的HandiHaler也带来了，胶囊放在这个器具里吸入，没有吞服。'
    facts.append({'fact_id':'episode6-product','section':'current_episode','topic':'current_product_label',
        'field':'current_product_label','value':detail,'status':'reported','source_kind':'constructed',
        'source_refs':[core['record_id']],'knowledge_state':'known_from_brought_record',
        'statement_as_of_offset_hours':168,'narrative_time_scope':'current product brought from fictional prior clinic visit',
        'construction_version':VERSION})
    result['evaluation']['reference_facts']=deepcopy(facts)
    result['provenance']['core_current_product_construction']={
        'source_kind':'constructed','previous_product_statement':old_product_statement,
        'chosen_formulary_product':deepcopy(product),'historical_exact_mapping_claim':False,
        'source_gate':gate,'selection_clinical_approval':False}
    result['provenance']['constructed_fields'].extend([
        'his_initial.current_use_record.items.core.current_product',
        'patient_initial.patient_facts.current_product_label'])
    validate_case(result)
    for field in HISTORY_FIELDS:
        if digest(case['his_initial'][field])!=digest(result['his_initial'][field]):raise ValueError('History changed')
    for field in ('patient_key','group','split','data_kind','formulary'):
        if result[field]!=case[field]:raise ValueError('Identity or partition changed')
    chart=HistoryChart(result['his_initial'])
    page=chart.query('current_use_items',limit=20)
    visible=next(r['record'] for r in page['records'] if r['record'].get('current_product'))
    if visible['current_product']!=core['current_product']:raise ValueError('Current product was lost by projection')
    return result,{'version':VERSION,'source_gate':gate,'base_episode_audit':episode_audit,
        'context_audit':context_audit,'core_product_visible':True,'source_history_unchanged':True,
        'new_laboratory_values':0,'clinical_reference_status':'pending_independent_review','formal_eligible':False}
