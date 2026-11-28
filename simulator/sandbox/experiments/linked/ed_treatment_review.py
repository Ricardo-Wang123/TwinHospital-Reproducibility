"""Therapy options and decision records for the primary-problem ED task."""
from collections import Counter
from copy import deepcopy
from math import isfinite
import re

from .ed_order_actions import ACTION_TYPES

PROTOCOL = 'primary_treatment_decisions_v1'
REVIEW_COVERAGE_ERROR = 'Give a reason for every order action, retained order, and current advisory product.'

REFERENCES = {
    'COPD': 'https://goldcopd.org/2026-gold-report-and-pocket-guide/',
    'Heart failure': 'https://doi.org/10.1161/cir.0000000000001063',
    'Pneumonia': 'https://doi.org/10.1164/rccm.202507-1692ST',
    'Type 2 diabetes': 'https://doi.org/10.2337/dc26-S016',
    'Urinary tract infection': 'https://www.idsociety.org/practice-guideline/complicated-urinary-tract-infections',
}

OPTION_GUIDE = {
    'COPD': {
        'short_acting_beta2_agonist': 'Assess short-acting beta2 bronchodilation for an acute exacerbation.',
        'short_acting_antimuscarinic': 'Assess adding a short-acting inhaled antimuscarinic to beta2 bronchodilation.',
        'systemic_corticosteroid': 'Assess systemic corticosteroids in the context of exacerbation severity and risks.',
        'antibacterial': 'Assess antibiotics using the respiratory findings, likely infection and available exposure history.',
    },
    'Heart failure': {
        'decongestion': 'Assess loop diuresis if congestion is present. Consider perfusion, renal function and electrolytes.',
        'heart_failure_regimen': 'Assess continuation or adjustment of established therapy as hemodynamics and renal function permit.',
    },
    'Pneumonia': {
        'antibacterial': 'Assess empiric antibacterial therapy using the clinical syndrome, severity and resistance risks.',
        'pneumonia_adjunct': 'Assess the indication for adjunctive therapy, including corticosteroids in selected severe cases.',
    },
    'Type 2 diabetes': {
        'acute_glucose_management': 'Assess glucose-lowering treatment, including insulin, with attention to glucose, acuity and intake.',
        'diabetes_regimen': 'Assess continuation or adjustment of home therapy given renal function and hypoglycemia risk.',
    },
    'Urinary tract infection': {
        'antibacterial': 'Assess the need for antibiotics and choice according to site, severity, prior cultures and susceptibilities.',
    },
}

DIAGNOSIS_PREFIXES = {
    'COPD': ('J44', '496', '4912'),
    'Heart failure': ('I50', '428'),
    'Pneumonia': ('J12', 'J13', 'J14', 'J15', 'J16', 'J17', 'J18', '480', '481', '482', '483', '485', '486'),
    'Type 2 diabetes': ('E11', '250'),
    'Urinary tract infection': ('N390', 'N10', 'N30', '5990', '590', '595'),
}

SIGN_FORMAT = (
    "Return {decision:'sign',reason,outstanding_checks,check_results,treatments,advice_review}. "
    'Use each required_treatment_options ID in treatments, with {orders:[...],maintained_order_ids:[string],'
    'reason:string}. Add a treatment option when needed. List each actual transaction once, including combination '
    'products. Include each retained signed request ID under its treatment. '
    'To maintain therapy, list its retained IDs without a transaction. To omit a therapy, leave both lists empty '
    'and explain why. Empty order lists sign no new transactions. '
    'For every medicine you decide to prescribe now, provide its complete order action in orders. '
    'For each medication_advice product code give {treatment_option:string,reason:string} in advice_review. '
    'For treatment_option use an exact key from the treatments object you return. '
    'Use advice_review={} when there are no advisory products. '
)

