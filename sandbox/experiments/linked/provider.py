"""Budgeted, journaled model transport. No network or secret reads at import."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from .json_boundary import LEGACY_POLICY as JSON_BOUNDARY_POLICY
from .provider_content import POLICY as CONTENT_BOUNDARY_POLICY, separate_final_content
from .provider_stream import receive as receive_stream, StreamProtocolError
from . import provider_profile

PRICES = {  # CNY / million tokens; conservative day rate, no cache discounts.
    'deepseek-v4.1-flash-1017': (2.0, 8.0),
    'deepseek-v4.1-flash': (2.0, 8.0),
    'deepseek-v4-flash': (2.0, 4.0),  # official MaaS day rate, checked 2026-11-07
    'glm-5.3': (8.0, 28.0),
    'glm-5.2': (8.0, 28.0),
    'glm-5.1': (8.0, 28.0),  # conservative upper tier incl. >=32K; official 2026-10-27 pricing
    'openpangu-2.0-flash': (0.8, 1.6),
    'openpangu-2.0-pro': (4.8, 17.6),  # conservative >=32K tier; official 2026-10-27
    'qwen3-32b': (2.0, 20.0),  # thinking-mode price; engineering only before scheduled retirement
    'qwen3-30b-a3b': (0.75, 7.5),  # official thinking-mode price checked 2026-11-07
    'kimi-k2.6': (6.5, 27.0),  # conservative day rate checked 2026-11-07
}
TOOLS = {'preconsult_ai', 'rx_ai', 'scribe'}
RESTRICTED = {'mimic_derived', 'hospital_derived', 'restricted'}
GENERATION_POLICY_V2 = {
    'role': {'thinking': False, 'max_completion_tokens': 8192, 'temperature': 0.2},
    'tools': {
        'deepseek': {'thinking': True, 'reasoning_effort': 'low', 'max_completion_tokens': 8192, 'temperature': 0.2},
        'glm': {'thinking': 'mandatory', 'reasoning_effort': 'low', 'max_completion_tokens': 8192, 'temperature': 0.2},
    },
}
GENERATION_POLICY_V3 = {**deepcopy(GENERATION_POLICY_V2), 'response_parsing':deepcopy(JSON_BOUNDARY_POLICY)}
GENERATION_POLICY_V4 = {**deepcopy(GENERATION_POLICY_V3),
    'role': {'thinking':True,'reasoning_effort':'low','max_completion_tokens':8192,'temperature':0.2}}
GENERATION_POLICY_V5 = deepcopy(GENERATION_POLICY_V4)
for _setting in [GENERATION_POLICY_V5['role'], *GENERATION_POLICY_V5['tools'].values()]:
    _setting['max_completion_tokens'] = 16384
    _setting['truncation_retry'] = {'max_retries':1,'max_completion_tokens':32768,
        'same_messages':True,'shares_max_http_attempts':True}
GENERATION_POLICY_V6 = {**deepcopy(GENERATION_POLICY_V5),
    'provider_content_boundary': deepcopy(CONTENT_BOUNDARY_POLICY)}
GENERATION_POLICY_V7 = deepcopy(GENERATION_POLICY_V6)
GENERATION_POLICY_V7['tools']['pangu'] = deepcopy(GENERATION_POLICY_V5['tools']['glm'])
GENERATION_POLICY_V7['tools']['pangu'].pop('reasoning_effort')
GENERATION_POLICY_V7['tools']['pangu']['thinking'] = True
GENERATION_POLICY_V8 = {**deepcopy(GENERATION_POLICY_V7),
    'model_parameter_overrides': {'glm-5.1': {'reasoning_effort': None}}}
GENERATION_POLICY_V9 = deepcopy(GENERATION_POLICY_V8)
GENERATION_POLICY_V9['supported_role_families'] = ['deepseek', 'glm', 'pangu']
for _model in ('openpangu-2.0-flash', 'openpangu-2.0-pro'):
    GENERATION_POLICY_V9['model_parameter_overrides'][_model] = {'reasoning_effort': None}
GENERATION_POLICY_V10 = deepcopy(GENERATION_POLICY_V9)
GENERATION_POLICY_V10['tools']['qwen'] = deepcopy(GENERATION_POLICY_V9['tools']['pangu'])
GENERATION_POLICY_V10['model_parameter_overrides']['qwen3-32b'] = {'reasoning_effort': None}
GENERATION_POLICY_V11 = deepcopy(GENERATION_POLICY_V10)
GENERATION_POLICY_V11['supported_role_families'].append('qwen')
GENERATION_POLICY_V11['model_parameter_overrides']['qwen3-30b-a3b'] = {'reasoning_effort': None}
GENERATION_POLICY_V12 = deepcopy(GENERATION_POLICY_V11)
GENERATION_POLICY_V12['supported_role_families'].append('kimi')
GENERATION_POLICY_V12['tools']['kimi'] = deepcopy(GENERATION_POLICY_V11['tools']['qwen'])
for _setting in [GENERATION_POLICY_V12['role'], GENERATION_POLICY_V12['tools']['kimi'], GENERATION_POLICY_V12['tools']['qwen']]:
    _setting.update(thinking=False,temperature=0.6)
    _setting.pop('reasoning_effort',None)
GENERATION_POLICY_V12['model_parameter_overrides']['kimi-k2.6'] = {
    'thinking':False,'temperature':0.6,'reasoning_effort':None,'completion_limit_field':'max_tokens'}
GENERATION_POLICY_V12['model_parameter_overrides']['qwen3-30b-a3b'].update(thinking=False,temperature=0.6)


def validate_generation_policy(config):
    if provider_profile.is_explicit(config):
        return provider_profile.validate(config)
    if config.get('generation_policy') not in (GENERATION_POLICY_V2, GENERATION_POLICY_V3, GENERATION_POLICY_V4, GENERATION_POLICY_V5, GENERATION_POLICY_V6, GENERATION_POLICY_V7, GENERATION_POLICY_V8, GENERATION_POLICY_V9, GENERATION_POLICY_V10, GENERATION_POLICY_V11, GENERATION_POLICY_V12):
        raise ValueError('an explicit fixed v2 through v12 generation_policy is required; settings are never silently substituted')
    return deepcopy(config['generation_policy'])


def generation_settings(config, role, model):
    if provider_profile.is_explicit(config):
        return provider_profile.settings(config, role, model, TOOLS)
    policy = validate_generation_policy(config)
    if model == 'qwen3-30b-a3b' and policy not in (GENERATION_POLICY_V11,GENERATION_POLICY_V12):
        raise ValueError('Qwen30B requires the explicitly versioned v11 generation policy')
    if model=='kimi-k2.6' and policy!=GENERATION_POLICY_V12:
        raise ValueError('Kimi requires the explicitly versioned v12 generation policy')
    overrides = deepcopy(policy.get('model_parameter_overrides',{}).get(model,{}))
    family = ('deepseek' if model.startswith('deepseek') else 'glm' if model.startswith('glm')
              else 'pangu' if model.startswith('openpangu-2.0-') else 'qwen' if model in {'qwen3-32b','qwen3-30b-a3b'}
              else 'kimi' if model=='kimi-k2.6' else None)
    if role == 'provider_probe':
        if family not in {'deepseek','glm','pangu','qwen','kimi'}:
            raise ValueError('unsupported recovery-probe model family')
        return {'thinking':False if family=='deepseek' else 'mandatory',
            'max_completion_tokens':1024,'temperature':0.2,
            **({'reasoning_effort':'low'} if family not in {'pangu','qwen','kimi'} else {}),
            'role_class':'infrastructure_probe','tool_family':family,'version':'provider_health_probe_v1',**overrides}
    if role in TOOLS:
        if family not in policy['tools']:
            raise ValueError('tool model family has no declared generation policy')
        result = {**policy['tools'][family], 'role_class': 'tool', 'tool_family': family}
        if 'response_parsing' in policy:result['response_parsing']=deepcopy(policy['response_parsing'])
        if 'provider_content_boundary' in policy:result['provider_content_boundary']=deepcopy(policy['provider_content_boundary'])
        result.update(overrides)
        return result
    declared_backbone=config.get('role_model')
    if ((declared_backbone is not None and model!=declared_backbone)
            or family not in policy.get('supported_role_families', ['deepseek', 'glm'])
            or (declared_backbone is None and family!='deepseek')):
        raise ValueError('simulated patient/staff role backbone must use the explicitly declared fixed model')
    result = {**policy['role'], 'role_class': 'simulated_role', 'tool_family': None}
    if 'response_parsing' in policy:result['response_parsing']=deepcopy(policy['response_parsing'])
    if 'provider_content_boundary' in policy:result['provider_content_boundary']=deepcopy(policy['provider_content_boundary'])
    result.update(overrides)
    return result


def configured_prices(config):
    if provider_profile.is_explicit(config):
        provider_profile.validate(config)
        return deepcopy(config['pricing_cny_per_million_tokens'])
    return deepcopy(PRICES)


class BudgetExceeded(RuntimeError):
    pass


class DataPolicyError(RuntimeError):
    pass


class ModelCallError(RuntimeError):
    def __init__(self, message, *, request_id=None):
        super().__init__(message)
        self.request_id = request_id


class ProviderUnavailable(ModelCallError):
    """Required model service unavailable; not proof of an account-wide outage."""


PROVIDER_FROZEN_CODE = 'ModelArts.81006'
PROVIDER_FROZEN_REASON = 'provider_resource_frozen_' + PROVIDER_FROZEN_CODE


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


class BudgetLedger:
    """SQLite transactions reserve worst-case fees across processes before calls.

    Unknown charges retain their full reservation. Restarting never resets cost.
    Amounts use integer micro-CNY. Actual charges are upper estimates without
    discounted cache/night prices; the vendor invoice remains authoritative.
    """
    def __init__(self, path, cap_cny=1000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cap = 0 if cap_cny is None else int(round(float(cap_cny) * 1_000_000))
        if cap_cny is not None and cap <= 0:
            raise ValueError('positive budget required')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),cap INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (cap,))
            if db.execute('SELECT cap FROM settings').fetchone()[0] != cap:
                raise ValueError('existing budget cap cannot be changed silently')
            db.execute('CREATE TABLE IF NOT EXISTS ledger_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO ledger_metadata VALUES(?,?)', ('ledger_id', uuid.uuid4().hex))
            db.execute('INSERT OR IGNORE INTO ledger_metadata VALUES(?,?)', ('halted', 'false'))
            db.execute('INSERT OR IGNORE INTO ledger_metadata VALUES(?,?)', ('halt_reason', ''))
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                request_id TEXT PRIMARY KEY, case_id TEXT, role TEXT, model TEXT,
                reserved INTEGER NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, created_at TEXT, usage_json TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS budget_authorizations (
                authorization_id TEXT PRIMARY KEY, previous_cap INTEGER NOT NULL,
                new_cap INTEGER NOT NULL, recorded_at TEXT NOT NULL, evidence_json TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS provider_interruptions (
                request_id TEXT PRIMARY KEY, error_code TEXT NOT NULL,
                recorded_at TEXT NOT NULL, previous_halt_reason TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS provider_recoveries (
                recovery_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS provider_dispatch (
                scope TEXT PRIMARY KEY, next_allowed REAL NOT NULL,
                minimum_interval REAL NOT NULL)''')
        os.chmod(self.path, 0o600)

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    def authorize_cap(self, cap_cny, *, authorization):
        """Explicit, audited user change; preserve every past call and charge."""
        if (not isinstance(authorization,dict) or authorization.get('authorized_by')!='user'
                or not all(isinstance(authorization.get(k),str) and authorization[k].strip()
                           for k in ('authorization_id','evidence','scope'))):
            raise ValueError('explicit user budget authorization evidence required')
        cap=0 if cap_cny is None else int(round(float(cap_cny)*1_000_000))
        if cap_cny is not None and cap<=0:raise ValueError('positive budget required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM calls WHERE status='reserved'").fetchone()[0]:
                raise ValueError('wait for in-flight reservations before changing budget policy')
            previous=db.execute('SELECT cap FROM settings').fetchone()[0]
            db.execute('INSERT INTO budget_authorizations VALUES(?,?,?,?,?)',(
                authorization['authorization_id'],previous,cap,datetime.now(timezone.utc).isoformat(),
                json.dumps(authorization,ensure_ascii=False)))
            db.execute('UPDATE settings SET cap=? WHERE id=1',(cap,))
            reason=db.execute("SELECT value FROM ledger_metadata WHERE key='halt_reason'").fetchone()[0]
            used=db.execute('SELECT COALESCE(SUM(reserved+charged),0) FROM calls').fetchone()[0]
            if reason=='reservation_exceeds_available_budget' and (cap==0 or cap>used):
                db.execute("UPDATE ledger_metadata SET value='false' WHERE key='halted'")
                db.execute("UPDATE ledger_metadata SET value='' WHERE key='halt_reason'")
        return self.summary()

    def reserve(self, request_id, amount, *, case_id, role, model, health_probe=False):
        if health_probe and (case_id != 'provider_health_probe' or role != 'provider_probe'):
            raise ValueError('recovery probes cannot contain clinical work')
        units = math.ceil(amount * 1_000_000)
        if not math.isfinite(amount) or units <= 0:
            raise ValueError('invalid cost reservation')
        denied = None
        provider_stopped = False
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            cap = db.execute('SELECT cap FROM settings').fetchone()[0]
            if db.execute("SELECT value FROM ledger_metadata WHERE key='halted'").fetchone()[0] == 'true':
                denied = 'budget ledger halted; no request sent'
                provider_stopped = db.execute("SELECT value FROM ledger_metadata WHERE key='halt_reason'").fetchone()[0] == PROVIDER_FROZEN_REASON
                if provider_stopped and health_probe:
                    denied = None
            used = db.execute('SELECT COALESCE(SUM(reserved+charged),0) FROM calls').fetchone()[0]
            if cap > 0 and used + units > cap:
                denied = 'API budget cannot cover the next complete request; no request sent'
                db.execute("UPDATE ledger_metadata SET value='true' WHERE key='halted'")
                db.execute("UPDATE ledger_metadata SET value='reservation_exceeds_available_budget' WHERE key='halt_reason'")
            if denied is None:
                verified = db.execute("SELECT value FROM ledger_metadata WHERE key='recovered_models'").fetchone()
                if verified and not health_probe and model not in json.loads(verified[0]):
                    raise ProviderUnavailable('model outside the verified recovery configuration; no request sent')
                db.execute('INSERT INTO calls VALUES(?,?,?,?,?,0,?,?,NULL)',
                           (request_id, case_id, role, model, units, 'reserved', datetime.now(timezone.utc).isoformat()))
        if denied:
            if provider_stopped:
                raise ProviderUnavailable('provider resource frozen; no request sent')
            raise BudgetExceeded(denied)

    def halt_provider(self, request_id, error_code):
        """Stop the current multi-model protocol when a required service freezes.

        This shared dispatch gate does not assert that other models on the same
        account are unavailable. Bounded nonclinical probes may check them.
        """
        if error_code != PROVIDER_FROZEN_CODE:
            raise ValueError('only the documented resource-frozen error halts all routes')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM calls WHERE request_id=?', (request_id,)).fetchone()
            if row is None or row[0] not in {'http_401','http_403'}:
                raise ValueError('provider halt requires the settled authentication response')
            previous = db.execute("SELECT value FROM ledger_metadata WHERE key='halt_reason'").fetchone()[0]
            db.execute('INSERT INTO provider_interruptions VALUES(?,?,?,?)',
                (request_id,error_code,datetime.now(timezone.utc).isoformat(),previous))
            # Never clear or conceal a previous accounting-reconciliation stop.
            if previous in {'',PROVIDER_FROZEN_REASON}:
                db.execute("UPDATE ledger_metadata SET value='true' WHERE key='halted'")
                db.execute("UPDATE ledger_metadata SET value=? WHERE key='halt_reason'",(PROVIDER_FROZEN_REASON,))

    def recover_provider(self, *, recovery_id, config_sha256, probes_by_model, evidence, config=None):
        """Resume a declared configuration after every model has a healthy probe.

        Preserve all failures, charges and the budget. This only clears a provider
        stop; accounting stops cannot be cleared by API availability checks.
        """
        if (not recovery_id or not isinstance(evidence, dict) or not evidence
                or len(config_sha256) != 64 or not probes_by_model):
            raise ValueError('recorded recovery and configuration evidence required')
        prices = configured_prices(config) if config is not None else PRICES
        if any(model not in prices for model in probes_by_model):
            raise ValueError('unpriced recovery model')
        if config is not None and set(probes_by_model) != {config['role_model'], *config['tool_models'].values()}:
            raise ValueError('probe every model selected in the explicit recovery configuration')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            reason = db.execute("SELECT value FROM ledger_metadata WHERE key='halt_reason'").fetchone()[0]
            if reason != PROVIDER_FROZEN_REASON:
                raise ValueError('only a provider-resource stop can be recovered by probes')
            if db.execute("SELECT COUNT(*) FROM calls WHERE status='reserved'").fetchone()[0]:
                raise ValueError('wait for in-flight requests before provider recovery')
            last = db.execute('SELECT MAX(recorded_at) FROM provider_interruptions').fetchone()[0]
            now = datetime.now(timezone.utc)
            for model, request_id in probes_by_model.items():
                row = db.execute('SELECT case_id,role,model,status,created_at FROM calls WHERE request_id=?',
                    (request_id,)).fetchone()
                if not row or row[:4] != ('provider_health_probe','provider_probe',model,'completed'):
                    raise ValueError('every configured model requires its own successful nonclinical probe')
                observed = datetime.fromisoformat(row[4])
                if (last and observed <= datetime.fromisoformat(last)) or not 0 <= (now-observed).total_seconds() <= 3600:
                    raise ValueError('recovery probes must be recent and after the last provider freeze')
            record = {'config_sha256':config_sha256,'probes_by_model':probes_by_model,
                'evidence':evidence,'prior_halt_reason':reason,'scope':'verified model configuration only'}
            if config is not None:
                record.update(endpoint=config['endpoint'],
                    pricing_cny_per_million_tokens={k: prices[k] for k in probes_by_model},
                    provider_profile_hash=hashlib.sha256(json.dumps(validate_generation_policy(config),
                        sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
            db.execute('INSERT INTO provider_recoveries VALUES(?,?,?)',
                (recovery_id,now.isoformat(),json.dumps(record,ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO ledger_metadata VALUES('recovered_models',?)",
                (json.dumps(sorted(probes_by_model)),))
            db.execute("UPDATE ledger_metadata SET value='false' WHERE key='halted'")
            db.execute("UPDATE ledger_metadata SET value='' WHERE key='halt_reason'")
        return self.summary()

    def dispatch_delay(self, *, scope, minimum_interval):
        """Atomically grant one start or return a delay, shared by all workers.

        Future slots are not prebooked: delayed workers must recheck, preventing
        several overdue slots from firing together after a process pause.
        """
        if not scope or not isinstance(minimum_interval,(float,int)) or not 0 < minimum_interval <= 60:
            raise ValueError('invalid shared API pacing interval')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT next_allowed,minimum_interval FROM provider_dispatch WHERE scope=?',(scope,)).fetchone()
            now=time.time()
            interval=max(minimum_interval,row[1] if row else minimum_interval)
            if row and row[0]>now:return row[0]-now
            db.execute('INSERT OR REPLACE INTO provider_dispatch VALUES(?,?,?)',(scope,now+interval,interval))
        return 0.0

    def settle(self, request_id, *, amount=None, status, usage=None):
        over_reservation = False
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT reserved,status FROM calls WHERE request_id=?', (request_id,)).fetchone()
            if row is None or row[1] != 'reserved':
                raise ValueError('unknown or already settled request')
            reserved = row[0]
            charged = reserved if amount is None else math.ceil(amount * 1_000_000)
            if charged < 0:
                raise ValueError('negative cost')
            db.execute('UPDATE calls SET reserved=0,charged=?,status=?,usage_json=? WHERE request_id=?',
                       (charged, status, json.dumps(usage), request_id))
            if charged > reserved:
                over_reservation = True
                db.execute("UPDATE ledger_metadata SET value='true' WHERE key='halted'")
                db.execute("UPDATE ledger_metadata SET value='actual_charge_exceeds_reservation' WHERE key='halt_reason'")
        # Persist the actual charge and halt before raising. Raising inside the
        # transaction would roll both changes back and conceal the overage.
        if over_reservation:
            raise BudgetExceeded('provider usage exceeded reserved upper bound; stop for investigation')

    def identity(self):
        with self._db() as db:
            ledger_id = db.execute("SELECT value FROM ledger_metadata WHERE key='ledger_id'").fetchone()[0]
        return {'ledger_id': ledger_id, 'resolved_path': str(self.path.resolve())}

    def summary(self):
        with self._db() as db:
            cap = db.execute('SELECT cap FROM settings').fetchone()[0]
            charged, reserved, count = db.execute('SELECT COALESCE(SUM(charged),0),COALESCE(SUM(reserved),0),COUNT(*) FROM calls').fetchone()
            statuses = dict(db.execute('SELECT status,COUNT(*) FROM calls GROUP BY status'))
            halted = db.execute("SELECT value FROM ledger_metadata WHERE key='halted'").fetchone()[0] == 'true'
            halt_reason = db.execute("SELECT value FROM ledger_metadata WHERE key='halt_reason'").fetchone()[0]
        return {'cap_cny': cap / 1e6 if cap else None, 'accounted_upper_cny': charged / 1e6,
                'budget_limit_mode':'capped' if cap else 'uncapped_user_authorized',
                'reserved_cny': reserved / 1e6, 'remaining_cny': (cap-charged-reserved)/1e6 if cap else None,
                'requests': count, 'statuses': statuses, 'new_requests_stopped': halted,
                'halt_reason': halt_reason or None,
                'provider_unavailable': halted and halt_reason == PROVIDER_FROZEN_REASON,
                'halted_for_reconciliation': halted and halt_reason == 'actual_charge_exceeds_reservation',
                'billing_basis': 'conservative_no_discount_upper_estimate'}


def check_data_policy(data_kind, policy, endpoint, *, formal=False):
    if data_kind == 'synthetic_independent':
        return {'authorization_basis': 'independent_synthetic_material', 'data_kind': data_kind,
                'independent_provider_verification': 'not_required_for_this_synthetic_input'}
    if data_kind not in RESTRICTED:
        raise DataPolicyError('unrecognized data provenance; external inference denied')
    mode = policy.get('mode', 'independently_verified')
    if mode == 'user_attested':
        # A user authorization is preserved as its own evidentiary basis. It is
        # not converted into provider retention/training/review verification.
        required_text = ('authorization_id', 'confirmed_at', 'evidence', 'endpoint')
        if not all(isinstance(policy.get(k), str) and policy[k].strip() for k in required_text):
            raise DataPolicyError('user attestation requires an authorization ID, timestamp, endpoint and evidence')
        if policy['endpoint'] != endpoint:
            raise DataPolicyError('user attestation does not cover this endpoint')
        allowed = policy.get('data_kinds')
        if not isinstance(allowed, list) or allowed != ['mimic_derived'] or data_kind not in allowed:
            raise DataPolicyError('this user attestation covers only MIMIC-derived material')
        if policy.get('dataset_scope') != 'local_mimic_iv_2.2_and_derivatives':
            raise DataPolicyError('user attestation must name the authorized local MIMIC-IV 2.2 scope')
        try:
            confirmed = datetime.fromisoformat(policy['confirmed_at'].replace('Z', '+00:00'))
        except ValueError as exc:
            raise DataPolicyError('invalid attestation timestamp') from exc
        if confirmed.tzinfo is None:
            raise DataPolicyError('attestation timestamp must include a timezone')
        provenance = {'authorization_basis': 'user_attested', 'data_kind': data_kind,
                      'authorization_id': policy['authorization_id'], 'confirmed_at': policy['confirmed_at'],
                      'endpoint': endpoint, 'data_kinds': allowed,
                      'dataset_scope': policy['dataset_scope'], 'evidence': policy['evidence'],
                      'independent_provider_verification': 'not_established_by_user_attestation'}
        processing = policy.get('processing_conditions')
        if processing is None and not formal:
            return provenance
        checks = ('zero_retention', 'no_training', 'no_human_review')
        if (not isinstance(processing, dict) or processing.get('scope') != 'endpoint_and_upstream'
                or not all(processing.get(key) is True for key in checks)):
            raise DataPolicyError('formal MIMIC use requires an explicit attestation of zero retention, '
                                  'no training and no human review across the endpoint and its upstream')
        provenance['attested_processing'] = {key: processing[key] for key in ('scope', *checks)}
        return provenance
    if mode != 'independently_verified':
        raise DataPolicyError('unknown data policy mode')
    checks = ('authorized_data_access', 'verified_no_retention', 'no_training',
              'no_human_review', 'cache_policy_verified')
    if not all(policy.get(k) is True for k in checks):
        raise DataPolicyError('restricted data API requirements not verified')
    if policy.get('endpoint') != endpoint or not policy.get('evidence') or not policy.get('verified_at'):
        raise DataPolicyError('data policy must name this endpoint and verification evidence')
    return {'authorization_basis': 'independently_verified', 'data_kind': data_kind,
            'endpoint': endpoint, 'verified_at': policy['verified_at'], 'evidence': policy['evidence'],
            'verified_checks': {key: policy[key] for key in checks},
            'independent_provider_verification': 'recorded_evidence_supplied'}


class BudgetedTransport:
    def __init__(self, config, ledger, evidence_dir, *, key=None, http_session=None):
        self.config = deepcopy(config)
        self.ledger = ledger
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.endpoint = config['endpoint'].rstrip('/')
        parsed = urlsplit(self.endpoint)
        self.api_protocol = config.get('api_protocol', 'openai_compatible')
        if self.api_protocol == provider_profile.PROTOCOL:
            provider_profile.validate(config)
        else:
            if parsed.scheme != 'https' or parsed.hostname != 'api.modelarts-maas.com' or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('this credential is restricted to the authorized HTTPS MaaS endpoint')
            expected_path = {'openai_compatible':'/openai/v1', 'maas_standard_v2':'/v2'}.get(self.api_protocol)
            if expected_path is None or parsed.path != expected_path:
                raise ValueError('endpoint path and explicitly selected MaaS API protocol disagree')
        self.prices = configured_prices(config)
        self.rounding_margin = (float(config.get('request_rounding_margin_cny', 0))
                                if provider_profile.is_explicit(config) else 0)
        self.key = key if key is not None else Path(config['key_file']).read_text().strip()
        if not self.key:
            raise ValueError('missing credential')
        self.http = http_session or requests.Session()
        self.timeout = float(config.get('timeout_seconds', 120))
        self.stream = config.get('response_transport', 'nonstream') == 'sse'
        if config.get('response_transport', 'nonstream') not in {'nonstream','sse'}:
            raise ValueError('unsupported response transport')
        self.stream_max_duration = config.get('stream_max_duration_seconds',900)
        if (self.stream and (self.api_protocol not in {'maas_standard_v2', provider_profile.PROTOCOL}
                or not isinstance(self.stream_max_duration,(int,float))
                or not math.isfinite(self.stream_max_duration) or not 60<=self.stream_max_duration<=1800)):
            raise ValueError('SSE requires a supported protocol and an explicit bounded generation duration')
        self.connect_timeout = config.get('connect_timeout_seconds')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('positive finite read timeout required')
        if self.connect_timeout is not None and (
                not isinstance(self.connect_timeout, (float, int))
                or not math.isfinite(self.connect_timeout) or not 0 < self.connect_timeout <= 60):
            raise ValueError('connect timeout must be between zero and 60 seconds')
        self.network_route = deepcopy(config.get('network_route', {'mode':'system_default'}))
        if self.network_route == {'mode':'system_default'}:
            pass  # Preserve the transport semantics of older frozen configurations.
        elif self.network_route.get('mode') == 'explicit_loopback_proxy':
            proxy = urlsplit(self.network_route.get('url', ''))
            if (proxy.scheme != 'http' or proxy.hostname != '127.0.0.1'
                    or not proxy.port or proxy.path or proxy.username or proxy.password
                    or proxy.query or proxy.fragment):
                raise ValueError('explicit route must be a credential-free loopback HTTP listener')
            self.http.trust_env = False
            self.http.proxies = {'http':self.network_route['url'], 'https':self.network_route['url']}
        elif self.network_route.get('mode') == 'macos_bound_interface':
            from .network_route import configure
            configure(self.http,self.endpoint,self.network_route)
        else:
            raise ValueError('unsupported explicit network route')
        self.policy = config.get('data_policy', {})
        self.minimum_interval = config.get('minimum_request_interval_seconds',0)
        if (not isinstance(self.minimum_interval,(int,float)) or not math.isfinite(self.minimum_interval)
                or not 0 <= self.minimum_interval <= 60):
            raise ValueError('invalid shared API pacing interval')
        self.dispatch_deadline = None
        if config.get('dispatch_deadline_utc') is not None:
            try:
                self.dispatch_deadline = datetime.fromisoformat(config['dispatch_deadline_utc'])
                if self.dispatch_deadline.utcoffset() is None:
                    raise ValueError('timezone required')
            except (ValueError, TypeError):
                raise ValueError('dispatch deadline requires an explicit timezone-aware ISO timestamp') from None

    def _check_dispatch_deadline(self):
        if self.dispatch_deadline is not None and datetime.now(timezone.utc) >= self.dispatch_deadline:
            raise ProviderUnavailable('declared model dispatch window has closed; no request sent')

    def complete(self, messages, *, model, case_id, role, data_kind,
                 config_id=None, run_id=None, max_completion_tokens=None, provider_probe=False):
        if provider_probe and (data_kind != 'synthetic_independent' or case_id != 'provider_health_probe'
                or role != 'provider_probe' or messages != [{'role':'user','content':'Return the word OK.'}]
                or int(self.config.get('max_attempts',3)) != 1):
            raise ValueError('provider probe requires the fixed nonclinical message and one HTTP attempt')
        policy_provenance = check_data_policy(data_kind, self.policy, self.endpoint)
        if model not in self.prices:
            raise ValueError('unpriced model cannot make a paid call')
        generation = generation_settings(self.config, role, model)
        limit = generation['max_completion_tokens']
        if max_completion_tokens is not None and max_completion_tokens != limit:
            raise ValueError('per-call completion limit cannot override the frozen generation policy')
        if not isinstance(limit, int) or not 1 <= limit <= 32768:
            raise ValueError('invalid total completion limit')
        limit_field=generation.get('completion_limit_field','max_completion_tokens')
        if limit_field not in {'max_tokens','max_completion_tokens'}:
            raise ValueError('unsupported completion-limit field')
        if (limit_field=='max_tokens' and generation.get('thinking') is not False
                and not generation.get('completion_limit_includes_reasoning')):
            raise ValueError('content-only max_tokens cannot bound a reasoning-enabled call')
        body = {'model': model, 'messages': deepcopy(messages), limit_field: limit, 'stream': self.stream}
        if generation['temperature'] is not None:
            body['temperature'] = generation['temperature']
        if self.api_protocol == provider_profile.PROTOCOL:
            body.update(deepcopy(generation.get('request_parameters', {})))
        if self.stream:body['stream_options']={'include_usage':True}
        if self.api_protocol == 'maas_standard_v2':
            body['thinking'] = {'type': 'disabled' if generation['thinking'] is False else 'enabled'}
        elif self.api_protocol != provider_profile.PROTOCOL and model.startswith('deepseek'):
            body['chat_template_kwargs'] = {'thinking': generation['thinking']}
        if generation.get('reasoning_effort') is not None:
            body['reasoning_effort'] = generation['reasoning_effort']
        # UTF-8 byte length plus framing allowance conservatively bounds tokenized
        # text for these byte-level vocabularies. Never assume cache discounts.
        prompt_upper = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 1024
        input_rate, output_rate = self.prices[model]
        attempts = int(self.config.get('max_attempts', 3))
        truncation_policy = generation.get('truncation_retry') or {}
        truncation_retries = 0
        effective_limit = limit
        base_body = deepcopy(body)
        for attempt in range(1, attempts+1):
            self._check_dispatch_deadline()
            queued=time.monotonic()
            if self.minimum_interval:
                while True:
                    self._check_dispatch_deadline()
                    delay=self.ledger.dispatch_delay(scope='maas_shared_account',minimum_interval=self.minimum_interval)
                    if not delay:break
                    time.sleep(min(delay,30))
            self._check_dispatch_deadline()
            queue_seconds=time.monotonic()-queued
            body = {**deepcopy(base_body), limit_field:effective_limit}
            reserve = max(0.000001, (prompt_upper*input_rate + effective_limit*output_rate) / 1e6 + self.rounding_margin)
            request_id = uuid.uuid4().hex
            self.ledger.reserve(request_id, reserve, case_id=case_id, role=role, model=model,
                health_probe=provider_probe)
            record = {'request_id': request_id, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                      'case_id': case_id, 'run_id': run_id, 'config_id': config_id, 'role': role,
                      'model': model, 'data_kind': data_kind, 'endpoint': self.endpoint,
                      'api_protocol': self.api_protocol,
                      'response_transport':'sse' if self.stream else 'nonstream',
                      'network_route':deepcopy(self.network_route),
                      'read_timeout_seconds':self.timeout,
                      'connect_timeout_seconds':self.connect_timeout,
                      'data_policy_provenance': policy_provenance,
                      'generation_policy': generation,
                      'attempt': attempt, 'request_body': deepcopy(body), 'reserved_upper_cny': reserve,
                      'request_rounding_margin_cny': self.rounding_margin,
                      'effective_max_completion_tokens':effective_limit,
                      'completion_limit_field':limit_field,
                      'truncation_retry_index':truncation_retries,
                      'api_dispatch_wait_seconds':queue_seconds,
                      'minimum_request_interval_seconds':self.minimum_interval}
            if self.dispatch_deadline is not None:
                record['dispatch_deadline_utc'] = self.dispatch_deadline.isoformat()
            if provider_probe:
                record['provider_health_probe'] = True
            started = time.monotonic()
            value, response, settled, retry = None, None, False, False
            try:
                response = self.http.post(self.endpoint+'/chat/completions', json=body,
                    headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'},
                    timeout=(self.connect_timeout,self.timeout) if self.connect_timeout is not None else self.timeout,
                    verify=True, allow_redirects=False, **({'stream':True} if self.stream else {}))
                record['http_status'] = response.status_code
                if self.stream and response.status_code==200:
                    value=receive_stream(response,model=model,started=started,
                        max_duration=self.stream_max_duration,record=record)
                else:
                    try:
                        value = response.json()
                    except ValueError:
                        value = {}
                    if self.stream:response.close()
                if isinstance(value, dict):
                    record['response_body'] = value
                if response.status_code != 200:
                    retry = response.status_code in (429, 500, 502, 503, 504)
                    # Authentication/validation/429 responses do not generate tokens.
                    known_free = response.status_code in (400, 401, 403, 404, 422, 429)
                    self.ledger.settle(request_id, amount=0 if known_free else None,
                                       status='http_'+str(response.status_code))
                    settled = True
                    error = value.get('error',value) if isinstance(value,dict) else None
                    if isinstance(error,dict) and isinstance(error.get('code'),str):
                        record['provider_error_code'] = error['code']
                    if (response.status_code in (401,403) and isinstance(error,dict)
                            and error.get('code') == PROVIDER_FROZEN_CODE):
                        record.update(provider_error_code=PROVIDER_FROZEN_CODE,
                            failure_reason_code='provider_resource_frozen')
                        self.ledger.halt_provider(request_id,PROVIDER_FROZEN_CODE)
                        raise ProviderUnavailable('provider resource frozen; request_id='+request_id,
                            request_id=request_id)
                    raise ModelCallError('HTTP '+str(response.status_code))
                if not isinstance(value, dict):
                    raise ModelCallError('invalid provider envelope')
                usage = value.get('usage')
                if not isinstance(usage, dict) or not all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ('prompt_tokens','completion_tokens')):
                    raise ModelCallError('missing verifiable token usage')
                charged = (usage['prompt_tokens']*input_rate + usage['completion_tokens']*output_rate) / 1e6 + self.rounding_margin
                record.update(usage=usage, accounted_upper_cny=charged, returned_model=value.get('model'))
                self.ledger.settle(request_id, amount=charged, status='completed', usage=usage)
                settled = True
                if self.api_protocol == provider_profile.PROTOCOL and value.get('model') != model:
                    record['generation_contract_error'] = 'returned_model_differs_from_frozen_identity'
                    raise ModelCallError('returned model differs from the explicitly configured model')
                choice = value['choices'][0]
                record['finish_reason'] = choice.get('finish_reason')
                # An endpoint can accept a parameter while silently ignoring it.
                # Preserve billed evidence, but do not feed such a response into
                # a role whose frozen protocol explicitly disables reasoning.
                reasoning_tokens = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                unexpected_reasoning = generation.get('thinking') is False and (
                    bool(choice.get('message', {}).get('reasoning_content')) or
                    (isinstance(reasoning_tokens, (int, float)) and reasoning_tokens > 0))
                if unexpected_reasoning:
                    record['generation_contract_error'] = 'reasoning_returned_when_disabled'
                    raise ModelCallError('provider did not honor disabled reasoning')
                if choice.get('finish_reason') == 'length':
                    record['failure_reason_code'] = 'completion_length_limit'
                    # No partial response enters a clinical role. A single
                    # predeclared larger allowance may repeat the identical
                    # inference request, sharing the existing HTTP-attempt cap.
                    # Every attempt is independently reserved and billed first.
                    if truncation_retries < truncation_policy.get('max_retries',0) and attempt < attempts:
                        truncation_retries += 1
                        effective_limit = truncation_policy['max_completion_tokens']
                        retry = True
                        record['retry_reason'] = 'declared_same_input_completion_limit_increase'
                    raise ModelCallError('completion truncated; not a valid task response')
                if not isinstance(choice.get('message', {}).get('content'), str):
                    raise ModelCallError('missing assistant content')
                if generation.get('provider_content_boundary') is not None:
                    try:
                        final, boundary = separate_final_content(choice['message']['content'],
                            model=model, finish_reason=choice.get('finish_reason'),
                            policy=generation['provider_content_boundary'])
                    except ValueError:
                        record['failure_reason_code'] = 'invalid_provider_content_boundary'
                        raise ModelCallError('provider final channel boundary is invalid') from None
                    record['content_boundary'] = boundary
                    # The raw provider envelope above remains unchanged in the
                    # private archive; only the final channel reaches CAMEL.
                    value = deepcopy(value)
                    value['choices'][0]['message']['content'] = final
                    value['choices'][0]['message'].pop('reasoning_content', None)
                record['outcome'] = 'completed'
                return value, request_id
            except KeyboardInterrupt:
                record.update(outcome='interrupted', error_type='KeyboardInterrupt', retry_planned=False)
                raise
            except ProviderUnavailable:
                record.update(outcome='stopped_provider',error_type='ProviderUnavailable',retry_planned=False)
                raise
            except BudgetExceeded:
                record.update(outcome='stopped_budget', error_type='BudgetExceeded', retry_planned=False)
                raise
            except (requests.RequestException, ModelCallError, StreamProtocolError, KeyError, IndexError, TypeError) as exc:
                if not settled:
                    partial_usage=(record.get('partial_response_body') or {}).get('usage')
                    if isinstance(partial_usage,dict) and all(isinstance(partial_usage.get(k),int) and partial_usage[k]>=0
                            for k in ('prompt_tokens','completion_tokens')):
                        amount=(partial_usage['prompt_tokens']*input_rate+partial_usage['completion_tokens']*output_rate)/1e6+self.rounding_margin
                        record.update(usage=partial_usage,accounted_upper_cny=amount)
                        self.ledger.settle(request_id,amount=amount,status='completed',usage=partial_usage)
                    else:
                        self.ledger.settle(request_id, amount=None, status='unknown_charge')
                    settled = True
                if isinstance(exc,StreamProtocolError):
                    record['failure_reason_code']='invalid_or_incomplete_sse_response'
                if isinstance(exc, requests.RequestException):
                    retry = True
                    record.setdefault('failure_reason_code', (
                        'connect_timeout' if isinstance(exc, requests.ConnectTimeout) else
                        'read_timeout' if isinstance(exc, requests.ReadTimeout) else
                        'tls_connection_error' if isinstance(exc, requests.exceptions.SSLError) else
                        'proxy_connection_error' if isinstance(exc, requests.exceptions.ProxyError) else
                        'http_transport_error'))
                    # SSL/proxy wrappers can contain a lower-level timeout. Only
                    # retain its classification, never exception text or headers.
                    record['transport_timeout_indicated'] = isinstance(exc, requests.Timeout) or 'timed out' in str(exc).lower()
                record['outcome'] = 'failed'
                record['error_type'] = type(exc).__name__
                # Never archive exception messages/headers that might echo a key.
                record['retry_planned'] = retry and attempt < attempts
                if not record['retry_planned']:
                    raise ModelCallError('model call failed; request_id='+request_id,request_id=request_id) from None
            finally:
                record['elapsed_seconds'] = time.monotonic()-started
                # Some gateways echo submitted headers in errors. Redact the exact
                # credential before writing any returned content.
                clean = json.loads(json.dumps(record, ensure_ascii=False).replace(self.key, '[REDACTED]'))
                private_json(self.evidence_dir/(request_id+'.json'), clean)
            time.sleep(min(2**attempt, 8))
        raise ModelCallError('attempt limit reached')
