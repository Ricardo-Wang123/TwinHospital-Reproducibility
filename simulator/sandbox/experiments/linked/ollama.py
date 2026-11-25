"""Local Ollama adapter for the study's role-specific JSON response formats."""

import json
from json import dumps
import time
from urllib.parse import urlsplit

import requests

from . import emergency
from .contracts import PRESCRIPTION_ITEM_REQUIRED_FIELDS, digest
from .response_contracts import CONTRACTS, DOCUMENTATION_ACTIONS, documentation_sequences


def closed_object(fields, required=None):
    return {'type': 'object', 'properties': fields, 'additionalProperties': False,
            'required': list(fields) if required is None else required}


def formulary_lookup_format():
    return closed_object({'action': {'enum': ['search_formulary']},
                          'queries': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                                      'minItems': 1, 'maxItems': 6},
                          'offset': {'type': 'integer', 'minimum': 0}}, ['action', 'queries'])


def history_query_format(observation):
    fields = {
        'action': {'enum': ['read_history']},
        'section': {'type': 'string', 'minLength': 1},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'term': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'encounter_key': {'type': 'string'},
    }
    required = ['action', 'section', 'offset', 'limit']
    if observation.get('ips_on_demand'):
        required.append('term')
    return closed_object(fields, required)


def prescribing_source_format(observation):
    lookup = history_query_format(observation)
    if sections := observation.get('available_chart_sections'):
        lookup['properties']['section'] = {'enum': list(dict.fromkeys(sections))}
    question = {'type': 'string', 'minLength': 1}
    patient = closed_object({'action': {'enum': ['ask']}, 'recipient': {'enum': ['patient']},
                             'question': question})
    reconcile = closed_object({'action': {'enum': ['request_medication_reconciliation']},
                               'issues': {'type': 'array', 'items': question, 'minItems': 1},
                               'reason': question})
    return {'oneOf': [lookup, patient, reconcile]}


def documentation_content_format(action, references, correction):
    if correction.get('ed_note_sections_required'):
        section = {'type': 'string', 'minLength': 1, 'maxLength': 3000}
        return closed_object({'sections': closed_object(dict.fromkeys(emergency.NOTE_HEADINGS, section)),
                              'source_sequences': references})
    text = {'type': 'string'}
    if action in {'document', 'finalize_note'}:
        text['maxLength'] = 12000
    return closed_object({'text': text, 'source_sequences': references})


def chart_receipt_format(observation):
    quotes = []
    for table in observation['tables']:
        for row in emergency.table_records(table):
            for value in emergency.review_source_fields(row).values():
                if type(value) in (str, int, float):
                    quote = ' '.join(str(value).split())[:160].rstrip()
                    if any(char.isalnum() for char in quote):
                        quotes.append(quote)
    previous = (observation.get('previous_reading_receipt') or {}).get('evidence_quote')
    if previous:
        quotes.append(previous)
    if observation.get('required_evidence_section'):
        quotes = list(emergency.review_source_values(observation))
    if not quotes:
        return 'json'
    fields = {'action': {'enum': ['chart_page_reviewed']},
              'page_number': {'enum': [observation['page_number']]},
              'findings': {'type': 'string'},
              'evidence_quote': {'enum': list(dict.fromkeys(quotes))}}
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def medication_advice_format(observation):
    string = {'type': 'string'}
    strings = {'type': 'array', 'items': string}
    advice = observation['action'] == 'advise_prescription'
    response_action = 'prescription_advice' if advice else 'prescription_draft'
    prescription = 'suggested_prescription' if advice else 'prescription'
    scopes = ['primary'] if observation.get('primary_treatment_only') else ['primary', 'other']
    item = dict.fromkeys(PRESCRIPTION_ITEM_REQUIRED_FIELDS, string)
    if observation.get('single_prescription') and observation['orderable_drug_codes']:
        item['drug_code'] = {'enum': observation['orderable_drug_codes']}
    target = observation.get('ed_prescribing_target')
    indication = {'scope': {'enum': scopes}, 'problem': {'enum': [target['display']]} if target else string}
    if observation.get('single_prescription'):
        indication['rationale'] = string
    item.update(indication=closed_object(indication, ['scope', 'problem']), device=string,
                administration_instructions=string,
                intended_course=closed_object({'start_offset_hours': {'type': 'number'},
                                               'duration_days': {'type': 'number'}, 'basis': string}))
    fields = {'action': {'enum': [response_action]},
              prescription: closed_object({'items': {'type': 'array', 'items': closed_object(
                  item, [*PRESCRIPTION_ITEM_REQUIRED_FIELDS, 'indication'])}}),
              'reason': string, 'outstanding_checks': strings}
    if advice:
        fields['issues'] = strings
    submitted = closed_object(fields)
    return {'oneOf': [submitted, formulary_lookup_format()]} if observation.get('formulary_search') else submitted


