"""Literal shared reviewer evidence for the optional development PPRS candidate.

No hidden case data, NLP extraction, elapsed-time inference or synthetic clinical
completion occurs here. Complete evidence indexes remain local, while all review
conditions receive the same bounded context, provenance refs and unknowns.
"""
from collections import Counter
from copy import deepcopy
import math

from .contracts import digest
from .documented_pprs import DocumentedCandidatePPRS
from .evidence_bindings import (CONTEXT_KEYS, build_visible_evidence,
    build_product_bindings, resolve_evidence, load_applicability_registry)


POLICY = {'version':'literal_shared_rule_context_dev_v5',
    'development_only':True, 'clinical_reference_status':'independent_review_pending',
    'reads_hidden_patient_or_evaluator_data':False, 'natural_language_extraction':False,
    'current_exposure_inferred_from_historical_orders':False,
    'missing_current_medication_or_allergy_means_negative':False,
    'approximate_age_promoted_to_exact':False, 'complete_index_visibility':'private_audit_only',
    'shared_conditions':'all_five_prescription_configurations', 'timestamps_added':False,
    'order_exposure_requires_literal_status_interval_intent_and_time_basis':True,
    'structured_reports_preserved_without_certainty_upgrade':True}
EXTENSIONS={'documented_rule_context','documented_rule_evidence'}
BINDING_FIELDS=('drug_code','reference_product_id','verification_status','identity_check',
    'evidence_ref','catalogue_evidence_ref','verification_scope','reason','rule_scope',
    'mapping_version','mapping_manifest_hash','reference_set_id','catalogue_set_ids')


def _reported_evidence(index, section):
    """Copy literal visible reports, separately from verified rule predicates.

    A chart directory can establish that a report exists, not the contents of
    unread items. Only queried pages in the named section supply item content.
    Conflicting reports stay separate; we neither reconcile nor pick a winner.
    """
    reports, seen = [], set()
    for entry in index['entries']:
        if entry['source_kind'] == 'visible_chart_directory':
            content = entry['content'].get(section)
            pointer = '/' + section
            # A directory's item_count is never evidence of the item contents.
            if not isinstance(content, dict):
                continue
            content = {k: deepcopy(v) for k,v in content.items()
                       if k not in {'item_count','items_expanded','items','query_section','scope_query_section'}}
        elif (entry['source_kind'] == 'queried_chart_record'
              and entry.get('chart_section') in {section, section.replace('_record','_items')}):
            content, pointer = entry['content'], ''
        else:
            continue
        if not isinstance(content, dict) or not content:
            continue
        signature = digest(content)
        if signature in seen:
            continue
        seen.add(signature)
        reports.append({'content': deepcopy(content), 'evidence_ref': entry['ref'],
            'source_pointer': entry['source_pointer'] + pointer,
            'time_basis': deepcopy(entry['time_basis']),
            'epistemic_status': entry['epistemic_status'],
            'clinical_verification': 'not_established_by_adapter'})
    return reports


