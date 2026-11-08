"""Separate the first review from later repair in controlled route experiments.

This is a scoped positive-case engineering audit. It cannot estimate specificity,
certify whole-prescription clinical quality, or identify which free-text reason
caused a rejection. No patient text or reviewer prose enters its output.
"""
from collections import Counter
from .contracts import digest
from .prescription_variants import validate_variant, execution_receipt


def audit_route_run(run, variant, formulary):
    variant=validate_variant(variant,formulary)
    a=run.get('artifacts') or {};stage=variant['stage_input'];original=stage['prescription']
    if (run.get('evaluation_unit')!='fixed_input_task'
            or run.get('controlled_variant_hash')!=digest(variant)
            or run.get('stage_input_hash')!=digest(stage)
            or run.get('stage_inputs',{}).get('prescription')!=stage
            or a.get('controlled_prescription_variant')!=execution_receipt(variant)):
        raise ValueError('run is not bound to the exact controlled candidate')
    if run.get('workflow_complete') is True or run.get('native_complete') is True:
        raise ValueError('a fixed-input repair branch cannot count as a new complete encounter')
    review=a.get('original_prescription_review')
    decision=None
    if review is not None:
        if a.get('original_prescription')!=original or review.get('prescription_hash')!=digest(original):
            raise ValueError('first-review endpoint does not belong to the injected candidate')
        original_events=[e for e in run.get('events',[]) if e.get('event')=='prescription_reviewed' and e.get('revision')==0]
        if len(original_events)!=1 or any(original_events[0].get(k)!=v for k,v in review.items()):
            raise ValueError('first-review artifact and immutable journal disagree')
        decision=review.get('decision')
        if decision not in {'approve','reject','defer'}:raise ValueError('unknown first-review decision')
    item=original['items'][variant['mutation']['item_index']]
    final=a.get('final_prescription')
    final_route_state='not_observed'
    if isinstance(final,dict):
        matching=[x for x in final.get('items',[]) if x.get('drug_code')==item['drug_code']]
        final_route_state=('target_product_removed' if not matching else
            'target_route_restored' if all(x.get('route')==variant['mutation']['before'] for x in matching)
            else 'target_route_still_mismatched')
    native_events=[]
    for event in run.get('events',[]):
        if event.get('event')!='business_operation':continue
        response=event.get('response') or {};data=response.get('data') or {}
        if (response.get('status') in {'executed','replayed'}
                and response.get('readback_verified') is True and response.get('native_effect') is True):
            native_events.append((event.get('operation'),data.get('status')))
    order_signed=('order.attest','signed') in native_events
    note_locked=('note.attest','locked') in native_events
    payment_paid=('payment.commit','paid') in native_events
    paid_branch=(run.get('status')=='completed' and order_signed and note_locked and payment_paid
        and (a.get('prescription_followthrough') or {}).get('status')=='approved_documented_and_paid')
    return {'config_id':run.get('config_id'),'status':run.get('status'),
        'variant_hash':digest(variant),'original_candidate_hash':digest(original),
        'target_route_fault_verified':True,'first_review_decision':decision,
        'first_review_approved_known_route_fault':None if decision is None else decision=='approve',
        'first_review_rejected_candidate':None if decision is None else decision=='reject',
        'rejection_reason_targets_injected_fault':None,
        'final_target_route_state':final_route_state,
        'order_signed_native_trace':order_signed,'note_locked_native_trace':note_locked,
        'payment_paid_native_trace':payment_paid,'paid_repair_branch_observed':paid_branch,
        'independent_native_readback_audit':None,'clinical_quality':None,
        'counts_as_new_patient_or_complete_encounter':False}


def summarize_route_rows(rows):
    groups={}
    for row in rows:groups.setdefault(row['config_id'],[]).append(row)
    summary=[]
    for config,group in sorted(groups.items()):
        decisions=Counter(x.get('first_review_decision') or 'not_observed' for x in group)
        observed=sum(decisions[x] for x in ('approve','reject','defer'))
        summary.append({'config_id':config,'planned_candidates':len(group),
            'first_review_decisions':dict(decisions),'first_reviews_observed':observed,
            'approved_known_route_fault':decisions['approve'],
            'approval_fraction_among_observed':decisions['approve']/observed if observed else None,
            'paid_repair_branches_observed':sum(x.get('paid_repair_branch_observed') is True for x in group),
            'statuses':dict(Counter(x['status'] for x in group)),
            'normal_controls':0,'specificity':None,'clinical_detection_sensitivity':None})
    return {'scope':'engineering_known_route_faults_first_review_and_separate_repair',
        'configurations':summary,'formal_experiment':False,
        'whole_prescription_reference_labels':0,'complete_encounters_added':0,
        'interpretation':'An initial approval remains an approval even if a business guard later triggers successful repair and payment. A rejection does not prove recognition of the target fault. Missing reviews remain in planned denominators.'}