def signed_order_format(observation):
    string = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
    strings = {'type': 'array', 'items': string, 'maxItems': 24}
    item = dict.fromkeys(PRESCRIPTION_ITEM_REQUIRED_FIELDS, string)
    single_prescription = observation.get('single_prescription')
    if single_prescription and observation['orderable_drug_codes']:
        orderable = observation['orderable_drug_codes']
        item['drug_code'] = {'enum': orderable}
        frequencies = {
            entry['drug_code']: tuple(entry.get('allowed_frequency_codes') or ())
            for entry in observation.get('formulary', [])
        }
        product_schedules = {frequencies.get(code, ()) for code in orderable}
        if len(product_schedules) == 1 and () not in product_schedules:
            item['frequency'] = {'enum': list(product_schedules.pop())}
    for key in ('dose', 'quantity'):
        item[key] = ({'type': 'number', 'exclusiveMinimum': 0} if single_prescription
                     else {'anyOf': [string, {'type': 'number'}]})
    target = observation.get('ed_prescribing_target')
    indication = {'scope': {'enum': ['primary']}, 'problem': {'enum': [target['display']]} if target else string}
    if single_prescription:
        indication['rationale'] = string
    item.update(indication=closed_object(indication, ['scope', 'problem']),
                device=string, administration_instructions=string,
                intended_course=closed_object({'start_offset_hours': {'type': 'number'},
                                               'duration_days': {'type': 'number'}, 'basis': string}))
    item_required = [*PRESCRIPTION_ITEM_REQUIRED_FIELDS, 'indication']
    item_format = closed_object(item, item_required)
    if single_prescription and observation.get('review_state') == 'encoding_correction':
        orderable = item.get('drug_code', {}).get('enum', [])
        field_limits = {}
        instructions_required = set()
        for finding in observation.get('business_validation', {}).get('findings', []):
            code = finding.get('drug_code')
            if code not in orderable:
                continue
            field = finding.get('field')
            accepted = finding.get('accepted_field_values')
            if accepted and field in {'route', 'frequency', 'dose_unit', 'quantity_unit'}:
                field_limits.setdefault(code, {})[field] = {'enum': accepted}
            if finding['code'] in {'prn_instructions_missing', 'invalid_administration_instructions'}:
                field_limits.setdefault(code, {})['administration_instructions'] = string
                instructions_required.add(code)
        if field_limits:
            choices = [
                closed_object({**item, 'drug_code': {'enum': [code]}, **fields},
                              [*item_required, 'administration_instructions'] if code in instructions_required
                              else item_required)
                for code, fields in field_limits.items()]
            remaining = [code for code in orderable if code not in field_limits]
            if remaining:
                choices.append(closed_object({**item, 'drug_code': {'enum': remaining}}, item_required))
            item_format = {'oneOf': choices} if len(choices) > 1 else choices[0]
    actions = [closed_object({'kind': {'enum': ['new', 'repeat']}, 'item': item_format})]
    active_ids = [row['request_id'] for row in observation['signed_order_lines']]
    stop_action = None
    if active_ids:
        active_id = {'enum': active_ids}
        stop_action = closed_object({'kind': {'enum': ['stop']}, 'request_id': active_id})
        actions.extend([stop_action,
                        closed_object({'kind': {'enum': ['change']}, 'request_id': active_id, 'item': item_format})])
    correction = observation.get('schema_correction', {})
    limits = observation.get('source_limits_review', {})
    previous = correction.get('previous_invalid_response') or {}
    previous_options = previous.get('treatments') or previous.get('treatment_review')
    option_ids = list(observation['required_treatment_options']) if single_prescription else None
    if isinstance(previous_options, dict):
        option_ids = list(dict.fromkeys([*observation['required_treatment_options'], *previous_options]))
    affected_treatments = strings
    if option_ids:
        affected_treatments = {'type': 'array', 'items': {'enum': option_ids}, 'maxItems': len(option_ids)}
    def check_fields(affected, affected_by_check=None):
        short_finding = {**string, 'maxLength': 420} if observation.get('complete_prescribing_citations') else string
        quotes = [] if observation.get('ips_on_demand') else ['quote']
        statuses = [('verified', ['source', *quotes]),
                    ('reviewed_with_limits', ['source', *quotes, 'affected_treatments', 'decision_basis']),
                    ('unresolved', ['affected_treatments', 'decision_basis'])]
        checks = {}
        for key in observation['required_prescribing_checks']:
            if key in limits.get('retained_check_results', {}):
                continue
            scoped = (affected_by_check or {}).get(key, affected)
            fields = {'finding': short_finding, 'source': string, 'quote': string, 'verification_status': string,
                      'affected_treatments': scoped, 'decision_basis': short_finding}
            correction = limits.get('citation_corrections', {}).get(key)
            fresh_sources = limits.get('fresh_citation_sources', [])
            candidates = correction['matching_read_sources'] if correction else None
            previous = observation.get('previous_check_findings', {}).get(key) or {}
            quote = previous.get('quote') if isinstance(previous, dict) else None
            choices = []
            for status, required in statuses:
                values = {**fields, 'status': {'enum': [status]}}
                if fresh_sources and status != 'unresolved':
                    available = set(fresh_sources)
                    if status == 'verified':
                        available.discard('original_chart_unknown')
                        ips = observation.get('checked_history_sources', {}).get('ips')
                        if ips is not None and not emergency.ips_documents_check(ips, key):
                            available.discard('ips')
                    if key == 'medicines':
                        available &= {'ips', 'patient', 'medication_history', 'medication_records', 'original_chart_unknown'}
                        if status == 'verified':
                            available.discard('medication_records')
                    if 'ips' in available:
                        choices.append(closed_object({**values, 'source': {'enum': ['ips']}},
                                                     ['status', 'finding', *required]))
                    selected_passages = {
                        'patient': limits.get('patient_reply_passages', []),
                        'original_chart_unknown': [
                            receipt['term'] for receipt in observation.get('chart_search_receipts', [])
                            if receipt['status'] == 'no_recorded_result'],
                    }
                    if key != 'medicines':
                        selected_passages['preconsult_report'] = limits.get('preconsult_report_passages', [])
                    chart_passages = (correction or {}).get('chart_passages', {})
                    selected_passages.update(chart_passages)
                    for source, passages in selected_passages.items():
                        if source not in available and source != 'preconsult_report':
                            continue
                        if not passages:
                            if source == 'original_chart_unknown' or source in chart_passages:
                                available.discard(source)
                            continue
                        available.discard(source)
                        if status == 'verified' and source == 'patient':
                            passages = [part for part in passages if part.strip(' .。') not in {
                                '这部分我不清楚', '不清楚', '不知道', 'I do not know', "I don't know"}]
                        if passages:
                            choices.append(closed_object({**values, 'source': {'enum': [source]},
                                                          'quote': {'enum': list(dict.fromkeys(passages))}},
                                                         ['status', 'finding', *dict.fromkeys([*required, 'quote'])]))
                    chart_sources = sorted(available - {'ips'})
                    if chart_sources:
                        choices.append(closed_object({**values, 'source': {'enum': chart_sources}},
                                                     ['status', 'finding', *dict.fromkeys([*required, 'quote'])]))
                    continue
                if correction and status != 'unresolved':
                    if not candidates or not quote:
                        continue
                    values.update(source={'enum': candidates}, quote={'enum': [quote]})
                elif correction:
                    for field in ('source', 'quote', 'verification_status'):
                        values.pop(field)
                choices.append(closed_object(values, ['status', 'finding', *required]))
            checks[key] = {'oneOf': choices} if len(choices) > 1 else choices[0]
        return closed_object(checks)

    if observation.get('complete_prescribing_citations'):
        return closed_object({'check_results': check_fields(affected_treatments)})

    preserve_no_actions = correction.get('preserve_no_order_actions')
    action_list = {'type': 'array', 'items': {'oneOf': actions}}
    if preserve_no_actions:
        action_list['maxItems'] = 0
    retained = {'type': 'array', 'items': {'enum': active_ids}} if active_ids else {'type': 'array', 'maxItems': 0}
    treatment = closed_object({'orders': action_list, 'maintained_order_ids': retained, 'reason': string})
    if single_prescription:
        items = {'type': 'array', 'items': item_format}
        treatment = closed_object({'items': items, 'reason': string})
    treatments = closed_object(dict.fromkeys(observation['required_treatment_options'], treatment))
    treatments['additionalProperties'] = treatment
    advised = [row['drug_code'] for row in observation.get('medication_advice', {}).get('items', [])]
    advice_option = {'enum': option_ids} if option_ids else string
    advice_review = closed_object(dict.fromkeys(advised, closed_object({'treatment_option': advice_option, 'reason': string})))
    limit_checks = limits and not limits.get('encoding_correction')
    fields = {'decision': {'enum': ['sign']}, 'treatments': treatments, 'check_results': check_fields(affected_treatments),
              'advice_review': advice_review, 'outstanding_checks': {'type': 'array', 'maxItems': 0} if limit_checks else strings,
              'reason': string}
    signed = closed_object(fields)
    conflicts = limits.get('treatments_marked_as_both_selected_and_blocked') or {}
    if conflicts and not preserve_no_actions and not correction.get('complete_declared_order'):
        known_conflicts = {option for involved in conflicts.values() for option in involved}
        selected = limits.get('prior_selected_treatment_options')
        withdrawn = known_conflicts if selected else {min(known_conflicts)}
        excluded = set(selected) if selected else withdrawn
        alternatives = sorted((known_conflicts | observation['required_treatment_options'].keys()) - excluded)
        links = {'type': 'array', 'items': {'enum': alternatives}, 'maxItems': len(alternatives)} if alternatives else {
            'type': 'array', 'maxItems': 0}
        scope = {key: links for key, options in conflicts.items() if withdrawn.intersection(options)}
        empty = {'type': 'array', 'maxItems': 0}
        withdrawal = {'type': 'array', 'items': stop_action} if stop_action else empty
        inactive = closed_object({'orders': withdrawal, 'maintained_order_ids': empty, 'reason': string})
        if single_prescription:
            inactive = closed_object({'items': empty, 'reason': string})
        changes = {**treatments, 'properties': {**treatments['properties'], **dict.fromkeys(withdrawn, inactive)}}
        signed = {'anyOf': [closed_object({**fields, 'treatments': changes}),
                           closed_object({**fields, 'check_results': check_fields(affected_treatments, scope)})]}
    if preserve_no_actions:
        return signed
    choices = [signed]
    if 'ask' in observation['allowed_response_actions']:
        ask = {'action': {'enum': ['ask']}, 'question': string,
               'question_purpose': {'enum': ['prescription_clarification']}}
        if observation.get('question_recipients'):
            ask['recipient'] = {'enum': list(observation['question_recipients'])}
        choices.append(closed_object(ask))
    if 'read_history' in observation['allowed_response_actions']:
        choices.append(history_query_format(observation))
    if 'request_medication_reconciliation' in observation['allowed_response_actions']:
        choices.append(closed_object({'action': {'enum': ['request_medication_reconciliation']},
                                      'issues': strings, 'reason': string}))
    if observation.get('formulary_search'):
        choices.append(formulary_lookup_format())
    return {'oneOf': choices} if len(choices) > 1 else signed


