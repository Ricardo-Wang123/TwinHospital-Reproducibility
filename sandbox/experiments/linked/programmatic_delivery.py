"""Task delivery checks for fully virtual APP II; no human or model grading.

These checks establish artifact/state consistency within their named coverage.
They do not establish the clinical truth of unrestricted narrative claims.
"""
from collections import Counter

from .contracts import digest
from .workflow_analysis import audit_run


VERSION = 'programmatic-task-delivery-v1'
PROTOCOL = 'fully_virtual_workflow_v1'
STAFF_ROLES = {'nurse', 'doctor', 'pharmacist', 'senior_pharmacist'}
COLLECTION_ACTIVITIES = {'information_collection', 'summary_documentation', 'documentation'}
VERIFICATION_ACTIVITIES = {'summary_review', 'handoff_verification', 'handoff_supplementation',
                         'handoff_correction', 'note_review', 'note_rework'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def evaluate(run, task):
    """Return only public-safe checks, never source text or identifiers.

    Fixed task execution must finish; a partly completed linked encounter can
    still supply a finished earlier task. Whole-encounter success is assessed
    separately and always requires medication settlement.
    """
    if task not in {'preconsult', 'documentation'}:
        return {'version': VERSION, 'applicable': False, 'qualifies': None,
                'reason': 'APP_II_is_preconsultation_and_documentation_only'}
    a = run.get('artifacts') or {}
    events = run.get('events') or []
    checks = {}
    if run.get('evaluation_unit') == 'fixed_input_task':
        checks['fixed_task_execution_completed'] = run.get('status') == 'completed'
    coverage = {'unrestricted_clinical_accuracy': 'not_assessed',
                'semantic_redundancy': 'not_assessed'}
    if task == 'preconsult':
        handoff, verified = a.get('final_handoff'), a.get('physician_verified_handoff')
        valid = isinstance(handoff, dict) and _text(handoff.get('text'))
        checks['nonempty_handoff'] = valid
        facts = handoff.get('facts') if valid else None
        checks['structured_handoff_schema'] = isinstance(facts, list) and all(
            isinstance(f, dict) and all(k in f for k in ('fact_id', 'topic', 'value', 'status'))
            and _text(f.get('fact_id')) and _text(f.get('topic')) and _text(f.get('status'))
            for f in facts)
        operations = {}
        for event in events:
            if event.get('event') == 'business_operation':
                operations[event.get('operation')] = event.get('response') or {}
        for name in ('handoff.save', 'handoff.read'):
            r = operations.get(name, {})
            checks[name.replace('.', '_') + '_matches'] = (
                valid and r.get('readback_verified') is True
                and (r.get('readback') or {}).get('content') == handoff
                and (r.get('data') or {}).get('content') == handoff)
        checks['physician_handoff_verification_recorded'] = (
            isinstance(verified, dict) and _text(verified.get('text')) and any(
                e.get('event') == 'preconsult_completed'
                and e.get('final_handoff_hash') == digest(handoff)
                and e.get('physician_verified_hash') == digest(verified) for e in events))
        coverage.update(structured_fact_rows=len(facts) if isinstance(facts, list) else None,
                        source_fact_id_matching='not_used_local_output_ids_are_not_reference_ids',
                        handoff_semantic_accuracy='not_assessed')
    else:
        audit = audit_run(run, {})
        attest = next((e.get('response') or {} for e in reversed(events)
                       if e.get('event') == 'business_operation' and e.get('operation') == 'note.attest'), {})
        checks['note_attestation_readback_verified'] = attest.get('readback_verified') is True and attest.get('native_effect') is True
        for name in ('note_version_chain', 'note_reviews_match_versions',
                     'final_note_review_accepted', 'native_current_note_locked_matches'):
            checks[name] = audit['checks'][name]['value']
        content = (a.get('attested_note') or {}).get('content')
        checks['nonempty_attested_note'] = isinstance(content, dict) and _text(content.get('text'))
        versions = a.get('note_versions') or []
        links, missing, invalid = 0, 0, 0
        for version in versions:
            value = version.get('content') or {}
            refs, permitted = value.get('source_sequences'), version.get('source_sequences')
            if not isinstance(refs, list) or not isinstance(permitted, list):
                missing += 1
                continue
            links += len(refs)
            invalid += sum(type(x) is not int or x not in permitted for x in refs)
        checks['declared_note_source_links_valid'] = bool(versions) and missing == 0 and invalid == 0
        coverage.update(note_versions=len(versions), declared_source_links=links,
                        versions_without_source_link_schema=missing, invalid_source_links=invalid,
                        note_atomic_fact_count=None, note_fact_accuracy='not_assessed',
                        source_link_scope='transcript_reference_integrity_not_fact_support')
    values = list(checks.values())
    qualified = False if any(v is False for v in values) else (True if all(v is True for v in values) else None)
    return {'version': VERSION, 'applicable': True, 'qualifies': qualified,
            'checks': checks, 'coverage': coverage}


def aggregate(runs, task, *, planned, unstarted=0):
    assessments = [evaluate(run, task) for run in runs]
    if task not in {'preconsult', 'documentation'}:
        return {'version': VERSION, 'applicable': False, 'qualified_tasks': None,
                'effort_per_qualified_task_seconds': None}
    counts = Counter(row['qualifies'] for row in assessments)
    missing = planned - len(runs) - unstarted
    if missing < 0:
        raise ValueError('Observed/unstarted units exceed the planned denominator')
    nq = counts[True]
    known = missing == 0 and not counts[None]
    roles = Counter()
    for run in runs:
        for activity in run['activities']:
            if activity.get('role') in STAFF_ROLES:
                roles[activity['role']] += activity['active_seconds']
    # Missing envelopes have unknown work, never silently zero. Explicitly
    # unstarted tasks have zero role activity, while API costs are separate.
    effort_complete = missing == 0
    per = sum(roles.values()) / nq if known and effort_complete and nq else None
    check_counts = {}
    for row in assessments:
        for name, value in row['checks'].items():
            target = check_counts.setdefault(name, {'pass': 0, 'fail': 0, 'unknown': 0})
            target['pass' if value is True else 'fail' if value is False else 'unknown'] += 1
    return {'version': VERSION, 'applicable': True, 'planned_tasks': planned,
            'observed_tasks': len(runs), 'unstarted_tasks': unstarted,
            'qualified_tasks': nq if known else None, 'observed_qualified_tasks': nq,
            'failed_delivery_checks': counts[False], 'unknown_qualification': counts[None] + missing,
            'qualification_denominator_complete': known, 'effort_observation_complete': effort_complete,
            'observed_staff_seconds': sum(roles.values()), 'observed_role_seconds': dict(roles),
            'effort_per_qualified_task_seconds': per,
            'role_effort_per_qualified_task_seconds': {r: v/nq if per is not None else None for r, v in roles.items()},
            'check_counts': check_counts, 'unrestricted_clinical_accuracy': 'not_assessed',
            'zero_denominator': known and nq == 0,
            'scope': 'declared_delivery_checks_all_observed_failed_task_effort_included_no_clinical_accuracy_claim'}


def timing_sensitivity(runs, task, *, planned, unstarted=0):
    """Reweight observed activities, preserving outcomes and failed-case work."""
    base = aggregate(runs, task, planned=planned, unstarted=unstarted)
    if not base['applicable']:
        return {'applicable': False, 'scenarios': []}
    activities = [a for run in runs for a in run['activities'] if a.get('role') in STAFF_ROLES]
    if any(not a.get('activity_type') for a in activities):
        return {'applicable': True, 'status': 'activity_type_missing', 'scenarios': []}
    scenarios = []
    nq = base['qualified_tasks']
    known = base['qualification_denominator_complete'] and base['effort_observation_complete']
    for collection_factor in (.5, 1., 2.):
        for verification_factor in (.5, 1., 2.):
            roles = Counter()
            for activity in activities:
                kind = activity['activity_type']
                factor = (collection_factor if kind in COLLECTION_ACTIVITIES else
                          verification_factor if kind in VERIFICATION_ACTIVITIES else 1.)
                roles[activity['role']] += activity['active_seconds'] * factor
            total = sum(roles.values())
            scenarios.append({'collection_factor': collection_factor, 'verification_factor': verification_factor,
                'qualified_tasks': nq, 'observed_staff_seconds': total,
                'effort_per_qualified_task_minutes': total / (60*nq) if known and nq else None,
                'observed_role_seconds': dict(roles)})
    return {'applicable': True, 'version': 'activity-weight-sensitivity-v1',
        'status': 'calculated' if known and nq else 'undefined_denominator_or_missing_evidence',
        'collection_activities': sorted(COLLECTION_ACTIVITIES),
        'verification_activities': sorted(VERIFICATION_ACTIVITIES),
        'other_activity_factor': 1., 'outcomes_recomputed': False, 'scenarios': scenarios}
