"""Shared, versioned medication directory for prescribing and retrospective scoring."""
from functools import lru_cache
import json
from pathlib import Path
import re

from .contracts import digest


FILENAME = 'formulary_directory.json'
NAME_SYSTEM = 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-medication-name'
CODE_SYSTEM = 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-medication-formulary-drug-cd'
PAGE_SIZE = 8


def terms(value):
    return tuple(re.findall(r'[\w]+', value.casefold()))


class FormularyDirectory:
    def __init__(self, data):
        self.descriptor = {'version': data['version'], 'sha256': digest(data)}
        self.mapping_hash = data['native_mapping_sha256']
        self.products = {p['drug_code']: p for p in data['products']}
        if len(self.products) != len(data['products']):
            raise ValueError('Duplicate product identity in the shared formulary')
        self.names = data['official_names']
        self.codes = data['official_formulary_codes']
        self.entries = []
        for p in self.products.values():
            keys = [p['drug_code'], p['name'], p.get('source_formulary_code', ''), p.get('ingredient', '')]
            text = ' '.join([*keys, p.get('strength', ''), p.get('route', ''), p.get('display_name_zh', '')])
            self.entries.append((set(x.casefold() for x in keys if x), ' '.join(terms(text)), p))

    def require_mimic_products(self):
        names, codes = set(self.names), set(self.codes)
        if not self.products or any(
            not code.startswith('MIM-') or row.get('source_formulary_code') not in codes or row['name'] not in names
            for code, row in self.products.items()
        ):
            raise ValueError('MIMIC ED prescribing requires products from its source medication directory.')

    def search(self, query, offset=0):
        words = terms(query)
        if not words or len(query) > 120 or type(offset) is not int or offset < 0:
            raise ValueError('A formulary query requires search terms and a nonnegative offset')
        exact = query.strip().casefold()
        ranked = [(exact not in keys, p['name'].casefold(), p.get('strength', ''), p['drug_code'], p)
                  for keys, searchable, p in self.entries if all(w in searchable for w in words)]
        ranked.sort(key=lambda x: x[:4])
        def page(vocabulary):
            matches = [v for v in vocabulary if all(w in ' '.join(terms(v)) for w in words)]
            matches.sort(key=lambda v: (v.casefold() != exact, v.casefold(), v))
            return {'terms': matches[offset:offset + PAGE_SIZE], 'total': len(matches)}
        return {'query': query, 'offset': offset, 'page_size': PAGE_SIZE, 'orderable_total': len(ranked),
                'products': [r[-1] for r in ranked[offset:offset + PAGE_SIZE]],
                'official_names': page(self.names), 'official_formulary_codes': page(self.codes)}


@lru_cache(maxsize=4)
def _load(path, modified, size):
    return FormularyDirectory(json.loads(path.read_bytes()))


def directory_for_case(case, case_path):
    descriptor = case.get('formulary_directory')
    if descriptor is None:
        return None
    path = Path(case_path).resolve().parent.parent / FILENAME
    stat = path.stat()
    directory = _load(path, stat.st_mtime_ns, stat.st_size)
    if descriptor != directory.descriptor:
        raise ValueError('The shared medication directory differs from the frozen case')
    return directory


def query_errors(response):
    queries = response.get('queries')
    if (set(response) - {'action', 'queries', 'offset'} or not isinstance(queries, list)
            or not 1 <= len(queries) <= 6 or any(not isinstance(q, str) or not terms(q) or len(q) > 120 for q in queries)
            or type(response.get('offset', 0)) is not int or response.get('offset', 0) < 0):
        return ['Use {action:"search_formulary",queries:[one to six named drugs or codes],offset:0}.']
    return []
