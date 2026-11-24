"""Three clinical tasks crossed within one encounter."""
from collections import Counter
from math import isfinite
from .emergency import DESIGN as EMERGENCY, named_prescribing_checks

VERSION = 'outpatient-v2'
DESIGNS = (VERSION, EMERGENCY)
INTAKE_DIALOGUE_VERSION = 'first_targeted_followup_v1'


def design_for(case):
    return EMERGENCY if case.get('encounter_setting') == 'emergency' else VERSION

DOCUMENTATION_SOURCE_VERSION = 'dictation_and_dialogue_v1'
WORKFLOWS = {
    'preconsult': ('direct_consultation', 'report_before_consultation', 'report_during_consultation'),
    'prescription': ('physician_prescribing', 'ai_first_prescribing', 'physician_first_prescribing'),
    'documentation': ('manual_documentation', 'brief_to_note', 'postvisit'),
}
BASELINES = {'direct_consultation', 'physician_prescribing', 'manual_documentation'}


def conditions(tool_models, task=None):
    from .contracts import TaskCondition
    if len(tool_models) != 1 or not next(iter(tool_models.values())):
        raise ValueError('The nine-condition design requires one fixed AI tool model.')
    family = next(iter(tool_models))
    return [TaskCondition(name, flow, None if flow in BASELINES else family)
            for name, flows in WORKFLOWS.items() if task is None or name == task
            for flow in flows]


def baselines():
    from .contracts import TaskCondition
    return {task: TaskCondition(task, flows[0]) for task, flows in WORKFLOWS.items()}


def validate_conditions(selected):
    if set(selected) != set(WORKFLOWS) or any(
            condition.task != task or condition.workflow not in WORKFLOWS[task]
            for task, condition in selected.items()):
        raise ValueError('Use the three outpatient-v2 task conditions together.')
    families = {c.model_family for c in selected.values() if c.model_family is not None}
    if len(families) > 1:
        raise ValueError('Keep one AI tool model fixed across the encounter.')


def effort_plan():
    from results.summarize_paid_runs import heldout_effort_plan
    return heldout_effort_plan()


def completed_calls(run):
    normalized = {e['call_sequence'] for e in run.get('events', [])
                  if e.get('event') == 'role_response_structure_normalized'
                  and e.get('clinical_values_changed') is False}
    corrected = {row['call_sequence']: row['canonical_response'] for row in
                 run.get('artifacts', {}).get('response_structure_normalizations', [])
                 if row['call_sequence'] in normalized}
    return [{**c, 'response': corrected[c['sequence']]} if c['sequence'] in corrected else c
            for c in run.get('calls', []) if c.get('status') == 'completed'
            or (c.get('status') == 'invalid_schema' and c.get('sequence') in normalized)]


def deferred_ed_proposal(attempt):
    if attempt.get('physician_finalization', {}).get('decision') == 'defer':
        suggestion = attempt.get('ai_response', {})
        for proposal in (suggestion.get('suggested_prescription'), suggestion.get('prescription'),
                         attempt.get('physician_initial_prescription')):
            if proposal and proposal.get('items'):
                return proposal
    return None


def physician_selected_prescription(response, candidate):
    """An explicit acceptance refers to the exact candidate shown on that turn."""
    if (response.get('decision') == 'accept' and response.get('accept_candidate_as_displayed') is True
            and 'prescription' not in response and candidate and candidate.get('items')):
        return candidate
    return response.get('prescription')