SINGLE_PRESCRIPTION = 'single_prescription_v1'
SINGLE_PRESCRIPTION_FORMAT = (
    "Return {decision:'sign',reason,outstanding_checks,check_results,treatments,advice_review}. "
    'For each required treatment option return {items:[complete medication orders],reason:string}. '
    'Select each intended medication from the currently displayed formulary. Use exact drug_code, route, '
    'dose units, quantity units and supported schedule. Specify any necessary administration precautions. '
    'List an identical regimen once, even if it serves several clinical purposes. Record the combined reasons '
    'on that item in indication.rationale. '
    'Together the items are your complete prescription. If pharmacy returns the signed order, resubmit '
    'the complete prescription you intend to sign. Use only option keys from your treatments for advice_review.'
)
OMISSION_FORMAT = (
    ' Include considered_not_ordered as an array of {drug_code,reason,check_ids}. '
    'Use exact codes of medicines you saw and decided to omit. Give the reason for each product. '
    'Use check_ids to point to prescribing checks you used, or [] if no check applies. '
    'Return [] when you considered no omitted products.'
)

CHECK_FORMAT = (
    ' Return check_results keyed by every ID in required_prescribing_checks. '
    'Copy each key exactly, including "allergies" and "medicines". '
    'For a verified fact use {status:"verified",source,quote,finding}. Cite the actual visible source and exact text. '
    'For an unverified fact use {status:"unresolved",finding,affected_treatments:[option IDs],decision_basis:string}. '
    'For a fact reviewed with limits use {status:"reviewed_with_limits",source,quote,finding,'
    'affected_treatments:[option IDs],decision_basis:string}. '
    'Both unverified statuses always require affected_treatments, including [] when no therapy is blocked. '
    'decision_basis explains exactly which treatment the missing fact blocks and why, or why the chosen treatment '
    'can proceed on the available facts. Include only option IDs from treatments that must wait for this fact. '
    'For verified allergies cite current_allergy, and for verified medication use cite medication_history. '
    'For prior allergy or medication evidence with limits cite allergy_history or medication_records. '
    'A historical allergy entry does not verify current allergies. A medication list does not establish a last dose.'
)


def normalize_check_ids(response, required):
    findings = response.get('check_results')
    if not isinstance(findings, dict):
        return response
    aliases = {f'q{index}': key for index, key in enumerate(required, 1)
               if key not in findings and f'q{index}' not in required and f'q{index}' in findings}
    if not aliases:
        return response
    return {**response, 'check_results': {aliases.get(key, key): value for key, value in findings.items()}}


def clinical_assessment(consultation):
    return {key: consultation[key] for key in ('diagnoses', 'medication_reconciliation', 'allergy_assessment')
            if key in consultation}


def required_options(diagnoses, admission_history=()):
    codes = [row.get('code', '').replace('.', '').upper() for row in diagnoses]
    options = {key: guide for condition, prefixes in DIAGNOSIS_PREFIXES.items()
               if any(code.startswith(prefixes) for code in codes)
               for key, guide in OPTION_GUIDE[condition].items()}
    if (any(code.startswith(('R05', 'R06')) for code in codes)
            and any(re.search(r'\bCOPD\b|chronic obstructive pulmonary disease', text, re.I)
                    for text in admission_history)):
        options.update(OPTION_GUIDE['COPD'])
    return options or {'presenting_problem': 'Assess symptom relief and treatments for the suspected cause.'}


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def declared_order_choices(response):
    review = response.get('treatment_review')
    if not isinstance(review, dict):
        return {}
    return {option: entry['decision'] for option, entry in review.items()
            if isinstance(entry, dict) and entry.get('decision') in ('order', 'stop')}


_STARTING = re.compile(
    r'\b(?:(?:i|we) (?:will|intend to|plan to)|(?:current )?plan (?:is )?to|sufficient evidence to) '
    r'(?:start|initiate|order|prescribe|give|administer|add|treat with|proceed with)\b'
    r'|\b(?:i am|we are) (?:starting|initiating|ordering|prescribing|giving|administering|adding|proceeding with)\b'
    r'|\b(?:start|initiate|order|prescribe|give|administer|add)\s+(?=\S.*\b(?:now|today|at this checkpoint)\b)'
    r'|(?:本次|现在|立即|我将|决定|计划)\s*(?:开始使用|给予|使用|开具|开立|开出|加开|新开|开|处方|给|用)', re.I)
