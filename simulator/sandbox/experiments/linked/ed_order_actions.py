"""Apply a clinician's simultaneous order transactions to this encounter's signed lines."""
from .contracts import WorkflowFailure, validate_prescription


ACTION_TYPES = ('new', 'repeat', 'change', 'stop')
INDICATION_POLICY = 'physician_signed_indication_v1'


def validate_withheld_treatments(entries, *, removed_codes=None, previously_withdrawn=()):
    if not isinstance(entries, list):
        raise ValueError('List the treatments you considered and withheld, or return withheld_treatments=[].')
    seen, explained = set(), set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('level') not in ('ingredient', 'class')
                or entry.get('decision') not in ('omit', 'defer', 'replace')
                or any(not isinstance(entry.get(k), str) or not entry[k].strip()
                       for k in ('name', 'reason', 'evidence'))):
            raise ValueError('Each withheld treatment needs a level, name, decision, reason and supporting chart finding or information gap.')
        key = (entry['level'], entry['name'].strip().casefold())
        if key in seen:
            raise ValueError('Give each considered treatment one disposition at this decision.')
        seen.add(key)
        codes = entry.get('prior_codes', [])
        if (not isinstance(codes, list) or any(not isinstance(code, str) or not code.strip() for code in codes)
                or len(set(codes)) != len(codes) or explained.intersection(codes)):
            raise ValueError('List each omitted or replaced prior product code once in prior_codes.')
        explained.update(codes)
    if removed_codes is not None:
        missing = removed_codes - explained
        if missing:
            raise ValueError('In withheld_treatments explain each prior product absent from your new regimen. '
                             'Use prior_codes for these products: ' + ', '.join(sorted(missing)) + '.')
        if explained - removed_codes - set(previously_withdrawn):
            raise ValueError('Use prior_codes for products removed here or previously withdrawn from this encounter.')


def indication_scope(item):
    indication = item.get('indication') if isinstance(item, dict) else None
    if (not isinstance(indication, dict) or indication.get('scope') not in ('primary', 'other')
            or not isinstance(indication.get('problem'), str) or not indication['problem'].strip()):
        raise ValueError("Each order item needs indication.scope ('primary' or 'other') and a named indication.problem.")
    if 'rationale' in indication and (not isinstance(indication['rationale'], str) or not indication['rationale'].strip()):
        raise ValueError('An order indication rationale must contain a clinical explanation.')
    return indication['scope']


def apply_actions(active, actions):
    if not isinstance(actions, list):
        raise ValueError('Return an explicit order_actions list for this decision.')
    prior = {line['request_id']: line['item'] for line in active}
    if len(prior) != len(active):
        raise ValueError('Duplicate active order identifier.')
    retired, issued = set(), []
    for action in actions:
        kind = action.get('kind') if isinstance(action, dict) else None
        if kind not in ACTION_TYPES:
            raise ValueError('Each order action must be new, repeat, change or stop.')
        fields = {'kind'}
        if kind in ('stop', 'change'):
            fields.add('request_id')
            rid = action.get('request_id')
            if not isinstance(rid, str) or rid not in prior or rid in retired:
                raise ValueError('Select one distinct active request_id from your signed order for each change or stop.')
            retired.add(rid)
        if kind != 'stop':
            fields.add('item')
            item = action.get('item')
            try:
                validate_prescription({'items': [item]})
            except WorkflowFailure as exc:
                raise ValueError(str(exc)) from exc
            issued.append(item)
        if set(action) != fields:
            raise ValueError(f'Expected fields for {kind}: {", ".join(sorted(fields))}.')
    retained = [rid for rid in prior if rid not in retired]
    return retained, issued


