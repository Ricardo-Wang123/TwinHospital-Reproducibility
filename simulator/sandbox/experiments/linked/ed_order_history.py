"""Signed ED prescribing decisions on the isolated OpenHIS instance."""
from copy import deepcopy
import json

from .ed_order_actions import apply_actions, signed_actions, validate_withheld_treatments
from .ed_treatment_review import SINGLE_PRESCRIPTION, validate_omitted_products
from .native_his import NativeBlocked, NativeError, ROLE_SETTINGS, VALID_SIGNED_REQUEST_STATES

ADVICE = '/doctorStationAdvice/'
PRODUCT = 'med_medication_definition'
POST_SIGNATURE_REVIEW = 'post_signature_review_v1'


def native_rows(port, pair):
    rows = port._client('doctor').post(ADVICE + 'listDoctorStationAdvice', pair)['content']
    return {str(r['requestId']): r for r in rows if r.get('productTableName') == PRODUCT
            and str(r['patientId']) == pair['patientId'] and str(r['encounterId']) == pair['encounterId']}


def prepare(port, payload, ctx, binding, pair):
    order_id = payload['order_id']
    previous = binding.get('signed_ed_order')
    if payload.get('parent_order_id') != previous or order_id in binding['orders']:
        raise NativeBlocked('Stale ED order revision')
    if any(o.get('course') and o['status'] != 'signed' for o in binding['orders'].values()):
        raise NativeBlocked('A prior ED revision awaits reconciliation')
    items = payload['items']
    if not items and not previous and payload.get('order_actions') != []:
        raise NativeBlocked('Initial ED order requires medication')
    old = binding['orders'].get(previous, {})
    when = payload.get('decision_offset_seconds')
    prior_time = old.get('course', {}).get('decision_offset_seconds')
    review_protocol = payload.get('review_protocol')
    if review_protocol is not None and (review_protocol != POST_SIGNATURE_REVIEW or type(when) is not int
            or not isinstance(payload.get('order_actions'), list)):
        raise NativeBlocked('Invalid ED pharmacy review protocol')
    if previous and old.get('course', {}).get('review_protocol') != review_protocol:
        raise NativeBlocked('The pharmacy review protocol changed within the encounter')
    prior_review = old.get('pharmacy_review', {}).get('decision')
    schedule = payload.get('prescribing_schedule')
    followup = payload.get('pharmacy_followup', {})
    if previous and old['course'].get('prescribing_schedule') != schedule:
        raise NativeBlocked('The prescribing schedule changed within the encounter')
    if schedule is not None:
        if (schedule != SINGLE_PRESCRIPTION or not review_protocol or not items or
                (previous and when != prior_time)):
            raise NativeBlocked('A single prescribing node requires a complete nonempty order at the same node')
        validation = port.preflight_order({'items': items}, require_unique=True)
        if not validation['native_encodable']:
            raise NativeBlocked('The complete ED prescription has invalid or duplicate medication lines')
        required = old.get('pharmacy_review', {}).get('required_followup', {})
        if (not isinstance(followup, dict) or set(followup) != set(required) or (previous and not required)
                or any(not isinstance(followup[key], dict) or followup[key].get('issue') != issue
                       for key, issue in required.items())):
            raise NativeBlocked('The revised ED order must address each pharmacy return item')
    if review_protocol and previous:
        expected = {'reject', 'defer'} if when == prior_time else {'approve', 'not_applicable'}
        if prior_review not in expected:
            raise NativeBlocked('The previous signed decision has no matching pharmacy outcome')
    if when is not None and (type(when) is not int or when < 0 or (prior_time is not None and
            (when < prior_time or (when == prior_time and not review_protocol)))):
        raise NativeBlocked('ED decision times must increase')
    if old.get('native_ids'):
        port._read_valid_signed_order(binding, old)
    remaining = list(zip(old.get('native_ids', []), old.get('items', []), strict=True))
    actions = payload.get('order_actions')
    attestation = {}
    if 'treatment_review' in payload:
        if (not isinstance(payload['treatment_review'], dict) or not payload['treatment_review']
                or type(payload.get('physician_call')) is not int or payload['physician_call'] < 1):
            raise NativeBlocked('The signed order must include its treatment review and physician call')
        attestation = {k: deepcopy(payload[k]) for k in ('physician_call', 'treatment_review')}
    elif 'withheld_treatments' in payload or 'physician_call' in payload:
        try:
            validate_withheld_treatments(payload.get('withheld_treatments'))
        except ValueError as exc:
            raise NativeBlocked(str(exc)) from exc
        if type(payload.get('physician_call')) is not int or payload['physician_call'] < 1:
            raise NativeBlocked('Signed treatment reasons require the verifying physician call')
        attestation = {k: deepcopy(payload[k]) for k in ('physician_call', 'withheld_treatments')}
    if 'considered_not_ordered' in payload:
        try:
            validate_omitted_products(payload['considered_not_ordered'], visible_codes=port.catalog_mapping,
                                      active_codes={item['drug_code'] for item in items})
        except ValueError as exc:
            raise NativeBlocked(str(exc)) from exc
        attestation['considered_not_ordered'] = deepcopy(payload['considered_not_ordered'])
    if actions is not None:
        active = [{'request_id': rid, 'item': item} for rid, item in remaining]
        try:
            retained, added = apply_actions(active, actions)
        except ValueError as exc:
            raise NativeBlocked(str(exc)) from exc
        kept = set(retained)
        if items != [item for rid, item in remaining if rid in kept] + added:
            raise NativeBlocked('The approved regimen differs from its ED transactions')
        ids = [*retained, *([None] * len(added))]
        remaining = [(rid, item) for rid, item in remaining if rid not in kept]
    else:
        ids, added = [], []
        for item in items:
            ix = next((i for i, (_, prior) in enumerate(remaining) if prior == item), None)
            if ix is None:
                ids.append(None)
                added.append(item)
            else:
                ids.append(remaining.pop(ix)[0])
    saved = port._save_order({**payload, 'items': added}, ctx, binding, pair) if added else None
    new_ids = saved['data']['native_ids'] if saved else []
    it = iter(new_ids)
    ids = [next(it) if rid is None else rid for rid in ids]
    order = binding['orders'].setdefault(order_id, {})
    order.update(native_ids=ids, items=deepcopy(items), status='prepared',
                 parent_order_id=previous, course={'created': new_ids, 'retired': [r for r,_ in remaining],
                     'decision_offset_seconds': when, 'reason': payload['decision_reason'], **attestation})
    if review_protocol:
        order['course']['review_protocol'] = review_protocol
    if schedule:
        order['course'].update(prescribing_schedule=schedule, pharmacy_followup=deepcopy(followup))
    if actions is not None:
        order['course']['order_actions'] = signed_actions(actions, new_ids)
    rows = native_rows(port, pair)
    return port._result({'order_id': order_id, 'status': 'prepared', 'native_ids': ids}, [rows[i] for i in ids])