_QUALIFIED = re.compile(r'\b(?:not|never|if|once|pending|consider|could|would|defer|withhold)\b|(?:暂不|不应|无需|若|如果|如需|待确认|考虑(?!到)|不予)', re.I)
_EXPLICIT_PRESCRIPTION = re.compile(r'\bprescribe\b|(?:开具处方|开立处方|加开|新开|开药|开方|处方)', re.I)


def _order_proposals(response):
    reasons = [response.get('reason')]
    for key in ('treatment_review', 'treatments'):
        review = response.get(key)
        if isinstance(review, dict):
            reasons.extend(entry.get('reason') for entry in review.values() if isinstance(entry, dict))
    for reason in reasons:
        if not isinstance(reason, str):
            continue
        for clause in re.split(r'(?<=[.!?;])\s+|[。！？；\n]', reason):
            match = _STARTING.search(clause)
            if match and not _QUALIFIED.search(clause):
                yield match.group(), clause[match.end():]


def declares_prescription(response):
    return any(_EXPLICIT_PRESCRIPTION.search(verb + tail) for verb, tail in _order_proposals(response))


def _product_actions(response):
    actions = response.get('order_actions')
    treatments = response.get('treatments')
    if not isinstance(actions, list) and isinstance(treatments, dict):
        actions = [action for entry in treatments.values()
                   if isinstance(entry, dict) and isinstance(entry.get('orders'), list)
                   for action in entry['orders']]
    return Counter((action['kind'], code) for action in (actions if isinstance(actions, list) else [])
                   if isinstance(action, dict) and action.get('kind') in ('new', 'repeat', 'change')
                   and isinstance(item := action.get('item'), dict)
                   and isinstance(code := item.get('drug_code'), str))


