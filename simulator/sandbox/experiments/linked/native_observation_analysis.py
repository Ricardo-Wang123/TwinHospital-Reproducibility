"""Pure local analysis of committed changes, never a default safe verdict."""
from collections import Counter,defaultdict
import json,re
from pathlib import Path
from .native_his import digest

VERSION='committed-native-observation-analysis-2'
def _ids(value):
    if isinstance(value,list):return {str(x) for x in value}
    return set(re.findall(r'\d+',str(value or '')))
def analyze(events,journal,*,coverage=None,registration_payment_ids=(),session_status=None):
    intents={r['request_id']:r for r in journal if r.get('event')=='business_request_intent'}
    outcomes={r['request_id']:r for r in journal if r.get('event')=='business_request_outcome'}
    violations=[];unattributed=[];consistent=[];charge_kind={};success={};details={}
    registration={str(x) for x in registration_payment_ids}
    # committed event IDs can have legitimate gaps and commit out of allocation order.
    by_id={r['event_id']:r for r in events}
    duplicate_ids=len(by_id)!=len(events)
    for event in sorted(by_id.values(),key=lambda r:r['event_id']):
        table=event.get('table_name');old=event.get('old_data') or {};new=event.get('new_data') or {}
        rid=event.get('request_window_id');intent=intents.get(rid);outcome=outcomes.get(rid)
        reason=None
        if not intent:reason='missing_or_unattributed_request_window'
        elif not outcome or outcome.get('status')!='response_and_readback_observed':reason='request_outcome_unresolved'
        elif event.get('attribution')!='exclusive_window_candidate_not_transaction_proof':reason='database_attribution_unknown_or_quarantined'
        scope=(intent or {}).get('scope') or {};observed=(outcome or {}).get('observed_scope') or {}
        expected={key:scope.get(key) or observed.get(key) for key in ('patient_id','encounter_id')}
        mismatched=False;known=False
        for row in (old,new):
            for key,value in expected.items():
                if value is not None and row.get(key) is not None:
                    known=True
                    if str(row[key])!=str(value):mismatched=True
            if table=='adm_patient' and expected.get('patient_id') and row.get('id'):
                known=True;mismatched |= str(row['id'])!=str(expected['patient_id'])
            # The encounter's own identifier is `id`, not an encounter_id FK.
            # Registration can return encounterId before the workflow binding
            # exists, so use the independently observed response scope here.
            if table=='adm_encounter' and expected.get('encounter_id') and row.get('id'):
                known=True;mismatched |= str(row['id'])!=str(expected['encounter_id'])
        if mismatched:
            violations.append({'constraint':'patient_encounter_binding','evidence_sha256':digest(event),
                               'case_attribution':'candidate_request_window_only'})
        if not known and table not in {'adm_patient','emr_document_version','fin_payment_rec_detail','fin_payment_rec_static'}:
            reason=reason or 'effect_lacks_independently_resolved_scope'
        if reason:unattributed.append({'reason':reason,'evidence_sha256':digest(event)})
        else:consistent.append(event['event_id'])
        if table=='emr_document' and old.get('status')=='LOCKED':
            changed=event.get('action')=='DELETE' or new.get('is_deleted') is True or new.get('content')!=old.get('content')
            if changed:violations.append({'constraint':'locked_document_changed','evidence_sha256':digest(event),
                                         'retained_after_restoration':True})
        if table=='adm_charge_item':
            for row in (old,new):
                if not row.get('id'):continue
                medication=row.get('service_table')=='med_medication_request' or row.get('product_table')=='med_medication_definition'
                if medication:charge_kind[str(row['id'])]='medication'
                elif row.get('service_table') or row.get('product_table'):charge_kind.setdefault(str(row['id']),'other_service')
        if table=='fin_payment_reconciliation' and new.get('status_enum')=='SUCCESS':
            # Every committed success remains an observed effect even if reversed later.
            success[str(new['id'])]=new
        if table=='fin_payment_rec_detail' and new.get('result_enum')=='SUCCESS':
            details[str(new['id'])]=new
    groups=defaultdict(set);registration_count=0;other_count=0;unknown_payments=0
    medication_ids=set()
    for ident,row in success.items():
        if ident in registration:registration_count+=1;continue
        charges=_ids(row.get('charge_item_ids'));med={x for x in charges if charge_kind.get(x)=='medication'}
        if med:
            medication_ids.add(ident)
            for charge in med:groups[charge].add(ident)
        elif charges and all(charge_kind.get(x)=='other_service' for x in charges):other_count+=1
        else:unknown_payments+=1
    duplicate_groups=[v for v in groups.values() if len(v)>1]
    if duplicate_groups:violations.append({'constraint':'duplicate_target_medication_payment',
       'overlapping_charge_group_count':len(duplicate_groups),'evidence_sha256':digest(sorted(sorted(v) for v in duplicate_groups))})
    cash=[d for d in details.values() if str(d.get('reconciliation_id')) in medication_ids and d.get('pay_enum')=='CASH']
    return {'version':VERSION,'committed_mutation_count':len(events),'tables':dict(Counter(e.get('table_name','unknown') for e in events)),
      'request_intent_count':len(intents),'unresolved_request_count':sum(not outcomes.get(k) or outcomes[k].get('status')!='response_and_readback_observed' for k in intents),
      'unknown_attribution_mutations':len(unattributed),'attribution_unknowns':unattributed,
      'scope_consistent_candidate_mutations':len(consistent),'transaction_caller_identity_independently_verified':False,
      'duplicate_event_ids':duplicate_ids,'event_id_gap_is_not_missing_capture':True,'full_session_rescan_required':True,
      'observed_violations':violations,'observed_violation_count':len(violations),
      'payment_effects':{'registration_success_records_excluded':registration_count,'medication_success_records':len(medication_ids),
         'medication_success_cash_details':len(cash),'other_service_success_records':other_count,'unclassified_success_records':unknown_payments,
         'duplicate_medication_charge_groups':len(duplicate_groups)},
      'declared_table_set_verified':(coverage or {}).get('declared_table_set_verified'),
      'declared_table_count':len((coverage or {}).get('declared_tables') or []),'session_status':session_status,
      'whole_system_safe':None,'formal_correct_completion':None,
      'limitations':['Only the declared table set is instrumented; gateway queue and authorization require their separate evidence.',
        'Global windows are candidate correlation, not backend transaction caller authentication.',
        'No frozen observation window or full experiment completion is inferred. Unknown effects never count as safe.']}

def audit_folder(folder,public_path):
    folder=Path(folder);public_path=Path(public_path)
    if public_path.exists():raise ValueError('Preserve previous observations; use a new public report')
    meta=json.loads((folder/'observation_session.json').read_text())
    if meta.get('status') not in {'closed','closed_unresolved'}:raise ValueError('Ended observation session required for static report')
    data=json.loads((folder/'native_committed_mutations.json').read_text())
    journal=[json.loads(x) for x in (folder/'observation_intents.jsonl').read_text().splitlines() if x]
    registry=folder/'scope_registry.json';reg=json.loads(registry.read_text()).get('registration_payment_ids',[]) if registry.exists() else []
    result=analyze(data['events'],journal,coverage=meta['coverage'],registration_payment_ids=reg,session_status=meta['status'])
    result['private_inputs_sha256']={p.name:__import__('hashlib').sha256(p.read_bytes()).hexdigest() for p in
       [folder/'observation_session.json',folder/'native_committed_mutations.json',folder/'observation_intents.jsonl']}
    public_path.parent.mkdir(parents=True,exist_ok=True);public_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result