def document_body(doc):
    content = doc['content']
    if isinstance(content, str):
        content = json.loads(content)
    return json.loads(content['content'][0]['content'][0]['text'])


def read_document(port, pair, doc_id):
    doc = port._client('doctor').post('/document/getContent', {'documentId': doc_id})
    if (doc.get('status') != 'LOCKED' or str(doc['patientId']) != pair['patientId']
            or str(doc['encounterId']) != pair['encounterId'] or str(doc['id']) != str(doc_id)):
        raise NativeError('ED prescription decision document not locked in this encounter')
    return document_body(doc)


def lock_document(port, pair, holder, logical, record, ctx):
    if not holder.get('note'):
        port._save_note({'note_id': logical, 'content': record}, ctx, holder, pair)
        port._save()
    doc_id = holder['note']['native_id']
    client = port._client('doctor')
    doc = client.post('/document/getContent', {'documentId': doc_id})
    if document_body(doc) != record:
        raise NativeError('Persisted ED decision differs')
    if doc.get('status') != 'LOCKED':
        if doc.get('status') != 'SUBMITTED':
            client.post('/document/submit', {'documentId': doc_id})
        client.post('/document/changeLockState', {'documentId': doc_id, 'action': 'LOCK'})
    if read_document(port, pair, doc_id) != record:
        raise NativeError('Locked ED decision content mismatch')
    return doc_id


