"""Original-prescription results within the declared executable rule scope."""
from collections import Counter, defaultdict
import random

from .analysis_counts import percentile
from .clinical_faults import evaluate_rule
from .contracts import digest
from .documented_pprs import adapt_visible_inputs


VERSION = 'original-prescription-rule-conformance-v2'
RESOLVED = {'approve', 'reject'}
CATALOGUE_ROUTE_RULE = 'exact_catalogue_product_route'
HIGH_RISK_RULE_SOURCES = {
    'amoxicillin_confirmed_serious_allergy':
        'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=4fb7837e-964c-4d38-a3a5-b384b1bd6e08',
    'lisinopril_sacubitril_concomitant':
        'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=000dc81d-ab91-450c-8eae-8eb74e72296f',
}


def catalogue_route_reference(items, formulary):
    """Compare each named product with the explicit routes in its frozen catalogue."""
    products = {product['drug_code']: product for product in formulary}
    if len(products) != len(formulary):
        raise ValueError('Route evidence requires distinct formulary product identities')
    unknown = False
    for item in items:
        product = products.get(item['drug_code']) or {}
        routes, route = product.get('allowed_routes'), item.get('route')
        if not isinstance(routes, list) or not routes or not all(isinstance(r, str) and r for r in routes) or not route:
            unknown = True
        elif route not in routes:
            return 'violation'
    return 'indeterminate' if unknown else 'scoped_nonviolation'


def reference_from_stage(stage, rule_ids, formulary):
    if not stage or not (stage.get('prescription') or {}).get('items'):
        return {'status': 'source_unavailable', 'rules': {}, 'high_risk_predicate_confirmed': False}
    adapted = adapt_visible_inputs(stage['prescription'], stage['clinical_context'])
    findings = {CATALOGUE_ROUTE_RULE: catalogue_route_reference(stage['prescription']['items'], formulary)}
    for rule_id in rule_ids:
        try:
            result = evaluate_rule(rule_id, adapted['prescription'], adapted['clinical_context'])
            findings[rule_id] = result['status']
        except ValueError:
            findings[rule_id] = 'indeterminate'
    statuses = set(findings.values())
    status = next((s for s in ('violation', 'indeterminate', 'scoped_nonviolation') if s in statuses),
                  'outside_scope')
    return {'status': status, 'rules': findings, 'high_risk_predicate_confirmed': any(
        findings.get(rule_id) == 'violation' for rule_id in HIGH_RISK_RULE_SOURCES)}


def original_disposition(run, stage):
    """Verify the first review against the shared input and journal."""
    if not stage or run.get('stage_input_hash') != digest(stage) or run['stage_inputs']['prescription'] != stage:
        raise ValueError('prescription run is not bound to the frozen original stage')
    artifacts = run['artifacts']
    review = artifacts.get('original_prescription_review')
    if not review:
        return 'unavailable'
    original = stage['prescription']
    events = [e for e in run['events'] if e.get('event') == 'prescription_reviewed' and e.get('revision') == 0]
    if (artifacts.get('original_prescription') != original or review.get('prescription_hash') != digest(original)
            or review.get('condition') != run['conditions']['prescription'] or review.get('revision') != 0
            or len(events) != 1 or any(events[0].get(k) != value for k, value in review.items())):
        raise ValueError('first review disagrees with the original prescription or journal')
    decision = review.get('decision')
    if decision not in RESOLVED | {'defer'}:
        raise ValueError('invalid original prescription disposition')
    return decision


