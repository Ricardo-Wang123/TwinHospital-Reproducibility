"""Explicit development interventions on a frozen, actual original order.

Source consultation, transcript and patient data stay unchanged. The intervention
is a faulty order-entry candidate after that consultation, never a claim that
the physician model originally authored the modified order. Reference metadata
and the mutation diff remain outside every actor observation.
"""
from copy import deepcopy
import json
from pathlib import Path

from .contracts import digest, validate_prescription
from .evidence_bindings import _guard
from ..visibility import assert_visible

VERSION='controlled_original_order_route_v1'
REGISTRY_PATH=Path(__file__).with_name('exact_product_route_reference_v1.json')
POLICY={'version':VERSION,'engineering_only':True,'formal_eligible':False,
    'intervention_point':'order_entry_after_frozen_physician_consultation_before_first_review',
    'source_consultation_modified':False,'patient_facts_modified':False,
    'source_model_outcome_used_for_selection':False,
    'same_variant_for_all_five_prescription_conditions':True,
    'scored_unit':'first_review_of_explicitly_modified_candidate_before_repair',
    'counts_as_new_patient':False,'all_category_normality_established':False,
    'six_category_coverage_complete':False}


class VariantNotApplicable(ValueError):pass


def registry():return json.loads(REGISTRY_PATH.read_text())


def make_route_variant(stage_input,formulary,*,item_index=0,new_route='intravenous'):
    if not isinstance(stage_input,dict) or set(stage_input)!={'prescription','clinical_context'}:
        raise ValueError('exact original prescription stage required')
    assert_visible(stage_input)
    _guard(stage_input)
    rx=validate_prescription(stage_input['prescription'])
    if stage_input['clinical_context'].get('consultation',{}).get('prescription')!=rx:
        raise ValueError('source must be the actual pre-review physician proposal')
    if type(item_index) is not int or not 0<=item_index<len(rx['items']):
        raise ValueError('explicit valid candidate item index required')
    if new_route not in ('oral','inhaled','intravenous','intramuscular'):
        raise ValueError('use an explicit supported route term, not ambiguous free text')
    book=registry();item=rx['items'][item_index];code=item['drug_code']
    rule=book['products'].get(code)
    if rule is None:raise VariantNotApplicable('no independently documented exact-product route rule')
    products=[p for p in formulary if p.get('drug_code')==code]
    if len(products)!=1 or {k:products[0].get(k) for k in book['identity_fields']}!=rule['identity']:
        raise VariantNotApplicable('frozen product identity does not match the exact label-backed definition')
    if item.get('route')!=rule['labelled_route']:
        raise VariantNotApplicable('source route is not a scoped compliant starting value')
    if new_route==rule['labelled_route']:
        raise VariantNotApplicable('route intervention would introduce no mismatch')
    changed=deepcopy(stage_input);changed['prescription']['items'][item_index]['route']=new_route
    value={'version':VERSION,'policy':deepcopy(POLICY),'registry_hash':digest(book),
        'formulary_hash':digest(formulary),'source_stage_input':deepcopy(stage_input),
        'source_stage_input_hash':digest(stage_input),'stage_input':changed,
        'stage_input_hash':digest(changed),
        'mutation':{'operation':'replace','path':f'/prescription/items/{item_index}/route',
            'item_index':item_index,'before':item['route'],'after':new_route},
        'reference_candidate':{'category':book['category'],'target_error_present':True,
            'target_error_basis':'verified_route_mismatch_for_same_exact_simulated_product',
            'source_route_scoped_nonviolation':True,'single_mutated_field':True,
            'whole_source_prescription_compliant':None,'all_other_error_categories_excluded':False,
            'single_clinical_fault_label':None,'severity':None,'clinical_gold_label':None,
            'source':deepcopy(rule['source']),'independent_reference_implementation':'exact_product_label_and_mutation_audit_not_working_pprs'}}
    value['variant_id']='rxvariant_'+digest(value)[:24]
    return value


def validate_variant(value,formulary):
    if not isinstance(value,dict) or value.get('version')!=VERSION:
        raise ValueError('unsupported controlled prescription variant')
    try:
        mutation=value['mutation']
        expected=make_route_variant(value['source_stage_input'],formulary,
            item_index=mutation['item_index'],new_route=mutation['after'])
    except (KeyError,TypeError) as exc:raise ValueError('malformed controlled variant') from exc
    if value!=expected:raise ValueError('variant differs from the reproducible single-field intervention')
    return deepcopy(value)


def continuation_state(anchor,variant,formulary):
    """Use actual pre-review budgets; the intervention grants no new turns."""
    from .run_batch import prescription_continuation_state
    value=validate_variant(variant,formulary)
    state=prescription_continuation_state(anchor,value['source_stage_input'])
    state.update(source_stage_input_hash=value['source_stage_input_hash'],
        stage_input_hash=value['stage_input_hash'],controlled_variant_hash=digest(value))
    return state


def execution_receipt(variant):
    """Private run provenance, not an actor observation or a clinical gold label."""
    return {k:deepcopy(variant[k]) for k in ('version','policy','variant_id','registry_hash',
        'source_stage_input_hash','stage_input_hash','mutation','reference_candidate')}