def changes(previous, current):
    new = dict(zip(current['native_ids'], current['items'], strict=True))
    old = dict(zip(previous.get('native_ids', []), previous.get('items', []), strict=True))
    return {'retained': [rid for rid in new if rid in old],
            'issued': [{'request_id': rid, 'item': item} for rid,item in new.items() if rid not in old],
            'withdrawn': [{'request_id': rid, 'item': item} for rid,item in old.items() if rid not in new]}


def attest(port, order_id, ctx, binding, pair):
    order = binding['orders'][order_id]
    previous = order['parent_order_id']
    if previous != binding.get('signed_ed_order'):
        raise NativeBlocked('Signed ED predecessor changed')
    spec = order['course']
    client = port._client('doctor')
    rows = native_rows(port, pair)
    def targets(ids):
        return [{'productTableName': PRODUCT, 'requestId': i} for i in ids]
    def status(rid):
        row = rows[rid]
        if str(row.get('authoredById')) != ROLE_SETTINGS['doctor'][2]:
            raise NativeBlocked('ED order author mismatch')
        return row['statusEnum']
    created, retired = spec['created'], spec['retired']
    if any(status(i) not in {'DRAFT', 'ACTIVE'} for i in created + retired):
        raise NativeBlocked('ED order has already advanced beyond revision')
    unsigned = [i for i in created if status(i) == 'DRAFT']
    if unsigned:
        client.post(ADVICE + 'signDoctorStationAdviceBatch', {**pair, 'signTargets': targets(unsigned)})
    signed = port._read_valid_signed_order(binding, order) if order['native_ids'] else []
    revoke = [i for i in retired if status(i) == 'ACTIVE']
    if revoke:
        client.post(ADVICE + 'withdrawDoctorStationAdviceBatch', {**pair, 'targets': targets(revoke)})
    rows = native_rows(port, pair)
    if any(status(i) != 'DRAFT' for i in retired) or any(status(i) not in VALID_SIGNED_REQUEST_STATES for i in order['native_ids']):
        raise NativeError('ED regimen read-back differs from the signed change')
    previous_order = binding['orders'].get(previous, {})
    record = {'order_id': order_id, 'parent_order_id': previous,
              'previous_document': previous_order.get('decision_document'),
              'author': ROLE_SETTINGS['doctor'][2], 'reason': spec['reason'],
              'decision_offset_seconds': spec.get('decision_offset_seconds'),
              'active': [{'request_id': i, 'item': item, 'native_signed_time': rows[i]['reqAuthoredTime']}
                         for i,item in zip(order['native_ids'], order['items'], strict=True)],
              **changes(previous_order, order)}
    if 'order_actions' in spec:
        record['order_actions'] = spec['order_actions']
    if 'withheld_treatments' in spec:
        record.update(physician_call=spec['physician_call'], withheld_treatments=spec['withheld_treatments'])
    if 'treatment_review' in spec:
        record.update(physician_call=spec['physician_call'], treatment_review=spec['treatment_review'])
    if 'considered_not_ordered' in spec:
        record['considered_not_ordered'] = spec['considered_not_ordered']
    # Keep the original signed decision when OpenHIS reverts a withdrawn, unfilled line to DRAFT.
    if spec.get('review_protocol'):
        record['review_protocol'] = spec['review_protocol']
    single = spec.get('prescribing_schedule') == SINGLE_PRESCRIPTION
    if single:
        record.update(prescribing_schedule=SINGLE_PRESCRIPTION, pharmacy_followup=spec['pharmacy_followup'])
    doc_id = lock_document(port, pair, order.setdefault('decision_record', {}), order_id, record, ctx)
    transition = {}
    if single and {rid for rid, row in rows.items() if row['statusEnum'] in VALID_SIGNED_REQUEST_STATES} != set(order['native_ids']):
        raise NativeError('The prescription has unaccounted active medication lines')
    if single and previous:
        parent = read_document(port, pair, record['previous_document'])
        if (parent['order_id'] != previous or [(line['request_id'], line['item']) for line in parent['active']]
                != list(zip(previous_order['native_ids'], previous_order['items'], strict=True))):
            raise NativeError('The predecessor differs from its locked signature')
        transition['replacement_readback'] = {
            'current_order_id': order_id, 'current_document_id': doc_id,
            'superseded_order_id': previous, 'superseded_document_id': record['previous_document'],
            'active_line_states': {rid: status(rid) for rid in order['native_ids']},
            'withdrawn_line_states': {rid: status(rid) for rid in retired},
        }
    order.update(status='signed', decision_document=doc_id)
    binding['signed_ed_order'] = order_id
    if spec.get('review_protocol') and not record['active'] and not record['order_actions']:
        order['pharmacy_review'] = {'decision': 'not_applicable'}
    return port._result({'status': 'signed', 'native_ids': order['native_ids'], 'order_id': order_id,
                         'decision_document': doc_id, **transition}, signed)


