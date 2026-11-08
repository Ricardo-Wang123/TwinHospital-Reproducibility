"""Native OpenHIS HTTP calls and evidence, without /sandbox/v1 or SQL.

Writes require an explicit allowlist of dedicated experimental objects. HTTP
business failures are returned for experiment classification; transport failures
are recorded then raised. There are no automatic retries of clinical writes.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


SECRET = re.compile(r'password|passwd|token|authorization|cookie|secret|api.?key|service.?key|prv.?key|pri(?:vate)?.?key|app.?key|access.?key|sign(?:ing)?.?key|encrypt(?:ion)?.?key', re.I)
OPTION_SAFE_FLAGS={'yb_switch','invoiceswitch','invoice_switch','invoice_enabled','invoiceenable',
    'invoice_enable','einvoice_switch','auto_invoice','autoinvoice'}
OPTION_SAFE_IDS={'tenantid','tenant_id','organizationid','organization_id','orgid','org_id','practitionerid','practitioner_id'}


def redact_option_json(value):
    was_string=isinstance(value,str)
    try: parsed=json.loads(value) if was_string else value
    except (ValueError,TypeError): return '[REDACTED_OPTION_JSON]'
    if not isinstance(parsed,dict): return '[REDACTED_OPTION_JSON]'
    safe={}
    for key,item in parsed.items():
        name=str(key).casefold()
        flag=name in OPTION_SAFE_FLAGS and (item is None or isinstance(item,bool) or
            str(item).casefold() in ('0','1','true','false','on','off','enabled','disabled'))
        identity=name in OPTION_SAFE_IDS and (item is None or
            (not isinstance(item,bool) and str(item).isdecimal() and len(str(item))<=24))
        safe[key]=item if flag or identity else '[REDACTED_OPTION]'
    return json.dumps(safe,ensure_ascii=False) if was_string else safe


def redact(value):
    if isinstance(value, dict):
        discriminator=next((v for k,v in value.items()
            if str(k).casefold() in ('optioncode','option_code','configkey','config_key')),None)
        sensitive_option=discriminator is not None and str(discriminator).casefold() not in OPTION_SAFE_FLAGS|OPTION_SAFE_IDS
        return {k: '[REDACTED]' if SECRET.search(str(k)) else
            '[REDACTED_OPTION]' if sensitive_option and str(k).casefold() in
                ('content','optioncontent','option_content','configvalue','config_value','value') else
            redact_option_json(v) if str(k).casefold() in ('optionjson','option_json') else redact(v)
            for k,v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value,str) and value.lstrip().startswith(('{','[')):
        try:
            parsed=json.loads(value)
            safe=redact(parsed)
            return json.dumps(safe,ensure_ascii=False) if safe!=parsed else value
        except (ValueError,TypeError): pass
    if isinstance(value,str) and re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',value):
        return '[REDACTED_PRIVATE_KEY]'
    return value


class NativeAPIError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeOpenHISClient:
    """config: base_url, timeout_s, tenant_id, token[_env], username[_env],
    password[_env], allow_writes, dedicated_patient_ids, dedicated_encounter_ids.

    Authentication secrets remain in memory. Evidence contains redacted request
    and response JSON, including HTTP status and business code separately.
    """

    def __init__(self, config, evidence_dir):
        self.config = dict(config)
        self.base_url = self.config.get('base_url', 'http://localhost:18080/openhis').rstrip('/')
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('base_url must be an HTTP origin/path without embedded credentials')
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(self.config.get('timeout_s', 30))
        self.token = self._secret('token')
        self.opener = build_opener(_NoRedirect)
        self._lock = threading.Lock()
        self.last_evidence = None

    def _secret(self, name):
        variable = self.config.get(name + '_env', 'OPENHIS_' + name.upper())
        direct = self.config.get(name) or os.environ.get(variable)
        if direct:
            return direct
        # A new CLI process does not inherit values merely saved in .env.
        # Read only this project's configured credential file, without exporting
        # its contents to the process environment or evidence records.
        from dotenv import dotenv_values
        configured = self.config.get('env_file')
        paths = [Path(configured)] if configured else [Path(__file__).resolve().parents[3] / '.env']
        for path in paths:
            if path.is_file():
                value = dotenv_values(path).get(variable)
                if value:
                    return value
        return None

    def request(self, method, path, *, params=None, body=None, authenticated=True, mutation=False):
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise ValueError('path must be a relative API path; use params for query fields')
        if path.startswith('/sandbox/'):
            raise ValueError('sandbox adapter routes are not native API evidence')
        mutation = mutation or (method.upper() != 'GET' and path not in ('/login', '/payment/payment/detail')) or path in (
            '/doctor-station/main/receive-encounter', '/doctor-station/main/complete-encounter')
        if mutation and not self.config.get('allow_writes', False):
            raise NativeAPIError('native writes have not been enabled for dedicated experiment objects')
        # Only explicitly wrapped APIs may be invoked; in particular, a GET can
        # mutate OpenHIS (receive/complete encounter), so HTTP verb is no guard.
        if (method.upper(), path) not in self._routes():
            raise ValueError('API route has not been mapped to current native source')
        if authenticated and not self.token:
            raise NativeAPIError('native authentication is required')
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if authenticated:
            token = str(self.token)
            headers['Authorization'] = token if token.startswith('Bearer ') else 'Bearer ' + token
        url = self.base_url + path + ('?' + urlencode(params, doseq=True) if params else '')
        encoded = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        record = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                  'request_id': uuid.uuid4().hex, 'request': {'method': method.upper(), 'path': path,
                  'base_url': self.base_url, 'params': redact(params), 'body': redact(body),
                  'authenticated': bool(authenticated), 'mutation': mutation}}
        try:
            try:
                response = self.opener.open(Request(url, data=encoded, headers=headers, method=method.upper()), timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            raw = response.read().decode('utf-8', errors='replace')
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                # Non-JSON error pages can contain server settings. Keep the
                # response length and avoid copying unstructured secrets.
                value = {'non_json_response': True, 'length': len(raw)}
            record['response'] = {'http_status': response.status, 'body': redact(value)}
        except (OSError, URLError, TimeoutError) as exc:
            record['transport_error'] = type(exc).__name__
            cause = getattr(exc, 'reason', exc)
            record['transport_os_error'] = {'type': type(cause).__name__,
                'errno': getattr(cause, 'errno', None), 'winerror': getattr(cause, 'winerror', None),
                'message': str(cause)[:300]}
            self._archive(record)
            raise NativeAPIError('native API transport unavailable; see request evidence') from None
        evidence = self._archive(record)
        return {'http_status': response.status, 'body': value, 'evidence': evidence,
                'business_code': value.get('code') if isinstance(value, dict) else None,
                'success': response.status < 400 and isinstance(value, dict) and value.get('code') in (0, 200)}

    def _archive(self, record):
        target = self.evidence_dir / (record['request_id'] + '.json')
        with target.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        with self._lock:
            self.last_evidence = str(target.resolve())
            with (self.evidence_dir / 'requests.jsonl').open('a', encoding='utf-8') as stream:
                json.dump({'request_id': record['request_id'], 'method': record['request']['method'],
                           'path': record['request']['path'], 'http_status': record.get('response', {}).get('http_status'),
                           'business_code': record.get('response', {}).get('body', {}).get('code') if isinstance(record.get('response', {}).get('body'), dict) else None,
                           'evidence': self.last_evidence, 'transport_error': record.get('transport_error')}, stream)
                stream.write('\n')
        return str(target.resolve())

    def _routes(self):
        routes = {('GET', p) for p in (
            '/captchaImage', '/getInfo', '/doctor-station/main/init',
            '/data-dictionary/medication/information-init', '/data-dictionary/medication/information-page',
            '/data-dictionary/medication/information-one', '/base-data-manage/organization/organization',
            '/charge-manage/register/org-list', '/charge-manage/register/init',
            '/base-data-manage/practitioner/user-practitioner-page',
            '/doctor-station/main/patient-info', '/doctor-station/main/prescription-page-info',
            '/doctor-station/main/prescription-detail-info', '/doctor-station/emr/emr-detail',
            '/doctor-station/emr/emr-page', '/doctor-station/advice/advice-base-info',
            '/doctor-station/advice/request-base-info', '/doctor-station/advice/get-encounter-contract',
            '/doctor-station/main/receive-encounter', '/doctor-station/main/complete-encounter',
            '/charge-manage/charge/patient-prescription', '/charge-manage/charge/patient-prescription-with-price',
            '/pharmacy-manage/prescription-review/info', '/jlau/review-prescription-records/review-by-prescription-no',
            '/jlau/review-prescription-records/review-by-encounter',
            '/patient-manage/information/patient-information-page', '/patient-manage/information/init',
            '/payment/payment/page', '/payment/bill/getDetail',
            '/document/record/getRecordById', '/document/record/getRecordByEncounterIdList')}
        routes |= {('GET', '/system/dict/data/type/' + k) for k in ('unit_code', 'rate_code', 'method_code')}
        routes |= {('POST', p) for p in ('/login', '/doctor-station/emr/emr',
            '/doctor-station/advice/save-advice', '/doctor-station/advice/sign-advice',
            '/payment/payment/precharge', '/payment/payment/charge', '/payment/payment/detail',
            '/pharmacy-manage/prescription-review/review', '/jlau/review-prescription-records/review-prescription',
            '/patient-manage/information/patient-information', '/payment/payment/reg-pre-pay', '/payment/payment/reg-pay')}
        return routes

    def login(self, *, code=None, captcha_uuid=None):
        username, password = self._secret('username'), self._secret('password')
        if not username or not password:
            raise NativeAPIError('native username/password configuration is missing')
        result = self.request('POST', '/login', authenticated=False, body={
            'username': username, 'password': password, 'tenantId': int(self.config.get('tenant_id', 1)),
            'code': code, 'uuid': captcha_uuid})
        value = result['body']
        token = value.get('token') or (value.get('data') or {}).get('token') if isinstance(value, dict) else None
        if result['success'] and token:
            self.token = token
        else:
            raise NativeAPIError('native login rejected; see redacted request evidence')
        return {k: v for k, v in result.items() if k != 'body'}

    def captcha_settings(self):
        return self.request('GET', '/captchaImage', authenticated=False)

    def identity(self):
        return self.request('GET', '/getInfo')

    def dictionary(self, kind):
        return self.request('GET', '/system/dict/data/type/' + kind)

    def medications(self, **query):
        return self.request('GET', '/data-dictionary/medication/information-page', params={'pageNo': 1, 'pageSize': 100, **query})

    def medication(self, medication_id):
        return self.request('GET', '/data-dictionary/medication/information-one', params={'id': medication_id})

    def organizations(self, **query):
        return self.request('GET', '/base-data-manage/organization/organization', params={'pageNo': 1, 'pageSize': 100, **query})

    def patients(self, **query):
        return self.request('GET', '/patient-manage/information/patient-information-page', params={'pageNo': 1, 'pageSize': 20, **query})

    def encounters(self, **query):
        return self.request('GET', '/doctor-station/main/patient-info', params={'pageNo': 1, 'pageSize': 20, **query})

    def prescriptions(self, **query):
        return self.request('GET', '/doctor-station/main/prescription-page-info', params={'pageNo': 1, 'pageSize': 20, **query})

    def prescription(self, prescription_no):
        return self.request('GET', '/doctor-station/main/prescription-detail-info', params={'prescriptionNo': prescription_no})

    def emr(self, encounter_id):
        return self.request('GET', '/doctor-station/emr/emr-detail', params={'encounterId': encounter_id})

    def orders(self, encounter_id):
        return self.request('GET', '/doctor-station/advice/request-base-info', params={'encounterId': encounter_id})

    def charges(self, encounter_id):
        return self.request('GET', '/charge-manage/charge/patient-prescription-with-price', params={'encounterId': encounter_id})

    def payment(self, payment_id):
        """Native receipt projection; paymentId in data is paymentNo, not PK."""
        return self.request('GET', '/payment/bill/getDetail', params={'paymentId': payment_id})

    def payment_details(self, payment_id):
        return self.request('POST', '/payment/payment/detail', body={'id': payment_id})

    def document(self, record_id):
        return self.request('GET', '/document/record/getRecordById', params={'recordId': record_id})

    def documents(self, **query):
        return self.request('GET', '/document/record/getRecordByEncounterIdList', params=query)

    def review(self, prescription_no):
        return self.request('GET', '/jlau/review-prescription-records/review-by-prescription-no', params={'prescriptionNo': prescription_no})

    def _dedicated(self, patient_id=None, encounter_id=None):
        if not self.config.get('allow_writes'):
            raise NativeAPIError('native writes disabled')
        if patient_id is None and encounter_id is None:
            raise NativeAPIError('native write target identity is missing')
        for key, value in (('dedicated_patient_ids', patient_id), ('dedicated_encounter_ids', encounter_id)):
            if value is not None and str(value) not in {str(x) for x in self.config.get(key, [])}:
                raise NativeAPIError('write target is not an explicitly dedicated experiment object')

    def save_emr(self, patient_id, encounter_id, context, *, emr_status=None):
        self._dedicated(patient_id, encounter_id)
        if not isinstance(context, dict) or not context or any(not isinstance(v, str) for v in context.values()):
            raise ValueError('native EMR context must map field names to strings')
        body = {'patientId': patient_id, 'encounterId': encounter_id, 'contextJson': context}
        if emr_status is not None:
            body['emrStatus'] = str(emr_status)
        return self.request('POST', '/doctor-station/emr/emr', body=body, mutation=True)

    def complete_encounter(self, encounter_id):
        self._dedicated(encounter_id=encounter_id)
        return self.request('GET', '/doctor-station/main/complete-encounter', params={'encounterId': encounter_id}, mutation=True)

    def receive_encounter(self, encounter_id):
        self._dedicated(encounter_id=encounter_id)
        return self.request('GET', '/doctor-station/main/receive-encounter', params={'encounterId': encounter_id}, mutation=True)

    def save_advice(self, native_dto, *, sign=False):
        for item in native_dto.get('adviceSaveList', []):
            self._dedicated(item.get('patientId'), item.get('encounterId'))
        if not native_dto.get('adviceSaveList'):
            raise ValueError('adviceSaveList required')
        path = '/doctor-station/advice/' + ('sign-advice' if sign else 'save-advice')
        return self.request('POST', path, body=native_dto, mutation=True)

    def prepare_payment(self, native_dto):
        self._dedicated(native_dto['patientId'], native_dto['encounterId'])
        return self.request('POST', '/payment/payment/precharge', body=native_dto, mutation=True)

    def commit_cash(self, native_dto):
        self._dedicated(encounter_id=native_dto['encounterId'])
        if not native_dto.get('paymentDetails') or any(int(x['payEnum']) != 220100 for x in native_dto['paymentDetails']):
            raise ValueError('only native self cash accounting is allowed')
        if not self.config.get('external_effects_disabled_verified'):
            raise NativeAPIError('cash charge can invoke invoice services; verify existing runtime switches first')
        return self.request('POST', '/payment/payment/charge', body=native_dto, mutation=True)

    def record_review(self, native_dto, *, decision=True):
        self._dedicated(encounter_id=native_dto['encounterId'])
        path = '/jlau/review-prescription-records/review-prescription' if decision else '/pharmacy-manage/prescription-review/review'
        return self.request('POST', path, body=native_dto, mutation=True)

    def create_patient(self, native_dto):
        if not self.config.get('allow_create_dedicated') or not self.config.get('dedicated_name_prefix') or not str(native_dto.get('name', '')).startswith(self.config['dedicated_name_prefix']):
            raise NativeAPIError('creation requires an explicit experimental name prefix and creation authorization')
        return self.request('POST', '/patient-manage/information/patient-information', body=native_dto, mutation=True)


def read_materials(client):
    """Acquire the actual dictionaries/master data needed to construct cases."""
    results = []
    for name, call in [('identity', client.identity), ('medications', client.medications),
                       ('organizations', client.organizations)] + [(k, lambda k=k: client.dictionary(k)) for k in ('unit_code', 'rate_code', 'method_code')]:
        try:
            r = call()
            results.append({'name': name, 'http_status': r['http_status'], 'business_code': r['business_code'], 'evidence': r['evidence']})
        except NativeAPIError as exc:
            results.append({'name': name, 'error': str(exc), 'evidence': client.last_evidence})
    return results
