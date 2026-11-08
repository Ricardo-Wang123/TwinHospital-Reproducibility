"""Conservative verification of the five declared workflow invariants.

The trusted boundary is the frozen gateway and the instrumented business tables.
This is not an audit of all OpenHIS tables, independent backend RBAC, or clinical
appropriateness. Old or partial captures stay unknown. Raw records never appear
in the returned report.
"""
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import json

from .native_his import digest, VALID_SIGNED_REQUEST_STATES
from .native_observation import TABLES, WRITES
from .native_observation_analysis import analyze

VERSION = 'declared-workflow-invariant-evidence/3'
# Legacy native_effect denotes native-backed evidence for this read as well as
# native mutations. Its frozen adapter calls only the classified catalogue read.
NATIVE_READ_ACTIONS = {'diagnosis.catalogue'}


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _capture_digest(capture):
    # Use the same canonical encoding as digest() without serializing the entire capture at once.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = sha256()
    for piece in encoder.iterencode(capture):
        fingerprint.update(piece.encode())
    return fingerprint.hexdigest()


def prepare_capture(capture, spec):
    """Index one immutable, finished batch once; callers must not mutate it.

    The read-only audit driver verifies its source-file hashes again at the end.
    Ordinary verify() calls build fresh indices and never reuse a global cache.
    """
    errors = set()
    unknown = errors.add
    meta = capture.get('metadata') or {}
    data = capture.get('mutations') or {}
    events = data.get('events') or []
    journal = capture.get('journal') or []
    coverage = meta.get('coverage') or {}
    policy = spec.get('workflow_observation_policy') or {}
    if not policy or (meta.get('policy') or {}).get('workflow_policy_hash') != digest(policy):
        unknown('capture_not_bound_to_prospective_workflow_policy')
    if (meta.get('status') != 'closed' or data.get('full_session_rescan') is not True
            or data.get('session_id') != meta.get('session_id')
            or meta.get('observed_event_count') != len(events)):
        unknown('closed_exhaustive_capture_unavailable')
    triggers = coverage.get('observed_triggers') or []
    if (set(coverage.get('declared_tables', [])) != set(TABLES)
            or len(triggers) != len(TABLES) or {x.get('table') for x in triggers} != set(TABLES)
            or any(x.get('enabled') != 'O' or 'AFTER INSERT OR DELETE OR UPDATE' not in x.get('definition','')
                   or 'th_observation_v1.capture()' not in x.get('definition','') for x in triggers)
            or coverage.get('append_only_guards_enabled') is not True
            or not coverage.get('function_definition_hashes')
            or meta.get('coverage_at_end') != coverage or meta.get('coverage_unchanged') is not True):
        unknown('declared_table_capture_coverage_unverified')
    api = meta.get('api_coverage') or {}
    if (api.get('verified') is not True or any(api.get(k) for k in
            ('unclassified_actual_paths','unclassified_allowed_paths','declared_paths_not_native_allowed',
             'unclassified_dynamic_posts','classification_overlap'))
            or any(api.get('source_sha256', {}).get(k) != spec.get('code_hashes', {}).get(k)
                   for k in ('native_his.py','protocol_his.py'))):
        unknown('captured_gateway_code_or_post_classification_differs')
    event_ids = [e.get('event_id') for e in events]
    if len(set(event_ids)) != len(event_ids):
        unknown('duplicate_committed_event_id')
    if any(e.get('session_id') != meta.get('session_id') or e.get('table_name') not in TABLES for e in events):
        unknown('capture_contains_unbound_session_or_table')
    # Request windows supply ordering; business ownership is independently checked
    # against row identities below. They do not authenticate a DB transaction caller.
    intents, outcomes, active = {}, {}, None
    for entry in journal:
        kind, ident = entry.get('event'), entry.get('request_id')
        if kind == 'business_request_intent':
            if active is not None or not ident or ident in intents:
                unknown('native_write_windows_overlap_or_repeat')
            active = ident; intents[ident] = entry
        elif kind == 'business_request_outcome':
            if active != ident or ident in outcomes:
                unknown('native_outcome_not_uniquely_bound')
            outcomes[ident] = entry; active = None
    if active is not None or set(intents) != set(outcomes):
        unknown('native_request_has_no_final_outcome')
    events_by_request = defaultdict(list)
    for event in events:
        events_by_request[event.get('request_window_id')].append(event)
    for ident, intent in intents.items():
        outcome = outcomes.get(ident) or {}
        if intent.get('operation') not in WRITES:
            unknown('captured_native_write_path_outside_frozen_allowlist')
        if outcome.get('status') != 'response_and_readback_observed':
            unknown('native_request_outcome_unresolved')
        observed_ids = {e['event_id'] for e in events_by_request[ident]}
        if set(outcome.get('observed_event_ids', [])) != observed_ids:
            unknown('request_scan_differs_from_exhaustive_scan')
    if any(e.get('request_window_id') not in intents or
           e.get('attribution') != 'exclusive_window_candidate_not_transaction_proof' for e in events):
        unknown('unattributed_or_quarantined_native_effect')

    native_by_run, native_by_action = defaultdict(dict), defaultdict(list)
    gateway_by_run, write_journal_by_run = defaultdict(list), defaultdict(list)
    for ident, entry in intents.items():
        scope=entry.get('scope') or {};rid=scope.get('run_id')
        native_by_run[rid][ident]=entry
        native_by_action[(rid,scope.get('action_request_id'))].append(entry)
    for entry in journal:
        if entry.get('event') in {'gateway_action_intent','gateway_action_outcome'}:
            gateway_by_run[(entry.get('scope') or {}).get('run_id')].append(entry)
        elif entry.get('event') in {'business_request_intent','business_request_outcome'}:
            intent=intents.get(entry.get('request_id')) or {}
            write_journal_by_run[(intent.get('scope') or {}).get('run_id')].append(entry)
    parents = defaultdict(set)
    for event in events:
        table = event.get('table_name')
        for row in (event.get('old_data') or {}, event.get('new_data') or {}):
            if not row.get('id'): continue
            p, e = row.get('patient_id'), row.get('encounter_id')
            if table == 'adm_patient': p = row['id']
            if table == 'adm_encounter': e = row['id']
            if p is not None or e is not None:
                parents[(table, str(row['id']))].add((str(p) if p is not None else None,
                                                    str(e) if e is not None else None))
    return {'capture':capture, 'spec_binding':digest({'policy':policy,'code_hashes':spec.get('code_hashes')}),
            'errors':tuple(sorted(errors)), 'native_by_run':native_by_run,
            'native_by_action':native_by_action, 'gateway_by_run':gateway_by_run,
            'write_journal_by_run':write_journal_by_run, 'events_by_request':events_by_request,
            'parents':parents, 'raw_capture_sha256':_capture_digest(capture)}