def review(port, payload, ctx, binding, pair):
    order_id = payload['order_id']
    order = binding['orders'].get(order_id, {})
    if (order_id != binding.get('signed_ed_order') or order.get('status') != 'signed'
            or order.get('course', {}).get('review_protocol') != POST_SIGNATURE_REVIEW):
        raise NativeBlocked('Pharmacy review requires a current formally signed ED decision')
    reviewer = port._client('pharmacist').identity
    if str(reviewer.get('practitionerId')) != ROLE_SETTINGS['pharmacist'][2]:
        raise NativeBlocked('Pharmacy reviewer identity does not match the registered role')
    details = payload['review']
    if (details.get('decision') not in {'approve', 'reject', 'defer'}
            or not (details.get('pharmacist_review') or {}).get('reason')):
        raise NativeBlocked('Pharmacy review requires an explicit outcome and reason')
    record = {**deepcopy(details), 'order_id': order_id, 'signed_decision_document': order['decision_document'],
              'pharmacist': str(reviewer['practitionerId'])}
    old = order.get('pharmacy_review')
    if old and {k: v for k, v in old.items() if k != 'document_id'} != record:
        raise NativeBlocked('An existing pharmacy review cannot be replaced')
    holder = order.setdefault('pharmacy_review_record', {})
    doc_id = lock_document(port, pair, holder, order_id + ':pharmacy', record, ctx)
    order['pharmacy_review'] = {'document_id': doc_id, **record}
    return port._result(order['pharmacy_review'], read_document(port, pair, doc_id))


def read_history(port, binding, pair):
    logical = binding.get('signed_ed_order')
    doc_id = binding['orders'].get(logical, {}).get('decision_document')
    records, seen = [], set()
    while doc_id:
        if doc_id in seen:
            raise NativeError('Circular ED order history')
        seen.add(doc_id)
        record = read_document(port, pair, doc_id)
        if record['order_id'] != logical or record['author'] != ROLE_SETTINGS['doctor'][2]:
            raise NativeError('Signed ED history linkage mismatch')
        records.append({'document_id': doc_id, **record})
        logical, doc_id = record['parent_order_id'], record['previous_document']
    if not records or logical:
        raise NativeBlocked('No complete signed ED prescription history')
    rows = native_rows(port, pair)
    active = {e['request_id'] for e in records[0]['active']}
    issued = {e['request_id'] for rec in records for e in rec['issued']}
    native_active = {rid for rid, row in rows.items() if row['statusEnum'] in VALID_SIGNED_REQUEST_STATES}
    if native_active != active or not issued <= rows.keys():
        raise NativeError('Signed ED history differs from current native order states')
    history = list(reversed(records))
    pharmacy = {}
    for record in history:
        if record.get('review_protocol') != POST_SIGNATURE_REVIEW:
            continue
        oid = record['order_id']
        review_record = binding['orders'][oid].get('pharmacy_review')
        if not review_record:
            raise NativeBlocked('A signed ED decision awaits pharmacy review')
        if doc_id := review_record.get('document_id'):
            if {'document_id': doc_id, **read_document(port, pair, doc_id)} != review_record:
                raise NativeError('Persisted pharmacy review differs from the operating record')
        pharmacy[oid] = review_record
    state = {'documents': [r['document_id'] for r in history],
             'line_states': {rid: rows[rid]['statusEnum'] for rid in sorted(issued)}}
    if history[-1].get('prescribing_schedule') == SINGLE_PRESCRIPTION:
        state['current_order_id'] = binding['signed_ed_order']
        state['version_states'] = {record['order_id']: ('current' if record is history[-1] else 'superseded')
                                   for record in history}
    return port._result({'versions': history, **({'pharmacy_reviews': pharmacy} if pharmacy else {})}, state)
