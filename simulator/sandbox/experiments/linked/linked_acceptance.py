"""Engineering coverage of every task condition in a fresh paid encounter."""
from collections import Counter
from itertools import chain, product

from .cases import GROUPS
from .contracts import linked_config_id
from . import scenario_design as scenarios


BASELINE_LABEL = 'linked_baseline_anchor'
PATIENTS_PER_GROUP = 10
SUPPORTED_PATIENTS_PER_GROUP = (2, PATIENTS_PER_GROUP, 20)
MAX_ENGINEERING_ATTEMPTS = 3
SCHEDULE_POLICY = 'first_two_baseline_regressions_then_remaining_profiles_stop_on_first_failure'
PARALLEL_SCHEDULE_POLICY = 'first_two_baseline_regressions_then_parallel_independent_patients'
ASSIGNMENT_SCHEDULE_POLICY = 'first_two_baseline_regressions_then_parallel_independent_assignments'
HELDOUT_DISPATCH_ORDER = 'round_robin_disease_then_rotating_configuration_v1'
READER_DESIGN_VERSION = 'linked_reader_comparison_v1'


def linked_profiles(tool_models, *, factorial=False, scenario_design=scenarios.VERSION):
    if set(tool_models) != {'tool_a'} or not tool_models['tool_a']:
        raise ValueError('linked acceptance requires the declared single AI tool')
    if scenario_design not in scenarios.DESIGNS:
        raise ValueError('Unsupported scenario design')
    baseline = scenarios.baselines()
    profiles = {BASELINE_LABEL: baseline}
    cell_profiles = {}
    conditions = scenarios.conditions(tool_models)
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
                  max_attempts=None, factorial=False, independent=False, reader=False, quick=False,
                  scenario_design=scenarios.VERSION):
    """Keep the declared distinct source patients in each disease denominator."""
    if quick and not reader:
        raise ValueError('A quick comparison requires the reader design')
    if reader and (independent or not factorial or max_attempts != 1):
        raise ValueError('A reader comparison requires fresh crossed assignments and one attempt per assignment')
    if independent and (not factorial or patients_per_group < 1 or max_attempts != 1):
        raise ValueError('An independent crossed batch requires every configuration and one attempt per assignment')
    if not (independent or reader) and patients_per_group not in SUPPORTED_PATIENTS_PER_GROUP:
        raise ValueError('Unsupported linked acceptance patient target')
    if patients_per_group == 2 and not factorial:
        raise ValueError('Two patients per disease is reserved for ten-patient factorial coverage')
    if max_attempts is None:
        max_attempts = MAX_ENGINEERING_ATTEMPTS if patients_per_group == 20 else 1
    if max_attempts not in (1, MAX_ENGINEERING_ATTEMPTS):
        raise ValueError('Unsupported linked acceptance attempt limit')
    counts = Counter(case['group'] for case in cases)
    patients = {case.get('patient_key') for case in cases}
    if (not counts or set(counts) - set(GROUPS)
            or None in patients or '' in patients or len(patients) != len(cases)
            or len({case['case_id'] for case in cases}) != len(cases)):
        raise ValueError('The design requires distinct cases and source patients in declared groups')
    if not reader and counts != Counter({g: patients_per_group for g in GROUPS}):
        raise ValueError(f'linked acceptance requires {patients_per_group} distinct source patients per disease')
    split = 'test' if independent else 'development'
    if not reader and any(case.get('data_kind') != 'mimic_derived' or case.get('split') != split
                          for case in cases):
        raise ValueError(f'This crossed design requires MIMIC-derived cases in the {split} partition')
    profiles, cells = linked_profiles(tool_models, factorial=factorial, scenario_design=scenario_design)
    if quick:
        quick_flows = set(zip(*scenarios.WORKFLOWS.values()))
        profiles = {label: conditions for label, conditions in profiles.items()
                    if tuple(conditions[task].workflow for task in
                             ('preconsult', 'prescription', 'documentation')) in quick_flows}
        if len(profiles) != len(quick_flows):
            raise ValueError('The selected conditions do not cover the declared quick design')
        cells = {}
    return {
        'version': READER_DESIGN_VERSION if reader else 'linked_acceptance_v1',
        'scenario_design': scenario_design,
        'schedule_policy': (ASSIGNMENT_SCHEDULE_POLICY if factorial and workers > len(cases)
                            else PARALLEL_SCHEDULE_POLICY if workers > 1 else SCHEDULE_POLICY),
        'factorial': factorial,
        'first_baseline_regressions': [] if independent or reader else [case['case_id'] for case in cases[:2]],
        **({'independent_crossed': True, 'evaluation_partition': split} if independent else {}),
        **({'planned_patients_by_group': dict(counts), 'quick': quick} if reader
           else {'target_per_disease_and_condition': patients_per_group}),
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


def heldout_dispatch_order(cases, profile_labels):
    """Interleave strata and rotate the first profile while preserving each patient's full crossing."""
    strata = {group: [] for group in GROUPS}
    for case in cases:
        strata[case['group']].append(case['case_id'])
    patients = chain.from_iterable(zip(*strata.values(), strict=True))
    labels = tuple(profile_labels)
    planned = []
    for index, case_id in enumerate(patients):
        offset = index % len(labels)
        planned.append((case_id, labels[offset:] + labels[:offset]))
    return planned


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
    if run.get('scenario_design') not in scenarios.DESIGNS:
        raise ValueError('The encounter belongs to a different workflow design.')
    return scenarios.execution_checks(run, profile, models)


def summarize_gate(design, rows):
    """Every planned source patient stays in the denominator of every cell."""
    if design.get('version') == 'linked_fixed_assignment_repeat_v1':
        from .assignment_repeat import summarize
        return summarize(design, rows)
    targets = (design['planned_patients_by_group'] if design['version'] == READER_DESIGN_VERSION
               else {group: design['target_per_disease_and_condition'] for group in GROUPS})
    counts = Counter((row['group'], row['profile']) for row in rows if row.get('verified_paid') is True)
    cells = [
        {'group': group, 'condition': condition, 'profile': label,
         'planned_patients': target, 'verified_paid_patients': counts[group, label]}
        for group, target in targets.items() for condition, label in design['task_condition_profiles'].items()
    ]
    profile_cells = [{'group': group, 'profile': profile['label'],
                      'planned_patients': target, 'verified_paid_patients': counts[group, profile['label']]}
                     for group, target in targets.items() for profile in design['profiles']]
    result = {
        'passed': (len(rows) == design['planned_unique_encounters']
                   and len({(row['study_id'], row['profile']) for row in rows}) == len(rows)
                   and all(cell['verified_paid_patients'] == cell['planned_patients'] for cell in profile_cells)),
        'planned_unique_encounters': design['planned_unique_encounters'],
        'verified_unique_encounters': sum(row.get('verified_paid') is True for row in rows),
        'cells': cells,
        'counts_as_formal_experiment': False,
    }
    if design.get('factorial'):
        result['profile_cells'] = profile_cells
    return result
