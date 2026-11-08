"""Engineering coverage of every task condition in a fresh paid encounter."""
from collections import Counter
from itertools import product

from .cases import GROUPS
from .contracts import baseline_conditions, linked_config_id, paper_conditions


BASELINE_LABEL = 'linked_baseline_anchor'
PATIENTS_PER_GROUP = 10
SUPPORTED_PATIENTS_PER_GROUP = (2, PATIENTS_PER_GROUP, 20)
MAX_ENGINEERING_ATTEMPTS = 3
SCHEDULE_POLICY = 'first_two_baseline_regressions_then_remaining_profiles_stop_on_first_failure'
PARALLEL_SCHEDULE_POLICY = 'first_two_baseline_regressions_then_parallel_independent_patients'
ASSIGNMENT_SCHEDULE_POLICY = 'first_two_baseline_regressions_then_parallel_independent_assignments'


def linked_profiles(tool_models, *, factorial=False):
    if set(tool_models) != {'tool_a'} or not tool_models['tool_a']:
        raise ValueError('linked acceptance requires the declared single AI tool')
    baseline = baseline_conditions()
    profiles = {BASELINE_LABEL: baseline}
    cell_profiles = {}
    conditions = paper_conditions(model_families=tool_models)
    for condition in conditions:
        label = BASELINE_LABEL
        if condition != baseline[condition.task]:
            label = 'linked__' + condition.config_id.replace(':', '__')
            profiles[label] = {**baseline, condition.task: condition}
        cell_profiles[condition.config_id] = label
    if factorial:
        factors = [[condition for condition in conditions if condition.task == task] for task in baseline]
        for combination in product(*factors):
            changed = [c for c in combination if c != baseline[c.task]]
            if len(changed) > 1:
                label = 'linked__' + '__'.join(c.config_id.replace(':', '__') for c in changed)
                profiles[label] = {c.task: c for c in combination}
    return profiles, cell_profiles


def freeze_design(cases, tool_models, *, patients_per_group=PATIENTS_PER_GROUP, workers=1,
                  max_attempts=None, factorial=False):
    """Keep the declared distinct source patients in each disease denominator."""
    if patients_per_group not in SUPPORTED_PATIENTS_PER_GROUP:
        raise ValueError('Unsupported linked acceptance patient target')
    if patients_per_group == 2 and not factorial:
        raise ValueError('Two patients per disease is reserved for ten-patient factorial coverage')
    if max_attempts is None:
        max_attempts = MAX_ENGINEERING_ATTEMPTS if patients_per_group == 20 else 1
    if max_attempts not in (1, MAX_ENGINEERING_ATTEMPTS):
        raise ValueError('Unsupported linked acceptance attempt limit')
    counts = Counter(case['group'] for case in cases)
    patients = {case.get('patient_key') for case in cases}
    if (counts != Counter({g: patients_per_group for g in GROUPS})
            or None in patients or '' in patients or len(patients) != len(cases)
            or len({case['case_id'] for case in cases}) != len(cases)):
        raise ValueError(f'linked acceptance requires {patients_per_group} distinct source patients per disease')
    if any(case.get('data_kind') != 'mimic_derived' or case.get('split') != 'development'
           for case in cases):
        raise ValueError('linked acceptance is restricted to the MIMIC-derived engineering cohort')
    profiles, cells = linked_profiles(tool_models, factorial=factorial)
    return {
        'version': 'linked_acceptance_v1',
        'schedule_policy': (ASSIGNMENT_SCHEDULE_POLICY if factorial and workers > len(cases)
                            else PARALLEL_SCHEDULE_POLICY if workers > 1 else SCHEDULE_POLICY),
        'factorial': factorial,
        'first_baseline_regressions': [case['case_id'] for case in cases[:2]],
        'target_per_disease_and_condition': patients_per_group,
        'planned_unique_encounters': len(cases) * len(profiles),
        'max_attempts_per_unit': max_attempts,
        'profiles': [
            {'label': label, 'config_id': linked_config_id(conditions),
             'conditions': {task: condition.to_dict() for task, condition in conditions.items()}}
            for label, conditions in profiles.items()
        ],
        'task_condition_profiles': cells,
        'counts_as_formal_experiment': False,
    }


def ordered_units(cases, profile_labels, *, rotate=False):
    """Manifest starts with a prior success and the first repair witness."""
    initial = [(case, BASELINE_LABEL) for case in cases[:2]]
    priority = {(case['case_id'], label) for case, label in initial}
    yield from initial
    labels = tuple(profile_labels)
    for number, case in enumerate(cases):
        offset = number * len(labels) // len(cases) if rotate else 0
        for label in labels[offset:] + labels[:offset]:
            if (case['case_id'], label) not in priority:
                yield case, label


def attempt_label(profile, number):
    return profile if number == 1 else f'{profile}__retry_{number}'


