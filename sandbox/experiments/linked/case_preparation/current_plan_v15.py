"""Avoid multiplying historical order patterns in a constructed current plan.

The source is not a current medication reconciliation. This authoring revision
keeps every historical row, and changes only the explicitly fictional prior
plan and the matching patient report. It never reads an execution outcome.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import math
import os
from pathlib import Path
import re

from .formulary_overlay import file_hash, write
from .case_contract import _text as source_text
from ..cases import validate_case
from ..chart import HistoryChart
from ..chart_projection import project_history_page, verify_projection
from ..contracts import digest, runtime_case

VERSION = 'constructed_single_order_plan_v15'
POLICY = {
    'version': VERSION, 'formal_eligible': False,
    'development_feedback_informed': True, 'reads_model_outputs': False,
    'source_patients_replaced': False,
    'logical_order': 'Nonempty private subject/admission/POE order identity, MAIN component, identical exact raw drug name, route, start/stop times, dose and unit, daily count and corroborated scheduled frequency. These are alternative source patterns for one constructed plan choice, not proven interchangeable products or source administrations.',
    'choice': 'Within an eligible logical-order group retain the already selected core pattern, otherwise the lexically first historical record identifier. Preserve all other source rows. Never sum identical ordered doses across pharmacy formulations.',
    'sequential_patterns': 'For the same exact drug name, route and explicit release class, nonoverlapping source order intervals may supply a single latest fictional plan pattern. All older intervals must end before the latest starts. Overlap, unknown release/times, or loss of the selected disease-core pattern leaves the group unchanged and flagged.',
    'current_use': 'The fictional patient used only the adopted pattern in the already defined episode. Nonadopted patterns remain visible as not taken in this constructed window; this does not establish actual source treatment, discharge intent or therapeutic equivalence.',
    'preserved': 'Original history, laboratory abnormalities, allergy status, patient identity and partition, all earlier case versions and execution outcomes. No prescription approval or normal-control label is assigned.',
    'references': ['https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html',
                   'https://mimic.mit.edu/docs/iv/modules/hosp/poe.html',
                   'https://mimic.mit.edu/docs/iv/modules/hosp/pharmacy.html'],
}


def _text(value):
    return str(value or '').strip().casefold()


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_private_order_links(case, private_data_root, source_package_root=None):
    """Return hashed order links only; raw source identifiers never enter a case."""
    provenance = case.get('provenance', {})
    while not provenance.get('source_case_id') and provenance.get('original_provenance'):
        provenance = provenance['original_provenance']
    source_id = provenance.get('source_case_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source_id):
        raise ValueError('Preserved source package identity is missing')
    bases = [Path(private_data_root)/name/'source_packages'/source_id for name in
             ('development_pilot50_v3', 'development_pilot50_replacement_v1', 'scaled_candidate_inputs_v1')]
    if source_package_root is not None:
        bases = [Path(source_package_root)/source_id]
    found = [p/'provenance.json' for p in bases if (p/'provenance.json').exists()]
    if len(found) != 1:
        raise ValueError('Expected exactly one immutable source package')
    path = found[0]
    source_hash = file_hash(path)
    source = json.loads(path.read_text())
    links = {}
    for order in case['his_initial']['historical_orders']:
        raw = source['source_rows'].get(order.get('source_ref'), {}).get('row')
        if not raw:
            continue
        # Verify the link really describes the unchanged historical pattern.
        regimen = order.get('regimen_raw', {})
        if source_text(raw.get('drug')) != order.get('drug_name_raw') or any(
                source_text(raw.get(k)) != regimen.get(k) for k in
                ('prod_strength', 'dose_val_rx', 'dose_unit_rx', 'doses_per_24_hrs', 'route')):
            raise ValueError('Private source row and retained historical pattern disagree')
        ids = [str(raw.get(k) or '').strip() for k in ('subject_id', 'hadm_id', 'poe_id')]
        links[order['record_id']] = {
            'logical_order_token': digest(ids) if all(ids) else None,
            'source_row_sha256': digest(raw), 'source_file_sha256': source_hash,
            'source_row_ref': order['source_ref'],
        }
    return links, {str(path): source_hash}


def _release_class(item):
    # Explicit marker only. No broad ingredient mapping or equivalence claim.
    value = _text(item.get('name')) + ' ' + _text(item.get('strength'))
    if re.search(r'\b(xl|xr|er|sr|cr|extended[- ]release|controlled[- ]release)\b', value):
        return 'explicit_modified_release'
    return None


def _logical_signature(item, order, link):
    token = link.get('logical_order_token')
    time = order.get('time', {})
    start, stop = _number(time.get('event_offset_hours')), _number(order.get('planned_stop_offset_hours'))
    dose, daily = _number(item.get('dose')), _number(item.get('planned_daily_dose_count'))
    if (not token or order.get('prescription_component_type_raw') != 'MAIN'
            or start is None or stop is None or dose is None or daily is None
            or not item.get('dose_unit') or not item.get('frequency')):
        return None
    return (token, _text(item.get('name')), _text(item.get('route')), start, stop,
            dose, _text(item['dose_unit']), daily, _text(item['frequency']))


def build(source, order_links):
    validate_case(source)
    case = deepcopy(source)
    current = case['his_initial'].get('current_use_record', {})
    items = current.get('items', [])
    orders = {o['record_id']: o for o in case['his_initial']['historical_orders']}
    core = case.get('provenance', {}).get('core_source_ref')
    scheduled = [i for i in items if i.get('plan')]
    logical, grouped = defaultdict(list), defaultdict(list)
    for item in scheduled:
        if item.get('source_kind') != 'constructed' or item['source_ref'] not in orders:
            raise ValueError('Only explicitly constructed, source-linked plans may be revised')
        signature = _logical_signature(item, orders[item['source_ref']], order_links.get(item['source_ref'], {}))
        if signature is not None:
            logical[signature].append(item)
    removals, decisions, unresolved = {}, [], []
    for signature, group in sorted(logical.items(), key=lambda entry: str(entry[0])):
        if len(group) < 2:
            continue
        winner = next((i for i in group if i['source_ref'] == core), None)
        winner = winner or min(group, key=lambda i: i['source_ref'])
        others = [i for i in group if i is not winner]
        reason = 'one_logical_source_order_one_constructed_pattern'
        for item in others:
            removals[item['record_id']] = (winner, reason)
        decisions.append({'reason': reason, 'retained_source_ref': winner['source_ref'],
            'nonadopted_source_refs': [i['source_ref'] for i in others],
            'logical_order_token': signature[0], 'historical_interchangeability_claimed': False})
    for item in scheduled:
        if item['record_id'] not in removals:
            grouped[(_text(item.get('name')), _text(item.get('route')))].append(item)
    for key, group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        times = [(i, _number(orders[i['source_ref']].get('time', {}).get('event_offset_hours')),
                  _number(orders[i['source_ref']].get('planned_stop_offset_hours'))) for i in group]
        if any(start is None or stop is None or start > 0 or stop < start for _, start, stop in times):
            unresolved.append({'source_refs': [i['source_ref'] for i in group], 'reason': 'unknown_or_invalid_intervals'})
            continue
        winner, start, _ = max(times, key=lambda row: (row[1], row[0]['source_ref']))
        others = [row for row in times if row[0] is not winner]
        if (not _release_class(winner) or any(_release_class(i) != _release_class(winner) for i, _, _ in others)
                or any(stop >= start for _, _, stop in others)
                or any(i['source_ref'] == core for i, _, _ in others)):
            unresolved.append({'source_refs': [i['source_ref'] for i in group],
                'reason': 'overlap_release_uncertainty_or_selected_core_would_change'})
            continue
        reason = 'latest_nonoverlapping_source_pattern_for_fictional_plan'
        for item, _, _ in others:
            removals[item['record_id']] = (winner, reason)
        decisions.append({'reason': reason, 'retained_source_ref': winner['source_ref'],
            'nonadopted_source_refs': [i['source_ref'] for i, _, _ in others],
            'source_discharge_intent_claimed': False})
    removed_facts, relinked_facts = [], []
    for item in items:
        if item['record_id'] not in removals:
            continue
        winner, reason = removals[item['record_id']]
        # Preserve the former scenario in provenance, not as active patient use.
        previous = deepcopy(item)
        for field in ('plan', 'actual_use', 'supply'):
            item.pop(field, None)
        item.update(status='nonadopted_historical_pattern_in_constructed_episode',
            current_use_status='reported_not_taken_in_defined_episode',
            planned_daily_dose_count=None,
            uncertainty='Historical treatment and product equivalence are not inferred. This pattern was not adopted into the fictional prior plan; current clinicians still assess the retained treatment.',
            construction_choice={'version': VERSION, 'reason': reason,
                'retained_current_item_ref': winner['record_id'], 'source_kind': 'constructed'})
        decisions.append({'nonadopted_item_ref': item['record_id'], 'previous_constructed_item': previous})
    facts = case['patient_initial']['patient_facts']
    kept_facts = []
    for fact in facts:
        refs = set(fact.get('source_refs', [])) & set(removals)
        if not refs:
            kept_facts.append(fact)
            continue
        if (fact.get('topic') == 'chief_complaint' and
                fact.get('construction_basis') == 'salience_of_existing_declared_supply_not_new_clinical_information'):
            retained_refs = [r for r in fact['source_refs'] if r not in removals]
            available = {i['record_id']: i for i in items if i.get('plan')}
            if not retained_refs or any(r not in available for r in retained_refs) or not any(
                    0 <= available[r].get('supply', {}).get('remaining_dose_equivalents', math.inf)
                    <= available[r]['planned_daily_dose_count'] for r in retained_refs):
                raise ValueError('Remaining supply no longer supports the original visit reason')
            relinked_facts.append(deepcopy(fact))
            fact['source_refs'] = retained_refs
            kept_facts.append(fact)
            continue
        if len(refs) != 1 or set(fact.get('source_refs', [])) != refs or fact.get('topic') not in {
                'current_medication', 'actual_medication_use', 'current_product_label'}:
            raise ValueError('Ambiguous patient-memory dependency; refuse a partial rewrite')
        removed_facts.append(deepcopy(fact))
    for record_id, (winner, _) in removals.items():
        item = next(i for i in items if i['record_id'] == record_id)
        kept_facts.append({'fact_id': 'plan15-' + digest(record_id)[:16], 'section': 'current_episode',
            'topic': 'nonadopted_medication_pattern', 'field': 'nonadopted_medication_pattern',
            'value': f"旧单上{item['name']}、{item['strength']}这一项，这六天没有用；这次带来的用法记录选的是{winner['name']}、{winner['strength']}，没有把这两项同时用。",
            'status': 'reported', 'source_kind': 'constructed',
            'source_refs': [record_id, winner['record_id']], 'knowledge_state': 'known_from_brought_record',
            'statement_as_of_offset_hours': current.get('as_of_offset_hours'),
            'narrative_time_scope': 'Defined fictional prior-plan use window only; no source administration inference.',
            'construction_version': VERSION})
    case['patient_initial']['patient_facts'] = kept_facts
    if removals:
        current['current_plan_item_count'] = sum(bool(i.get('plan')) for i in items)
        current['construction_policy_version'] = VERSION
        case['evaluation']['reference_facts'] = deepcopy(kept_facts)
    case['case_id'] += '_plan15'
    case.setdefault('provenance', {})['current_plan_v15'] = {
        'policy': deepcopy(POLICY), 'parent_case_id': source['case_id'], 'parent_case_hash': digest(source),
        'decisions': decisions, 'unresolved_groups': unresolved, 'removed_prior_scenario_facts': removed_facts,
        'previous_supply_reason_reference_links': relinked_facts,
        'order_links': {i['source_ref']: deepcopy(order_links.get(i['source_ref'])) for i in scheduled},
    }
    for key, value in source['his_initial'].items():
        if key != 'current_use_record' and case['his_initial'].get(key) != value:
            raise ValueError('Source history, observations or allergy state changed')
    for key in ('patient_key', 'group', 'split', 'data_kind', 'formulary'):
        if case[key] != source[key]:
            raise ValueError('Identity, partition or catalogue changed')
    validate_case(case)
    chart = HistoryChart(case['his_initial'])
    for section in ('current_use_record', 'current_use_items'):
        offset = 0
        while True:
            page = chart.query(section, offset=offset, limit=5)
            if not verify_projection(page, project_history_page(page)):
                raise ValueError('Clinical current-use projection lost facts')
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
    return case, {'nonadopted_pattern_count': len(removals), 'unresolved_groups': unresolved,
        'source_history_unchanged': True, 'current_tests_and_allergies_unchanged': True,
        'current_clinical_decision_assigned': False}


def prepare(manifest_path, private_data_root, output):
    manifest_path, output = Path(manifest_path), Path(output)
    if output.exists():
        raise FileExistsError('Do not overwrite previous input versions')
    manifest = json.loads(manifest_path.read_text())
    protected = {str(manifest_path): file_hash(manifest_path), str(Path(__file__)): file_hash(__file__)}
    policy_freeze = {'frozen_at': datetime.now(timezone.utc).isoformat(),
        'policy': POLICY, 'source_hashes': deepcopy(protected)}
    prepared = []
    for entry in manifest['cases']:
        path = Path(entry['path'])
        raw_hash = file_hash(path)
        if raw_hash != entry.get('file_sha256', entry.get('sha256')):
            raise ValueError('Frozen input changed')
        source = json.loads(path.read_text())
        if source['split'] != 'development' or source['data_kind'] != 'mimic_derived':
            raise ValueError('This engineering constructor accepts only MIMIC-derived development inputs')
        protected[str(path)] = raw_hash
        names = Counter((_text(i.get('name')), _text(i.get('route'))) for i in
                        source['his_initial'].get('current_use_record', {}).get('items', []) if i.get('plan'))
        # Private order identities are needed only for possible duplicated plans.
        links, hashes = load_private_order_links(source, private_data_root) if any(n > 1 for n in names.values()) else ({}, {})
        protected.update(hashes)
        case, audit = build(source, links)
        prepared.append((source, case, audit, raw_hash))
    if any(file_hash(p) != h for p, h in protected.items()):
        raise ValueError('Source changed while preparing inputs')
    output.mkdir(parents=True, mode=0o700)
    write(output/'policy_frozen_before_cases.json', policy_freeze)
    rows, audits = [], []
    for source, case, audit, raw_hash in prepared:
        target = output/'cases'/(case['case_id'] + '.json')
        write(target, case)
        rows.append({**{k: case[k] for k in ('case_id', 'patient_key', 'group', 'split', 'data_kind')},
            'path': str(target.resolve()), 'file_sha256': file_hash(target), 'case_hash': digest(case),
            'runtime_input_hash': digest(runtime_case(case)), 'formal_eligible': False,
            'source_case_id': source['case_id'], 'source_file_sha256': raw_hash})
        audits.append({'case_id': case['case_id'], 'group': case['group'], **audit})
    if any(file_hash(p) != h for p, h in protected.items()):
        raise ValueError('Source changed while preparing inputs')
    write(output/'manifest.json', {'version': VERSION, 'cases': rows, 'case_count': len(rows),
        'formal_eligible': False, 'policy': POLICY})
    write(output/'input_audit.json', {'cases': audits, 'source_hashes': protected, 'clinical_outputs_read': False})
    summary = {'version': VERSION, 'source_patients': len(rows),
        'groups': dict(Counter(r['group'] for r in rows)),
        'cases_with_revised_current_plan': sum(a['nonadopted_pattern_count'] > 0 for a in audits),
        'nonadopted_patterns': sum(a['nonadopted_pattern_count'] for a in audits),
        'unresolved_groups': sum(len(a['unresolved_groups']) for a in audits),
        'source_history_changed': False, 'model_calls': 0, 'execution_started': False}
    write(output/'summary.json', summary)
    return summary


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('manifest', 'private-data-root', 'output'):
        parser.add_argument('--' + arg, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.manifest, args.private_data_root, args.output)))
