"""Runtime signals for clinical rework and execution issues."""
from .contracts import digest

RESPONSE_FAILURES = {'role_response_json_invalid', 'role_response_schema_invalid', 'role_response_boundary_invalid'}
POLICY = {
    'version': 'workflow_event_signals_v1',
    'classes': ['clinical_collaboration', 'model_execution'],
    'repetition_threshold': 2,
    'classification': 'observed event or suspected anomaly; clinical validity and root cause require review',
    'duration_alone_establishes_error': False,
}


def observe_event(event, events, artifacts):
    kind = event['event']
    category = code = owner = None
    status = 'observed'
    if kind == 'prescription_reviewed' and event.get('decision') in {'defer', 'reject', 'flag'}:
        if event.get('return_source') == 'verification':
            category, code, owner = 'model_execution', 'prescribing_verification_block', 'adapter'
            status = 'review_pending'
        else:
            category, code, owner = 'clinical_collaboration', 'prescription_return', 'doctor'
    elif kind == 'ed_checks_unresolved_after_review':
        category, code, owner = 'clinical_collaboration', 'prescribing_check_unresolved', 'doctor'
        status = 'review_pending'
    elif kind == 'ed_report_inventory_mismatch':
        category, code, owner = 'model_execution', 'result_delivery_mismatch', 'adapter'
        status = 'review_pending'
    elif kind == 'prescription_candidate_recorded' and event.get('business_validation', {}).get('status') in {
            'unmapped_product', 'native_encoding_unavailable'}:
        category, code, owner = 'model_execution', 'order_encoding_feedback', 'doctor'
        status = 'review_pending'
    elif kind == 'signed_order_validation_failed':
        category, code, owner = 'model_execution', 'order_encoding_feedback', 'doctor'
        status = 'review_pending'
    elif kind == 'clinical_work_routed':
        category, code = 'clinical_collaboration', 'pending_clinical_work'
        owner = event['work']['owner']
    elif kind in {'clinical_work_returned_to_physician', 'focused_assessment_completed'}:
        category, code, owner = 'clinical_collaboration', 'clinical_work_response', 'doctor'
    elif kind == 'note_returned_for_revision':
        category, code, owner = 'clinical_collaboration', 'note_revision', 'doctor'
    elif kind in RESPONSE_FAILURES or kind in {
                  'patient_fact_pointer_error', 'repeated_care_request', 'repeated_patient_question',
                  'intake_summary_before_patient_answer'}:
        category, code, owner = 'model_execution', kind, event.get('role', 'doctor')
        status = 'review_pending'
    elif kind == 'provider_retry_queued':
        category, code, owner = 'model_execution', 'provider_interruption', 'dispatcher'
        status = 'waiting_retry'
    elif kind == 'business_request_outcome' and event.get('status') != 'response_observed':
        category, code, owner = 'model_execution', 'business_state_uncertain', 'adapter'
        status = 'review_pending'
    elif kind == 'business_transport_recovered':
        category, code, owner = 'model_execution', 'business_transport_recovered', 'adapter'
        status = 'response_observed'
    if category is None:
        return None
    issue = {
        'issue_id': digest([event['run_id'], event.get('execution_segment', 1), event['sequence'], code])[:24],
        'run_id': event['run_id'], 'case_id': event['case_id'], 'config_id': event['config_id'],
        'event_class': category, 'code': code, 'owner': owner,
        'source_event_sequence': event['sequence'], 'occurred_at': event['occurred_at'],
        'source_execution_segment': event.get('execution_segment', 1),
        'elapsed_seconds': event['elapsed_seconds'], 'status': status,
        'review_status': 'not_reviewed', 'clinical_validity_inferred': False,
    }
    if kind == 'clinical_work_routed':
        issue['work_id'] = event['work']['work_id']
    if kind == 'prescription_reviewed' and 'checkpoint' in event:
        issue['checkpoint'] = event['checkpoint']
        issue['requires_review_readback'] = 'required_followup' in event
    if kind == 'business_request_outcome':
        issue['request_id'] = event['request_id']
    if kind == 'business_transport_recovered':
        issue.update(request_id=event['request_id'], resolved_at=event['occurred_at'],
                     resolution_event_sequence=event['sequence'], resolution_execution_segment=event['execution_segment'])
    if kind == 'provider_retry_queued':
        issue.update(issue_id=digest([event['run_id'], 'provider', event['issue']['number']])[:24],
                     interruption_number=event['issue']['number'])
    return issue