def documented_unpaid_stop(run):
    artifacts = run.get('artifacts', {})
    events = run.get('events', [])
    if (run.get('native_complete') is not False or 'settlement' in artifacts
            or any(e.get('event') == 'business_request_intent' and str(e.get('operation')).startswith('payment.')
                   or e.get('event') == 'business_request_outcome' and e.get('status') == 'unresolved_exception'
                   for e in events)):
        return False
    if run.get('status') == 'incomplete_clinical_decision':
        return ((artifacts.get('clinical_disposition') or {}).get('drug_settlement_attempted') is False
                and (artifacts.get('disposition_documentation_outcome') or {}).get('status') == 'attested')
    if run.get('status') == 'failed' and run.get('error_type') in {'JSONDecodeError', 'DuplicateJSONKeyError'}:
        if len(events) < 2:
            return False
        invalid, terminal = events[-2:]
        return (invalid.get('event') == 'role_response_json_invalid'
                and invalid.get('correction_scheduled') is False
                and invalid.get('parse_metadata', {}).get('finish_reason') == 'stop'
                and terminal.get('event') == 'run_failed'
                and terminal.get('error_type') == run['error_type'])
    return (run.get('status') == 'failed' and run.get('error_type') == 'WorkflowFailure'
            and run.get('failure_reason') == 'note review limit exhausted without acceptance'
            and bool(artifacts.get('note_versions')) and 'attested_note' not in artifacts
            and (artifacts.get('prescription_business_state') or {}).get('status') == 'signed')


def execution_checks(run, profile, models):
    """Check which workflow actually ran, including live vs final drafting."""
    conditions = profile['conditions']
    calls = [call for call in run.get('calls', []) if call.get('status') == 'completed']
    events = run.get('events', [])
    role_model, tool_models = models['role_model'], models['tool_models']
    pre = conditions['preconsult']
    rx = conditions['prescription']
    doc = conditions['documentation']

    def called(role, phase, family):
        return any(c.get('role') == role and c.get('phase', '').startswith(phase)
                   and c.get('model_family') == family for c in calls)

    pre_events = [e for e in events if e.get('event') == 'preconsult_completed']
    pre_role = 'nurse' if pre['workflow'] == 'nurse_only' else 'preconsult_ai'
    reviews = [e for e in events if e.get('event') == 'prescription_reviewed']
    final_review = reviews[-1] if reviews else {}
    docs = [c for c in calls if c.get('observation', {}).get('action') == 'document']
    doc_role = 'doctor' if doc['workflow'] == 'manual_documentation' else 'scribe'
    consult_done = [e['sequence'] for e in events if e.get('event') == 'consultation_completed']
    live = [e for e in events if e.get('event') == 'realtime_note_available_during_consultation']
    interim = [c for c in docs if c['observation'].get('is_final_prefix') is False]
    final = [c for c in docs if c['observation'].get('is_final_prefix') is True]
    return {
        'runtime_reported_complete': run.get('status') == 'completed'
            and run.get('evaluation_unit') == 'complete_encounter'
            and run.get('workflow_complete') is True and run.get('native_complete') is True,
        'exact_profile_identity': run.get('config_id') == profile['config_id']
            and run.get('conditions') == conditions,
        'model_routes': bool(calls) and all(
            c.get('model_name') == (tool_models.get(c['model_family']) if c.get('model_family')
                                    else role_model) for c in calls),
        'preconsult_condition_observed': len(pre_events) == 1 and pre_events[0].get('condition') == pre
            and called(pre_role, 'preconsult_collection', pre['model_family'])
            and called('nurse', 'nurse_summary_review', None) == (pre['workflow'] == 'ai_nurse_review'),
        'prescription_condition_observed': bool(reviews) and all(e.get('condition') == rx for e in reviews)
            and final_review.get('decision') == 'approve'
            and (final_review.get('ai_review') is not None) == (rx['workflow'] != 'pprs_baseline')
            and called('rx_ai', 'prescription_review_', rx['model_family']) == (rx['workflow'] != 'pprs_baseline')
            and (rx['workflow'] != 'parallel' or final_review.get('pharmacist_review') is not None),
        'documentation_condition_observed': len(final) == 1 and len(docs) == len(interim) + 1
            and all(c.get('role') == doc_role and c.get('model_family') == doc['model_family'] for c in docs)
            and (bool(interim) and bool(consult_done) and bool(live)
                 and any(e['sequence'] < consult_done[-1] for e in live)
                 and all(c['observation'].get('consultation') is None
                         and c['observation'].get('approved_prescription') is None for c in interim)
                 if doc['workflow'] == 'realtime' else not interim and not live),
    }


def summarize_gate(design, rows):
    """Every planned source patient stays in the denominator of every cell."""
    target = design['target_per_disease_and_condition']
    counts = Counter((row['group'], row['profile']) for row in rows if row.get('verified_paid') is True)
    cells = [
        {'group': group, 'condition': condition, 'profile': label,
         'planned_patients': target, 'verified_paid_patients': counts[group, label]}
        for group in GROUPS for condition, label in design['task_condition_profiles'].items()
    ]
    profile_cells = [{'group': group, 'profile': profile['label'],
                      'planned_patients': target, 'verified_paid_patients': counts[group, profile['label']]}
                     for group in GROUPS for profile in design['profiles']]
    result = {
        'passed': (len(rows) == design['planned_unique_encounters']
                   and len({(row['study_id'], row['profile']) for row in rows}) == len(rows)
                   and all(cell['verified_paid_patients'] == target for cell in profile_cells)),
        'planned_unique_encounters': design['planned_unique_encounters'],
        'verified_unique_encounters': sum(row.get('verified_paid') is True for row in rows),
        'cells': cells,
        'counts_as_formal_experiment': False,
    }
    if design.get('factorial'):
        result['profile_cells'] = profile_cells
    return result