def missing_committed_products(observation, response):
    """Check affirmative prescribing statements against the clinician's own current transactions."""
    issued = {code for _, code in _product_actions(response)}
    proposed = [tail.casefold() for _, tail in _order_proposals(response)]
    if not proposed:
        return set()
    chinese_names = {match[1] for clause in proposed
                     if (match := re.match(r'^[\s:：]*([\u3400-\u9fff]{3,})', clause))}
    products = {}
    for item in observation.get('formulary', []):
        code = item.get('drug_code')
        if not isinstance(code, str):
            continue
        for field in ('name', 'ingredient', 'display_name_zh', 'drug_code'):
            alias = item.get(field)
            if isinstance(alias, str) and alias.strip():
                products.setdefault(alias.strip().casefold(), set()).add(code)
                if field == 'ingredient':
                    for ingredient in alias.split('+'):
                        if ingredient.strip():
                            products.setdefault(ingredient.strip().casefold(), set()).add(code)
        label = item.get('display_name_zh')
        if isinstance(label, str):
            for name in chinese_names:
                if name in label:
                    products.setdefault(name, set()).add(code)
    return {name for name, codes in products.items() if not codes & issued and (
            any(name in clause for clause in proposed) if re.search(r'[\u3400-\u9fff]', name) else
            any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', clause) for clause in proposed))}


def order_commitment_errors(observation, response):
    if 'signed_order_lines' not in observation or response.get('decision') not in ('none', 'keep', 'revise', 'stop_all'):
        return []
    correction = observation.get('schema_correction', {})
    previous = {} if correction.get('unaddressed_signed_order_ids') else correction.get('previous_invalid_response') or {}
    missing = missing_committed_products(observation, response)
    if previous:
        # In a format correction the original affirmative statement must still be backed by an action.
        missing.update(missing_committed_products(observation, {**previous, 'order_actions': response.get('order_actions')}))
    errors = []
    if missing:
        errors.append('Enter a complete order action for each medicine you committed to start now: ' + ', '.join(sorted(missing)) + '.')
    selected = _product_actions(response)
    if not missing and not selected and (declares_prescription(response) or declares_prescription(previous)):
        errors.append('Supply the complete order action for the prescription you described.')
    visible_codes = {item.get('drug_code') for item in observation.get('formulary', [])}
    for kind, code in sorted(_product_actions(previous) - selected):
        if code in visible_codes:
            errors.append(f'Keep the previously selected {kind} order for {code} when correcting its format.')
    chosen = declared_order_choices(response)
    for option, decision in declared_order_choices(previous).items():
        if chosen.get(option) != decision:
            errors.append(f'Keep your declared {decision} decision for {option} and supply its missing order action.')
    return errors


def validate_review(review, *, required, actions, signed_lines, advisory):
    if not isinstance(review, dict) or not review or required.keys() - review.keys():
        raise ValueError('Explain every required treatment option in treatment_review.')
    active = {row['request_id'] for row in signed_lines}
    retired = {action['request_id'] for action in actions if action['kind'] in {'stop', 'change'}}
    retained = active - retired
    accounted_for, maintained, considered = set(), set(), set()
    for option, entry in review.items():
        if (not isinstance(entry, dict) or entry.get('decision') not in {'order', 'maintain', 'stop', 'omit'}
                or not isinstance(entry.get('reason'), str) or not entry['reason'].strip()):
            raise ValueError(f'Treatment option {option} needs a decision and a reason.')
        indices = entry.get('action_indices')
        if not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(actions) for i in indices):
            raise ValueError(f'Treatment option {option} must reference valid order_actions indices, or [].')
        ids, advice = entry.get('maintained_order_ids', []), entry.get('advice_codes', [])
        if not _string_list(ids) or set(ids) - retained or not _string_list(advice) or set(advice) - advisory:
            raise ValueError(f'Treatment option {option} refers to unknown retained orders or advice.')
        expected = ('order' if any(actions[i]['kind'] != 'stop' for i in indices) else
                    'stop' if indices else 'maintain' if ids else 'omit')
        if entry['decision'] != expected:
            raise ValueError(f'Treatment option {option} has decision {entry["decision"]!r} but its order references imply {expected!r}.')
        accounted_for.update(indices)
        maintained.update(ids)
        considered.update(advice)
    if accounted_for != set(range(len(actions))) or maintained != retained or considered != advisory:
        raise ValueError(REVIEW_COVERAGE_ERROR)


def missing_advice_review(response, observation):
    review = response['treatment_review']
    mentioned = {code for entry in review.values() for code in entry.get('advice_codes', [])}
    try:
        validate_review(review, required=observation['required_treatment_options'], actions=response['order_actions'],
                        signed_lines=observation['signed_order_lines'], advisory=mentioned)
    except ValueError:
        return []
    return sorted({row['drug_code'] for row in observation['medication_advice']['items']} - mentioned)


def unaddressed_signed_orders(response, signed_lines):
    review = response.get('treatment_review')
    if not isinstance(review, dict):
        return []
    addressed = {row['request_id'] for row in response['order_actions'] if row['kind'] in {'stop', 'change'}}
    addressed.update(request_id for entry in review.values() for request_id in entry.get('maintained_order_ids', []))
    return sorted({line['request_id'] for line in signed_lines} - addressed)


def complete_advice_review(submitted, correction):
    response = deepcopy(correction['previous_invalid_response'])
    review = response['treatment_review']
    details = submitted.get('advice_review')
    required = correction['advice_products_to_review']
    if set(submitted) != {'advice_review'} or not isinstance(details, dict) or set(details) != set(required):
        raise ValueError('Return one advice_review entry for each specified advisory product.')
    for code, entry in details.items():
        if (not isinstance(entry, dict) or set(entry) != {'treatment_option', 'reason'}
                or not isinstance(entry['treatment_option'], str) or entry['treatment_option'] not in review
                or not isinstance(entry['reason'], str) or not entry['reason'].strip()):
            raise ValueError('Give each specified advisory product a valid treatment option and rationale.')
        related = review[entry['treatment_option']]
        related.setdefault('advice_codes', []).append(code)
        related['reason'] += f' Advisory product {code}. {entry["reason"].strip()}'
    return response


def validate_omitted_products(rows, *, visible_codes, active_codes, check_ids=None):
    if not isinstance(rows, list):
        raise ValueError('Return considered_not_ordered as a list of explicitly omitted products.')
    used = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'drug_code', 'reason', 'check_ids'}:
            raise ValueError('Each considered_not_ordered entry needs a product code, reason and check_ids.')
        code = row['drug_code']
        if (not isinstance(code, str) or code in used or code not in visible_codes or code in active_codes
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or not _string_list(row['check_ids'])
                or (check_ids is not None and set(row['check_ids']) - check_ids)):
            raise ValueError('Use distinct visible, unprescribed product codes and valid prescribing check IDs.')
        used.add(code)