def update_resolutions(event, issues):
    changes = []
    for issue in issues:
        if issue.get('resolved_at'):
            continue
        resolved = (
            issue['code'] in {'prescription_return', 'prescribing_verification_block'}
            and event['event'] == ('ed_pharmacy_review_read_back' if issue.get('requires_review_readback') else 'prescription_reviewed')
            and event.get('decision') == 'approve'
            and issue.get('checkpoint') == event.get('checkpoint')
        ) or (
            issue['code'] in RESPONSE_FAILURES and event['event'] == 'role_response_corrected'
            and issue['source_event_sequence'] in event['corrected_events']
        ) or (
            issue['code'] == 'business_state_uncertain' and event['event'] == 'business_request_outcome'
            and event.get('status') == 'response_observed' and event.get('readback_verified') is True
            and issue['request_id'] == event['request_id']
        ) or (
            issue['code'] == 'pending_clinical_work' and event['event'] == 'clinical_work_returned_to_physician'
            and issue.get('work_id') == event.get('work_id')
        ) or (
            issue['code'] == 'order_encoding_feedback' and event['event'] == 'business_operation'
            and event.get('operation') == 'order.attest' and event.get('response', {}).get('readback_verified')
        ) or (
            issue['code'] == 'note_revision' and event.get('operation') == 'note.attest'
            and event['event'] == 'business_operation' and event.get('response', {}).get('readback_verified')
        )
        if resolved:
            issue.update(status='response_observed', resolved_at=event['occurred_at'],
                         resolution_event_sequence=event['sequence'],
                         resolution_execution_segment=event.get('execution_segment', 1))
            changes.append(dict(issue))
        if event['event'] == 'run_completed':
            issue['workflow_endpoint_observed'] = True
    return changes


def summarize(issues, events):
    completed = any(e['event'] == 'run_completed' for e in events)
    return {
        'policy': POLICY,
        'clinical_collaboration_events': sum(i['event_class'] == 'clinical_collaboration' for i in issues),
        'model_execution_signals': sum(i['event_class'] == 'model_execution' for i in issues),
        'model_execution_recoveries': sum(i['event_class'] == 'model_execution' and bool(i.get('resolved_at')) for i in issues),
        'workflow_endpoint_observed': completed,
        'clinical_validity': 'not_inferred_from_workflow_completion',
    }


def trace_counts(run):
    """Count signals and observed resolutions in a single attempt's retained trace."""
    keys = ('clinical_collaboration_events', 'model_execution_signals', 'model_execution_recoveries')
    if 'workflow_issues' not in run:
        return {k: run.get('workflow_event_summary', {}).get(k, 0) for k in keys}

    events = run['events']
    issues = {issue['issue_id']: dict(issue) for issue in run['workflow_issues']}
    by_sequence = {event['sequence']: event for event in events}
    for issue in issues.values():
        if issue['code'] == 'prescription_return' and (
                by_sequence.get(issue.get('source_event_sequence'), {}).get('return_source') == 'verification'):
            issue.update(event_class='model_execution', code='prescribing_verification_block', owner='adapter')
    by_source = {issue.get('source_event_sequence'): [] for issue in issues.values()}
    for issue in issues.values():
        if issue['code'] in RESPONSE_FAILURES and not issue.get('resolved_at'):
            by_source[issue.get('source_event_sequence')].append(issue)
    for event in events:
        if event['event'] == 'role_response_corrected':
            for sequence in event['corrected_events']:
                for issue in by_source.get(sequence, []):
                    update_resolutions(event, (issue,))
    counts = summarize(issues.values(), events)
    return {k: counts[k] for k in keys}