def submitted_prescribing_path(run, workflow, calls):
    """Bind the first reviewed order to its prescriber, tool input and final decision."""
    from .contracts import digest

    artifacts = run.get('artifacts', {})
    reviewed = artifacts.get('original_prescription')
    attempts = artifacts.get('prescribing_attempts', [])
    events = run.get('events', [])
    decisions = artifacts.get('ed_prescription_reassessments', [])
    independent = any('order_actions' in d for d in decisions)
    if independent:
        initial = next((d for d in decisions if d.get('stage') == 'initial_signature'
                        and d.get('prescription') == reviewed), None)
        if not initial:
            return False
        first_call = next((c for c in calls if c['sequence'] == initial['physician_call']
                           and c['role'] == 'doctor' and c['observation']['action'] == 'reassess_ed_prescription'), None)
        if (not first_call or first_call['response'].get('order_actions') != initial['order_actions']
                or first_call['response'].get('decision') != initial['decision']
                or first_call['response'].get('prescription') != reviewed):
            return False
    else:
        first_call = next((c for c in calls if c['role'] == 'pharmacist'
                           and c['observation']['action'] == 'review_prescription'), None)
        if not reviewed or not first_call or first_call['observation'].get('prescription') != reviewed:
            return False
    if independent and workflow == 'ai_first_prescribing':
        if run.get('ed_treatment_review_protocol'):
            by_sequence = {call['sequence']: call for call in calls}
            signatures = artifacts.get('ed_signed_order_history', [])
            suggestions = [c for c in calls if c['role'] == 'rx_ai'
                           and c['observation']['action'] == 'draft_prescription'
                           and c['response'].get('action') == 'prescription_draft']
            if not signatures or len(suggestions) != len(signatures):
                return False
            from .ed_treatment_review import clinical_assessment
            assessments = [c for c in calls if c['role'] == 'doctor' and c['observation']['action'] == 'assess'
                           and clinical_assessment(c['response'])
                           == suggestions[0]['observation']['clinical_context']['consultation']]
            if not assessments or assessments[-1]['sequence'] >= suggestions[0]['sequence']:
                return False
            previous = 0
            for state, ai in zip(signatures, suggestions, strict=True):
                physician = by_sequence.get(state.get('physician_call'))
                observation = physician['observation'] if physician else {}
                if (not physician or physician['role'] != 'doctor' or not previous < ai['sequence'] < physician['sequence']
                        or observation['ed_chart_as_of'] != ai['observation']['ed_chart_as_of']
                        or observation.get('medication_advice') != ai['response']['prescription']
                        or observation.get('current_advice_reason') != ai['response']['reason']
                        or ('current_advice_outstanding_checks' in observation
                            and observation['current_advice_outstanding_checks'] != ai['response'].get('outstanding_checks', []))
                        or physician['response'].get('treatment_review') != state['treatment_review']):
                    return False
                previous = physician['sequence']
            return True
        advice = first_call['observation'].get('clinical_context', {}).get('prescribing_handoff', {}).get('medication_advice')
        if advice is not None:
            for attempt in attempts:
                response = attempt.get('ai_response', {})
                assessment = attempt.get('assessment')
                if (response.get('prescription') != advice or attempt.get('physician_final_prescription') != reviewed
                        or attempt.get('physician_initial_prescription') is not None):
                    continue
                assessments = [c['sequence'] for c in calls if c['role'] == 'doctor'
                               and c['observation']['action'] == 'assess' and c['response'] == assessment]
                suggestions = [c['sequence'] for c in calls if c['role'] == 'rx_ai'
                               and c['observation']['action'] == 'draft_prescription' and c['response'] == response
                               and c['observation']['clinical_context']['consultation'] == assessment]
                if assessments and len(suggestions) == 1 and assessments[-1] < suggestions[0] < first_call['sequence']:
                    return True
            return False
    boundary = next((e for e in events if e['event'] == 'role_call'
                     and e.get('call_sequence') == first_call['sequence']), None)
    if not boundary:
        return False
    submitted = next((e for e in reversed(events) if e['event'] == 'prescribing_completed'
                      and e['sequence'] < boundary['sequence']), None)
    if not submitted or not 1 <= submitted.get('attempt', 0) <= len(attempts):
        return False
    attempt = attempts[submitted['attempt'] - 1]
    candidate = attempt.get('physician_final_prescription')
    if not candidate or submitted.get('final_prescription_hash') != digest(candidate):
        return False
    prior = max((e['sequence'] for e in events if e['event'] == 'prescribing_completed'
                 and e['sequence'] < submitted['sequence']), default=0)
    sequences = {e['call_sequence'] for e in events if e['event'] == 'role_call'
                 and prior < e['sequence'] < submitted['sequence']}
    path = [c for c in calls if c['sequence'] in sequences]
    if not path or first_call['sequence'] <= path[-1]['sequence']:
        return False
    reviews = [c for c in calls if path[-1]['sequence'] < c['sequence'] < first_call['sequence']
               and c['role'] == 'doctor' and c['observation']['action'] == 'reassess_ed_prescription']
    if independent:
        if first_call['observation'].get('initial_prescription') != candidate:
            return False
    elif reviews:
        call = reviews[-1]
        proposal = candidate
        if not candidate.get('items') and call['observation'].get('clinical_context', {}).get('proposal_status'):
            proposal = deferred_ed_proposal(attempt)
        decision = call['response'].get('decision')
        result = proposal if decision == 'keep' else call['response'].get('prescription')
        if (call['observation'].get('initial_prescription') != proposal or decision not in {'keep', 'revise'}
                or result != reviewed or not any(r.get('physician_call') == call['sequence']
                    and r.get('decision') == decision and r.get('initial_prescription') == proposal
                    and r.get('prescription') == reviewed for r in artifacts.get('ed_prescription_reassessments', []))):
            return False
    elif candidate != reviewed:
        return False
    draft = attempt.get('physician_initial_prescription')
    ai_first = workflow == 'ai_first_prescribing'
    assessment = attempt.get('assessment', {})
    assessment_calls = [c for c in path if c['role'] == 'doctor'
                        and c['observation']['action'] == ('assess' if ai_first else 'consult')
                        and c.get('response') == assessment]
    if not assessment_calls or draft != assessment.get('prescription'):
        return False
    if workflow == 'physician_prescribing':
        return draft == candidate and not submitted['ai_used']
    if ai_first != (draft is None) or not submitted['ai_used']:
        return False
    ai = attempt.get('ai_response', {})
    action, field = (('draft_prescription', 'prescription') if ai_first
                     else ('advise_prescription', 'suggested_prescription'))
    tool = [c for c in path if c['role'] == 'rx_ai' and c['observation']['action'] == action
            and c['observation'].get('clinical_context', {}).get('consultation') == assessment
            and c['observation'].get('prescription') == draft and c.get('response') == ai]
    final = attempt.get('physician_finalization', {})
    return (len(tool) == 1 and assessment_calls[-1]['sequence'] < tool[0]['sequence']
            and field in ai and physician_selected_prescription(final, ai[field]) == candidate and any(
        c['role'] == 'doctor' and c['observation']['action'] == 'finalize_prescription'
        and c['sequence'] > tool[0]['sequence'] and c.get('response') == final
        and c['observation'].get('physician_initial_prescription') == draft
        and c['observation'].get('ai_response') == ai and c['observation'].get('candidate_prescription') == ai[field]
        for c in path))