def response_format(messages):
    user = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    if not user.startswith('{'):
        return 'json'
    observation = json.loads(user)
    action = observation.get('action')
    correction = observation.get('schema_correction', {})
    if completion := observation.get('complete_native_instructions'):
        text = {'type': 'string', 'minLength': 1, 'maxLength': 400}
        instructions = closed_object(dict.fromkeys(completion['lines'], text))
        return closed_object({'administration_instructions': instructions})
    if action == 'locate_ed_prescribing_source' and observation.get('ips_on_demand') and correction:
        return prescribing_source_format(observation)
    if observation.get('formulary_search_required'):
        return formulary_lookup_format()
    if action == 'collect':
        string = {'type': 'string'}
        fact_ids = {'type': 'array', 'items': string}
        report = closed_object({'action': {'enum': ['summary']}, 'fact_ids': fact_ids, 'risk_fact_ids': fact_ids})
        if observation.get('require_intake_summary'):
            return report
        return {'oneOf': [report, closed_object({'action': {'enum': ['ask']}, 'question': string})]}
    if action == 'assess':
        if choices := correction.get('primary_diagnosis_choices'):
            return closed_object({'primary_diagnosis_index': {'type': 'integer',
                                  'enum': [row['index'] for row in choices]}})
        string = {'type': 'string'}
        strings = {'type': 'array', 'items': string}
        brief = string
        label = string
        if observation.get('require_working_assessment'):
            brief = {'type': 'string', 'maxLength': 1600}
            label = {'type': 'string', 'maxLength': 240}
            strings = {'type': 'array', 'items': label, 'maxItems': 16}
        diagnosis = closed_object({'code': label, 'display': label, 'system': label, 'primary': {'type': 'boolean'},
                                   'certainty': {'enum': ['working', 'confirmed']}})
        diagnoses = {'type': 'array', 'items': diagnosis}
        if observation.get('require_working_assessment'):
            diagnoses['maxItems'] = 24
        completed = closed_object({'action': {'enum': ['assessment_complete']}, 'assessment': brief, 'plan': brief,
            'diagnoses': diagnoses, 'outstanding_checks': strings,
            'medication_reconciliation': closed_object({'status': {'enum': ['complete_declared_episode', 'partial', 'not_assessed']},
                                                       'basis': brief, 'unresolved': strings}),
            'allergy_assessment': closed_object({'status': {'enum': ['none_known', 'allergy_reported', 'unable_to_ascertain']},
                                                'basis': brief})})
        if observation.get('fixed_allergy_history'):
            completed['properties'].pop('allergy_assessment')
            completed['required'].remove('allergy_assessment')
        if observation.get('require_working_assessment'):
            return completed
        query = history_query_format(observation)
        ask = {'action': {'enum': ['ask']}, 'question': string, 'question_purpose': {'enum': ['diagnostic']}}
        if recipients := observation.get('question_recipients'):
            ask['recipient'] = {'enum': list(recipients)}
        return {'oneOf': [completed, query, closed_object(ask)]}
    if missing := correction.get('advice_products_to_review'):
        options = list(correction['previous_invalid_response']['treatment_review'])
        entry = {'type': 'object', 'properties': {'treatment_option': {'enum': options},
                                                 'reason': {'type': 'string', 'minLength': 1}},
                 'required': ['treatment_option', 'reason'], 'additionalProperties': False}
        review = {'type': 'object', 'properties': dict.fromkeys(missing, entry),
                  'required': missing, 'additionalProperties': False}
        return {'type': 'object', 'properties': {'advice_review': review}, 'required': ['advice_review'],
                'additionalProperties': False}
    if action in {'draft_prescription', 'advise_prescription'} and observation.get('ed_order_indication_policy'):
        return medication_advice_format(observation)
    if (action == 'review_ed_chart_page' and observation.get('source_quote_required')
            and (observation.get('schema_correction') or observation.get('required_evidence_section'))):
        return chart_receipt_format(observation)
    if action == 'decide_ed_disposition':
        return closed_object({'action': {'enum': ['ed_disposition']},
                              'code': {'enum': list(emergency.DISPOSITIONS)},
                              'reason': {'type': 'string', 'minLength': 1}})
    if action in DOCUMENTATION_ACTIONS:
        allowed = documentation_sequences(observation)
        references = {'type': 'array', 'items': {'type': 'integer'}}
        if allowed:
            references['items']['enum'] = allowed
            references['maxItems'] = len(allowed)
        else:
            references['maxItems'] = 0
        content = documentation_content_format(action, references, correction)
        if action == 'finalize_note':
            fields = {'decision': {'enum': CONTRACTS[action]['allowed_decisions']}, 'reason': {'type': 'string'},
                      'content': content, 'finalized': {'type': 'boolean'},
                      'reviewed_allergy_updates': {'type': 'array', 'items': {'type': 'string'}}}
            if observation.get('required_plan_lines'):
                revised = closed_object({**fields, 'decision': {'enum': ['revise']}},
                                        ['decision', 'reason', 'content'])
                rejected = closed_object({**fields, 'decision': {'enum': ['reject']}}, ['decision', 'reason'])
                return {'oneOf': [revised, rejected]}
            required = ['decision', 'reason']
        else:
            fields = {'action': {'enum': CONTRACTS[action]['response_actions']}, 'content': content}
            required = list(fields)
        return {'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False}
    if 'signed_order_lines' in observation and observation.get('required_treatment_options'):
        return signed_order_format(observation)
    return 'json'


class OllamaChat(requests.Session):
    """Supply the provider envelope from Ollama's native, per-call context API."""

    def __init__(self, endpoint, models):
        super().__init__()
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
                or parsed.username or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Ollama requires a loopback base URL without a path')
        self.trust_env = False
        self.endpoint = endpoint
        self.models = models

    def post(self, url, *, json, headers=None, **kwargs):
        if url != self.endpoint + '/chat/completions' or json['model'] not in self.models or json['stream']:
            raise ValueError('Unexpected request for the configured local models')
        limit = json['max_tokens']
        request_bytes = len(dumps(json['messages'], ensure_ascii=False).encode('utf-8'))
        context = self.models[json['model']]['context_window_tokens']
        output_format = response_format(json['messages'])
        body = {'model': json['model'], 'messages': json['messages'], 'think': False,
                'keep_alive': '30m', 'stream': False, 'format': output_format,
                'options': {'num_ctx': context, 'num_predict': limit, 'temperature': json['temperature']}}
        response = super().post(self.endpoint + '/api/chat', json=body, **kwargs)
        if response.status_code != 200:
            return response
        result = response.json()
        prompt, completion = result['prompt_eval_count'], result['eval_count']
        if prompt + limit + 256 >= context:
            raise ValueError('Ollama did not leave room for the complete recorded prompt and reply')
        envelope = {
            'id': 'ollama-' + str(time.time_ns()), 'object': 'chat.completion', 'created': int(time.time()),
            'model': result['model'], 'choices': [{'index': 0, 'message': result['message'],
                                                 'finish_reason': result['done_reason']}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                      'total_tokens': prompt + completion},
            'ollama': {'context_tokens': context, 'request_bytes': request_bytes,
                       'response_format': 'json_schema' if isinstance(output_format, dict) else output_format,
                       **({'response_schema_hash': digest(output_format)} if isinstance(output_format, dict) else {}),
                       **{key: result[key] for key in ('load_duration', 'total_duration',
                          'prompt_eval_duration', 'eval_duration') if key in result}},
        }
        thinking = envelope['choices'][0]['message'].get('thinking')
        if thinking:
            envelope['choices'][0]['message']['reasoning_content'] = thinking
        response._content = dumps(envelope, ensure_ascii=False).encode('utf-8')
        return response
