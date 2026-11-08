"""Pure native-order encoding, shared by preflight and the actual write.

Encoding is not clinical approval. An unrepresentable candidate can still be
reviewed in the local journal; it must be explicitly revised before native
signature or payment. No route, dose, quantity or schedule is substituted.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .regimen import normalize_frequency

VERSION = 'native_order_encoding_preflight_v2'


class OrderEncodingError(ValueError):
    def __init__(self, code, item_index=None, field=None):
        super().__init__(code)
        self.finding = {'code':code,'item_index':item_index,'field':field}


def encode_items(items, catalogue):
    if not isinstance(items,list) or not items:
        raise OrderEncodingError('empty_or_invalid_prescription')
    encoded=[];frequencies=[];units=[]
    for i,item in enumerate(items):
        def invalid(code,field):raise OrderEncodingError(code,i,field)
        if not isinstance(item,dict):invalid('invalid_item','items')
        code=str(item.get('drug_code',item.get('medication_code','')))
        mapping=catalogue.get(code)
        if not mapping:invalid('unmapped_product','drug_code')
        for field in ('dose','quantity'):
            value=item.get(field)
            try:valid=not isinstance(value,bool) and Decimal(str(value)).is_finite() and Decimal(str(value))>0
            except (InvalidOperation,TypeError,ValueError):valid=False
            if not valid:invalid('invalid_positive_number',field)
        try:frequency=normalize_frequency(item.get('frequency'),mapping['frequency_codes'])
        except (ValueError,TypeError):invalid('unmapped_or_ambiguous_frequency','frequency')
        instructions=item.get('administration_instructions')
        if instructions is not None and (not isinstance(instructions,str) or not instructions.strip()):
            invalid('invalid_administration_instructions','administration_instructions')
        if frequency['canonical']=='PRN' and instructions is None:
            invalid('prn_instructions_missing','administration_instructions')
        dose_codes=mapping.get('dose_unit_codes') or {mapping['source_dose_unit']:mapping['dose_unit_code']}
        unit=item.get('dose_unit')
        if not isinstance(unit,str) or unit not in dose_codes:invalid('unmapped_dose_unit','dose_unit')
        route=item.get('route')
        if not isinstance(route,str) or route not in mapping['route_codes']:invalid('unmapped_route','route')
        if item.get('quantity_unit')!=mapping['source_quantity_unit']:
            invalid('unmapped_quantity_unit','quantity_unit')
        frequencies.append(frequency)
        units.append({'original':unit,'native_code':dose_codes[unit],
            'numeric_value_unchanged':True,'quantity_unit_unchanged':True,
            'version':mapping.get('catalogue_version','legacy')})
        encoded.append({'medicationDefinitionId':str(mapping['medicationDefinitionId']),
            'quantity':deepcopy(item['quantity']),'unitCode':mapping['quantity_unit_code'],
            'dose':deepcopy(item['dose']),'doseUnitCode':dose_codes[unit],
            'methodCode':mapping['route_codes'][route],
            'rateCode':mapping['frequency_codes'][frequency['canonical']],
            'dispensePerDuration':item.get('duration_days',1),
            'dispenseLocationId':str(mapping['dispenseLocationId'])})
    return {'medications':encoded,'frequency_normalizations':frequencies,'dose_unit_mappings':units}


def preflight(prescription,catalogue):
    result={'version':VERSION,'native_encodable':False,'findings':[],
        'clinical_appropriateness':'not_assessed','native_write_attempted':False,
        'automatic_substitution':False}
    items = prescription.get('items') if isinstance(prescription, dict) else None
    if not isinstance(items, list) or not items:
        result['findings'].append(OrderEncodingError('empty_or_invalid_prescription').finding)
        return result
    # Report an actionable error for each invalid item in one pass. Use the
    # same encoder as the native write; no clinical field is changed.
    for index, item in enumerate(items):
        try:
            encode_items([item], catalogue)
        except OrderEncodingError as exc:
            finding = {**exc.finding, 'item_index': index}
            if isinstance(item, dict):
                code = str(item.get('drug_code', item.get('medication_code', '')))
                finding['drug_code'] = code
                mapping = catalogue.get(code) or {}
                field = finding['field']
                if mapping and field == 'frequency':
                    finding['accepted_field_values'] = sorted(mapping['frequency_codes'])
                elif mapping and field == 'route':
                    finding['accepted_field_values'] = sorted(mapping['route_codes'])
                elif mapping and field == 'dose_unit':
                    finding['accepted_field_values'] = sorted(
                        mapping.get('dose_unit_codes') or {mapping['source_dose_unit']})
                elif mapping and field == 'quantity_unit':
                    finding['accepted_field_values'] = [mapping['source_quantity_unit']]
            result['findings'].append(finding)
    if result['findings']:
        result['revision_instruction'] = (
            'Resolve the listed order-entry fields using the exact product and native codes. '
            'Use a listed representation only if it expresses the intended regimen. '
            'A format correction is not clinical approval; do not change a clinical decision to fit a field.')
    result['native_encodable'] = not result['findings']
    return result
