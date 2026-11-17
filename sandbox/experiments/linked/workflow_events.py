"""Runtime signals for clinical rework and execution issues."""
from .contracts import digest

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
    if kind == 'prescription_reviewed' and event.get('decision') in {'defer', 'reject'}:
        category, code, owner = 'clinical_collaboration', 'prescription_return', 'doctor'
    elif kind == 'clinical_work_routed':
        category, code = 'clinical_collaboration', 'pending_clinical_work'
        owner = event['work']['owner']
    elif kind in {'clinical_work_returned_to_physician', 'focused_assessment_completed'}:
        category, code, owner = 'clinical_collaboration', 'clinical_work_response', 'doctor'
    elif kind == 'note_returned_for_revision':
        category, code, owner = 'clinical_collaboration', 'note_revision', 'doctor'
    elif kind in {'role_response_json_invalid', 'role_response_schema_invalid', 'role_response_boundary_invalid',
                  'patient_fact_pointer_error', 'repeated_care_request', 'repeated_patient_question'}:
        category, code, owner = 'model_execution', kind, event.get('role', 'doctor')
        status = 'review_pending'
    elif kind == 'provider_retry_queued':
        category, code, owner = 'model_execution', 'provider_interruption', 'dispatcher'
        status = 'waiting_retry'
    elif kind == 'business_request_outcome' and event.get('status') != 'response_observed':
        category, code, owner = 'model_execution', 'business_state_uncertain', 'adapter'
        status = 'review_pending'
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
            issue['code'] == 'prescription_return' and event['event'] == 'prescription_reviewed'
            and event.get('decision') == 'approve'
        ) or (
            issue['code'] == 'pending_clinical_work' and event['event'] == 'clinical_work_returned_to_physician'
            and issue.get('work_id') == event.get('work_id')
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
        'workflow_endpoint_observed': completed,
        'clinical_validity': 'not_inferred_from_workflow_completion',
    }
