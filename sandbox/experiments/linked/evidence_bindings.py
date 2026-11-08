"""Development-only literal evidence resolution and exact product applicability.

Only the supplied, already-visible clinical snapshot is indexed. No case loader,
evaluation data, model, network, hidden source history or injection diff is read.
Resolving a citation proves literal support and visibility, not clinical truth.
Persisted indexes must be compared with the hash frozen in the run specification.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

VERSION = 'visible-evidence-bindings-dev-4-initial-laboratory-records'
REGISTRY_PATH = Path(__file__).with_name('product_applicability_dev_v1.json')
REVIEW_ROLES = ('doctor', 'pharmacist', 'senior_pharmacist', 'rx_ai', 'rule_engine')
CONTEXT_KEYS = {'hospital_history', 'handoff', 'current_encounter_measurements', 'consultation', 'conversation'}
FORBIDDEN = {'evaluation', 'provenance', 'reference_candidate', 'gold_label', 'clinical_gold_label',
    'expected_label', 'mutation_diff', 'truth', 'complete_truth', 'patient_view', 'his_initial',
    'subject_id', 'hadm_id', 'stay_id', 'patient_id', 'encounter_id', 'patient_name',
    'source_identifier_map', 'api_key', 'password', 'authorization'}
IDENTITY_KEYS = ('ingredient', 'dosage_form', 'strength', 'components', 'quantity_unit', 'allowed_routes')
UNSET = object()


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _guard(value):
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k.casefold() in FORBIDDEN for k in value):
            raise ValueError('Hidden evaluation, raw identity or secret fields are not allowed')
        for child in value.values():
            _guard(child)
    elif isinstance(value, list):
        for child in value:
            _guard(child)


def _pointer(value, pointer):
    """Strict JSON Pointer; no fuzzy keys, text interpretation or negative indexes."""
    if pointer == '':
        return value
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('A JSON Pointer is required')
    current = value
    for token in pointer[1:].split('/'):
        if re.search(r'~(?![01])', token):
            raise ValueError('Invalid JSON Pointer escaping')
        key = token.replace('~1', '/').replace('~0', '~')
        if isinstance(current, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', key):
                raise ValueError('Invalid array index')
            current = current[int(key)]
        elif isinstance(current, dict):
            current = current[key]
        else:
            raise ValueError('Pointer enters a scalar')
    return current


def _record_epistemic(record):
    kind = str(record.get('source_kind', '')) if isinstance(record, dict) else ''
    if 'constructed' in kind or 'synthetic' in kind or 'simulated' in kind:
        return 'constructed'
    if kind:
        return 'raw_record'
    return 'source_status_unspecified'


def build_visible_evidence(clinical_context, formulary, source_catalogue=None, *,
                           phase='prescription_review', available_to_roles=REVIEW_ROLES):
    """Index actual snapshot objects; missing and unqueried records stay missing.

    ``available_to_roles`` is a runtime declaration of the shared reviewer view,
    not authentication. The patient role is intentionally excluded. The caller
    must not pass the complete case or retrospectively fetched chart records.
    """
    if not isinstance(clinical_context, dict) or set(clinical_context) - CONTEXT_KEYS:
        raise ValueError('Only the actual clinical_context snapshot is accepted')
    if not phase or not available_to_roles or set(available_to_roles) - set(REVIEW_ROLES):
        raise ValueError('Explicit reviewer visibility is required')
    if not isinstance(formulary, list) or (source_catalogue is not None and not isinstance(source_catalogue, list)):
        raise ValueError('Frozen formularies must be lists')
    _guard(clinical_context); _guard(formulary); _guard(source_catalogue)
    entries = []

    def add(content, pointer, kind, epistemic, *, time_basis=None, sequence=None,
            record_ref=None, original_phase=None, projection=None, chart_section=None):
        if content is None:
            return
        entry = {'source_kind': kind, 'source_pointer': pointer, 'content': deepcopy(content),
            'source_hash': _hash(content), 'epistemic_status': epistemic,
            'available_phase': phase, 'available_to_roles': sorted(set(available_to_roles)),
            'available_after_sequence': sequence, 'original_phase': original_phase,
            'record_ref': record_ref, 'time_basis': deepcopy(time_basis),
            'source_claim_clinically_verified': False, 'chart_section': chart_section}
        if projection is not None:
            entry['source_projection'] = deepcopy(projection)
        entry['ref'] = 'ev_' + _hash(entry)[:32]
        entries.append(entry)

    history = clinical_context.get('hospital_history') or {}
    if not isinstance(history, dict) or set(history) - {'initial_view', 'queried_pages', 'last_query_feedback',
                                                      'visible_history_byte_limit', 'unshown_is_not_absent'}:
        raise ValueError('Hospital evidence must be the bounded visible chart, not complete history')
    initial = history.get('initial_view')
    add(initial, '/hospital_history/initial_view', 'visible_chart_directory',
        'directory_not_complete_record', time_basis=(initial or {}).get('time_basis'))
    overview = (initial or {}).get('historical_laboratory_overview')
    if overview is not None:
        if not isinstance(overview, dict) or overview.get('version') != 'fixed_analyte_latest_two_v1':
            raise ValueError('Unknown initial laboratory overview')
        for i, row in enumerate(overview.get('records', [])):
            if not isinstance(row, dict) or 'record' not in row or not row.get('record_ref'):
                raise ValueError('Initial laboratory rows require literal content and a stable reference')
            add(row['record'], f'/hospital_history/initial_view/historical_laboratory_overview/records/{i}/record',
                'initial_chart_record', _record_epistemic(row['record']),
                time_basis=(initial or {}).get('time_basis'), sequence=0,
                record_ref=row['record_ref'], original_phase='chart_open',
                chart_section='historical_observations')
    for i, query in enumerate(history.get('queried_pages', [])):
        page = query['page']
        for j, row in enumerate(page.get('records', [])):
            if 'record' not in row or not row.get('record_ref'):
                raise ValueError('Visible chart rows require content and stable record_ref')
            record = row['record']; projection = None
            if page.get('projection_version'):
                from .chart_projection import expand_projected_record
                record = expand_projected_record(page, row)
                projection = {'version': page['projection_version'],
                    'source_page_sha256': page.get('source_page_sha256'),
                    'source_defaults_pointer': f'/hospital_history/queried_pages/{i}/page/record_defaults',
                    'projected_row_hash': _hash(row['record']),
                    'reconstruction': 'explicit_page_defaults_overridden_by_row_fields',
                    'raw_page_independently_loaded': False}
            add(record, f'/hospital_history/queried_pages/{i}/page/records/{j}/record',
                'queried_chart_record', _record_epistemic(record),
                time_basis=page.get('time_basis'), sequence=query.get('queried_after_sequence'),
                record_ref=row['record_ref'], original_phase=query.get('phase'), projection=projection,
                chart_section=page.get('section'))
    panel = clinical_context.get('current_encounter_measurements') or {}
    if panel and (not isinstance(panel, dict) or panel.get('readback_verified') is not True):
        raise ValueError('Current measurement evidence requires an observed readback')
    for i, row in enumerate(panel.get('records', [])):
        if 'record' not in row or not row.get('record_ref'):
            raise ValueError('Visible measurement rows require stable record_ref')
        add(row['record'], f'/current_encounter_measurements/records/{i}/record',
            'current_measurement_readback', _record_epistemic(row['record']),
            time_basis=panel.get('time_basis'), sequence=panel.get('queried_after_sequence'),
            record_ref=row['record_ref'], original_phase=panel.get('available_at'))
    for key in ('handoff', 'consultation'):
        add(clinical_context.get(key), '/' + key, key, 'agent_reported')
    for i, segment in enumerate(clinical_context.get('conversation', [])):
        if segment.get('phase') not in {'doctor_handoff_verification', 'consultation'}:
            raise ValueError('Conversation outside physician visibility is not accepted')
        add(segment, f'/conversation/{i}', 'physician_visible_dialogue', 'agent_reported',
            sequence=segment.get('sequence'), original_phase=segment.get('phase'),
            time_basis={'available_at_ms': segment.get('available_at_ms'), 'clock': 'conversation_relative'})
    codes = set()
    for i, product in enumerate(formulary):
        if not product.get('drug_code') or product['drug_code'] in codes:
            raise ValueError('Frozen formulary needs unique product codes')
        codes.add(product['drug_code'])
        add(product, f'/formulary/{i}', 'frozen_formulary', 'constructed_product_identity')
    # Only source metadata for products actually present in the shared formulary.
    for i, product in enumerate(source_catalogue or []):
        if product.get('drug_code') in codes:
            add(product, f'/source_catalogue/{i}', 'frozen_catalogue_source', 'catalogue_identity_metadata')
    index = {'version': VERSION, 'phase': phase, 'entries': entries,
        'snapshot_hash': _hash({'clinical_context': clinical_context, 'formulary': formulary}),
        'clinical_facts_inferred': False, 'unknown_is_not_negative': True,
        'current_use_inferred_from_history': False, 'clinical_reference_status': 'independent_review_pending'}
    index['index_hash'] = _hash(index)
    return index


def _index_valid(index, expected_index_hash=None):
    if not isinstance(index, dict) or index.get('version') != VERSION:
        return False
    try:
        if _hash({k: v for k, v in index.items() if k != 'index_hash'}) != index.get('index_hash'):
            return False
        if expected_index_hash is not None and expected_index_hash != index['index_hash']:
            return False
        refs = [e['ref'] for e in index['entries']]
        return len(refs) == len(set(refs)) and all(e['source_hash'] == _hash(e['content']) and
            e['ref'] == 'ev_' + _hash({k: v for k, v in e.items() if k != 'ref'})[:32] for e in index['entries'])
    except (KeyError, TypeError, ValueError):
        return False


def resolve_evidence(index, ref, *, pointer='', expected_value=UNSET, unit_pointer=None,
                     expected_unit=UNSET, role, phase, at_sequence=None,
                     expected_time_basis=UNSET, required_epistemic_status=None, expected_index_hash=None):
    """Literal value/unit/time/role checks. No NLU, unit conversion or truth upgrade."""
    def unknown(reason):
        return {'status': 'indeterminate', 'reason': reason, 'ref': ref,
                'clinical_truth': 'not_established', 'version': VERSION}
    if not _index_valid(index, expected_index_hash):
        return unknown('evidence_index_integrity_mismatch')
    entries = [e for e in index['entries'] if e['ref'] == ref]
    if len(entries) != 1:
        return unknown('reference_not_in_visible_snapshot')
    entry = entries[0]
    if role not in entry['available_to_roles'] or phase != entry['available_phase']:
        return unknown('role_or_phase_outside_frozen_visibility')
    if at_sequence is not None and (entry['available_after_sequence'] is None or
                                    entry['available_after_sequence'] > at_sequence):
        return unknown('availability_sequence_unknown_or_in_future')
    if expected_time_basis is not UNSET and _hash(expected_time_basis) != _hash(entry['time_basis']):
        return unknown('time_basis_mismatch')
    if required_epistemic_status is not None and entry['epistemic_status'] != required_epistemic_status:
        return unknown('source_certainty_does_not_support_claim')
    try:
        value = _pointer(entry['content'], pointer)
        if expected_value is not UNSET and _hash(value) != _hash(expected_value):
            return unknown('literal_value_mismatch')
        unit = _pointer(entry['content'], unit_pointer) if unit_pointer is not None else None
        if expected_unit is not UNSET and (unit_pointer is None or _hash(unit) != _hash(expected_unit)):
            return unknown('literal_unit_mismatch_or_unavailable')
    except (KeyError, IndexError, TypeError, ValueError):
        return unknown('field_pointer_unavailable')
    return {'status': 'resolved', 'ref': ref, 'pointer': pointer, 'typed_value': deepcopy(value),
        'unit': deepcopy(unit), 'source_hash': entry['source_hash'], 'source_kind': entry['source_kind'],
        'source_pointer': entry['source_pointer'], 'epistemic_status': entry['epistemic_status'],
        'time_basis': deepcopy(entry['time_basis']), 'clinical_truth': 'not_established',
        'meaning': 'Literal support and declared snapshot visibility only', 'version': VERSION}


def validate_claims(index, claims, *, role, phase, expected_index_hash=None):
    """Resolve each typed claim without turning it into a new confirmed fact.

    Claim format: name, evidence_ref, pointer, value, optional unit_pointer/unit,
    time_basis and required_epistemic_status. Textual entailment is not attempted.
    """
    results = []
    for claim in claims:
        _guard(claim)
        result = resolve_evidence(index, claim['evidence_ref'], pointer=claim.get('pointer', ''),
            expected_value=claim['value'], unit_pointer=claim.get('unit_pointer'),
            expected_unit=claim.get('unit', UNSET), expected_time_basis=claim.get('time_basis', UNSET),
            required_epistemic_status=claim.get('required_epistemic_status'), role=role, phase=phase,
            expected_index_hash=expected_index_hash)
        results.append({'name': claim['name'], **result})
    return {'version': VERSION, 'results': results,
        'all_literals_resolved': all(x['status'] == 'resolved' for x in results) if results else False,
        'clinical_reference_status': 'independent_review_pending', 'clinical_gold_label': None}


def product_identity(product):
    """No synonym, salt, release, device or dose-number conversion is implicit."""
    return {k: deepcopy(product.get(k)) for k in IDENTITY_KEYS}


def load_applicability_registry():
    return json.loads(REGISTRY_PATH.read_text())


def build_product_bindings(index, *, registry=None):
    """Bind only indexed SIM definitions to explicit, rule-scoped label mappings.

    Cross-label mapping is a technical applicability record, never an assertion
    of marketed-product substitution, patient suitability or independent gold.
    Unknown products and changed form/strength stay pending, with no fallback.
    """
    if not _index_valid(index):
        raise ValueError('Evidence index integrity mismatch')
    registry = deepcopy(registry if registry is not None else load_applicability_registry())
    if registry.get('manifest_hash') != _hash({k: v for k, v in registry.items() if k != 'manifest_hash'}):
        raise ValueError('Product applicability registry integrity mismatch')
    bindings = {}
    for entry in index['entries']:
        if entry['source_kind'] != 'frozen_formulary':
            continue
        product = entry['content']; code = product['drug_code']
        mapping = registry['products'].get(code)
        row = {'drug_code': code, 'verification_status': 'indeterminate',
            'identity_check': 'not_established', 'evidence_ref': entry['ref'],
            'independent_review_status': 'independent_review_pending', 'clinical_gold_label': None,
            'patient_suitability': 'not_assessed', 'current_use_inferred': False,
            'mapping_version': registry['version'], 'mapping_manifest_hash': registry['manifest_hash']}
        bindings[code] = row
        if mapping is None:
            row['reason'] = 'no_explicit_rule_product_mapping'; continue
        row['reference_product_id'] = mapping['reference_product_id']
        if product_identity(product) != mapping['expected_formulary_identity']:
            row['reason'] = 'exact_ingredient_form_release_strength_unit_mismatch'; continue
        metadata = [e for e in index['entries'] if e['source_kind'] == 'frozen_catalogue_source'
                    and e['content'].get('drug_code') == code]
        if len(metadata) != 1:
            row['reason'] = 'source_catalogue_metadata_missing_or_ambiguous'; continue
        source = metadata[0]['content']
        if product_identity(source) != mapping['expected_formulary_identity']:
            row['reason'] = 'catalogue_identity_conflicts_with_formulary'; continue
        set_ids = sorted({s['setid'] for s in source.get('source_references', []) if s.get('setid')})
        if set_ids != sorted(mapping['catalogue_set_ids']):
            row['reason'] = 'catalogue_setid_not_covered_by_mapping'; continue
        required = set(set_ids) | {mapping['reference_set_id']}
        facts = registry['label_identity_records']
        if not required <= set(facts) or any(facts[s]['identity'] != mapping['label_identity'] for s in required):
            row['reason'] = 'label_identity_or_basis_not_equivalent_in_declared_scope'; continue
        if not all(set(mapping['required_label_facts']) <= set(facts[s]['supported_facts']) for s in required):
            row['reason'] = 'rule_applicability_not_supported_by_both_labels'; continue
        row.update(verification_status='verified', identity_check='exact_constructed_product_and_scoped_label_applicability',
            verification_scope='technical_identity_and_named_rule_scope_only', reason='explicit_mapping_checks_passed',
            catalogue_evidence_ref=metadata[0]['ref'], catalogue_set_ids=set_ids,
            reference_set_id=mapping['reference_set_id'], rule_scope=mapping['rule_scope'],
            label_identity=deepcopy(mapping['label_identity']),
            label_source_urls=[facts[s]['url'] for s in sorted(required)],
            label_evidence_record_hashes={s: _hash(facts[s]) for s in sorted(required)},
            archive_status=registry['archive_status'], full_label_snapshot_verified=False,
            clinical_gold_eligible=False, iv_appropriateness_inferred=False)
    return {'version': VERSION, 'index_hash': index['index_hash'], 'product_bindings': bindings,
        'independent_review_status': 'independent_review_pending', 'enabled_as_clinical_gold': False}