def materialize_treatment_orders(submitted, observation):
    record_omissions = observation.get('record_omission_decisions')
    active_codes = set()
    if observation.get('single_prescription'):
        submitted = single_prescription_actions(submitted, observation['signed_order_lines'])
        if record_omissions:
            retained = {rid for option in submitted['treatments'].values() for rid in option['maintained_order_ids']}
            active_codes = {line['item']['drug_code'] for line in observation['signed_order_lines']
                            if line['request_id'] in retained}
            active_codes.update(order['item'].get('drug_code') for option in submitted['treatments'].values()
                                for order in option['orders'] if order['kind'] == 'new')
    decisions = submitted.get('treatments')
    fields = {'decision', 'reason', 'outstanding_checks', 'check_results', 'treatments', 'advice_review'}
    if record_omissions:
        fields.add('considered_not_ordered')
    if (submitted.get('decision') != 'sign' or set(submitted) != fields or not isinstance(decisions, dict)
            or set(observation['required_treatment_options']) - decisions.keys()):
        raise ValueError('Submit decision=sign with one entry in treatments for each required option and all prescribing checks.')
    actions, review = [], {}
    for option, entry in decisions.items():
        if (not isinstance(entry, dict) or set(entry) != {'orders', 'maintained_order_ids', 'reason'}
                or not isinstance(entry['orders'], list) or not _string_list(entry['maintained_order_ids'])
                or not all(isinstance(order, dict) and order.get('kind') in ACTION_TYPES for order in entry['orders'])
                or not isinstance(entry['reason'], str) or not entry['reason'].strip()):
            raise ValueError(f'Treatment {option} needs explicit orders, maintained order IDs and a reason.')
        selected = entry['orders']
        retained = entry['maintained_order_ids']
        decision = ('order' if any(order['kind'] != 'stop' for order in selected) else
                    'stop' if selected else 'maintain' if retained else 'omit')
        review[option] = {'decision': decision, 'reason': entry['reason'],
                          'action_indices': list(range(len(actions), len(actions) + len(selected))),
                          'maintained_order_ids': retained, 'advice_codes': []}
        actions.extend(selected)
    response = {key: submitted[key] for key in ('reason', 'outstanding_checks', 'check_results')}
    if record_omissions:
        rows = submitted['considered_not_ordered']
        validate_omitted_products(rows, visible_codes=set(observation['orderable_drug_codes']),
                                  active_codes=active_codes, check_ids=set(observation['required_prescribing_checks']))
        response['considered_not_ordered'] = deepcopy(rows)
    if isinstance(response['check_results'], dict):
        findings = {}
        for key, finding in response['check_results'].items():
            affected = finding.get('affected_treatments') if isinstance(finding, dict) else None
            if isinstance(affected, list) and affected and all(option == 'all' for option in affected):
                finding = {**finding, 'affected_treatments': list(review)}
            findings[key] = finding
        response['check_results'] = findings
    response.update(decision='revise' if actions else 'none', order_actions=actions, treatment_review=review)
    advice = [row['drug_code'] for row in observation.get('medication_advice', {}).get('items', [])]
    return complete_advice_review({'advice_review': submitted['advice_review']}, {
        'previous_invalid_response': response, 'advice_products_to_review': advice})


