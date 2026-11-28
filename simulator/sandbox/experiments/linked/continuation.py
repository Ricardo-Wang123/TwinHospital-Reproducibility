"""Durable continuation of one encounter after a provider interruption."""
from copy import deepcopy
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from .contracts import digest
from .json_boundary import DuplicateJSONKeyError
from .native_his import NativeTransportError
from .provider import ModelCallError
from .response_contracts import ResponseBoundaryError

POLICY = {'version': 'encounter_continuation_v2',
    'retry_unit': 'same_run_same_encounter_same_condition',
    'dispatch': 'retryable_provider_and_registration_interruptions_go_to_queue_tail',
    'completed_effects': 'reconstruct_from_recorded_responses_without_repeating_requests',
    'uncertain_business_effects': 'require_reconciliation_before_replay',
    'completion': 'verified_payment_not_queue_removal'}


class EncounterDeferred(RuntimeError):
    def __init__(self, issue):
        super().__init__('Encounter is waiting for retry')
        self.issue = deepcopy(issue)


class EncounterContinuation:
    def __init__(self, path, identity):
        self.path = Path(path)
        self.resuming = self.path.exists()
        self.data = (json.loads(self.path.read_text()) if self.resuming else
            {'policy': POLICY, 'identity': deepcopy(identity), 'entries': [], 'events': [],
             'interruptions': [], 'segments': 0,
             'started_at': datetime.now(timezone.utc).isoformat(), 'monotonic_origin': time.monotonic()})
        if self.data['identity'] != identity or self.data['policy'] != POLICY:
            raise ValueError('Continuation requires the unchanged case, condition, models and implementation')
        if self.data.get('inflight') and self.data['inflight'].get('operation') != 'registration.create':
            raise ValueError('Unresolved external request requires reconciliation before continuation')
        self.clock_origin = self.data.get('monotonic_origin')
        if self.clock_origin is None:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(self.data['started_at'])
            self.clock_origin = time.monotonic() - age.total_seconds()
        previous_elapsed = self.data['events'][-1]['elapsed_seconds'] if self.data['events'] else 0
        if time.monotonic() - self.clock_origin < previous_elapsed:
            raise ValueError('The saved encounter clock cannot continue from its last recorded event')
        self.position = 0
        self.event_position = 0
        self.last_entry = None
        self.data['segments'] += 1
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def invoke(self, kind, request, function, metadata=None):
        self.last_entry = None
        request_hash = digest(request)
        if self.position < len(self.data['entries']):
            entry = self.data['entries'][self.position]
            if (entry['kind'], entry['request_hash']) != (kind, request_hash):
                raise ValueError('Continuation diverged from recorded inputs; no new request dispatched')
            self.position += 1
            self.last_entry = {**deepcopy(entry), 'replayed': True}
            if 'error' in entry:
                self.raise_recorded(entry['error'])
            return deepcopy(entry['value'])
        registration = kind == 'business' and request.get('operation') == 'registration.create'
        inflight = {'kind': kind, 'request_hash': request_hash}
        if registration:
            inflight['operation'] = 'registration.create'
        if self.data.get('inflight') and self.data['inflight'] != inflight:
            raise ValueError('The resumed request differs from the registered in-flight operation')
        self.data['inflight'] = inflight
        self.save()
        started = time.monotonic()
        entry = {'kind': kind, 'request_hash': request_hash,
                 'execution_segment': self.data['segments'],
                 'started_at': datetime.now(timezone.utc).isoformat()}
        try:
            value = function()
            entry['value'] = deepcopy(value)
        except (json.JSONDecodeError, DuplicateJSONKeyError, ResponseBoundaryError) as exc:
            entry['error'] = self.record_error(exc)
            raise
        except ModelCallError as exc:
            self.data.pop('inflight', None)
            if kind != 'role' or not exc.retryable:
                self.save()
                raise
            issue = {'number': len(self.data['interruptions'])+1,
                'occurred_at': datetime.now(timezone.utc).isoformat(),
                'request_id': exc.request_id, 'reason_code': exc.reason_code,
                'entry_position': self.position, 'request_hash': request_hash,
                'status': 'waiting_retry', 'event_class': 'model_execution',
                'owner': 'dispatcher', 'resolved_at': None}
            self.data['interruptions'].append(issue)
            self.save()
            raise EncounterDeferred(issue) from None
        except NativeTransportError:
            if not registration:
                raise
            issue = {'number': len(self.data['interruptions']) + 1,
                     'occurred_at': datetime.now(timezone.utc).isoformat(),
                     'request_id': request['context']['action_request_id'],
                     'reason_code': 'registration_readback_pending',
                     'entry_position': self.position, 'request_hash': request_hash,
                     'status': 'waiting_retry', 'event_class': 'model_execution',
                     'owner': 'dispatcher', 'resolved_at': None}
            self.data['interruptions'].append(issue)
            self.save()
            raise EncounterDeferred(issue) from None
        except BaseException:
            if kind != 'business':
                self.data.pop('inflight', None)
                self.save()
            raise
        finally:
            if 'value' in entry or 'error' in entry:
                entry['elapsed_seconds'] = time.monotonic()-started
                entry['metadata'] = deepcopy(metadata() if metadata else {})
                self.data.pop('inflight', None)
                self.data['entries'].append(entry)
                self.position += 1
                self.last_entry = {**deepcopy(entry), 'replayed': False}
                for issue in self.data['interruptions']:
                    if issue['entry_position'] == self.position-1 and issue['resolved_at'] is None:
                        issue.update(status='response_recovered', resolved_at=datetime.now(timezone.utc).isoformat(),
                                     recovery_request_id=entry['metadata'].get('last_request_id'),
                                     recovery_segment=entry['execution_segment'])
                self.save()
        return deepcopy(value)

    def observe_event(self, row):
        transient = row['event'] in {'provider_retry_queued', 'native_registration_retry_queued', 'run_failed'} or (
            row['event'] == 'role_call' and row.get('status') == 'waiting_retry')
        if transient:
            return row, False
        if self.event_position < len(self.data['events']):
            old = self.data['events'][self.event_position]
            if old['event'] != row['event']:
                raise ValueError('Continuation event order changed; unchanged implementation required')
            self.event_position += 1
            return deepcopy(old), True
        self.data['events'].append(deepcopy(row))
        self.event_position += 1
        self.save()
        return row, False

    @staticmethod
    def record_error(exc):
        row = {'type': type(exc).__name__, 'parse_metadata': deepcopy(getattr(exc, 'parse_metadata', None))}
        if isinstance(exc, json.JSONDecodeError):
            row.update(msg=exc.msg, doc=exc.doc, pos=exc.pos)
        elif isinstance(exc, DuplicateJSONKeyError):
            row['doc'] = exc.doc
        else:
            row.update(response=deepcopy(exc.response), reason=exc.boundary_reason)
        return row

    @staticmethod
    def raise_recorded(row):
        if row['type'] == 'JSONDecodeError':
            exc = json.JSONDecodeError(row['msg'], row['doc'], row['pos'])
        elif row['type'] == 'DuplicateJSONKeyError':
            exc = DuplicateJSONKeyError(row['doc'])
        else:
            exc = ResponseBoundaryError(row['response'], row['reason'])
        exc.parse_metadata = deepcopy(row.get('parse_metadata'))
        raise exc

    def factory(self, original):
        continuation = self
        class Factory:
            def session(self, scope, role, model_family=None):
                delegate = original.session(scope, role, model_family)
                class Session:
                    def __getattr__(self, name):
                        return getattr(delegate, name)
                    def respond(self, observation):
                        def metadata():
                            return {key: deepcopy(getattr(delegate, key, None)) for key in
                                ('last_request_id', 'last_usage', 'last_parse_metadata', 'model_name', 'memory_policy')}
                        for key, value in metadata().items():
                            setattr(self, key, deepcopy(value))
                        self.replayed = False
                        self.observed_seconds = None
                        try:
                            return continuation.invoke('role', {'scope': scope, 'role': role,
                                'model_family': model_family, 'observation': observation},
                                lambda: delegate.respond(observation), metadata)
                        finally:
                            entry = continuation.last_entry
                            if entry and entry['kind'] == 'role':
                                for key, value in entry.get('metadata', {}).items():
                                    setattr(self, key, deepcopy(value))
                                self.replayed = entry['replayed']
                                self.observed_seconds = entry['elapsed_seconds']
                                self.observed_started_at = entry['started_at']
                                self.origin_segment = entry['execution_segment']
                            else:
                                for key, value in metadata().items():
                                    setattr(self, key, deepcopy(value))
                return Session()
        return Factory()

    def backend(self, original):
        continuation = self
        class Backend:
            def __getattr__(self, name):
                return getattr(original, name)
            def execute(self, operation, payload, context):
                return continuation.invoke('business', {'operation': operation, 'payload': payload, 'context': context},
                    lambda: original.execute(operation, payload, context),
                    lambda: {'last_request_id': context.get('action_request_id')})
        return Backend()

    def timestamp(self, key):
        return self.invoke('timestamp', key, lambda: datetime.now(timezone.utc).isoformat())


def run_retry_queue(units, execute, *, workers=1, stopped=lambda: False,
                    on_requeue=lambda unit, dispatch, delay: None, retry_seconds=30):
    """Release a worker on interruption; dispatch other units before retrying."""
    ready = deque((unit, 0, 1) for unit in units)
    active = {}
    completed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while ready or active:
            stopping = stopped()
            now = time.monotonic()
            if not stopping:
                for _ in range(len(ready)):
                    if len(active) >= workers:
                        break
                    unit, due, dispatch = ready.popleft()
                    if due > now:
                        ready.append((unit, due, dispatch))
                        continue
                    active[pool.submit(execute, unit, dispatch)] = (unit, dispatch)
            if not active:
                if stopping:
                    break
                delay = max(0, min(item[1] for item in ready)-time.monotonic())
                time.sleep(min(delay, 1))
                continue
            done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                unit, dispatch = active.pop(future)
                result = future.result()
                if result.get('status') == 'waiting_retry':
                    delay = min(retry_seconds * 2**min(dispatch-1, 3), 300)
                    on_requeue(unit, dispatch, delay)
                    ready.append((unit, time.monotonic()+delay, dispatch+1))
                else:
                    completed.append((unit, result))
    return completed, [unit for unit, _, _ in ready]