def prepare_shared_rule_context(clinical_context, formulary, source_catalogue):
    if not isinstance(clinical_context,dict) or set(clinical_context)-CONTEXT_KEYS-EXTENSIONS:
        raise ValueError('Only actual stage-visible clinical context is permitted')
    base={k:deepcopy(v) for k,v in clinical_context.items() if k in CONTEXT_KEYS}
    index=build_visible_evidence(base,formulary,source_catalogue)
    bound=build_product_bindings(index)
    bindings={code:{k:deepcopy(row[k]) for k in BINDING_FIELDS if k in row}
        for code,row in bound['product_bindings'].items()}
    demographics={'age':None,'age_status':'not_established','reported_age':None,'age_population':None,
        'evidence_ref':None, 'meaning':'Only an explicitly exact visible age enters a numerical age predicate; source age remains visible separately.'}
    checks=[]
    directory=next((e for e in index['entries'] if e['source_kind']=='visible_chart_directory'),None)
    if directory:
        source=directory['content'].get('demographics') or {}
        for field in ('age','age_status','age_time_basis','age_population'):
            if field not in source:continue
            checked=resolve_evidence(index,directory['ref'],pointer='/demographics/'+field,
                expected_value=source[field],expected_time_basis=directory['time_basis'],
                role='rule_engine',phase='prescription_review',expected_index_hash=index['index_hash'])
            checks.append({'target':'demographics.'+field,**checked})
        if 'age' in source and all(r['status']=='resolved' for r in checks):
            demographics.update(reported_age=deepcopy(source['age']),
                age_status=source.get('age_status','unspecified'),evidence_ref=directory['ref'],
                source_pointer='/demographics/age',age_time_basis=source.get('age_time_basis'))
            age=source['age']
            if type(age) in (int,float) and 0<=age<150 and source.get('age_status')=='exact':
                demographics['age']=age
        population=source.get('age_population')
        if isinstance(population,dict) and any(r['target']=='demographics.age_population' and r['status']=='resolved' for r in checks):
            # Category evidence and numerical age precision are independent.
            # Preserve a literal source-supported category, never round an age.
            demographics['age_population']={**deepcopy(population),'evidence_ref':directory['ref'],
                'source_pointer':'/demographics/age_population'}
            if type(source.get('age')) in (int,float) and source['age']<18 and population.get('category')=='adult':
                demographics['age_population']['status']='conflicting_demographic_evidence'
            elif (population.get('category')=='adult' and population.get('minimum_age_years')==18
                  and population.get('basis') and population.get('source_url')):
                demographics['age_population']['status']='documented_category'
    medication_reports = _reported_evidence(index, 'current_use_record')
    allergy_reports = _reported_evidence(index, 'allergy_record')
    has_medication_report = any(r['content'].get('current_use_status') or r['content'].get('items')
        or str(r['content'].get('status','')).startswith(('reported_', 'patient_report'))
        for r in medication_reports)
    has_allergy_report = any(r['content'].get('patient_report') for r in allergy_reports)
    consultation=next((e for e in index['entries'] if e['source_kind']=='consultation'),None)
    assessments={}
    if consultation and isinstance(consultation['content'],dict):
        for name in ('medication_reconciliation','allergy_assessment'):
            value=consultation['content'].get(name)
            if isinstance(value,dict):
                check=resolve_evidence(index,consultation['ref'],pointer='/'+name,expected_value=value,
                    role='rule_engine',phase='prescription_review',expected_index_hash=index['index_hash'])
                checks.append({'target':'current_physician_assessment.'+name,**check})
                if check['status']=='resolved':
                    assessments[name]={'content':deepcopy(value),'evidence_ref':consultation['ref'],
                        'source_pointer':consultation['source_pointer']+'/'+name,
                        'epistemic_status':'agent_reported','independent_clinical_truth':'not_established'}
    unknowns=['verified_current_medication_exposure_intervals',
        'complete_current_medication_reconciliation_and_product_family_scope',
        'confirmed_allergy_assessment_and_specialist_exceptions',
        'independent_single_active_order_intent', 'iv_therapy_appropriateness_and_diluent_preparation']
    if demographics['age'] is None:unknowns.append('exact_age_for_numerical_rule_predicate')
    # Reported evidence and verified predicates are deliberately distinct. An
    # observed report must not be discarded, nor upgraded to completed clinical
    # reconciliation, confirmed no allergy or verified current exposure.
    documented={'demographics':demographics,'product_bindings':bindings,
        'active_medications':[],
        'medication_reconciliation':{'status':'reported_unverified' if has_medication_report else 'not_established',
            'all_current_products_mapped':None, 'evidence_ref':None,
            'reported_evidence':medication_reports,
            'scope':'Visible reports are preserved below. They do not establish complete verified active exposure; do not infer absence.'},
        'allergies':{'assessment_status':'patient_report_available_unverified' if has_allergy_report else 'unknown',
            'assessment_ref':None,'items':[], 'reported_evidence':allergy_reports,
            'desensitization_or_rechallenge':None,'scope':'Source status and patient report remain separate; no confirmed assessment or negative allergy is inferred.'},
        'regimen_intents':{},'scenario_setting':None,'iv_therapy_appropriate':None,'iv_therapy_basis_ref':None}
    # A statement that the physician assessed the report is useful to reviewers,
    # but is not automatic verified exposure, a negative allergy gold label, or
    # authority to enable a mechanical rule's missing clinical predicates.
    if 'medication_reconciliation' in assessments:
        documented['medication_reconciliation']['current_physician_assessment']=assessments['medication_reconciliation']
    if 'allergy_assessment' in assessments:
        documented['allergies']['current_physician_assessment']=assessments['allergy_assessment']
    metadata={'policy':deepcopy(POLICY),'evidence_index_hash':index['index_hash'],
        'visible_snapshot_hash':index['snapshot_hash'],'source_catalogue_hash':digest(source_catalogue),
        'product_mapping_manifest_hash':load_applicability_registry()['manifest_hash'],
        'structured_context_hash':digest(documented),'unknown_structured_fields':unknowns,
        'literal_field_checks':[{k:deepcopy(row[k]) for k in ('target','status','ref','pointer','source_pointer','epistemic_status') if k in row} for row in checks],
        'product_binding_status_counts':dict(Counter(row['verification_status'] for row in bindings.values()))}
    shared={**base,'documented_rule_context':documented,'documented_rule_evidence':metadata}
    existing=set(clinical_context)&EXTENSIONS
    if existing and (existing!=EXTENSIONS or any(clinical_context[k]!=shared[k] for k in EXTENSIONS)):
        raise ValueError('Prepared shared evidence was changed or does not match this visible snapshot/catalogue')
    return {'shared_context':shared,'evidence_index':index,'audit':deepcopy(metadata)}


