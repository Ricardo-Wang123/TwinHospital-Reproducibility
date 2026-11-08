"""Gateway-compatible native HTTP port with an explicitly attributed experiment ledger.

Queue entries, immutable draft versions and trusted doctor confirmations belong
to TwinHospital. Hospital EMR, medication requests and payment effects are only
reported from real native HTTP responses. Missing readback remains unresolved.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from decimal import Decimal
from pathlib import Path

from ..contracts import canonical, digest, utc_now
from ..gateway import SandboxGateway, StateObserver
from .native_materials import MaterialGap, equivalent, resolve_native_config, NativePreparationClient
from .openhis_native import NativeOpenHISClient, NativeAPIError


class NativeObservationGap(RuntimeError):
    pass


def data(result):
    if not result.get('success'):
        raise NativeObservationGap('native readback rejected; evidence=' + str(result.get('evidence')))
    return result['body'].get('data')


def records(result):
    value = data(result)
    if isinstance(value, dict):
        value = value.get('records')
    if not isinstance(value, list):
        raise NativeObservationGap('native response does not expose the expected record array')
    return value


class NativeStateObserver(StateObserver):
    def observation_gaps(self, action, state):
        return sorted(set(super().observation_gaps(action, state) + state.get('_native_observation_gaps', [])))


class NativeSandboxGateway(SandboxGateway):
    """Reuse all existing checks; replace only observation/provenance handling."""
    def __init__(self, backend, *, epoch, sink=None, observation_window_s=0):
        super().__init__(backend, epoch=epoch, sink=None, observation_window_s=observation_window_s)
        self.observer = NativeStateObserver(backend)
        self.native_sink = sink

    def execute(self, action, context):
        started = time.monotonic()
        result = super().execute(action, context)
        submitted = action.to_dict() if hasattr(action, 'to_dict') else action
        op, payload = submitted.get('operation'), submitted.get('payload', {})
        before, after = result.get('before_state') or {}, result.get('after_state') or {}
        applied = False
        if not after.get('_native_observation_gaps') and result.get('outcome') == 'downstream_safe':
            # A service may return an error after committing a partial action.
            # A normal request whose exact intended effect is observed is not
            # a false block; retain the raw rejection alongside effect outcome.
            if op in ('note.save', 'note.revise'):
                old = before.get('notes', {}).get(payload.get('note_id'), {})
                new = after.get('notes', {}).get(payload.get('note_id'), {})
                applied = new.get('version') == old.get('version', 0) + 1 and bool(new.get('versions')) and new['versions'][-1].get('content') == payload.get('content')
            elif op == 'payment.commit':
                old = before.get('bills', {}).get(payload.get('bill_id'), {})
                new = after.get('bills', {}).get(payload.get('bill_id'), {})
                applied = old.get('status') != 'paid' and new.get('status') == 'paid' and any(
                    x.get('bill_id') == payload.get('bill_id') for x in after.get('payment_effects', []))
            elif op == 'order.attest':
                old = before.get('orders', {}).get(payload.get('order_id'), {})
                new = after.get('orders', {}).get(payload.get('order_id'), {})
                applied = old.get('status') != 'signed' and new.get('status') == 'signed' and new.get('authorized') is True
            elif op == 'encounter.end':
                applied = not before.get('encounter_ended_at') and bool(after.get('encounter_ended_at'))
        if applied:
            result['native_reply_status'] = result.get('status')
            result.update(status='executed', outcome='executed', effect_applied_despite_rejected_reply=True)
        # Native APIs are separate transactions; an HTTP terminal response is
        # never relabelled as a cross-service atomic commit.
        result['completion_evidence'] = 'native_http_readback_and_experiment_ledger'
        result['protection_boundary'] = 'experiment_gateway' if result.get('protection_source') == 'sandbox' else result.get('enforcement_source', 'unknown')
        result['native_execution_timing'] = {'started_monotonic': started, 'finished_monotonic': time.monotonic(),
                                             'source': 'experiment_process_monotonic_clock'}
        if self.native_sink:
            self.native_sink(result)
        return result


class NativeHTTPPort:
    research_eligible = True

    def __init__(self, config, manifest, evidence_dir, *, store=None):
        self.config, self.manifest = copy.deepcopy(config), manifest
        self.materials = manifest['cases']
        self.personnel = self.config.get('personnel', {})
        self.native_personnel = manifest.get('personnel', {})
        self.store = store
        self.evidence_dir = Path(evidence_dir)
        client_config = {**self.config, 'dedicated_patient_ids': [m['patient_id'] for m in self.materials.values()],
                         'dedicated_encounter_ids': [m['encounter_id'] for m in self.materials.values()]}
        client_config = resolve_native_config(client_config)
        self.client = NativePreparationClient(client_config, self.evidence_dir / 'http')
        self._states, self._locks, self._guard = {}, {}, threading.RLock()
        self.active_epoch = None
        self.run_id = self.config_id = None
        self.last_response = {}

    def _lock(self, case_id):
        with self._guard:
            return self._locks.setdefault(case_id, threading.RLock())

    def _ledger(self, case_id, event, **fields):
        row = {'source': 'experiment_ledger', 'case_id': case_id, 'run_id': self.run_id,
               'config_id': self.config_id, 'epoch': self.active_epoch, 'event': event,
               'recorded_at': utc_now(), **copy.deepcopy(fields)}
        row['recorded_monotonic'] = time.monotonic()
        if self.store:
            self.store.append('native_ledger.jsonl', row)
        else:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            with self._guard, (self.evidence_dir / 'experiment_ledger.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(canonical(row) + '\n')

    def configure_run(self, run_id, epoch, config_id=None, config=None):
        self.run_id, self.active_epoch, self.config_id = run_id, epoch, config_id
        self._ledger(None, 'configuration_activated', restore_performed=False,
                     initial_state_policy='disjoint_prevalidated_native_objects; no SQL restore')

    def deactivate(self):
        self.active_epoch = -1

    def restore(self, seed):
        raise RuntimeError('native HTTP execution does not restore hospital data; supply fresh dedicated objects')

    def fingerprint(self):
        return digest({'manifest': self.manifest.get('_manifest_sha256'), 'ledger': self._states})

    def authorized(self, signatory, operation):
        return operation in self.personnel.get(signatory, [])

    def _encounter(self, case_id):
        material = self.materials[case_id]
        result = self.client.encounters(encounterId=material['encounter_id'], pageSize=100)
        matches = [r for r in records(result) if str(r.get('encounterId')) == str(material['encounter_id'])]
        if len(matches) != 1 or str(matches[0].get('patientId')) != str(material['patient_id']):
            raise NativeObservationGap('native encounter readback has missing or ambiguous patient ownership')
        if not str(matches[0].get('patientName', '')).startswith(self.manifest['dedicated_name_prefix']):
            raise MaterialGap('current native patient name is outside the dedicated experimental namespace')
        return matches[0], result['evidence']

    def check_identity_and_channels(self):
        """Called once before any study writes; no default switch values."""
        result = self.client.identity()
        if not result['success']:
            raise MaterialGap('native identity unavailable')
        identity = result['body']
        expected = self.manifest.get('native_identity', {})
        if not expected.get('practitioner_id') or str(identity.get('practitionerId')) != str(expected['practitioner_id']):
            raise MaterialGap('native practitioner identity differs from the frozen material')
        options = identity.get('optionJson')
        if isinstance(options, str):
            options = json.loads(options)
        if not isinstance(options, dict):
            raise MaterialGap('native identity does not expose verifiable runtime options')
        for channel in ('funds', 'insurance', 'invoice'):
            rule = self.manifest['external_channels'][channel]
            if channel == 'funds' and rule.get('mode') == 'native_cash_accounting_only' and rule.get('external_transfer_requested') is False:
                continue
            key = rule.get('runtime_option_key')
            if rule.get('runtime_tenant_option_code'):
                response = self.client.request('GET', '/system/tenant-option/detail-list/' + str(self.config.get('tenant_id', 1)))
                rows = records(response)
                matches = [r for r in rows if str(r.get('code')) == str(rule['runtime_tenant_option_code'])]
                if len(matches) != 1 or not equivalent(matches[0].get('content'), rule['disabled_value']):
                    raise MaterialGap(channel + ' actual tenant option is not disabled')
                continue
            if not key or key not in options or not equivalent(options[key], rule['disabled_value']):
                raise MaterialGap(channel + ' runtime channel disabled state is not established')
        self.client.config['external_effects_disabled_verified'] = True
        self._ledger(None, 'native_identity_and_channels_verified', evidence=result['evidence'],
                     practitioner_id=str(identity['practitionerId']), source_of_configuration='native_http')
        return {'identity_evidence': result['evidence'], 'practitioner_id': str(identity['practitionerId'])}

    def _emr(self, case_id):
        material = self.materials[case_id]
        result = self.client.emr(material['encounter_id'])
        row = data(result)
        if row is None:
            return None, None, result['evidence']
        if str(row.get('patientId')) != str(material['patient_id']) or str(row.get('encounterId')) != str(material['encounter_id']):
            raise NativeObservationGap('native EMR ownership differs from the bound case')
        context = row.get('contextJson')
        if isinstance(context, str):
            context = json.loads(context)
        if not isinstance(context, dict):
            raise NativeObservationGap('native EMR contextJson is not an object')
        field = material['emr']['content_field']
        serialized = context.get(field)
        if serialized is None:
            return row, None, result['evidence']
        if not isinstance(serialized, str):
            raise NativeObservationGap('native EMR selected field is not a string')
        # The whole experiment object is a string in a verified existing EMR
        # field. Decode the actual server value, never the sent request.
        return row, json.loads(serialized), result['evidence']

    def _order_rows(self, case_id):
        result = self.client.orders(self.materials[case_id]['encounter_id'])
        return records(result), result['evidence']

    def snapshot(self, case_id):
        with self._lock(case_id):
            if case_id not in self._states:
                return {}
            state = copy.deepcopy(self._states[case_id])
            gaps = state['_native_observation_gaps'] = []
            sources = state['native_readbacks'] = []
            encounter, proof = self._encounter(case_id)
            sources.append({'kind': 'encounter', 'evidence': proof, 'row': encounter})
            if state.get('encounter_ended_at') and encounter.get('statusEnum') != 4:
                gaps.append('native_encounter_not_discharged')
            if state['notes'] or state.get('_note_attempted'):
                emr, content, proof = self._emr(case_id)
                sources.append({'kind': 'EMR', 'evidence': proof, 'row': emr})
                if state.get('_note_attempted') and not state['notes']:
                    gaps.append('native_EMR_write_outcome_has_no_confirmed_version')
                for note in state['notes'].values():
                    current = note['versions'][-1]
                    # Preserve immutable archived versions while independently
                    # checking the current hospital projection for corruption.
                    note['native_current_content'] = content
                    note['native_current_evidence'] = proof
                    if content != current['content']:
                        current['content'], current['content_hash'] = content, digest(content)
                        gaps.append('native_EMR_current_version_content_changed')
                    note['content_source'] = 'native_http_readback'
            if state['orders'] or state.get('_order_attempted'):
                rows, proof = self._order_rows(case_id)
                sources.append({'kind': 'orders', 'evidence': proof, 'rows': rows})
                if state.get('_order_attempted') and not state['orders']:
                    gaps.append('native_order_write_outcome_has_no_confirmed_ID_mapping')
                for order in state['orders'].values():
                    selected = [r for r in rows if str(r.get('requestId')) in order['native_request_ids']]
                    order['native_rows'], order['items_source'] = selected, 'native_http_readback'
                    fields = ('adviceName', 'dose', 'doseUnitCode', 'quantity', 'unitCode', 'methodCode', 'rateCode')
                    order['items_observed'] = len(selected) == len(order['native_request_ids']) and all(all(r.get(k) is not None for k in fields) for r in selected)
                    order['items'] = [{'drug_name': r.get('adviceName'), 'dose': r.get('dose'), 'dose_unit': r.get('doseUnitCode'),
                        'quantity': r.get('quantity'), 'quantity_unit': r.get('unitCode'), 'frequency': r.get('rateCode'),
                        'route': r.get('methodCode'), 'native_request_id': str(r.get('requestId')), 'evidence': proof} for r in selected]
                    native_signers = {str(r.get('requesterId')) for r in selected}
                    order['native_requester_ids'] = sorted(native_signers)
                    # RequestStatus.COMPLETED=3 is the native post-charge
                    # checked state. It retains the previously observed signer;
                    # do not mistake this legitimate progression for unsigned.
                    if selected and all(r.get('statusEnum') in (2, 3) for r in selected):
                        order['status'] = 'signed'
                    elif selected and all(r.get('statusEnum') == 1 for r in selected):
                        order['status'] = 'draft'
                    else:
                        order['status'] = 'unknown'
                        gaps.append('native_order_status_or_coverage_incomplete')
                    expected = self.native_personnel.get(order.get('signatory_id'), {}).get('practitioner_id')
                    order['authorized'] = self.authorized(order.get('signatory_id'), 'order.attest') and expected is not None and native_signers == {str(expected)}
                    if not order['items_observed']:
                        gaps.append('native_order_items_unobserved')
                    expected_items = self.materials[case_id]['order_save_request']['adviceSaveList']
                    for native_id, expected_item in zip(order['native_request_ids'], expected_items):
                        actual = next((r for r in selected if str(r.get('requestId')) == native_id), None)
                        if actual is None or any(not equivalent(actual.get(k), expected_item.get(k)) for k in fields):
                            gaps.append('native_order_item_differs_from_frozen_material')
            self._observe_payments(case_id, state, gaps, sources)
            state['payment_attribution_complete'] = not any(g.startswith('payment:') for g in gaps)
            return state

    def _observe_payments(self, case_id, state, gaps, sources):
        effects = []
        for bill_id, bill in state['bills'].items():
            if not bill.get('commit_attempted'):
                continue
            # Controller spells this parameter kinsEnum. Its mapper requires
            # kind_enum equality even though the query wrapper accepts id.
            # Obtain the value from precharge rather than guessing its enum.
            native_kind = bill.get('native_create_response', {}).get('kindEnum')
            if native_kind is None:
                gaps.append('payment:native_kind_not_observed')
                bill['status'] = 'unknown'
                continue
            result = self.client.request('GET', '/payment/payment/page', params={
                'id': bill['native_payment_id'], 'kinsEnum': native_kind,
                'searchKey': bill.get('native_payment_no') or '', 'pageNo': 1, 'pageSize': 100})
            rows = records(result)
            matches = [r for r in rows if str(r.get('id')) == bill['native_payment_id']]
            sources.append({'kind': 'payment_status', 'evidence': result['evidence'], 'rows': matches})
            if len(matches) != 1:
                gaps.append('payment:successful_payment_state_not_observed')
                bill['status'] = 'unknown'
                continue
            row = matches[0]
            bill['native_status'], bill['native_row'] = row.get('statusEnum'), row
            bill['patient_id'], bill['encounter_id'] = str(row.get('patientId')), str(row.get('encounterId'))
            bill['status'] = 'paid' if row.get('statusEnum') == 1 else 'unknown'
            if bill['status'] != 'paid' or not equivalent(row.get('tenderedAmount'), bill['amount']):
                gaps.append('payment:status_or_amount_unconfirmed')
            receipt = self.client.payment(bill['native_payment_id'])
            actual = data(receipt)
            sources.append({'kind': 'payment_receipt', 'evidence': receipt['evidence'], 'row': actual})
            details = actual.get('detail') if isinstance(actual, dict) else None
            if not isinstance(details, list):
                gaps.append('payment:actual_detail_rows_not_returned')
                continue
            cash = [r for r in details if r.get('payEnum') == 220100]
            if not cash or any(r.get('id') is None or str(r.get('reconciliationId')) != bill['native_payment_id'] for r in cash):
                gaps.append('payment:cash_detail_identity_incomplete')
                continue
            if sum(Decimal(str(r['amount'])) for r in cash) != Decimal(bill['amount']):
                gaps.append('payment:cash_effect_total_differs')
            # One submitted cash component -> one expected native effect. Keep
            # every native detail, including duplicates, for the observer.
            for detail in cash:
                effects.append({'transaction_id': bill['transaction_id'], 'bill_id': bill_id,
                    'patient_id': bill['patient_id'], 'encounter_id': bill['encounter_id'],
                    'native_payment_id': bill['native_payment_id'], 'native_detail_id': str(detail['id']),
                    'amount': str(detail['amount']), 'source': 'native_http_readback', 'evidence': receipt['evidence']})
        state['payment_effects'] = effects

    def execute(self, action, context):
        case_id, op, payload = action['case_id'], action['operation'], action['payload']
        with self._lock(case_id):
            if action['epoch'] != self.active_epoch:
                return {'status': 'rejected', 'reason': 'inactive_epoch', 'enforcement_source': 'experiment_ledger'}
            try:
                result = self._execute(case_id, op, payload, context)
            except (NativeAPIError, NativeObservationGap, MaterialGap, KeyError, ValueError) as exc:
                return {'status': 'unresolved', 'reason': type(exc).__name__ + ': ' + str(exc), 'enforcement_source': 'unknown'}
            self._ledger(case_id, 'action_finished', operation=op, request_id=action['request_id'],
                         result={k: v for k, v in result.items() if k != 'body'})
            return result

    def _result(self, result):
        return {'status': 'executed' if result['success'] else 'rejected',
                'reason': None if result['success'] else 'native_business_rejection',
                'http_status': result['http_status'], 'business_code': result['business_code'],
                'native_evidence': result['evidence'], 'enforcement_source': 'native_http_boundary'}

    def _execute(self, case_id, op, p, context):
        m = self.materials[case_id]
        if op == 'case.bind':
            if (str(context['patient_id']), str(context['encounter_id'])) != (str(m['patient_id']), str(m['encounter_id'])):
                raise MaterialGap('trusted case context differs from frozen native binding')
            row, proof = self._encounter(case_id)
            if case_id in self._states:
                return {'status': 'replayed', 'reason': None, 'enforcement_source': 'experiment_ledger'}
            self._states[case_id] = {'patient_id': str(m['patient_id']), 'encounter_id': str(m['encounter_id']),
                'queue_entries': [], 'notes': {}, 'orders': {}, 'bills': {}, 'transactions': {}, 'payment_effects': [],
                'encounter_ended_at': None, 'binding_evidence': proof,
                'field_sources': {'queue_entries': 'experiment_ledger', 'note_versions': 'experiment_ledger',
                    'note_attestation': 'experiment_ledger', 'native_EMR_content': 'native_http_readback',
                    'orders': 'native_http_readback', 'bills': 'native_http_readback',
                    'encounter_ended_at': 'experiment_ledger_clock_after_native_status_readback'}}
            return {'status': 'executed', 'reason': None, 'native_evidence': proof, 'enforcement_source': 'experiment_ledger'}
        state = self._states[case_id]
        if op == 'triage.enqueue':
            state['queue_entries'].append({'patient_id': state['patient_id'], 'encounter_id': state['encounter_id'],
                                           'source': 'experiment_ledger', 'enqueued_at': utc_now(),
                                           'enqueued_monotonic': time.monotonic()})
            return {'status': 'executed', 'reason': None, 'enforcement_source': 'experiment_ledger'}
        if op in ('note.save', 'note.revise'):
            note = state['notes'].get(p['note_id'])
            if note is None:
                existing, _, _ = self._emr(case_id)
                if existing is not None:
                    raise MaterialGap('dedicated initial encounter already contains an EMR; do not overwrite it')
            native_context = copy.deepcopy(m['emr'].get('base_context', {}))
            native_context[m['emr']['content_field']] = canonical(p['content'])
            state['_note_attempted'] = True
            response = self.client.save_emr(m['patient_id'], m['encounter_id'], native_context)
            row, content, proof = self._emr(case_id)
            if row is None or content != p['content']:
                raise NativeObservationGap('actual native EMR does not equal submitted structured artifact')
            versions = copy.deepcopy(note['versions']) if note else []
            version = len(versions) + 1
            versions.append({'version': version, 'parent_version': version - 1 or None, 'content': content,
                'content_hash': digest(content), 'author_id': context['actor_id'], 'status': 'draft',
                'content_source': 'native_http_readback', 'version_source': 'experiment_ledger',
                'native_emr_id': str(row['id']), 'native_evidence': proof})
            state['notes'][p['note_id']] = {'version': version, 'status': 'draft', 'versions': versions}
            return self._result(response)
        if op == 'note.attest':
            note = state['notes'][p['note_id']]
            _, actual, proof = self._emr(case_id)
            if p['version'] != note['version'] or actual != note['versions'][-1]['content']:
                raise NativeObservationGap('trusted confirmation lacks matching current hospital EMR')
            note['status'] = 'locked'
            note['versions'][-1].update(status='locked', signatory_id=p['signatory_id'], attested_at=utc_now(),
                                       attestation_source='experiment_ledger', attestation_native_content_evidence=proof)
            return {'status': 'executed', 'reason': None, 'enforcement_source': 'experiment_ledger'}
        if op == 'encounter.end':
            response = self.client.complete_encounter(m['encounter_id'])
            row, proof = self._encounter(case_id)
            if row.get('statusEnum') != 4:
                raise NativeObservationGap('native completed encounter state not read back')
            state['encounter_ended_at'] = utc_now()
            state['native_completion_evidence'] = proof
            return self._result(response)
        if op == 'order.save':
            if p['order_id'] in state['orders']:
                return {'status': 'rejected', 'reason': 'order_already_exists', 'enforcement_source': 'experiment_ledger'}
            if digest(p.get('items')) != digest(m['prescription']['items']):
                raise MaterialGap('requested prescription differs from frozen native mapping')
            existing, _ = self._order_rows(case_id)
            if existing:
                raise MaterialGap('dedicated starting encounter already has native orders')
            state['_order_attempted'] = True
            response = self.client.save_advice(copy.deepcopy(m['order_save_request']))
            ids = data(response)
            if not isinstance(ids, list) or len(ids) != len(m['order_save_request']['adviceSaveList']):
                raise NativeObservationGap('native saveAdvice did not return complete medication request IDs')
            state['orders'][p['order_id']] = {'status': 'draft', 'native_request_ids': [str(i) for i in ids],
                'signatory_id': None, 'patient_id': state['patient_id'], 'encounter_id': state['encounter_id'],
                'save_evidence': response['evidence']}
            return self._result(response)
        if op == 'order.attest':
            order = state['orders'][p['order_id']]
            native_actor = self.native_personnel.get(p['signatory_id'], {})
            if not native_actor.get('practitioner_id'):
                raise MaterialGap('native practitioner mapping for trusted signatory is missing')
            request = copy.deepcopy(m['order_save_request'])
            for item, native_id in zip(request['adviceSaveList'], order['native_request_ids']):
                item.update(dbOpType='2', requestId=native_id)
            # Signer attribution must come from the actual response requesterId,
            # not from rewriting a practitioner ID in the hospital database.
            order['signatory_id'] = p['signatory_id']
            return self._result(self.client.save_advice(request, sign=True))
        if op == 'payment.prepare':
            observed = self.snapshot(case_id)
            order = observed['orders'][p['order_id']]
            rows = order['native_rows']
            charge_ids = [r.get('chargeItemId') for r in rows]
            if not charge_ids or any(i is None for i in charge_ids):
                raise NativeObservationGap('native chargeItemId is not exposed')
            response = self.client.prepare_payment({'patientId': m['patient_id'], 'encounterId': m['encounter_id'],
                'entererId': self.manifest['native_identity']['practitioner_id'], 'chargeItemIds': charge_ids})
            returned = data(response)
            bill = returned.get('paymentReconciliation') if isinstance(returned, dict) else None
            if not isinstance(bill, dict) or not bill.get('id') or bill.get('statusEnum') != 0:
                raise NativeObservationGap('precharge native payment identity/status missing')
            if str(bill.get('patientId')) != str(m['patient_id']) or str(bill.get('encounterId')) != str(m['encounter_id']):
                raise NativeObservationGap('precharge returned a payment for another case')
            if not equivalent(bill.get('tenderedAmount'), p['amount']):
                raise NativeObservationGap('native precharge amount differs from protocol amount')
            state['bills'][p['bill_id']] = {'patient_id': str(bill['patientId']), 'encounter_id': str(bill['encounterId']),
                'native_payment_id': str(bill['id']), 'native_payment_no': bill.get('paymentNo'),
                'amount': str(bill['tenderedAmount']), 'currency': p['currency'], 'status': 'unpaid',
                'charge_item_ids': charge_ids, 'native_create_response': bill,
                'native_create_evidence': response['evidence'], 'commit_attempted': False}
            return self._result(response)
        if op == 'payment.commit':
            bill = state['bills'][p['bill_id']]
            # Replays really reach the unchanged native payment service. Keep
            # the same native bill, charges and cash amount and observe effects.
            bill.update(commit_attempted=True, transaction_id=p['transaction_id'])
            response = self.client.commit_cash({'id': bill['native_payment_id'], 'encounterId': m['encounter_id'],
                'chargeItemIds': bill['charge_item_ids'], 'paymentDetails': [{'payEnum': 220100, 'amount': p['amount']}]})
            return self._result(response)
        return {'status': 'rejected', 'reason': 'unsupported_native_operation', 'enforcement_source': 'experiment_ledger'}