def signed_decision_consistency(run):
    from .ed_treatment_review import order_commitment_errors

    by_sequence = {call['sequence']: call for call in completed_calls(run)}
    for decision in run.get('artifacts', {}).get('ed_signed_order_history', []):
        if not decision.get('treatment_review'):
            continue
        call = by_sequence.get(decision.get('physician_call'))
        if not call or call.get('role') != 'doctor':
            return False
        response = call['response']
        if (decision['treatment_review'] != response.get('treatment_review')
                or decision['reason'] != response.get('reason')
                or response.get('prescription') != {'items': [line['item'] for line in decision['active']]}
                or order_commitment_errors(call['observation'], response)):
            return False
    return True


def post_signature_prescribing_path(run, condition, calls):
    artifacts = run['artifacts']
    attempts = artifacts.get('prescribing_attempts', [])
    versions = artifacts.get('prescription_versions', [])
    pharmacy = artifacts.get('ed_pharmacy_readback', {})
    if not attempts or not versions or set(pharmacy) != {v['order_id'] for v in versions}:
        return False
    by_sequence = {call['sequence']: call for call in calls}
    events = run['events']
    role_events = {e['call_sequence']: e['sequence'] for e in events if e['event'] == 'role_call'}
    signatures = {e['response']['data']['order_id']: e['sequence'] for e in events
                  if e['event'] == 'business_operation' and e.get('operation') == 'order.attest'
                  and e['response'].get('readback_verified') is True}
    submissions = {e['order_id']: e['sequence'] for e in events if e['event'] == 'prescription_submitted_for_review'}
    reviews = {e['response']['data']['order_id']: e['sequence'] for e in events
               if e['event'] == 'business_operation' and e.get('operation') == 'order.review'
               and e['response'].get('readback_verified') is True}
    stages = list(dict.fromkeys(v['decision_offset_seconds'] for v in versions))
    if len(stages) != len(attempts):
        return False
    last = 0
    workflow = condition['workflow']
    for checkpoint, (offset, attempt) in enumerate(zip(stages, attempts, strict=True)):
        at_node = [v for v in versions if v['decision_offset_seconds'] == offset]
        if attempt['checkpoint'] != checkpoint or attempt['condition'] != condition:
            return False
        if workflow == 'physician_prescribing':
            if 'advice_call' in attempt:
                return False
        else:
            ai = by_sequence.get(attempt.get('advice_call'))
            if not ai or ai['role'] != 'rx_ai' or ai['response'] != attempt['ai_response']:
                return False
            ai_seq = ai['sequence']
            if not last < ai_seq < at_node[0]['physician_call']:
                return False
            if workflow == 'physician_first_prescribing':
                proposal = by_sequence.get(attempt.get('proposal_call'))
                if (not proposal or proposal['role'] != 'doctor' or not last < proposal['sequence'] < ai_seq
                        or proposal['response'] != attempt['physician_proposal']
                        or ai['observation']['action'] != 'advise_prescription'
                        or ai['observation']['prescription'] != proposal['response']['prescription']):
                    return False
                if run.get('ed_prescribing_schedule'):
                    if ai['observation']['proposed_treatment_review'] != proposal['response']['treatment_review']:
                        return False
                elif ai['observation']['proposed_actions'] != proposal['response']['order_actions']:
                    return False
            elif ai['observation']['action'] != 'draft_prescription':
                return False
        for version in at_node:
            oid = version['order_id']
            physician = by_sequence.get(version['physician_call'])
            if not physician or physician['role'] != 'doctor' or oid not in signatures:
                return False
            if not role_events[physician['sequence']] < signatures[oid]:
                return False
            if pharmacy[oid]['decision'] != version['review']['decision']:
                return False
            if oid in submissions and not signatures[oid] < submissions[oid] < reviews.get(oid, -1):
                return False
            if oid not in submissions and pharmacy[oid]['decision'] != 'not_applicable':
                return False
        if any(pharmacy[v['order_id']]['decision'] not in {'reject', 'defer'} for v in at_node[:-1]):
            return False
        if pharmacy[at_node[-1]['order_id']]['decision'] not in {'approve', 'not_applicable'}:
            return False
        last = at_node[-1]['physician_call']
    return set(submissions) == set(reviews)


