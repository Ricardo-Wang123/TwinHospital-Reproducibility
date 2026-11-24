"""Build the fixed allergy history for a prepared ED cohort from local source records."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
import gzip
import json
import os
from pathlib import Path
import re
import unicodedata
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'simulator'))
from sandbox.experiments.linked import run_batch
from sandbox.experiments.linked import ed_allergy_review as allergies
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.contracts import digest, runtime_case


HEAD = re.compile(r'^[ \t]*(?:drug +)?allerg(?:y|ies)[ \t]*:[ \t]*([^\r\n]*)', re.M | re.I)
NEXT_SECTION = re.compile(r'^\s*(?:[A-Za-z][A-Za-z /-]+:|home medications\s*$|'
                          r'medications on admission\s*$|physical exam(?:ination)?\s*$|'
                          r'assessment and plan\s*$|[=\-]{3,}\s*$)', re.I)
NARRATIVE = re.compile(r'\b(?:patient|she|he) (?:is|was|reports being) allergic to '
                       r'(?P<allergen>[^.!?\n]+?)(?=\s+(?:but|and (?:has|is|had|reports))\b|[.!?\n]|$)', re.I)
NEGATIVE = re.compile(r'^(?:nkda|nka|no known (?:drug |medication )?allergies(?: / adverse drug reactions)?|'
                      r'patient recorded as having no known allergies to drugs|none known|none|'
                      r'no known allergies or adverse drug reactions)[ .]*$', re.I)
MISSING = {'', '___', 'no allergies/adrs on file', 'unknown', 'not recorded', 'not documented'}
DISPLAY_ZH = {'azithromycin': '阿奇霉素', 'latex': '乳胶', 'metronidazole': '甲硝唑（Flagyl）'}
SOURCE_FIELDS = ('note_id', 'hadm_id', 'charttime', 'storetime')


def norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().strip(' .,:;').split())


def note_passages(text, source):
    for match in HEAD.finditer(text):
        lines = [match.group(1).strip()] if match.group(1).strip() else []
        after = match.end()
        next_line = after + (2 if text[after:after + 2] == '\r\n' else 1)
        for line in text[next_line:].splitlines(keepends=True):
            if not line.strip() or NEXT_SECTION.match(line.strip()):
                break
            lines.append(line.strip())
            next_line += len(line)
        yield {**source, 'field': 'allergies', 'text': text[match.start():next_line].rstrip('\r\n'),
               'value': ' '.join(lines)}
    for match in NARRATIVE.finditer(text):
        yield {**source, 'field': 'narrative', 'text': match.group(0), 'value': match['allergen']}


def source_records(cohort, references, *, discharge, radiology):
    patients = {r['source_subject_id']: case for case, r in references.items()}
    entries = defaultdict(list)
    counts = defaultdict(Counter)
    for kind, path in (('discharge', discharge), ('radiology', radiology)):
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                case_id = patients.get(row['subject_id'])
                if case_id is None:
                    continue
                counts[case_id][kind] += 1
                source = {'source': 'MIMIC-IV-Note ' + kind, **{key: row[key] for key in SOURCE_FIELDS}}
                entries[case_id].extend(note_passages(row['text'], source))
    for case_id in references:
        path = cohort / 'patients' / case_id / '原表索引.private.json'
        index = read(path)
        archive = Path(index['archive'])
        if run_batch.file_sha256(archive) != index['archive_sha256']:
            raise ValueError('Source archive changed: ' + case_id)
        for name, rows in read(archive)['原表数据'].items():
            counts[case_id]['source_tables'] += 1
            for row in rows:
                field = row.get('field_name') or row.get('result_name') or ''
                if re.search(r'^(?:patient |drug )?allerg(?:y|ies)$', field.strip(), re.I):
                    value = row.get('field_value') or row.get('result_value') or row.get('value') or ''
                    entries[case_id].append({
                        'source': name, 'hadm_id': row.get('hadm_id'), 'poe_id': row.get('poe_id'),
                        'charttime': row.get('charttime') or row.get('chartdate'), 'storetime': row.get('storetime'),
                        'field': field, 'text': value, 'value': value})
    return {'entries': dict(entries), 'reviewed_source_counts': dict(counts)}


class Catalogue:
    def __init__(self, catalogue):
        self.names = {norm(row['name']) for row in catalogue['products']}
        candidates = defaultdict(set)
        for row in catalogue['products']:
            name = row['name']
            if row.get('ingredient'):
                candidates[norm(name)].add((norm(row['ingredient']), name))
            match = re.fullmatch(r'([\w-]+)\s+\(([\w-]+)\)', name.strip())
            if match and {norm(value) for value in match.groups()} <= self.names:
                candidates[norm(match[2])].add((norm(match[1]), name))
        self.mapping = {name: sorted(options)[0] for name, options in candidates.items()
                        if len({target for target, _ in options}) == 1}

    def identify(self, recorded):
        name = norm(recorded)
        if name in {'latex', 'latex, natural rubber'}:
            return 'latex', 'same material explicitly named'
        return self.mapping.get(name, (name, None))


def items(value):
    value = value.strip(' /')
    if norm(value) in MISSING or NEGATIVE.fullmatch(value):
        return []
    if re.search(r'^all allergies\s*/\s*adverse drug reactions previously recorded', value, re.I):
        return []
    for name in re.split(r'\s+/\s+|(?<=\)),\s+(?=\w[^,]+\()', value):
        literal = name.strip(' ./')
        key = norm(literal)
        if (key in MISSING or NEGATIVE.fullmatch(literal) or
                key in {'all allergies', 'g6pd deficiency', 'adverse drug reactions'}):
            continue
        if key.startswith('g6pd deficiency/') and '/' in literal:
            literal = literal.split('/', 1)[1].strip()
        reaction = None
        detail = re.fullmatch(r'(.*?)\s+\((cough|constipation|rash|hives|nausea|vomiting|anaphylaxis)\)', literal, re.I)
        if detail:
            name, reaction = detail.groups()
        else:
            name = literal
        yield name, reaction


def reconcile(case_id, records, catalogue, defaults):
    retained = {}
    reviewed = []
    for passage in records:
        raw = passage['value']
        if NEGATIVE.fullmatch(raw):
            reviewed.append({**passage, 'interpretation': 'explicit_no_known_allergy'})
            continue
        names = list(items(raw))
        if not names:
            reviewed.append({**passage, 'interpretation': 'no_named_allergen'})
        for name, reaction in names:
            key, basis = catalogue.identify(name)
            item = retained.setdefault(key, {'name': name, 'identity': key, 'sources': [], 'recorded_names': []})
            if basis:
                item['identity_basis'] = basis
            if name not in item['recorded_names']:
                item['recorded_names'].append(name)
            item['sources'].append({**passage, 'recorded_name': name, 'reaction': reaction})
    if retained:
        return {'status': 'allergy_reported', 'source_status': 'named_allergen_recorded',
                'items': list(retained.values()), 'other_recorded_statements': reviewed}
    explicit = any(row['interpretation'] == 'explicit_no_known_allergy' for row in reviewed)
    status = 'none_known' if explicit or case_id in defaults else 'unknown'
    return {'status': status, 'source_status': ('explicit_no_known_allergy' if explicit else
            'simulation_default_source_missing' if case_id in defaults else 'source_missing'),
            'items': [], 'other_recorded_statements': reviewed}


def spoken_parts(summary):
    names = []
    known_reactions = {'cough': '咳嗽', 'constipation': '便秘', 'rash': '起疹子', 'hives': '起荨麻疹',
                       'nausea': '恶心', 'vomiting': '呕吐', 'anaphylaxis': '严重过敏反应'}
    for row in summary['items']:
        name = DISPLAY_ZH.get(row['identity'], row['name'])
        reactions = list(dict.fromkeys(v['reaction'] for v in row['sources'] if v.get('reaction')))
        names.append({'name': name, 'reaction': '、'.join(known_reactions.get(value.casefold(), value)
                                                         for value in reactions) or None})
    if names:
        words = '我对' + '、'.join(item['name'] for item in names) + '过敏。'
    elif summary['status'] == 'none_known':
        words = '就我知道的，没有。'
    else:
        words = '我的过敏史我说不清。'
    return words, names


def prepare_case(case, summary):
    words, names = spoken_parts(summary)
    status = summary['status']
    items = [{'name': row['name'], **({'reaction': displayed['reaction']} if displayed['reaction'] else {}),
              **({'ingredient': row['identity']} if row.get('identity_basis') and row['identity'] != 'latex' else {}),
              'source_kind': 'longitudinal allergy history supplied in simulation'}
             for row, displayed in zip(summary['items'], names, strict=True)]
    chart = {'status': status, 'source_kind': 'longitudinal allergy history supplied in simulation',
             'as_of_offset_hours': 0, 'items': items,
             'patient_report': {'status': status, 'value': words, 'as_of_offset_hours': 0}}
    patient = case['patient_initial']
    fact = {'fact_id': 'fact-' + digest([case['case_id'], 'allergy_history'])[:24],
            'topic': allergies.PATIENT_TOPIC, 'value': words, 'status': 'reported',
            'reporter': 'patient', 'source_kind': chart['source_kind'], 'allergens': names}
    patient['patient_facts'] = [row for row in patient['patient_facts'] if row.get('topic') != allergies.PATIENT_TOPIC]
    patient['patient_facts'].append(fact)
    patient['allergy_history_policy'] = allergies.LONGITUDINAL_POLICY
    case['his_initial']['allergy_record'] = chart
    return fact


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--discharge', type=Path, required=True)
    parser.add_argument('--radiology', type=Path, required=True)
    parser.add_argument('--assume-none-known', action='append', default=[], metavar='CASE_ID')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    cohort_dir = args.cohort.resolve()
    work = args.work.resolve()
    defaults = set(args.assume_none_known)
    os.umask(0o077)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    references = read(cohort_dir / 'reference.private.json')
    cached = work / 'longitudinal_allergy_passages.private.json'
    sources = source_records(cohort_dir, references, discharge=args.discharge, radiology=args.radiology)
    run_batch._atomic(cached, sources)
    catalogue = Catalogue(read(cohort_dir / 'formulary_directory.json'))
    records = {cid: reconcile(cid, sources['entries'].get(cid, []), catalogue, defaults) for cid in references}
    counts = Counter(r['source_status'] for r in records.values())
    print('Allergy history:', dict(counts), flush=True)
    for cid, review in records.items():
        if review['status'] == 'unknown':
            print('Unresolved source status:', cid, flush=True)
    if not args.apply:
        audit = work / 'longitudinal_allergy_dry_run.private.json'
        run_batch._atomic(audit, records)
        return
    if not records or any(row['status'] == 'unknown' for row in records.values()):
        raise ValueError('Unresolved cohort allergy histories')
    if {cid for cid, row in records.items() if row['source_status'] == 'simulation_default_source_missing'} != defaults:
        raise ValueError('The explicitly configured defaults must agree with the source audit')
    outputs = {}
    cohort = read(cohort_dir / '病例核验.json')
    counts_by_case = {row['case_id']: row for row in cohort['cases']}
    for cid, review in records.items():
        path = cohort_dir / 'cases' / (cid + '.json')
        case = read(path)
        expected_before = references[cid]['runtime_input_hash']
        if digest(runtime_case(case)) != expected_before:
            raise ValueError('Source case and frozen reference are inconsistent: ' + cid)
        fact = prepare_case(case, review)
        projected = HistoryChart(case['his_initial']).initial_view()
        if projected['allergy_record']['status'] != review['status'] or projected['allergy_record']['item_count'] != len(review['items']):
            raise ValueError('Allergy list changed in the physician chart: ' + cid)
        references[cid]['runtime_input_hash'] = digest(runtime_case(case))
        outputs[path] = case
        patient = cohort_dir / 'patients' / cid
        ips = read(patient / '开诊摘要.json')
        allergy = ips['opening_summary']['allergies']
        allergy.update(status=review['status'], entries=deepcopy(case['his_initial']['allergy_record']['items']),
                       entry_count=len(review['items']), source_record_status='longitudinal_simulation_history')
        outputs[patient / '开诊摘要.json'] = ips
        interview = read(patient / '预问诊事实.private.json')
        interview['opening_patient_facts'] = [row for row in interview['opening_patient_facts']
                                             if row.get('topic') != allergies.PATIENT_TOPIC] + [fact]
        interview['opening_unknowns'].pop('allergy', None)
        interview['allergy_source_status'] = review['source_status']
        outputs[patient / '预问诊事实.private.json'] = interview
        counts_by_case[cid]['opening_patient_facts'] = len(case['patient_initial']['patient_facts'])
    cohort['fixed_allergy_history'] = {'policy': allergies.LONGITUDINAL_POLICY, 'counts': dict(counts)}
    outputs[cohort_dir / '病例核验.json'] = cohort
    outputs[cohort_dir / 'reference.private.json'] = references
    outputs[cohort_dir / '过敏史来源与归并.private.json'] = {
        'policy': allergies.LONGITUDINAL_POLICY, 'reviewed_sources': sources['reviewed_source_counts'], 'cases': records}
    backup = work / 'ED-100.before_fixed_allergies'
    if not backup.exists():
        backup.mkdir(mode=0o700)
        for path in outputs:
            if path.exists():
                name = backup / path.relative_to(cohort_dir)
                name.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                name.write_bytes(path.read_bytes())
    for path, content in outputs.items():
        run_batch._atomic(path, content)
    print('Verified', len(records), 'updated cases', flush=True)


if __name__ == '__main__':
    main()
