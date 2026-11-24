"""Matched workload estimates for completed outpatient encounters."""
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from statistics import fmean

from sandbox.experiments.linked import scenario_design
from sandbox.experiments.linked.cases import GROUPS


def heldout_effort_plan():
    sources = (Path(__file__), Path(scenario_design.__file__),
               Path(__file__).resolve().parents[1] / 'handoff/reader_report.py')
    return {
        'version': 'outpatient-role-effort-v3',
        'contrasts': {task: {'baseline': flows[0], 'assisted': list(flows[1:])}
                      for task, flows in scenario_design.WORKFLOWS.items()},
        'population': 'matched_verified_paid_encounters',
        'bootstrap': {'seed': 20261107, 'repeats': 10000, 'alpha': .05, 'stratify_by_disease': True},
        'resampling_unit': 'contributing_source_history_within_disease',
        'minimum_histories_per_represented_stratum': 2,
        'analysis_sha256': {source.name: sha256(source.read_bytes()).hexdigest() for source in sources},
    }


def read(path):
    return json.loads(path.read_bytes())


def matched_effort_interval(history_means, group_by_history, plan):
    strata = {group: [] for group in GROUPS}
    for case_id in sorted(history_means):
        strata[group_by_history[case_id]].append(history_means[case_id])
    represented = [values for values in strata.values() if values]
    result = {'ci': None, 'valid_resamples': 0, 'undefined_resamples': 0,
              'stratum_patient_counts': {group: len(values) for group, values in strata.items()}}
    if len(history_means) < 2:
        return {**result, 'status': 'fewer_than_two_source_patients'}
    if any(len(values) < plan['minimum_histories_per_represented_stratum'] for values in represented):
        return {**result, 'status': 'insufficient_within_stratum_replication'}

    settings = plan['bootstrap']
    rng = Random(settings['seed'])
    draws = []
    for _ in range(settings['repeats']):
        sample = (value for stratum in represented for value in rng.choices(stratum, k=len(stratum)))
        draws.append(fmean(sample))
    alpha = settings['alpha']
    draws.sort()
    def quantile(probability):
        position = (len(draws) - 1) * probability
        low = int(position)
        high = min(low + 1, len(draws) - 1)
        return draws[low] + (draws[high] - draws[low]) * (position - low)

    return {**result, 'ci': [quantile(alpha / 2), quantile(1 - alpha / 2)],
            'status': 'estimated', 'valid_resamples': len(draws)}


def matched_stage_effort(runs, task, baseline, *, analysis_plan=None):
    """Match delivery-qualified traces by history and the other two configurations."""
    blocks = defaultdict(lambda: defaultdict(list))
    variants = set()
    for run in runs:
        conditions = run['conditions']
        workflow = conditions[task]['workflow']
        variants.add(workflow)
        stage = run['stages'][task]
        if stage['qualifies'] is not True:
            continue
        other = tuple((name, conditions[name]['workflow']) for name in sorted(conditions) if name != task)
        staff_seconds = sum(seconds for _, _, seconds in stage['activities'])
        blocks[run['case_id'], other][workflow].append(staff_seconds)

    assisted_workflows = sorted(variants - {baseline})
    if analysis_plan is not None:
        contrast = analysis_plan['contrasts'][task]
        if contrast['baseline'] != baseline or variants - {baseline, *contrast['assisted']}:
            raise ValueError('Observed workflows differ from the prespecified effort contrasts.')
        assisted_workflows = contrast['assisted']
        groups = {run['case_id']: run['group'] for run in runs}

    result = {}
    for assisted in assisted_workflows:
        by_history = defaultdict(list)
        for (case_id, _), observed in blocks.items():
            if baseline in observed and assisted in observed:
                delta = fmean(observed[assisted]) - fmean(observed[baseline])
                by_history[case_id].append(delta / 60)
        means_by_history = {case_id: fmean(deltas) for case_id, deltas in by_history.items()}
        history_means = means_by_history.values()
        result[assisted] = {
            'matched_blocks': sum(map(len, by_history.values())),
            'source_histories': len(by_history),
            'mean_difference_minutes': fmean(history_means) if history_means else None,
            'min_history_difference_minutes': min(history_means, default=None),
            'max_history_difference_minutes': max(history_means, default=None),
        }
        if analysis_plan is not None:
            result[assisted]['bootstrap'] = matched_effort_interval(means_by_history, groups, analysis_plan)
    return result