def physician_order(response, active, *, reassessment=False):
    """Materialize only transactions explicitly selected by the treating physician."""
    decision = response.get('decision')
    if decision == 'none':
        if response.get('order_actions') not in (None, []):
            raise ValueError('A no-action decision cannot include order transactions.')
        response = {**response, 'decision': 'revise', 'order_actions': []}
        decision = 'revise'
    if decision not in ('keep', 'revise', 'stop_all'):
        return response
    actions = response.get('order_actions')
    if decision == 'keep':
        if actions not in (None, []):
            raise ValueError('Use revise to submit order actions, or keep with no actions.')
        actions = []
    elif decision == 'stop_all':
        if actions is None:
            actions = [{'kind': 'stop', 'request_id': line['request_id']} for line in active]
        if not actions or any(not isinstance(a, dict) or a.get('kind') != 'stop' for a in actions):
            raise ValueError('stop_all requires withdrawal of all active orders.')
    retained, issued = apply_actions(active, actions)
    if decision == 'stop_all' and retained:
        raise ValueError('stop_all leaves an active order. Use revise for a partial withdrawal.')
    by_id = {line['request_id']: line['item'] for line in active}
    prescription = {'items': [by_id[rid] for rid in retained] + issued}
    if 'prescription' in response and response['prescription'] != prescription:
        supplied = response['prescription']
        items = supplied.get('items') if isinstance(supplied, dict) and set(supplied) == {'items'} else None
        if (not isinstance(items, list) or len(items) != len(prescription['items'])
                or any(not isinstance(item, dict) or
                       {k: v for k, v in item.items() if k != 'reason'} !=
                       {k: v for k, v in expected.items() if k != 'reason'}
                       or not isinstance(item.get('reason', ''), str)
                       or not isinstance(expected.get('reason', ''), str)
                       or ('reason' in item and 'reason' in expected and item['reason'] != expected['reason'])
                       for item, expected in zip(items, prescription['items']))):
            raise ValueError('The supplied prescription disagrees with the specified order actions.')
    if reassessment and decision == 'revise':
        if not actions:
            decision = 'keep'
        elif not prescription['items']:
            decision = 'stop_all'
    return {**response, 'decision': decision, 'order_actions': actions, 'prescription': prescription}


def signed_actions(actions, new_ids):
    fresh = iter(new_ids)
    result = []
    for action in actions:
        kind = action['kind']
        row = {'kind': kind}
        if kind in ('change', 'stop'):
            row['prior_id'] = action['request_id']
        if kind != 'stop':
            row['issued_id'] = next(fresh)
        result.append(row)
    if next(fresh, None) is not None:
        raise ValueError('Unassigned new ED order identifier.')
    return result


def read_actions(previous, current, *, by_indication=False):
    """Verify the locked transaction against the preceding and resulting native order lines."""
    before = {line['request_id']: line['item'] for line in (previous or {}).get('active', [])}
    after = {line['request_id']: line['item'] for line in current['active']}
    removed, created, rows = set(), set(), []
    for action in current['order_actions']:
        kind = action['kind']
        if kind not in ACTION_TYPES:
            raise ValueError('Unrecognized signed ED transaction')
        row = {'kind': kind}
        if kind in ('stop', 'change'):
            rid = action['prior_id']
            if rid not in before or rid in removed or rid in after:
                raise ValueError('Withdrawn line differs from the preceding signed order')
            removed.add(rid)
            row.update(prior_id=rid, prior_code=before[rid]['drug_code'])
            if by_indication:
                row['prior_scope'] = indication_scope(before[rid])
        if kind != 'stop':
            rid = action['issued_id']
            if rid not in after or rid in created or rid in before:
                raise ValueError('Issued line has no distinct signed order identifier')
            created.add(rid)
            row.update(issued_id=rid, drug_code=after[rid]['drug_code'])
            if by_indication:
                row['issued_scope'] = indication_scope(after[rid])
        rows.append(row)
    if (created != after.keys() - before.keys() or removed != before.keys() - after.keys()
            or any(before[rid] != after[rid] for rid in before.keys() & after.keys())
            or {x['request_id']: x['item'] for x in current['issued']} != {i: after[i] for i in created}
            or {x['request_id']: x['item'] for x in current['withdrawn']} != {i: before[i] for i in removed}):
        raise ValueError('Signed actions do not account for the native order-line changes')
    return rows


def confirmed_checkpoint_transactions(states):
    """Score the approved state change across all signed versions at a single checkpoint."""
    transactions = [row for state in states for row in state['transactions']]
    if len(states) == 1:
        return transactions
    issued = {row['issued_id']: row for row in transactions if 'issued_id' in row}
    retired = {row['prior_id']: row for row in transactions if 'prior_id' in row}
    selected = []
    replacement = set()
    for rid in issued.keys() - retired.keys():
        new = issued[rid]
        old = new
        while old.get('prior_id') in issued:
            old = issued[old['prior_id']]
        if 'prior_id' in old:
            replacement.add(old['prior_id'])
            selected.append({**new, 'kind': 'change',
                **{key: val for key, val in old.items() if key.startswith('prior_')}})
        else:
            selected.append({key: val for key, val in new.items() if not key.startswith('prior_')} | {'kind': 'new'})
    for rid in retired.keys() - issued.keys() - replacement:
        selected.append({key: val for key, val in retired[rid].items() if key.startswith('prior_')} | {'kind': 'stop'})
    return sorted(selected, key=lambda row: (row['kind'], row.get('prior_id', ''), row.get('issued_id', '')))