def single_prescription_actions(submitted, signed_lines):
    """Translate the prescriber's complete order into the native version changes."""
    decisions = submitted.get('treatments')
    if not isinstance(decisions, dict) or 'previous_signed_regimen' in decisions:
        raise ValueError('Use the listed treatment options for a complete prescription.')
    outstanding = list(signed_lines)
    treatments = {}
    for option, entry in decisions.items():
        if (not isinstance(entry, dict) or set(entry) != {'items', 'reason'}
                or not isinstance(entry['items'], list) or any(not isinstance(item, dict) for item in entry['items'])):
            raise ValueError('Each treatment option needs complete medication items and a reason.')
        new_orders, retained = [], []
        for item in entry['items']:
            prior = next((line for line in outstanding if line['item'] == item), None)
            if prior is None:
                new_orders.append({'kind': 'new', 'item': item})
            else:
                outstanding.remove(prior)
                retained.append(prior['request_id'])
        treatments[option] = {'orders': new_orders, 'maintained_order_ids': retained, 'reason': entry['reason']}
    if outstanding:
        treatments['previous_signed_regimen'] = {
            'orders': [{'kind': 'stop', 'request_id': line['request_id']} for line in outstanding],
            'maintained_order_ids': [], 'reason': submitted['reason'],
        }
    return {**submitted, 'treatments': treatments}


def complete_administration_instructions(response, completion):
    instructions = response.get('administration_instructions')
    if (set(response) != {'administration_instructions'} or not isinstance(instructions, dict)
            or set(instructions) != set(completion['lines'])
            or not _string_list(list(instructions.values()))):
        raise ValueError('Supply an administration instruction for each returned order line.')
    corrected = deepcopy(completion['prior_submission'])
    items = [item for option in corrected['treatments'].values() for item in option['items']]
    for line, instruction in instructions.items():
        items[int(line) - 1]['administration_instructions'] = instruction
    return corrected


def complete_dose_units(response, completion):
    corrections = response.get('dose_corrections')
    if (set(response) != {'dose_corrections'} or not isinstance(corrections, dict)
            or set(corrections) != set(completion['lines'])):
        raise ValueError('Supply the dose and unit for each returned order line.')
    corrected = deepcopy(completion['prior_submission'])
    items = [item for option in corrected['treatments'].values() for item in option['items']]
    for line, fields in corrections.items():
        if not isinstance(fields, dict) or set(fields) != {'dose', 'dose_unit'}:
            raise ValueError('Correct only the dose and dose unit on each returned line.')
        dose = fields['dose']
        if (type(dose) not in (int, float) or dose <= 0 or (isinstance(dose, float) and not isfinite(dose))
                or fields['dose_unit'] not in completion['lines'][line]):
            raise ValueError('Supply a positive dose and its listed unit for each returned line.')
        items[int(line) - 1].update(fields)
    return corrected


def advice_decisions(observation, response):
    suggested = observation.get('medication_advice', {}).get('items', [])
    decisions = []
    selected = response['prescription']['items']
    for index, recommendation in enumerate(suggested):
        code = recommendation['drug_code']
        option, review = next((option, review) for option, review in response['treatment_review'].items()
                              if code in review.get('advice_codes', []))
        def same_regimen(item):
            return all(item.get(key) == recommendation.get(key) for key in
                       ('drug_code', 'dose', 'dose_unit', 'route', 'frequency', 'quantity', 'quantity_unit',
                        'device', 'administration_instructions', 'intended_course'))
        status = ('accepted' if any(same_regimen(item) for item in selected) else
                  'modified' if review['decision'] in {'order', 'maintain'} else 'not_adopted')
        decisions.append({'recommendation_index': index, 'product_code': code, 'status': status,
                          'treatment_option': option, 'reason': review['reason']})
    return decisions


def check_scope_errors(findings, reviewed):
    errors = []
    for check, entry in findings.items() if isinstance(findings, dict) else ():
        if not isinstance(entry, dict) or entry.get('status') not in {'unresolved', 'reviewed_with_limits'}:
            continue
        affected = entry.get('affected_treatments')
        reason = entry.get('decision_basis') or entry.get('scope_reason')
        if (not _string_list(affected) or set(affected) - reviewed.keys()
                or not isinstance(reason, str) or not reason.strip()):
            errors.append(f'In check_results.{check} use valid treatment option IDs in affected_treatments and give decision_basis.')
    return errors


def permits_other_treatments(check, finding, review):
    """Allow a checked but unavailable fact only outside orders it affects."""
    return (not check.startswith('allergy_')
            and finding.get('status') in {'unresolved', 'reviewed_with_limits'}
            and not any(review[option]['decision'] in {'order', 'maintain'}
                        for option in finding['affected_treatments']))