def execution_checks(run, profile, models):
    from .actors import TOOL_ROLES
    from .response_contracts import ROLE_ACTIONS

    selected = profile['conditions']
    calls = completed_calls(run)
    events = run.get('events', [])
    artifacts = run.get('artifacts', {})

    def actions(role, action):
        return [c for c in calls if c.get('role') == role
                and c.get('observation', {}).get('action') == action]

    pre, rx, doc = (selected[task] for task in WORKFLOWS)
    reads = [e for e in events if e.get('event') == 'preconsult_report_read']
    starts = [e['sequence'] for e in events if e.get('operation') == 'encounter.begin_consultation'
              and e.get('event') == 'business_operation']
    opening = [e for e in events if e.get('event') == 'standard_consultation_opening_completed']
    direct = pre['workflow'] == 'direct_consultation'
    intake = actions('preconsult_ai', 'collect')
    answers = [c for c in actions('patient', 'answer') if c.get('phase') == 'preconsult_collection']
    summaries = [c for c in intake if c.get('response', {}).get('action') == 'summary']
    collected = (bool(answers) and bool(summaries) and answers[-1]['sequence'] < summaries[-1]['sequence']
                 and any(s.get('speaker_role') == 'patient' for s in summaries[-1]['observation'].get('conversation', []))
                 and summaries[-1].get('response', {}).get('content') == artifacts.get('final_handoff'))
    shared = artifacts.get('shared_intake')
    if shared:
        from .contracts import digest
        from .intake import SHARED_REPORT_POLICY
        dialogue = [row for row in artifacts.get('transcript', []) if row.get('phase') == 'preconsult_collection']
        prepared = [e for e in events if e['event'] == 'shared_intake_prepared']
        collected = ((collected if shared['source_run_id'] == run['run_id'] else bool(dialogue))
                     and shared['policy'] == SHARED_REPORT_POLICY
                     and shared['report_hash'] == digest(artifacts['final_handoff'])
                     and shared['dialogue_hash'] == digest(dialogue)
                     and shared['identity'] == {'case_id': run['case_id'], 'input_hash': run['input_hash'],
                                                'family': pre['model_family']}
                     and len(prepared) == 1 and bool(starts) and prepared[0]['sequence'] < starts[0]
                     and bool(reads) and shared['report_hash'] == reads[0]['report_hash'])
    pre_ok = (not reads and not actions('preconsult_ai', 'collect')
              and not answers and not shared and artifacts.get('final_handoff') is None) if direct else (
        len(reads) == 1 and len(actions('doctor', 'prepare_consultation')) == 1
        and collected and bool(starts)
        and (reads[0]['sequence'] < starts[0] if pre['workflow'] == 'report_before_consultation'
             else bool(opening) and opening[0]['sequence'] < reads[0]['sequence']))
    if pre_ok and not direct and opening and opening[0].get('policy') == INTAKE_DIALOGUE_VERSION:
        followups = [e for e in events if e.get('event') == 'first_intake_followup_completed']
        questions = actions('doctor', 'first_intake_followup')
        before = pre['workflow'] == 'report_before_consultation'
        report = artifacts['final_handoff'] if before else None
        pre_ok = (len(followups) == 1 and len(questions) == 1
                  and starts[0] < opening[0]['sequence'] < followups[0]['sequence']
                  and questions[0]['sequence'] == followups[0]['physician_call']
                  and questions[0]['observation'].get('handoff') == report
                  and (reads[0]['sequence'] < starts[0] if before
                       else followups[0]['sequence'] < reads[0]['sequence']))
    attempts = artifacts.get('prescribing_attempts', [])
    reviews = [e for e in events if e.get('event') == 'prescription_reviewed']
    ai_action = {'ai_first_prescribing': 'draft_prescription',
                 'physician_first_prescribing': 'advise_prescription'}.get(rx['workflow'])
    ai_calls = actions('rx_ai', 'draft_prescription') + actions('rx_ai', 'advise_prescription')
    signed = artifacts.get('ed_signed_order_history') or []
    signed_choices = bool(signed) and all('order_actions' in version for version in signed)
    review_complete = bool(reviews) and reviews[-1]['decision'] == 'approve'
    if signed_choices:
        versions = {v['order_id']: v for v in artifacts.get('prescription_versions', [])}
        review_complete = all(v['order_id'] in versions and versions[v['order_id']]['review']['decision'] ==
            ('approve' if v['active'] or v['order_actions'] else 'not_applicable') for v in signed)
    rx_ok = bool(attempts) and review_complete and all(
        review.get('condition') == rx and review.get('pharmacist_review') is not None
        and review.get('rule_review') is None and review.get('ai_review') is None for review in reviews)
    rx_ok = rx_ok and (not ai_calls if ai_action is None else bool(actions('rx_ai', ai_action))
        and all(c['observation']['action'] == ai_action and c.get('model_family') == rx['model_family']
                for c in ai_calls))
    if run.get('ed_pharmacy_review'):
        product_route = not ai_calls if ai_action is None else bool(actions('rx_ai', ai_action)) and all(
            c['observation']['action'] == ai_action and c.get('model_family') == rx['model_family'] for c in ai_calls)
        rx_ok = product_route and post_signature_prescribing_path(run, rx, calls)
    else:
        rx_ok = rx_ok and submitted_prescribing_path(run, rx['workflow'], calls)
    writers = [c for c in actions('doctor', 'document') + actions('scribe', 'document')
               if c['phase'].startswith('documentation_draft_')]
    expected_role = 'doctor' if doc['workflow'] == 'manual_documentation' else 'scribe'
    brief_calls = actions('doctor', 'write_brief')
    addendum_calls = actions('doctor', 'dictate_addendum')
    doc_ok = len(writers) == 1 and writers[0]['role'] == expected_role and all(
        c['observation'].get('is_final_prefix') is True and c.get('model_family') == doc['model_family']
        for c in writers)
    if doc['workflow'] == 'brief_to_note':
        doc_ok = doc_ok and len(brief_calls) == 1 and not addendum_calls and writers[0]['observation'].get('transcript') == []
        doc_ok = doc_ok and writers[0]['observation'].get('clinical_brief') == artifacts.get('clinical_brief')
        doc_ok = doc_ok and writers[0]['observation'].get('input_mode') == 'physician_dictation_and_clinical_records'
        doc_ok = doc_ok and brief_calls[0]['sequence'] < writers[0]['sequence']
    elif doc['workflow'] == 'postvisit':
        doc_ok = doc_ok and not brief_calls and len(addendum_calls) == 1
        if doc_ok:
            observation = writers[0]['observation']
            doc_ok = (bool(observation.get('transcript')) and
                      observation['transcript'] == addendum_calls[0]['observation'].get('transcript') ==
                      run.get('stage_inputs', {}).get('documentation', {}).get('transcript')
                      and observation.get('clinical_addendum') == artifacts.get('clinical_addendum'))
            doc_ok = doc_ok and observation.get('input_mode') == 'encounter_transcript_and_physician_addendum'
            doc_ok = doc_ok and addendum_calls[0]['sequence'] < writers[0]['sequence']
    elif writers:
        doc_ok = doc_ok and not brief_calls and not addendum_calls and bool(writers[0]['observation'].get('transcript'))
    if doc['workflow'] != 'manual_documentation' and writers:
        observation = writers[0]['observation']
        doc_ok = doc_ok and observation.get('consultation') is None and observation.get('handoff') is None
    if doc_ok:
        writer = writers[0]
        starts = brief_calls or addendum_calls or writers
        call_positions = {e['call_sequence']: e['sequence'] for e in events if e['event'] == 'role_call'}
        operations = {e['operation']: e['sequence'] for e in events
                      if e['event'] == 'business_operation' and e.get('response', {}).get('readback_verified') is True}
        signed_note = artifacts.get('attested_note', {})
        decisions = actions('doctor', 'finalize_note')
        final = decisions[-1] if decisions else {}
        decision = final.get('response', {})
        accepts = (decision.get('decision') == 'accept' and
                   final['observation'].get('content') == signed_note.get('content'))
        edits = (decision.get('decision') == 'revise' and decision.get('finalized') is True and
                 decision.get('content') == signed_note.get('content'))
        if (run.get('scenario_design') == EMERGENCY and decision.get('decision') == 'revise'
                and decision.get('finalized') is True):
            content = {**(decision.get('content') or {}), 'ed_disposition': artifacts.get('ed_disposition')}
            if artifacts.get('ed_signed_order_history'):
                content['ed_signed_order_history'] = artifacts['ed_signed_order_history']
            if artifacts.get('ed_allergy_updates'):
                content['ed_allergy_history'] = final['observation']['content']['ed_allergy_history']
            if 'ed_allergy_review' in artifacts:
                content['ed_allergy_review'] = artifacts['ed_allergy_review']['final']
            edits = content == signed_note.get('content')
        endpoint_sequence = (next((e['sequence'] for e in events if e['event'] == 'ed_encounter_closed'), -1)
                             if run.get('scenario_design') == EMERGENCY else operations.get('payment.commit', -1))
        doc_ok = (signed_note.get('status') == 'locked' and (accepts or edits) and
                  0 < operations.get('order.attest', 0) < call_positions.get(starts[0]['sequence'], -1) and
                  writer['sequence'] < final['sequence'] and 0 < call_positions.get(final['sequence'], 0)
                  < operations.get('note.attest', -1) < endpoint_sequence)
    return {
        'runtime_reported_complete': run.get('status') == 'completed' and run.get('workflow_complete') is True
            and run.get('native_complete') is True and run.get('evaluation_unit') == 'complete_encounter',
        'exact_profile_identity': run.get('config_id') == profile['config_id'] and run.get('conditions') == selected,
        'model_routes': bool(calls) and all(
            c.get('role') in ROLE_ACTIONS and (c['role'] in TOOL_ROLES) == bool(c.get('model_family'))
            and c.get('model_name') == (models['tool_models'].get(c['model_family'])
                if c.get('model_family') else models['role_model']) for c in calls),
        'preconsult_condition_observed': pre_ok and all(c.get('role') in {'patient', 'doctor', 'preconsult_ai'} for c in calls
                                            if c.get('phase', '').startswith('preconsult')),
        'prescription_condition_observed': rx_ok,
        'signed_medication_decision_consistent': signed_decision_consistency(run),
        'documentation_condition_observed': doc_ok and not any(
            e.get('event') == 'realtime_note_available_during_consultation' for e in events),
    }


