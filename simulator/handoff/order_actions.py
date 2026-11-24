"""Compare source and signed ED order transactions after encounter closure."""
from collections import Counter, defaultdict
from datetime import datetime


def source_steps(timeline, *, named_predecessors=False):
    seen, origins, result = set(), {}, []
    births = Counter()
    for point in timeline:
        when = point['offset_seconds']
        step, issued = [], {}
        for event in point['actions']:
            drug, rid = event['ingredient'], event['order_id']
            kind = {'New': 'repeat' if drug in seen else 'new', 'Change': 'change', 'D/C': 'stop'}[event['transaction']]
            parents = event['supersedes']
            origin = origins.get((parents[0], drug)) if len(parents) == 1 else None
            prior_times = event.get('prior_offsets_seconds')
            if named_predecessors and kind in ('change', 'stop'):
                origin = tuple(sorted((parent['offset_seconds'], tuple(parent['ingredients']))
                                      for parent in event['prior_orders']))
            elif prior_times is not None:
                origin = tuple(prior_times)
            else:
                origin = (origin,) if origin is not None and births[origin, drug] == 1 else ()
            step.append((kind, drug, origin, rid))
            if kind != 'stop':
                issued[rid, drug] = when
        origins.update(issued)
        births.update((when, drug) for _, drug in issued)
        seen.update(drug for _, drug in issued)
        result.append(step)
    return result


def index_predecessors(reference, headers, *, predecessor_ingredients=None):
    """Keep the source link and its time entirely on the evaluator side."""
    start = datetime.fromisoformat(reference['ed_start'])
    events = {row['poe_id']:row for row in headers if row['subject_id'] == reference['source_subject_id']
              and row['hadm_id'] == reference['source_hadm_id'] and row['order_type'] == 'Medications'
              and reference['ed_start'] <= row['ordertime'] < reference['ed_end']}
    offsets = {p['offset_seconds'] for p in reference['order_timeline']}
    for point in reference['order_timeline']:
        if point['not_assessable']:
            raise ValueError('The reference primary state is incomplete for a scheduled decision')
        for action in point['actions']:
            if action['transaction'] not in ('Change', 'D/C'):
                continue
            parents = action['supersedes']
            order = events[action['order_id']]
            if not parents or any(order.get('discontinue_of_poe_id') != rid and
                                  events[rid].get('discontinued_by_poe_id') != action['order_id'] for rid in parents):
                raise ValueError('No documented source predecessor for the order')
            times = sorted(int((datetime.fromisoformat(events[rid]['ordertime']) - start).total_seconds()) for rid in parents)
            if any(t not in offsets or t >= point['offset_seconds'] for t in times):
                raise ValueError('Predecessor outside the comparable decision clock')
            action['prior_offsets_seconds'] = times
            if predecessor_ingredients is not None:
                prior_orders = []
                for rid in parents:
                    ingredients = sorted(predecessor_ingredients.get(rid, ()))
                    if not ingredients:
                        raise ValueError('Source predecessor lacks a recorded medication identity')
                    seconds = int((datetime.fromisoformat(events[rid]['ordertime']) - start).total_seconds())
                    prior_orders.append({'offset_seconds': seconds, 'ingredients': ingredients})
                action['prior_orders'] = prior_orders
    reference['order_task_protocol'] = 'independent_order_actions_v1'
    if predecessor_ingredients is not None:
        reference['predecessor_comparison'] = 'ingredient_and_checkpoint'


def primary_transactions(actions):
    """Project a signed change onto the principal problem using each line's recorded purpose."""
    for action in actions:
        old = 'prior_id' in action and action['prior_scope'] == 'primary'
        new = 'issued_id' in action and action['issued_scope'] == 'primary'
        if not (old or new):
            continue
        if action['kind'] != 'change' or (old and new):
            yield action
        elif old:
            yield {'kind': 'stop', 'prior_id': action['prior_id'], 'prior_code': action['prior_code']}
        else:
            yield {'kind': 'new', 'issued_id': action['issued_id'], 'drug_code': action['drug_code']}


def scored_steps(states, expected, index, *, named_predecessors=False):
    """Use signed indications when expected is None. Older records used a reference-ingredient filter."""
    seen, origins, result = set(), {}, []
    for state in states:
        when = state['offset_seconds']
        step, issued, new_ingredients = [], [], set()
        actions = state['transactions']
        if expected is None:
            actions = primary_transactions(actions)
        for action in actions:
            prior = index.by_code[action['prior_code']] if 'prior_code' in action else set()
            dest = index.by_code[action['drug_code']] if 'drug_code' in action else set()
            kind = action['kind']
            affected = prior if kind == 'stop' else dest
            if expected is not None:
                affected = affected & expected
            for drug in affected:
                label = ('repeat' if drug in seen else 'new') if kind in ('new', 'repeat') else kind
                at = (origins[action['prior_id']],) if 'prior_id' in action else ()
                if at and named_predecessors:
                    at = ((at[0], tuple(sorted(prior))),)
                step.append((label, drug, at, action.get('issued_id') or action['prior_id']))
            if 'issued_id' in action:
                issued.append(action['issued_id'])
            new_ingredients.update(dest)
        origins.update((rid, when) for rid in issued)
        seen.update(new_ingredients)
        result.append(step)
    return result


def compare_step(source, observed):
    def group_lines(rows):
        grouped = defaultdict(list)
        for kind, drug, at, rid in rows:
            grouped[rid].append((kind, drug, at))
        lines, targets = Counter(), Counter()
        for ingredients in grouped.values():
            signature = tuple(sorted((k, d) for k, d, _ in ingredients))
            lines[signature] += 1
            parents = [(d, at) for k, d, at in ingredients if k in ('change', 'stop')]
            if parents:
                if not all(at for _, at in parents):
                    raise ValueError('Missing predecessor decision time for order-action comparison')
                targets[signature, tuple(sorted(parents))] += 1
        return lines, targets
    expected, known = group_lines(source)
    actual, actual_targets = group_lines(observed)
    def components(signature):
        return [{'kind': k, 'ingredient': d} for k, d in signature]
    def entries(values):
        return [{'components': components(signature), 'count': n} for signature, n in sorted(values.items())]
    source_types = {r[0] for r in source}
    physician_types = {r[0] for r in observed}
    def missed_target(signature, priors, count):
        offsets, ingredients = {}, {}
        for drug, targets in priors:
            if targets and isinstance(targets[0], tuple):
                offsets[drug] = [at for at, _ in targets]
                ingredients[drug] = [list(names) for _, names in targets]
            else:
                offsets[drug] = list(targets)
        item = {'components': components(signature), 'prior_offsets': offsets, 'count': count}
        if ingredients:
            item['prior_ingredients'] = ingredients
        return item

    return {'reference_none': not source_types, 'physician_none': not physician_types,
            'matched': entries(expected & actual), 'missing': entries(expected - actual),
            'additional': entries(actual - expected), 'content_exact': actual == expected,
            'exact': actual == expected and known == actual_targets,
            'by_action': {kind: {'reference': kind in source_types, 'physician': kind in physician_types,
                                 'agreement': (kind in source_types) == (kind in physician_types)}
                          for kind in ('new', 'repeat', 'change', 'stop')},
            'targets_evaluable': known.total(), 'targets_matched': (known & actual_targets).total(),
            'missed_targets': [missed_target(sig, priors, n)
                               for (sig, priors), n in sorted((known - actual_targets).items())]}
