"""Three clinical tasks crossed within one outpatient encounter."""
VERSION = 'outpatient-v2'
LEGACY_VERSION = 'legacy-v1'
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


def is_current(selected):
    return any(c.workflow in set(WORKFLOWS[task]) - {'manual_documentation', 'postvisit'}
               for task, c in selected.items())


def effort_plan():
    from results.summarize_paid_runs import heldout_effort_plan
    plan = heldout_effort_plan()
    plan['version'] = 'outpatient-role-effort-v2'
    plan['contrasts'] = {task: {'baseline': flows[0], 'assisted': list(flows[1:])}
                         for task, flows in WORKFLOWS.items()}
    plan['population'] = 'matched_verified_paid_encounters'
    return plan


def completed_calls(run):
    normalized = {e['call_sequence'] for e in run.get('events', [])
                  if e.get('event') == 'role_response_structure_normalized'
                  and e.get('clinical_values_changed') is False}
    return [c for c in run.get('calls', []) if c.get('status') == 'completed'
            or (c.get('status') == 'invalid_schema' and c.get('sequence') in normalized)]


def execution_checks(run, profile, models):
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
    opening = [e['sequence'] for e in events if e.get('event') == 'standard_consultation_opening_completed']
    direct = pre['workflow'] == 'direct_consultation'
    pre_ok = (not reads and not actions('preconsult_ai', 'collect')
              and artifacts.get('final_handoff') is None) if direct else (
        len(reads) == 1 and len(actions('doctor', 'prepare_consultation')) == 1
        and bool(actions('preconsult_ai', 'collect')) and bool(starts)
        and (reads[0]['sequence'] < starts[0] if pre['workflow'] == 'report_before_consultation'
             else bool(opening) and opening[0] < reads[0]['sequence']))
    attempts = artifacts.get('prescribing_attempts', [])
    reviews = [e for e in events if e.get('event') == 'prescription_reviewed']
    ai_action = {'ai_first_prescribing': 'draft_prescription',
                 'physician_first_prescribing': 'advise_prescription'}.get(rx['workflow'])
    ai_calls = actions('rx_ai', 'draft_prescription') + actions('rx_ai', 'advise_prescription')
    rx_ok = bool(attempts) and bool(reviews) and all(
        review.get('condition') == rx and review.get('pharmacist_review') is not None
        and review.get('rule_review') is None and review.get('ai_review') is None for review in reviews)
    rx_ok = rx_ok and (not ai_calls if ai_action is None else bool(actions('rx_ai', ai_action))
        and all(c['observation']['action'] == ai_action and c.get('model_family') == rx['model_family']
                for c in ai_calls))
    writers = actions('doctor', 'document') + actions('scribe', 'document')
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
            doc_ok = bool(observation.get('transcript')) and observation.get('clinical_addendum') == artifacts.get('clinical_addendum')
            doc_ok = doc_ok and observation.get('input_mode') == 'encounter_transcript_and_physician_addendum'
            doc_ok = doc_ok and addendum_calls[0]['sequence'] < writers[0]['sequence']
    elif writers:
        doc_ok = doc_ok and not brief_calls and not addendum_calls and bool(writers[0]['observation'].get('transcript'))
    if doc['workflow'] != 'manual_documentation' and writers:
        observation = writers[0]['observation']
        doc_ok = doc_ok and observation.get('consultation') is None and observation.get('handoff') is None
    return {
        'runtime_reported_complete': run.get('status') == 'completed' and run.get('workflow_complete') is True
            and run.get('native_complete') is True and run.get('evaluation_unit') == 'complete_encounter',
        'exact_profile_identity': run.get('config_id') == profile['config_id'] and run.get('conditions') == selected,
        'model_routes': bool(calls) and all(c.get('model_name') == (
            models['tool_models'].get(c['model_family']) if c.get('model_family') else models['role_model']) for c in calls),
        'preconsult_condition_observed': pre_ok and not any(c.get('role') == 'nurse' for c in calls),
        'prescription_condition_observed': rx_ok and reviews[-1]['decision'] == 'approve',
        'documentation_condition_observed': doc_ok and not any(
            e.get('event') == 'realtime_note_available_during_consultation' for e in events),
    }


def measurements(run):
    activities = run.get('activities', [])
    calls = completed_calls(run)
    transcript = run.get('artifacts', {}).get('transcript', [])
    artifacts = run.get('artifacts', {})

    def seconds(kinds, role=None):
        return sum(a['active_seconds'] for a in activities
                   if a['activity_type'] in kinds and (role is None or a['role'] == role))

    return {
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