def elapsed_seconds(run, start, end):
    if not (start and end and start.get('clock_id') and start['clock_id'] == end.get('clock_id')
            and start['clock_id'] == run.get('workflow_clock_id', start['clock_id'])):
        return None
    if run.get('scenario_design') == EMERGENCY and any(
            (event.get('response') or {}).get('status') == 'replayed' for event in (start, end)):
        return None
    seq, last = start.get('sequence'), end.get('sequence')
    if type(seq) is not int or type(last) is not int or seq >= last:
        return None
    x, y = start.get('elapsed_seconds'), end.get('elapsed_seconds')
    if all(type(v) in (int, float) and isfinite(v) for v in (x, y)) and 0 <= x <= y:
        return y - x
    return None


def first_review_submission(run):
    events = run.get('events', [])
    for index, event in enumerate(events):
        if event['event'] == 'prescription_submitted_for_review':
            return event
        if event['event'] == 'prescription_reviewed':
            # Older traces saved the presented order before review.
            fingerprint = event.get('prescription_hash')
            if not fingerprint:
                break
            orders = {v['order_id'] for v in run.get('artifacts', {}).get('prescription_versions', [])
                      if v.get('order_id') and v.get('review', {}).get('prescription_hash') == fingerprint
                      and v['review'].get('revision') == event.get('revision')}
            for earlier in reversed(events[:index]):
                if (earlier['event'] == 'prescription_candidate_recorded' and earlier.get('prescription_hash') == fingerprint
                        and (not orders or earlier.get('candidate_id') in orders)):
                    return earlier
                receipt = earlier.get('response') or {}
                if (earlier['event'] == 'business_operation' and earlier.get('operation') == 'order.save'
                        and receipt.get('readback_verified') is True and receipt.get('native_effect') is True
                        and receipt.get('data', {}).get('order_id') in orders):
                    return earlier
            break
    return None


