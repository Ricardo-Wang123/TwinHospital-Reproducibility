"""Verify the actual request inputs for the paper's three clinical configurations."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path

from results.audit_workflow_feedback import (
    archived_paid_cohort, business_events, matching_model_record, paid_traces, single,
)
from sandbox.experiments.linked import actors
from sandbox.experiments.linked.clinical import PHYSICIAN_DIALOGUE_PHASES
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.json_boundary import DuplicateJSONKeyError, parse_json_response
from sandbox.experiments.visibility import assert_visible


INTAKE = {'preconsult_collection', 'nurse_summary_review', 'doctor_handoff_verification'}
REVIEW_INPUT = {'action', 'prescription', 'clinical_context', 'formulary'}
TOOL_TASK = {'preconsult_ai': 'preconsult', 'rx_ai': 'prescription', 'scribe': 'documentation'}
ENVELOPE = {'allowed_decisions', 'allowed_response_actions', 'required_response', 'response_contract_version',
            'simulation_scope', 'data_kind', 'schema_correction'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked_calls(run, archive, models):
    phases = defaultdict(list)
    events = {row['call_sequence']: row for row in run['events'] if row['event'] == 'role_call'}
    normalized = {row['call_sequence']: row for row in run['artifacts'].get('response_structure_normalizations', [])}
    for call in run['calls']:
        phase, role = call['phase'], call['role']
        if not (phase in INTAKE and role != 'patient'
                or phase.startswith(('prescription_review_', 'documentation_draft_', 'note_review_'))):
            continue
        record = matching_model_record(run, call, archive)
        event = events[call['sequence']]
        require(record is not None and event['observation_hash'] == call['observation_hash']
                and event['role'] == role and event['phase'] == phase,
                'A stage call does not match its provider request and runtime event')
        messages = record['request_body']['messages']
        family = run['conditions'][TOOL_TASK[role]]['model_family'] if role in TOOL_TASK else None
        model = models['role_model'] if family is None else models['tool_models'][family]
        require([row['role'] for row in messages] == ['system', 'user']
                and messages[0]['content'] == actors.system_prompt(role)
                and record['request_body']['model'] == call['model_name'] == model and call['model_family'] == family,
                'An archived request contains extra context or a different role/model')
        observation = call['observation']
        base = {key: value for key, value in observation.items() if key not in {'data_kind', 'schema_correction'}}
        require(digest(base) == call['original_clinical_input_hash'], 'Clinical input changed on a schema retry')
        choice = single(record['response_body']['choices'], 'provider response choice')
        if 'schema_correction' in observation:
            previous = phases[phase][-1]
            correction = observation['schema_correction']
            require(previous['role'] == role and previous['base'] == base
                    and previous['status'] != 'completed'
                    and correction['original_clinical_input_hash'] == digest(base)
                    and (correction.get('previous_invalid_response_hash') == previous['invalid_text_hash']
                         if 'previous_invalid_response_hash' in correction
                         else correction['previous_invalid_response'] == previous['response']),
                    'Correction input does not bind the same role and its preceding response')
        response = None
        invalid_text_hash = None
        try:
            response, _ = parse_json_response(choice['message']['content'], finish_reason=choice['finish_reason'],
                                             policy=call['response_parse']['policy'])
        except (json.JSONDecodeError, DuplicateJSONKeyError) as exc:
            require(call['status'] == 'failed', 'Archived invalid JSON was accepted by the runtime')
            invalid_text_hash = digest(exc.doc)
        else:
            require(call['status'] in {'completed', 'invalid_schema', 'invalid_response_boundary'}
                    and response == call['response'] and digest(response) == event['response_hash'],
                    'Provider response does not match the recorded role decision')
            if call['status'] == 'invalid_response_boundary':
                try:
                    assert_visible(response)
                except ValueError as exc:
                    require(str(exc) == call.get('response_boundary_error'),
                            'Output boundary rejection does not match the archived response')
                else:
                    raise ValueError('An output marked boundary-invalid passes the strict boundary')
            if call['sequence'] in normalized:
                proof = normalized[call['sequence']]
                require(proof['original_response'] == response and proof['clinical_values_changed'] is False,
                        'Role response normalization has no matching raw response')
                response = proof['canonical_response']
            invalid_text_hash = digest(response)
        phases[phase].append({'role': role, 'status': call['status'], 'input': observation, 'base': base,
                              'invalid_text_hash': invalid_text_hash, 'response': response, 'sequence': event['sequence']})
    return phases


def check_intake(run, calls, counts):
    workflow = run['conditions']['preconsult']['workflow']
    artifacts = run['artifacts']
    collected = calls['preconsult_collection']
    reviewed = calls['nurse_summary_review']
    expected_role = 'nurse' if workflow == 'nurse_only' else 'preconsult_ai'
    require(collected and all(row['role'] == expected_role and row['input']['prior_summary'] is None
                              for row in collected), 'Initial handoff did not follow its assigned collector')
    initial = single((row['response']['content'] for row in collected if row['status'] == 'completed'
                      and row['response']['action'] == 'summary'), 'initial handoff summary')
    require(initial == artifacts['initial_summary'] and bool(reviewed) == (workflow == 'ai_nurse_review'),
            'Initial handoff or review routing disagrees with the assigned workflow')
    if reviewed:
        require(all(row['role'] == 'nurse' and row['input']['prior_summary'] == initial for row in reviewed),
                'Nurse review did not receive the generated intake summary')
    handoff = single((row['response']['content'] for row in reviewed if row['status'] == 'completed'
                      and row['response']['action'] == 'summary'), 'nurse-reviewed summary') if reviewed else initial
    retrieved = single((row for row in run['events'] if row['event'] == 'business_operation'
                        and row['operation'] == 'handoff.read' and row['response']['readback_verified']), 'handoff retrieval')
    require(handoff == artifacts['final_handoff'] == retrieved['response']['data']['content'],
            'The delivered handoff differs from its recorded generation and retrieval')
    verification = calls['doctor_handoff_verification']
    require(verification and all(row['role'] == 'doctor' and row['input']['prior_summary'] == handoff
            and row['sequence'] > retrieved['sequence']
            and all(segment['phase'] == 'doctor_handoff_verification' for segment in row['input']['conversation'])
            for row in verification), 'Physician verification did not use the retrieved summary and permitted dialogue')
    counts['verified_handoffs_by_workflow'][workflow] += 1


def check_prescription(run, calls, counts):
    workflow = run['conditions']['prescription']['workflow']
    for version in run['artifacts']['prescription_versions']:
        review = version['review']
        rule, ai, pharmacist, senior = (review[key + '_review'] for key in ('rule', 'ai', 'pharmacist', 'senior'))
        phase = f"prescription_review_{review['revision']}"
        ai_calls = calls.pop(phase + ':ai', [])
        independent = calls.pop(phase + ':independent', [])
        flagged = calls.pop(phase + ':flagged_review', [])
        adjudication = calls.pop(phase + ':adjudication', [])
        automated = ('reject' if ai and ai['decision'] == 'reject' else
                     'flag' if rule['decision'] == 'flag' or ai and ai['decision'] == 'flag' else 'approve')
        disagreement = (workflow == 'parallel' and rule['decision'] != 'reject'
                        and pharmacist is not None and automated != pharmacist['decision'])
        require(bool(ai_calls) == (workflow == 'parallel' or workflow == 'sequential' and rule['decision'] != 'reject')
                and bool(independent) == (workflow == 'parallel')
                and bool(flagged) == (workflow != 'parallel' and rule['decision'] != 'reject'
                                      and (rule['decision'] == 'flag' or ai is not None and ai['decision'] in {'flag', 'reject'}))
                and bool(adjudication) == (senior is not None) == disagreement,
                'Prescription review routing differs from the assigned configuration')
        for group, role, decision, extra in (
            (ai_calls, 'rx_ai', ai, set()), (independent, 'pharmacist', pharmacist, set()),
            (flagged, 'pharmacist', pharmacist, {'rule_review', 'ai_review'}),
            (adjudication, 'senior_pharmacist', senior, {'reviews', 'defer_semantics'}),
        ):
            if not group:
                continue
            require(all(row['role'] == role and set(row['input']) - ENVELOPE == REVIEW_INPUT | extra
                        and row['input']['action'] == ('adjudicate' if role == 'senior_pharmacist' else 'review_prescription')
                        and row['input']['prescription'] == version['prescription'] for row in group)
                    and single((row['response'] for row in group if row['status'] == 'completed'), 'review decision') == decision,
                    'A review has a different candidate, input view, or accepted response')
        if independent:
            require(all(row['input'][key] == independent[0]['input'][key]
                        for row in independent + ai_calls for key in REVIEW_INPUT),
                    'Parallel reviewers did not receive the same independent clinical inputs')
        if flagged:
            require(all(row['input']['rule_review'] == rule and row['input']['ai_review'] == ai
                        for row in flagged) and (not ai_calls or ai_calls[-1]['sequence'] < flagged[0]['sequence']),
                    'Triggered pharmacist review did not use the prior screens')
        if adjudication:
            require(all(row['input']['reviews'] == [rule, ai, pharmacist] for row in adjudication)
                    and max(independent[-1]['sequence'], ai_calls[-1]['sequence']) < adjudication[0]['sequence'],
                    'Adjudication did not follow both independent reviews')
            counts['adjudication_rounds'] += 1
        counts['prescription_rounds_by_workflow'][workflow] += 1
    require(not any(phase.startswith('prescription_review_') for phase in calls),
            'The archive contains a review request outside its recorded rounds')


def physician_dialogue(run):
    transcript = run['artifacts']['transcript']
    visible = [row for row in transcript if row['phase'] in PHYSICIAN_DIALOGUE_PHASES]
    by_sequence = {row['sequence']: row for row in transcript}
    events = {row['call_sequence']: row['sequence'] for row in run['events'] if row['event'] == 'role_call'}
    available = {}
    for call in run['calls']:
        if call['role'] != 'patient' or call['status'] != 'completed' or call['phase'] not in PHYSICIAN_DIALOGUE_PHASES:
            continue
        question = call['observation']['conversation'][-1]
        sequence = question['sequence']
        answer = by_sequence[sequence + 1]
        require(question == by_sequence[sequence] and question['speaker_role'] == 'doctor'
                and answer['speaker_role'] == 'patient' and answer['phase'] == question['phase'] == call['phase']
                and answer['text'] == call['response']['text'], 'Physician dialogue is not bound to its patient turns')
        available[sequence] = available[sequence + 1] = events[call['sequence']]
    require(set(available) == {row['sequence'] for row in visible}, 'Some physician dialogue turns have no timing evidence')
    return visible, available


def check_documentation(run, calls, counts):
    workflow = run['conditions']['documentation']['workflow']
    visible, available = physician_dialogue(run)
    closed = single((row['sequence'] for row in run['events'] if row['event'] == 'consultation_completed'),
                    'consultation completion')
    signed = single(business_events(run, 'order.attest'), 'signed prescription')['sequence']
    saved = sorted((row['sequence'], row['response']['data']['version'])
                   for op in ('note.save', 'note.revise') for row in business_events(run, op))
    versions = {row['version']: row for row in run['artifacts']['note_versions']}
    drafts = []
    for phase, rows in calls.items():
        if not phase.startswith(('documentation_draft_', 'note_review_')):
            continue
        for row in rows:
            value = row['input']
            prefix = value['transcript']
            sequences = [segment['sequence'] for segment in prefix]
            require(prefix == visible[:len(prefix)] and value['allowed_source_scope']['source_sequences'] == sequences
                    and all(available[seq] < row['sequence'] for seq in sequences),
                    'Documentation request includes dialogue outside the available physician prefix')
            previous = next((versions[version] for order, version in reversed(saved) if order < row['sequence']), None)
            if value['action'] == 'document':
                require(row['role'] == ('doctor' if workflow == 'manual_documentation' else 'scribe')
                        and value['previous_draft'] == (previous['content'] if previous else None),
                        'Drafting role or prior document version differs')
                if row['status'] == 'completed':
                    drafts.append(value['is_final_prefix'])
            else:
                require(value['action'] == 'review_note' and row['role'] == 'doctor' and previous
                        and value['content'] == previous['content'] and value['version'] == previous['version'],
                        'Physician did not review the saved version')
            if value['is_final_prefix']:
                require(closed < signed < row['sequence'] and prefix == visible
                        and value['approved_prescription'] == run['artifacts']['final_prescription']
                        and value['observed_business_state']['prescription'] == run['artifacts']['prescription_business_state'],
                        'Final documentation does not use the complete encounter and signed prescription')
            else:
                history = value['hospital_history']
                panel = value['current_encounter_measurements']
                cutoff = sequences[-1] if sequences else 0
                require(workflow == 'realtime' and row['sequence'] < closed
                        and all(value[key] is None for key in ('consultation', 'approved_prescription', 'clinical_disposition'))
                        and value['observed_business_state']['prescription'] is None
                        and history['last_query_feedback'] is None
                        and all(page['queried_after_sequence'] < cutoff for page in history['queried_pages'])
                        and (panel is None or panel['queried_after_sequence'] < cutoff),
                        'Interim documentation receives later clinical evidence or is generated after consultation')
            counts['documentation_requests_by_scope']['final' if value['is_final_prefix'] else 'interim'] += 1
    require(drafts.count(True) == 1 and (False in drafts) == (workflow == 'realtime'),
            'Draft scope does not follow the assigned documentation configuration')
    counts['verified_documentation_by_workflow'][workflow] += 1


def audit_stage_inputs(bundle, archive):
    bundle, archive = bundle.resolve(), archive.resolve()
    spec, fingerprint, paid = archived_paid_cohort(bundle)
    require(sha256(Path(actors.__file__).read_bytes()).hexdigest() == spec['code_hashes']['actors.py'],
            'Use the frozen role prompts when comparing provider requests')
    profiles = {row['label']: row['conditions'] for row in spec['linked_acceptance']['profiles']}
    counts = {key: Counter() for key in ('verified_handoffs_by_workflow', 'prescription_rounds_by_workflow',
              'verified_documentation_by_workflow', 'documentation_requests_by_scope')}
    counts.update(verified_provider_requests=0, adjudication_rounds=0)
    for run, audit in paid_traces(bundle, paid):
        require(run['conditions'] == profiles[audit['profile']], 'The run differs from its frozen configuration')
        calls = checked_calls(run, archive, spec['models'])
        counts['verified_provider_requests'] += sum(map(len, calls.values()))
        check_intake(run, calls, counts)
        check_prescription(run, calls, counts)
        check_documentation(run, calls, counts)
    return {'scope': 'archived intake, prescription review and documentation request inputs',
            'paid_cohort_sha256': fingerprint, 'verified_paid': len(paid), **counts,
            'model_calls': 0, 'native_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--model-evidence', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_stage_inputs(args.bundle, args.model_evidence), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