def _declared_candidate_courses(prescription,index):
    """Bind the physician's actual proposed course, not historical ingestion.

    An evidence reference is generated by the adapter only after literal equality
    to the visible physician output. End time is arithmetic over explicitly chosen
    duration; quantity never determines a duration. Old/current exposure remains
    unknown unless separately established.
    """
    directory=next((e for e in index['entries'] if e['source_kind']=='visible_chart_directory'),None)
    clock=(directory or {}).get('time_basis') or {}
    current=clock.get('new_visit_offset_hours')
    if not clock.get('history_offset_origin') or type(current) not in (int,float):return {}
    consultation=next((e for e in index['entries'] if e['source_kind']=='consultation'),None)
    if not consultation:return {}
    content=consultation['content'];sources=[('/prescription',content.get('prescription'))]
    for i,followup in enumerate(content.get('prescription_review_followups',[])):
        sources.append((f'/prescription_review_followups/{i}/physician_response/prescription',
                        followup.get('physician_response',{}).get('prescription')))
    source=next((path for path,value in reversed(sources) if value==prescription),None)
    if source is None:return {}
    found={}
    for i,item in enumerate(prescription.get('items',[])):
        course=item.get('intended_course')
        if not isinstance(course,dict) or item.get('exposure') or item.get('status'):continue
        start,days=course.get('start_offset_hours'),course.get('duration_days')
        if (type(start) not in (int,float) or type(days) not in (int,float)
            or not math.isfinite(start) or not math.isfinite(days) or days<=0 or start<current
            or not isinstance(course.get('basis'),str) or not course['basis'].strip()):continue
        end=start+24*days
        if not math.isfinite(end):continue
        pointer=f'{source}/items/{i}/intended_course'
        check=resolve_evidence(index,consultation['ref'],pointer=pointer,expected_value=course,
            role='rule_engine',phase='prescription_review',expected_index_hash=index['index_hash'])
        if check['status']!='resolved':continue
        found[i]={'status':'active','exposure':{'start_hour':start,'end_hour':end,
            'administration_intent':'concurrent_active','evidence_ref':consultation['ref'],
            'evidence_pointer':pointer,'time_basis':deepcopy(clock)},
            'check':check,'meaning':'intended_new_candidate_course_not_observed_ingestion_or_native_schedule'}
    return found