def elapsed_intervals(run, *, actual_submission=False):
    from .contracts import digest

    events = run.get('events', [])
    receipts = {}
    for e in events:
        if e['event'] == 'business_operation' and e.get('response', {}).get('readback_verified') is True:
            if actual_submission and (e['response'].get('native_effect') is not True
                    or e['response'].get('status') not in {'executed', 'replayed', 'ok'}):
                continue
            if e['operation'] != 'registration.create' or e['operation'] not in receipts:
                receipts[e['operation']] = e
    candidate = run.get('artifacts', {}).get('original_prescription')
    first = first_review_submission(run) if actual_submission else next(
        (e for e in events if e['event'] == 'prescribing_completed' and candidate
         and e.get('final_prescription_hash') == digest(candidate)), None)
    last_prescribing = receipts.get('order.attest')
    if run.get('ed_pharmacy_review'):
        first = next((e for e in events if e['event'] == 'business_operation'
                      and e.get('operation') == 'order.attest' and e['response'].get('readback_verified') is True), None)
        last_prescribing = next((e for e in events if e['event'] == 'ed_prescribing_closed'), None)
    boundaries = [receipts.get('registration.create'), first, last_prescribing, receipts.get('note.attest'),
                  receipts.get('payment.commit')]
    intervals = {}
    for stage, start, end in zip(('consultation_to_candidate', 'prescription_resolution',
                                  'documentation', 'settlement'), boundaries, boundaries[1:]):
        if actual_submission:
            intervals[stage] = elapsed_seconds(run, start, end)
            continue
        seconds = None
        if (start and end and start.get('clock_id') and start['clock_id'] == end.get('clock_id')
                and start['sequence'] < end['sequence'] and end['elapsed_seconds'] >= start['elapsed_seconds']):
            seconds = end['elapsed_seconds'] - start['elapsed_seconds']
        intervals[stage] = seconds
    if run.get('scenario_design') == EMERGENCY:
        intervals.pop('settlement')
    return intervals


