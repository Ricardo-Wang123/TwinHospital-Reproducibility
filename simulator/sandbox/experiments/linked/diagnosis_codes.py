"""Lossless ICD-10 display normalization against a returned HIS catalogue."""
from copy import deepcopy
import re


POLICY = {
    'version': 'same_system_icd10_decimal_v1',
    'allowed_change': 'insert or remove the display decimal after character 3',
    'matching': 'one exact alphanumeric code within the same declared system',
    'clinical_inference': False,
    'source_response_preserved': True,
    'references': [
        'https://www.cms.gov/files/document/2020-icd-10-cm-codes-file-pdf.pdf',
        'https://www.cms.gov/files/document/032310icd10quickreferencepdf'],
}
SYSTEMS = {'ICD-10', 'ICD-10-CM'}
CODE = re.compile(r'[A-Z][0-9][A-Z0-9](?:\.?[A-Z0-9]{1,4})?\Z')


def canonicalize(diagnoses, catalogue):
    """Never change a disease, system, certainty, primary flag or other field."""
    result = deepcopy(diagnoses)
    changes = []
    if not isinstance(result, list):
        return result, changes
    for index, row in enumerate(result):
        if not isinstance(row, dict):
            continue
        code, system = row.get('code'), row.get('system')
        if system not in SYSTEMS or not isinstance(code, str) or not CODE.fullmatch(code):
            continue
        matches = [item for item in catalogue if isinstance(item, dict)
                   and item.get('system') == system and isinstance(item.get('code'), str)
                   and CODE.fullmatch(item['code'])
                   and item['code'].replace('.', '') == code.replace('.', '')]
        if len(matches) != 1 or matches[0]['code'] == code:
            continue
        row['code'] = matches[0]['code']
        changes.append({'diagnosis_index': index, 'system': system,
                        'original_code': code, 'catalogue_code': row['code'],
                        'reason': 'same_code_display_decimal_only'})
    return result, changes


def all_mapped(diagnoses, catalogue):
    allowed = {(row['system'], row['code']) for row in catalogue}
    return bool(isinstance(diagnoses, list) and diagnoses and all(
        isinstance(row, dict) and (row.get('system'), row.get('code')) in allowed
        for row in diagnoses))