class VisibleDocumentedCandidatePPRS:
    rule_version='visible-documented-candidate-pprs-dev-v5'
    development_only=True
    engineering_only=False
    policy=POLICY

    def __init__(self,formulary,source_catalogue,*,rule_ids=None):
        self.formulary=deepcopy(formulary)
        self.source_catalogue=deepcopy(source_catalogue)
        self.candidate=DocumentedCandidatePPRS(formulary,rule_ids=rule_ids)
        self.registry_hash=self.candidate.registry_hash
        self.rule_ids=self.candidate.rule_ids
        self.source_catalogue_hash=digest(self.source_catalogue)
        self.product_mapping_manifest_hash=load_applicability_registry()['manifest_hash']

    def prepare_context(self,context):
        return prepare_shared_rule_context(context,self.formulary,self.source_catalogue)

    def prepare_review_inputs(self,prescription,clinical_context=None):
        """Expose the same literal projection for reference-material preparation.

        This is a data projection, not an independent reference label. It never
        upgrades missing evidence or changes the physician's source proposal.
        """
        prepared=self.prepare_context(clinical_context or {})
        # A nonempty evidence_ref inside a candidate is not proof. Only a fully
        # resolved, actually visible structured order intent may enable exposure
        # predicates. The original candidate is untouched for all reviewers.
        evaluated=deepcopy(prescription); exposure_checks=[]
        declared_courses=_declared_candidate_courses(prescription,prepared['evidence_index'])
        for i,item in enumerate(evaluated.get('items',[])):
            if i in declared_courses:
                declared=declared_courses[i]
                item['status']=declared['status'];item['exposure']=deepcopy(declared['exposure'])
                exposure_checks.append({'item_index':i,'status':'resolved',
                    'reason':'literal_physician_proposed_course','meaning':declared['meaning'],
                    'checks':[declared['check']]})
                continue
            exposure=item.get('exposure') or {}
            supplied=isinstance(exposure,dict) and isinstance(exposure.get('evidence_pointer'),str)
            supported=supplied and bool(exposure.get('evidence_ref')) and isinstance(exposure.get('time_basis'),dict)
            checks=[]
            if supported:
                for suffix,value in [('/status',item.get('status'))]+[
                        ('/exposure/'+field,exposure.get(field)) for field in
                        ('start_hour','end_hour','administration_intent')]:
                    check=resolve_evidence(prepared['evidence_index'],exposure['evidence_ref'],
                        pointer=exposure['evidence_pointer']+suffix,expected_value=value,
                        expected_time_basis=exposure['time_basis'],role='rule_engine',
                        phase='prescription_review',expected_index_hash=prepared['evidence_index']['index_hash'])
                    checks.append(check)
                supported=all(check['status']=='resolved' for check in checks)
            exposure_checks.append({'item_index':i,'status':'resolved' if supported else 'indeterminate',
                'reason':'literal_visible_order_intent' if supported else 'unverified_active_exposure_not_used_by_rules',
                'checks':[{k:check[k] for k in ('status','ref','pointer','source_hash','reason') if k in check} for check in checks]})
            if not supported:
                item.pop('status',None);item.pop('exposure',None)
        return {**prepared, 'prescription':evaluated, 'order_exposure_resolution':exposure_checks}

    def review(self,prescription,clinical_context=None):
        prepared=self.prepare_review_inputs(prescription,clinical_context)
        evaluated=prepared['prescription']
        result=self.candidate.review(evaluated,prepared['shared_context'])
        result.update(rule_version=self.rule_version,development_only=True,
            visible_evidence_audit=prepared['audit'],
            finding_status_counts=dict(Counter(f['status'] for f in result['documented_rule_findings'])),
            no_clinical_fields_inferred=True,order_exposure_resolution=prepared['order_exposure_resolution'],
            original_candidate_hash=digest(prescription),rule_input_projection_hash=digest(evaluated))
        return result