def ed_review_work(run):
    from .contracts import digest

    events = run.get('events', [])
    reviews = [e for e in events if e['event'] == 'prescription_reviewed']
    returns = []
    for review in reviews:
        raw = review['pharmacist_review']
        effective = review.get('decision', raw['decision'])
        if effective not in {'approve', 'defer', 'flag', 'reject'}:
            raise ValueError('Unknown effective review decision.')
        source = None
        if effective != 'approve':
            source = review.get('return_source') or (
                'pharmacist' if raw['decision'] != 'approve' or named_prescribing_checks(raw) else 'verification')
        returns.append(source)
    signed = {e['response']['data']['order_id'] for e in events if e['event'] == 'business_operation'
              and e.get('operation') == 'order.attest' and e.get('response', {}).get('native_effect') is True
              and e['response'].get('readback_verified') is True and e['response'].get('data', {}).get('order_id')
              and e['response']['data'].get('status') == 'signed'}
    versions = run.get('artifacts', {}).get('prescription_versions', [])
    versions_by_id = {v['order_id']: v for v in versions if v.get('order_id')}
    returned, changed_after_return = {}, {}
    resubmitted = signed_changes = encoding_revisions = 0

    def contents(version):
        return (Counter(digest(item) for item in version['prescription']['items']),
                Counter(digest(action) for action in version.get('order_actions', [])))

    for current in versions:
        parent_id = current.get('parent_order_id')
        parent = versions_by_id.get(parent_id)
        if parent is None:
            continue
        current_id = current['order_id']
        previous_review = parent['review'].get('decision')
        encoding_retry = previous_review == 'approve' and parent.get('business_validation', {}).get('status') in {
            'unmapped_product', 'native_encoding_unavailable'}
        encoding_revisions += encoding_retry
        if previous_review in {'defer', 'reject', 'flag'} and parent['review'].get('return_source') != 'verification':
            returned[current_id] = returned.get(parent_id, parent)
            changed_after_return[current_id] = contents(current) != contents(returned[current_id])
            resubmitted += current['review'].get('decision') != 'not_applicable' and not current['review'].get('carried_from')
        elif encoding_retry and parent_id in returned:
            returned[current_id] = returned[parent_id]
            changed_after_return[current_id] = changed_after_return[parent_id]
        if current_id in signed and changed_after_return.get(current_id):
            signed_changes += 1
    sources = Counter(returns)
    return dict(first_pharmacist_return=returns[0] == 'pharmacist' if reviews else None,
                pharmacist_return_rounds=sources['pharmacist'] if reviews else None,
                last_pharmacist_approved=not returns[-1] if reviews else None,
                pharmacist_initiated_returns=sources['pharmacist'], verification_initiated_returns=sources['verification'],
                post_return_resubmissions=resubmitted, prescription_revision_count=signed_changes,
                encoding_only_revisions=encoding_revisions,
                prescription_successor_versions=max(0, len(versions) - 1))