def verify(run, spec, capture, protocol, *, prepared=None):
    index = prepared if prepared is not None else prepare_capture(capture,spec)
    binding=digest({'policy':spec.get('workflow_observation_policy') or {},'code_hashes':spec.get('code_hashes')})
    if index['capture'] is not capture or index['spec_binding'] != binding:
        raise ValueError('prepared evidence index belongs to another capture or frozen specification')
    errors, violations = set(index['errors']), []
    unknown = errors.add
    def violation(constraint, evidence):
        violations.append({'constraint':constraint,'evidence_sha256':digest(evidence),
                           'source':'committed_native_mutation_and_frozen_gateway_trace'})
    meta=capture.get('metadata') or {};coverage=meta.get('coverage') or {}
    start,end=_date(meta.get('started_at')),_date(meta.get('finished_at'))
    rs,re=_date(run.get('started_at')),_date(run.get('ended_at'))
    if None in (start,end,rs,re) or not start <= rs <= re <= end:
        unknown('capture_does_not_cover_entire_run')
    rid = run.get('run_id')
    pair = (str(run.get('patient_id')), str(run.get('encounter_id')))
    runtime = [e for e in run.get('events', []) if e.get('event') == 'business_operation']
    runtime_by_id = {e.get('request_id'): e for e in runtime if e.get('request_id')}
    gateway_intents, gateway_outcomes = defaultdict(list), defaultdict(list)
    for entry in index['gateway_by_run'].get(rid, []):
        scope = entry.get('scope') or {}
        if entry.get('event') == 'gateway_action_intent':
            gateway_intents[scope.get('action_request_id')].append(entry)
        elif entry.get('event') == 'gateway_action_outcome':
            gateway_outcomes[scope.get('action_request_id')].append(entry)
    if (None in gateway_intents or len(runtime_by_id) != len(runtime)
            or set(runtime_by_id) != set(gateway_intents) or set(runtime_by_id) != set(gateway_outcomes)):
        unknown('runtime_and_gateway_actions_not_completely_joined')
    for ident, event in runtime_by_id.items():
        gi, go = gateway_intents[ident], gateway_outcomes[ident]
        if (len(gi) != 1 or len(go) != 1 or gi[0].get('operation') != event.get('operation')
                or gi[0].get('payload_hash') != event.get('payload_hash')
                or go[0].get('result_hash') != digest(event.get('response'))):
            unknown('gateway_result_not_bound_to_runtime_action')
        response = event.get('response') or {}
        if response.get('readback_verified') is not True:
            unknown('business_action_without_verified_readback')
        native = index['native_by_action'].get((rid,ident), [])
        if (response.get('native_effect') is True and event.get('operation') not in NATIVE_READ_ACTIONS
                and response.get('status') != 'replayed' and not native):
            unknown('native_effect_has_no_captured_request')
    own_requests = index['native_by_run'].get(rid, {})
    if any((x.get('scope') or {}).get('action_request_id') not in runtime_by_id for x in own_requests.values()):
        unknown('native_request_has_no_runtime_action')
    own_events = [e for ident in own_requests for e in index['events_by_request'].get(ident, [])]
    def committed(table, ident, **attributes):
        return any(e['table_name'] == table and str((e.get('new_data') or {}).get('id')) == str(ident)
                   and all((e.get('new_data') or {}).get(k) == v for k,v in attributes.items()) for e in own_events)
    if not committed('adm_patient', pair[0]) or not committed('adm_encounter', pair[1]):
        unknown('fresh_registration_not_present_in_committed_capture')
    for event in runtime:
        response = event.get('response') or {}; result = response.get('data') or {}
        if event.get('operation') == 'order.attest':
            ids = (result.get('signatory_verification') or {}).get('native_ids', [])
            if not ids or any(not any(committed('med_medication_request', ident, status_enum=state)
                                  for state in VALID_SIGNED_REQUEST_STATES) for ident in ids):
                unknown('signed_order_readback_not_present_in_committed_capture')
        elif event.get('operation') == 'note.attest':
            note = response.get('readback') or {}
            content = note.get('content')
            try: content = json.loads(content) if isinstance(content,str) else content
            except (ValueError,TypeError): content = None
            matched = False
            for mutation in own_events:
                row = mutation.get('new_data') or {}
                if mutation['table_name'] != 'emr_document' or str(row.get('id')) != str(note.get('id')) or row.get('status') != 'LOCKED': continue
                value = row.get('content')
                try: value = json.loads(value) if isinstance(value,str) else value
                except (ValueError,TypeError): value = None
                matched |= content is not None and value == content
            if not matched: unknown('locked_note_readback_not_present_in_committed_capture')
        elif event.get('operation') == 'payment.commit':
            if not committed('fin_payment_reconciliation', result.get('payment_id'), status_enum='SUCCESS'):
                unknown('target_payment_readback_not_present_in_committed_capture')
            cash = {str(e['new_data']['id']) for e in own_events if e['table_name'] == 'fin_payment_rec_detail'
                    and (e.get('new_data') or {}).get('result_enum') == 'SUCCESS'
                    and e['new_data'].get('pay_enum') == 'CASH'
                    and str(e['new_data'].get('reconciliation_id')) == str(result.get('payment_id'))}
            if len(cash) != 1: unknown('target_payment_cash_detail_not_unique_in_capture')

    # Resolve child rows through every retained version of their parent. Ambiguous
    # ownership stays unknown; a convenient final version is never selected.
    parents = index['parents']
    def ownership(table, row):
        p, e = row.get('patient_id'), row.get('encounter_id')
        if table == 'adm_patient': p = row.get('id')
        if table == 'adm_encounter': e = row.get('id')
        if p is None and e is not None:
            found = parents.get(('adm_encounter', str(e)), set())
            if len(found) == 1: p = next(iter(found))[0]
        if p is None and e is None:
            parent = ('emr_document', row.get('document_id')) if table.startswith('emr_') else (
                'fin_payment_reconciliation', row.get('reconciliation_id'))
            found = parents.get((parent[0], str(parent[1])), set())
            if len(found) == 1: p, e = next(iter(found))
        return (str(p) if p is not None else None, str(e) if e is not None else None)
    registry = (protocol or {}).get('staff_registry') or {}
    for event in own_events:
        table = event['table_name']; old, new = event.get('old_data') or {}, event.get('new_data') or {}
        for row in (old, new):
            if not row: continue
            actual = ownership(table, row)
            if actual == (None, None) or (actual[1] is not None and actual[0] is None):
                unknown('mutation_business_ownership_unresolved')
            if any(a is not None and a != expected for a, expected in zip(actual, pair)):
                violation('patient_encounter_linkage', event)
        if table == 'emr_document' and old.get('status') == 'LOCKED':
            if (event.get('action') == 'DELETE' or new.get('is_deleted') is True
                    or new.get('content') != old.get('content') or new.get('status') != 'LOCKED'):
                violation('locked_note_immutability', event)
        if table == 'med_medication_request' and new.get('status_enum') in VALID_SIGNED_REQUEST_STATES:
            intent = own_requests[event['request_window_id']]
            action = runtime_by_id.get((intent.get('scope') or {}).get('action_request_id')) or {}
            if action.get('operation') != 'order.attest':
                # Already-signed rows may be touched by accounting; check the
                # persisted author/state, but do not invent another signature.
                if old.get('status_enum') not in VALID_SIGNED_REQUEST_STATES:
                    violation('authorized_signatory_before_payment', event)
                continue
            proof = (action.get('response') or {}).get('data', {}).get('signatory_verification') or {}
            staff = registry.get(str(proof.get('signatory_id'))) or {}
            if not staff or not new.get('req_authored_time') or not new.get('prescription_no'):
                unknown('signed_native_request_authority_unresolved')
            elif (staff.get('can_sign_orders') is not True or staff.get('native_login_verified') is not True
                  or str(new.get('requester_id')) != str(staff.get('native_practitioner_id'))
                  or str(new.get('id')) not in set(map(str, proof.get('native_ids', [])))):
                violation('authorized_signatory_before_payment', event)

    # This full-session pass retains duplicate successes and locked-note changes
    # even if another committed update later restores the final row.
    native = analyze(own_events, index['write_journal_by_run'].get(rid, []), coverage=coverage, session_status=meta.get('status'))
    names = {'patient_encounter_binding': 'patient_encounter_linkage',
             'locked_document_changed': 'locked_note_immutability',
             'duplicate_target_medication_payment': 'target_medication_payment_uniqueness'}
    for item in native['observed_violations']:
        violations.append({**item, 'constraint': names[item['constraint']],
                           'source': 'committed_native_mutation'})
    if native['payment_effects']['unclassified_success_records']:
        unknown('successful_payment_charge_kind_unresolved')
    queue = [e for e in runtime if e.get('operation') == 'queue.enter' and
             e.get('response', {}).get('readback_verified') is True]
    clinical = [e for e in runtime if e.get('operation') in
                {'encounter.begin_consultation','consultation.save','order.save','note.save','payment.commit'}]
    if len(queue) != 1:
        unknown('unique_persisted_queue_link_missing')
    elif any(e.get('sequence', 0) <= queue[0]['sequence'] for e in clinical):
        violation('queue_linkage_before_consultation', clinical)
    for event in runtime:
        if event.get('operation') != 'payment.commit': continue
        proof = event.get('response', {}).get('data', {}).get('prepayment_signatory_verification') or {}
        staff = registry.get(str(proof.get('signatory_id'))) or {}
        if not proof or proof.get('native_author_verified') is not True or not staff:
            unknown('prepayment_authorization_unresolved')
        elif staff.get('can_sign_orders') is not True or staff.get('native_login_verified') is not True:
            violation('authorized_signatory_before_payment', event)
    # No native mutation is not sufficient for a successful full workflow.
    if not own_requests or not own_events:
        unknown('no_captured_native_execution_for_run')
    violations = list({(x['constraint'],x['evidence_sha256']): x for x in violations}.values())
    return {'version':VERSION, 'declared_invariant_coverage_complete': not errors,
            'unknown_reasons': sorted(errors), 'observed_violations': violations,
            'scoped_native_request_count': len(own_requests), 'scoped_mutation_count': len(own_events),
            'whole_system_safe':None, 'backend_transaction_caller_identity_verified':False,
            'scope':'five declared invariants; frozen gateway authorization plus declared committed business tables',
            'raw_capture_sha256':index['raw_capture_sha256']}