def measures(counts):
    def rate(num, den):
        return {'estimate': num/den if den else None, 'numerator': num, 'denominator': den,
                'status': 'estimated' if den else 'undefined_zero_denominator'}

    decisions, classified, high_risk = Counter(), Counter(), Counter()
    for (decision, reference, confirmed_high_risk), n in counts.items():
        decisions[decision] += n
        classified[decision, reference] += n
        if confirmed_high_risk:
            high_risk[decision] += n
    approvals, rejections = decisions['approve'], decisions['reject']
    tp, fp = classified['reject', 'violation'], classified['reject', 'scoped_nonviolation']
    fn, tn = classified['approve', 'violation'], classified['approve', 'scoped_nonviolation']
    return {
        'pass_rate': rate(approvals, approvals+rejections),
        'severe_miss_rate': rate(high_risk['approve'], high_risk['approve']+high_risk['reject']),
        'fpr': rate(fp, fp+tn), 'recall': rate(tp, tp+fn), 'precision': rate(tp, tp+fp),
        'f1': rate(2*tp, 2*tp+fp+fn) if tp+fp and tp+fn else {
            'estimate': None, 'numerator': None, 'denominator': None, 'status': 'undefined_precision_or_recall'},
    }


def paired_scope_statistics(units, outcomes, contrasts, bootstrap):
    """Count observed rule scope, using identical patient draws across conditions."""
    strata = defaultdict(list)
    for unit in sorted(units, key=lambda u: (u['group'], u['patient_key'])):
        strata[unit['group']].append(unit['unit_id'])
    ids = {u['unit_id'] for u in units}
    if len(ids) != len(units) or len({u['patient_key'] for u in units}) != len(units):
        raise ValueError('prescription analysis requires one planned unit per source patient')
    if any(set(rows) != ids for rows in outcomes.values()):
        raise ValueError('retain every planned prescription assignment')
    full = {cid: Counter(rows.values()) for cid, rows in outcomes.items()}
    values = {cid: measures(counts) for cid, counts in full.items()}
    differences = []
    for contrast in contrasts:
        a, b = contrast['a'], contrast['b']
        result = {}
        for metric in values[a]:
            left, right = values[a][metric]['estimate'], values[b][metric]['estimate']
            result[metric] = {'estimate': left-right if left is not None and right is not None else None}
        differences.append({**contrast, 'direction': 'a_minus_b', 'metrics': result})

    points = {(cid, metric): value for cid, metrics in values.items() for metric, value in metrics.items()}
    points.update({(i, metric): value for i, contrast in enumerate(differences)
                   for metric, value in contrast['metrics'].items()})
    draws = {key: [] for key, value in points.items() if value['estimate'] is not None}
    can_resample = all(len(s) >= 2 for s in strata.values())
    if draws and can_resample:
        rng = random.Random(bootstrap['seed'])
        for _ in range(bootstrap['repeats']):
            sample = [uid for group in strata.values() for uid in rng.choices(group, k=len(group))]
            current = {cid: measures(Counter(rows[uid] for uid in sample)) for cid, rows in outcomes.items()}
            for (key, metric), values_for_draws in draws.items():
                if type(key) is int:
                    a, b = differences[key]['a'], differences[key]['b']
                    left, right = current[a][metric]['estimate'], current[b][metric]['estimate']
                    estimate = left-right if left is not None and right is not None else None
                else:
                    estimate = current[key][metric]['estimate']
                values_for_draws.append(estimate)
    for key, point in points.items():
        observed = draws.get(key, [])
        valid = [v for v in observed if v is not None]
        undefined = len(observed) - len(valid)
        alpha = bootstrap['alpha']
        interval = [percentile(valid, alpha/2), percentile(valid, 1-alpha/2)] if valid and not undefined else None
        status = ('estimated' if interval else 'undefined_in_resamples' if observed else
                  'point_estimate_unavailable' if point['estimate'] is None else 'insufficient_within_stratum_replication')
        point['bootstrap'] = {'ci': interval, 'valid_resamples': len(valid), 'undefined_resamples': undefined,
                              'status': status}
    return {'planned_source_patients': len(ids), 'configurations': {
        cid: {'metrics': values[cid], 'outcome_counts': [
            {'original_decision': decision, 'reference_status': reference,
             'high_risk_predicate_confirmed': high_risk, 'count': count}
            for (decision, reference, high_risk), count in sorted(counts.items())]}
        for cid, counts in full.items()}, 'contrasts': differences}