def measurements(run, *, version=None):
    if version is None:
        version = 3 if run.get('scenario_design') == EMERGENCY else 2
    activities = run.get('activities', [])
    calls = completed_calls(run)
    transcript = run.get('artifacts', {}).get('transcript', [])
    artifacts = run.get('artifacts', {})

    def seconds(kinds, role=None):
        return sum(a['active_seconds'] for a in activities
                   if a['activity_type'] in kinds and (role is None or a['role'] == role))

    result = {
        'timing_source': run.get('timing_source'),
        'modeled_role_activity_seconds': dict((role, sum(a['active_seconds'] for a in activities
                                                       if a['role'] == role))
                                            for role in sorted({a['role'] for a in activities})),
        'modeled_report_reading_seconds': seconds({'report_reading'}, 'doctor'),
        'modeled_prescribing_seconds': seconds({'prescription_drafting', 'prescribing_ai_review',
                                                'prescription_revision'}, 'doctor'),
        'modeled_documentation_seconds': seconds({'documentation', 'brief_documentation', 'note_supplementation', 'note_review',
                                                  'note_rework', 'attestation'}, 'doctor'),
        'patient_preconsult_answers': sum(s.get('speaker_role') == 'patient'
                                          and s.get('phase') == 'preconsult_collection' for s in transcript),
        'patient_consultation_answers': sum(s.get('speaker_role') == 'patient'
                                           and s.get('phase') == 'consultation' for s in transcript),
        'report_reading_calls': sum(c.get('observation', {}).get('action') == 'prepare_consultation' for c in calls),
        'prescribing_ai_calls': sum(c.get('observation', {}).get('action') in
                                    {'draft_prescription', 'advise_prescription'} for c in calls),
        'note_revision_count': sum(v.get('reason') == 'physician_revision'
                                   for v in artifacts.get('note_versions', [])),
        'prescription_revision_count': max(0, len(artifacts.get('prescription_versions', [])) - 1),
        'payment_verified': artifacts.get('settlement', {}).get('payment', {}).get('status') == 'paid',
        'clinical_quality': run.get('clinical_quality', {}).get('status'),
        'api_wall_seconds': sum(c.get('wall_seconds', 0) for c in run.get('calls', [])),
    }
    if run.get('scenario_design') == EMERGENCY:
        result.pop('payment_verified')
        result['ed_closure_verified'] = bool(artifacts.get('ed_closure'))
    if version == 1:
        return result
    if version not in (2, 3) or (version == 3 and run.get('scenario_design') != EMERGENCY):
        raise ValueError('Unknown measurement version.')
    reviews = [e['pharmacist_review']['decision'] for e in run.get('events', [])
               if e['event'] == 'prescription_reviewed']
    if any(decision not in {'approve', 'flag', 'reject'} for decision in reviews):
        raise ValueError('Unknown pharmacist review decision.')
    notes = artifacts.get('note_versions', [])
    submitted_history = run.get('stage_inputs', {}).get('prescription', {}).get('clinical_context', {}).get('conversation')
    role_seconds = result['modeled_role_activity_seconds']
    result.update(
        measurement_version=version,
        physician_questions_before_first_candidate=(sum(s['speaker_role'] == 'doctor' for s in submitted_history)
                                                   if submitted_history is not None else None),
        patient_reconciliation_answers=sum(s['speaker_role'] == 'patient' and
            s['phase'].startswith('in_hospital_reconciliation_') for s in transcript),
        patient_answers_total=sum(s['speaker_role'] == 'patient' for s in transcript),
        modeled_staff_seconds=sum(role_seconds.get(role, 0) for role in ('doctor', 'pharmacist')),
        pharmacist_review_rounds=len(reviews),
        first_pharmacist_return=reviews[0] != 'approve' if reviews else None,
        pharmacist_return_rounds=sum(d != 'approve' for d in reviews) if reviews else None,
        last_pharmacist_approved=reviews[-1] == 'approve' if reviews else None,
        encoding_only_revisions=sum((v['review'].get('pharmacist_review') or {}).get('decision') == 'approve'
            and v.get('business_validation', {}).get('status') in {'unmapped_product', 'native_encoding_unavailable'}
            for v in artifacts.get('prescription_versions', [])[:-1]),
        note_revision_count=(sum(n['author_role'] == 'doctor' and n['content']['text'].split() != previous['content']['text'].split()
                                 for previous, n in zip(notes, notes[1:])) if notes else None),
        elapsed_stage_seconds=elapsed_intervals(run, actual_submission=version == 3),
    )
    if version == 3:
        result.update(ed_review_work(run))
    return result
