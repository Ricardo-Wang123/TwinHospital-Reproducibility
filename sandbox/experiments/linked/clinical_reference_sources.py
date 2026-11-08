"""Assess injection preconditions on actual frozen original prescriptions.

The working PPRS is deliberately not the accuracy reference. Its findings are
coordinator-only navigation hints. Independent reviewers receive the original
clinical evidence, all six rule sources and blank decisions. No case is excluded
because of its model outcome or its missing error-injection prerequisites.
"""
from copy import deepcopy

from .contracts import digest
from .documented_pprs import adapt_visible_inputs
from .clinical_faults import evaluate_rule, registry
from .safe_context_adapter import VisibleDocumentedCandidatePPRS


VERSION = 'actual_original_prescription_reference_sources_v1'
PROTOCOL = {
    'version': VERSION,
    'reference_status': 'independent_review_pending',
    'scored_unit': 'original_prescription_before_revision',
    'clinical_normality': 'not_established',
    'existing_rule_findings_are_gold': False,
    'missing_or_inapplicable_is_negative': False,
    'source_model_outcomes_used_for_selection': False,
    'clinical_inputs_mutated': False,
    'native_writes': False,
    'model_calls': False,
}


def assess(stage_input, formulary, source_catalogue):
    if not isinstance(stage_input, dict) or set(stage_input) != {'prescription', 'clinical_context'}:
        raise ValueError('exact original prescription stage input required')
    engine = VisibleDocumentedCandidatePPRS(formulary, source_catalogue)
    prepared = engine.prepare_review_inputs(stage_input['prescription'], stage_input['clinical_context'])
    adapted = adapt_visible_inputs(prepared['prescription'], prepared['shared_context'])
    findings = []
    for rule in registry()['rules']:
        try:
            finding = evaluate_rule(rule['rule_id'], adapted['prescription'], adapted['clinical_context'])
        except ValueError:
            finding = {'rule_id':rule['rule_id'], 'category':rule['category'],
                       'status':'indeterminate', 'reason':'structured_evidence_invalid'}
        findings.append({**finding,
            'complete_scoped_baseline_for_mutation':finding['status']=='scoped_nonviolation',
            'independent_gold_label':None,
            'whole_prescription_compliant':None,
            'other_error_categories_excluded':False})
    return {'protocol':deepcopy(PROTOCOL), 'stage_input_hash':digest(stage_input),
        'original_prescription_hash':digest(stage_input['prescription']),
        'clinical_context_hash':digest(stage_input['clinical_context']),
        'registry_hash':digest(registry()), 'formulary_hash':digest(formulary),
        'source_catalogue_hash':digest(source_catalogue),
        'rule_projection':{'prescription':adapted['prescription'],
            'clinical_context':adapted['clinical_context'], 'adaptation':adapted['adaptation'],
            'order_exposure_resolution':prepared['order_exposure_resolution']},
        'rule_applicability':findings,
        'independent_review_input':{'prescription':deepcopy(stage_input['prescription']),
            'clinical_context':deepcopy(stage_input['clinical_context']),
            'formulary':deepcopy(formulary)},
        'clinical_gold_label':None, 'clinical_reference_qualified':False}


def review_packet(review_id, value):
    """Do not reveal predicted category, status, mutation choice or model name."""
    if not isinstance(review_id,str) or not review_id.startswith('rxref_'):
        raise ValueError('blinded review ID required')
    return {'review_id':review_id, **deepcopy(value['independent_review_input']),
        'rating':{'reviewer_id':None,'reviewed_at':None,'evidence_sufficient':None,
            'overall_appropriateness':None,'error_categories':None,'severity':None,
            'single_fault_or_multiple':None,'basis_refs':[], 'rationale':None,
            'additional_missing_information':[]},
        'instructions':'Judge the original proposed prescription using the visible clinical evidence and product sources. Mark unknown or inapplicable explicitly. Assess every error category and other clinical problems; a rule-scope pass is not a whole-prescription negative label. Do not infer absent drugs or allergies from missing data.'}
