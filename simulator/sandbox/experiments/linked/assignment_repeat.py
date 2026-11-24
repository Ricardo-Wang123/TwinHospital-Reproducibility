"""Freeze and audit an explicitly selected repetition of prior assignments."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import re

from .contracts import linked_config_id
from .linked_acceptance import ASSIGNMENT_SCHEDULE_POLICY


VERSION = 'linked_fixed_assignment_repeat_v1'
PLAN_VERSION = 'twinhospital.same-assignment-rerun/1'


def freeze(design, plan, cases, profiles, *, plan_sha256):
    if plan.get('version') != PLAN_VERSION or not re.fullmatch('[a-f0-9]{64}', plan_sha256):
        raise ValueError('A versioned assignment plan and its file hash are required')
    refs = {row['case_id']: row for row in cases}
    rows = plan.get('assignments')
    if (not isinstance(rows, list) or not rows or len(rows) != plan.get('planned_assignments')
            or plan.get('source_case_count') != len(cases)):
        raise ValueError('Assignment plan counts differ from the selected inputs')
    pairs = set()
    for row in rows:
        pair = (row['case_id'], row['profile'])
        if pair in pairs or pair[0] not in refs or pair[1] not in profiles:
            raise ValueError('Unknown or duplicate assignment')
        if (row.get('group') != refs[pair[0]]['group']
                or row.get('config_id') != linked_config_id(profiles[pair[1]])
                or not re.fullmatch('[a-f0-9]{64}', row.get('source_trace_sha256', ''))):
            raise ValueError('Assignment identity or historical attribution differs')
        pairs.add(pair)
    if {case for case, _ in pairs} != set(refs):
        raise ValueError('Every selected source case must occur in the plan')
    result = deepcopy(design)
    result.pop('target_per_disease_and_condition', None)
    result.update(version=VERSION, schedule_policy=ASSIGNMENT_SCHEDULE_POLICY,
        planned_unique_encounters=len(rows), first_baseline_regressions=[],
        initial_admission_assignments=min(2,len(rows)), max_attempts_per_unit=1,
        source_patients_by_group=dict(Counter(row['group'] for row in cases)),
        assignment_plan_sha256=plan_sha256, assignment_plan=deepcopy(plan),
        selection_scope='exact_frozen_assignments_not_an_additional_complete_factorial')
    return result


def from_file(design, path, cases, profiles):
    raw = path.read_bytes()
    return freeze(design, json.loads(raw), cases, profiles, plan_sha256=sha256(raw).hexdigest())


def units(cases, design):
    refs = {row['case_id']: row for row in cases}
    for row in design['assignment_plan']['assignments']:
        yield refs[row['case_id']], row['profile']


def summarize(design, rows):
    expected = Counter((row['group'], row['profile']) for row in design['assignment_plan']['assignments'])
    observed = Counter((row['group'], row['profile']) for row in rows)
    paid = Counter((row['group'], row['profile']) for row in rows if row.get('verified_paid') is True)
    expected_pairs = {(row['case_id'],row['profile']) for row in design['assignment_plan']['assignments']}
    observed_pairs = [(row.get('case_id'),row['profile']) for row in rows]
    cells = [{'group':group, 'profile':profile, 'planned_patients':count,
              'verified_paid_patients':paid[group,profile]} for (group,profile), count in sorted(expected.items())]
    return {'passed':observed == expected and paid == expected
            and len(observed_pairs) == len(set(observed_pairs)) and set(observed_pairs) == expected_pairs
            and len({row['study_id'] for row in rows}) == len(rows),
        'planned_unique_encounters':sum(expected.values()),
        'verified_unique_encounters':sum(paid.values()), 'cells':[], 'profile_cells':cells,
        'counts_as_formal_experiment':False, 'assignment_plan_sha256':design['assignment_plan_sha256']}
