"""Verify published workflow links against the authorized encounter and model archives."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re

from handoff.evidence import contained_file, paid_index
from results.render_linked_manuscript import cohort_fingerprint, paid_cohort
from results.summarize_paid_runs import read
from sandbox.experiments.linked import order_encoding
from sandbox.experiments.linked.contracts import digest, validate_prescription
from sandbox.experiments.linked.json_boundary import parse_json_response


REVISION_CHECKS = ('findings_reproduced', 'revision_encodable', 'feedback_in_revision_request',
                   'response_binds_revised_candidate', 'signed_order_save_binds_final_candidate')


def single(rows, name):
    rows = list(rows)
    if len(rows) != 1:
        raise ValueError(f'Expected one archived {name} record.')
    return rows[0]


def business_events(run, operation):
    return (event for event in run['events'] if event['event'] == 'business_operation'
            and event['operation'] == operation and event['response'].get('readback_verified') is True
            and event['response'].get('native_effect') is True)


def role_event(run, call):
    return single((event for event in run['events'] if event['event'] == 'role_call'
                   and event['call_sequence'] == call['sequence']), 'role call')


def matching_model_record(run, call, directory):
    request_id = call.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        return None
    path = directory / (request_id + '.json')
    if not path.is_file():
        return None
    record = read(contained_file(directory, path.name, portable=True))
    if (record.get('request_id') != request_id or record.get('model') != call.get('model_name')
            or record.get('http_status') != 200 or record.get('outcome') != 'completed'
            or record.get('role') != call.get('role')
            or any(record.get(key) != run[key] for key in ('case_id', 'run_id', 'config_id'))):
        return None
    messages = [row for row in record['request_body']['messages'] if row.get('role') == 'user']
    if len(messages) != 1:
        return None
    observed = json.loads(messages[0]['content'])
    if observed != call['observation'] or digest(observed) != call['observation_hash']:
        return None
    return record


def note_uses_signed_order(run, signed, directory):
    artifacts = run['artifacts']
    calls = [call for call in run['calls'] if call['status'] == 'completed'
             and call['observation'].get('action') == 'document'
             and call['observation'].get('is_final_prefix') is True]
    if len(calls) != 1:
        return False
    call = calls[0]
    if matching_model_record(run, call, directory) is None:
        return False
    stage = call['observation']
    state = stage['observed_business_state']['prescription']
    order = signed['response']['data']
    note = single(business_events(run, 'note.attest'), 'note attestation')
    event = role_event(run, call)
    return (state == artifacts['prescription_business_state']
            and state['readback_verified'] is True
            and state['source_operation'] == 'order.attest'
            and state['logical_order_id'] == order['order_id'] == artifacts['prescription_versions'][-1]['order_id']
            and state['status'] == order['status'] == 'signed'
            and stage['approved_prescription'] == artifacts['final_prescription']
            and state['items'] == stage['approved_prescription']['items']
            and event['observation_hash'] == call['observation_hash']
            and signed['sequence'] < event['sequence'] < note['sequence'])


def settlement_uses_signed_order(run, signed, audit):
    prepared = single(business_events(run, 'payment.prepare'), 'payment preparation')
    committed = single(business_events(run, 'payment.commit'), 'payment commit')
    order = signed['response']['data']
    bill = prepared['response']['data']
    payment = committed['response']['data']
    items = prepared['response']['readback']['native_charge_items']
    settlement = run['artifacts']['settlement']
    bill_fields = ('bill_id', 'amount', 'currency')
    payment_payload = {key: bill[key] for key in bill_fields}
    payment_payload.update(channel='CASH', transaction_id=payment['transaction_id'])
    return (signed['sequence'] < prepared['sequence'] < committed['sequence']
            and order['status'] == 'signed' and payment['status'] == 'paid'
            and settlement['simulated'] is True and settlement['bill'] == bill and settlement['payment'] == payment
            and bill['order_id'] == payment['order_id'] == order['order_id'] == run['artifacts']['prescription_versions'][-1]['order_id']
            and {str(item['serviceId']) for item in items} == set(map(str, order['native_ids']))
            and {str(item['chargeItemId']) for item in items} == set(map(str, bill['charge_item_ids']))
            and all(bill[key] == payment[key] for key in (*bill_fields, 'charge_item_ids'))
            and committed['payload_hash'] == digest(payment_payload)
            and all(audit['checks'][key]['value'] is True for key in (
                'target_payment_http_readback', 'target_bill_is_final_reviewed_order',
                'fresh_target_payment_paid_exactly_once')))


def encoding_feedback(run, signed, directory, catalogue):
    artifacts = run['artifacts']
    versions = artifacts['prescription_versions']
    first, revised = versions[:2]
    final = versions[-1]
    validation = first['business_validation']
    before = order_encoding.preflight(first['prescription'], catalogue)
    after = order_encoding.preflight(revised['prescription'], catalogue)
    checks = dict.fromkeys(REVISION_CHECKS, False)
    checks['findings_reproduced'] = before == validation['native_encoding'] and before['native_encodable'] is False
    checks['revision_encodable'] = (after == revised['business_validation']['native_encoding']
                                    and after['native_encodable'] is True)
    followup = single((row for row in artifacts['prescription_followups'] if row['revision'] == 0),
                      'first prescription follow-up')
    call = single((row for row in run['calls'] if row['role'] == 'doctor' and row['phase'] == 'prescription_revision_0'
                   and row['status'] == 'completed' and row['response'] == followup['physician_response']),
                  'accepted prescription revision')
    record = matching_model_record(run, call, directory)
    if record is None:
        return checks
    event = role_event(run, call)
    candidate = single((row for row in run['events'] if row['event'] == 'prescription_candidate_recorded'
                        and row['candidate_id'] == first['order_id']), 'local prescription candidate')
    observation = call['observation']
    checks['feedback_in_revision_request'] = (
        candidate['native_write_attempted'] is False and first['native_write_attempted'] is False
        and candidate['business_validation'] == observation['business_validation'] == validation
        and candidate['prescription'] == observation['prescription'] == first['prescription']
        and observation['reviews'] == first['review'] and event['observation_hash'] == call['observation_hash']
        and candidate['sequence'] < event['sequence'])
    choice = single(record['response_body']['choices'], 'model response choice')
    parsed, _ = parse_json_response(choice['message']['content'], finish_reason=choice['finish_reason'],
                                    policy=call['response_parse']['policy'])
    checks['response_binds_revised_candidate'] = (
        parsed == call['response'] == followup['physician_response']
        and digest(parsed) == call['response_hash'] == event['response_hash']
        and validate_prescription(parsed['prescription']) == revised['prescription']
        and revised['parent_order_id'] == first['order_id']
        and followup['previous_prescription_hash'] == digest(first['prescription'])
        and followup['review_hash'] == digest(first['review']))
    payload = {'order_id': final['order_id'], 'items': final['prescription']['items'],
               'parent_order_id': final['native_parent_order_id']}
    checks['signed_order_save_binds_final_candidate'] = sum(
        operation['payload_hash'] == digest(payload)
        and operation['response']['data']['native_ids'] == signed['response']['data']['native_ids']
        and event['sequence'] < operation['sequence'] < signed['sequence']
        for operation in business_events(run, 'order.save')) == 1
    return checks


def archived_paid_cohort(bundle):
    receipt = read(bundle / 'bundle.json')
    bound = {}
    for name in ('audit.json', 'batch/specification.json', 'batch/batch_summary.json'):
        payload = contained_file(bundle, name, portable=True).read_bytes()
        if sha256(payload).hexdigest() != receipt['files'][name]:
            raise ValueError('The archived audit or batch specification differs from its receipt.')
        bound[name] = json.loads(payload)
    spec = bound['batch/specification.json']
    audit = bound['audit.json']
    if audit.get('audit', audit)['source_specification_sha256'] != receipt['files']['batch/specification.json']:
        raise ValueError('Native audit does not bind the selected experiment specification.')
    cohort = paid_cohort(audit)
    fingerprint = cohort_fingerprint(cohort)
    if fingerprint != receipt['paid_cohort_sha256'] or len(cohort) != receipt['verified_paid']:
        raise ValueError('The native audit does not bind the frozen paid cohort.')
    return spec, fingerprint, {row['source_run_file_sha256']: row for row in cohort}


def paid_traces(bundle, paid):
    seen = set()
    for case_id, entry in paid_index(bundle / 'batch'):
        payload = contained_file(bundle / 'batch', entry['result_path'], portable=True).read_bytes()
        file_hash = sha256(payload).hexdigest()
        if file_hash not in paid or file_hash in seen:
            raise ValueError('A trace falls outside or duplicates the frozen paid cohort.')
        seen.add(file_hash)
        run = json.loads(payload)
        if run['case_id'] != case_id:
            raise ValueError('The encounter trace does not match its case index.')
        yield run, paid[file_hash]
    if seen != paid.keys():
        raise ValueError('The archive is missing a paid trace.')


def audit_feedback(bundle, model_evidence, catalogue_path):
    bundle, model_evidence = bundle.resolve(), model_evidence.resolve()
    spec, fingerprint, paid = archived_paid_cohort(bundle)
    catalogue = read(catalogue_path)
    if (digest(catalogue) != spec['catalog_hash']
            or sha256(Path(order_encoding.__file__).read_bytes()).hexdigest() != spec['code_hashes']['order_encoding.py']):
        raise ValueError('Use the frozen OpenHIS catalogue and encoder for this check.')

    counts = Counter(signed_order_in_final_drafting_request=0, signed_order_in_verified_settlement=0,
                     first_approved_unencodable=0)
    revision_checks = Counter(dict.fromkeys(REVISION_CHECKS, 0))
    for run, native_audit in paid_traces(bundle, paid):
        signed = single(business_events(run, 'order.attest'), 'signed order')
        counts['signed_order_in_final_drafting_request'] += note_uses_signed_order(run, signed, model_evidence)
        counts['signed_order_in_verified_settlement'] += settlement_uses_signed_order(run, signed, native_audit)
        first = run['artifacts']['prescription_versions'][0]
        if first['review']['decision'] == 'approve' and first['business_validation']['status'] == 'native_encoding_unavailable':
            counts['first_approved_unencodable'] += 1
            for name, verified in encoding_feedback(run, signed, model_evidence, catalogue).items():
                revision_checks[name] += int(verified)
    return {'scope': 'archived development cohort workflow links', 'paid_cohort_sha256': fingerprint,
            'verified_paid': len(paid), **counts, 'encoding_feedback_checks': dict(revision_checks),
            'model_calls': 0, 'native_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'model-evidence', 'catalogue'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_feedback(args.bundle, args.model_evidence, args.catalogue), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
