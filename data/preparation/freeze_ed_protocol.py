"""Link the frozen ED cohort to dated source evidence and a local reference index."""

import argparse
from collections import Counter
import csv
from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'simulator'))
from handoff.ed_workup import clinical_note_sections, ed_order_checkpoints, source_ref
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.emergency import is_triage_reason, patient_account
from sandbox.experiments.linked.ed_order_history import POST_SIGNATURE_REVIEW
from sandbox.experiments.linked.patient_medications import with_medication_memory
from sandbox.experiments.linked.run_batch import file_sha256


VERSION = 'ed_source_replay_v2'
PATIENT_REPORT = re.compile(
    r'\b(?:the patient|patient|pt\.?|he|she|they|i)\s+'
    r'(?:(?:also|initially|currently|previously|now)\s+)?'
    r'(?:reports?|reported|endorses?|endorsed|describes?|described|denies|denied|'
    r'says?|said|states?|stated|recalls?|recalled|complains? of|complained of)\b', re.I)
LATER_CARE = re.compile(
    r'\b(?:this admission|prior to discharge|at discharge|after admission|since admission|'
    r'in the (?:ed|emergency (?:room|department)|hospital|icu)|'
    r'(?:ed|hospital) course|after (?:treatment|receiving|being given|administration)|'
    r'was (?:given|administered|treated|diagnosed|discharged|admitted|started|prescribed)|'
    r'ordered|computed tomography|chest x.ray|blood culture|lab results?)\b', re.I)
BREAK = re.compile(r'(?:[.!?](?=\s|$)|\n\s*\n)')
UNKNOWN = 'not documented in an attributable preconsultation source'


def read(path):
    return json.loads(path.read_bytes())


def save(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def patient_statements(case_id, note):
    raw = note['text']
    provenance, facts = [], []
    for section, start, end in clinical_note_sections(raw):
        if section != 'history of present illness':
            continue
        part = raw[start:end]
        later = LATER_CARE.search(part)
        left = 0
        for right in [*(m.end() for m in BREAK.finditer(part)), len(part)]:
            if later and right > later.start():
                break
            sentence = part[left:right].strip()
            match = PATIENT_REPORT.match(sentence)
            if match and len(sentence) <= 750 and re.search(r'[a-zA-Z]{3}', sentence[match.end():]):
                position = start + left + len(part[left:right]) - len(part[left:right].lstrip())
                stop = position + len(sentence)
                ref = source_ref(case_id, 'intake_statement', f"{note['note_id']}/{position}/{stop}")
                denied = bool(re.search(r'\bdeni(?:es|ed)\b', match[0], re.I))
                value = ' '.join(sentence.split())
                facts.append({'fact_id': 'fact-' + ref[4:], 'topic': 'present_illness', 'value': value,
                              'status': 'denied' if denied else 'reported', 'reporter': 'patient',
                              'source_kind': 'MIMIC-IV-Note attributed admission history', 'source_refs': [ref]})
                provenance.append({'source_ref': ref, 'note_id': note['note_id'], 'section': section,
                                   'start': position, 'end': stop, 'verbatim': raw[position:stop],
                                   'recorded_at': note['storetime'],
                                   'availability': 'patient knowledge elicited at intake',
                                   'event_time': 'reported in the admission history; precision follows the quote'})
            left = right
    return facts, provenance


def linked_nodes(case, archive, reference):
    case_id = case['case_id']
    visit, = [row for row in archive['就诊索引'] if row['编号'] == archive['本次实验急诊']]
    subject, stay, admission = (str(reference[field]) for field in
                               ('source_subject_id', 'source_stay_id', 'source_hadm_id'))
    actual = tuple(map(str, (archive['患者登记']['subject_id'], visit['原始就诊号'], visit['关联住院号'])))
    if (actual != (subject, stay, admission) or visit['类型'] != '急诊' or
            (visit['开始'], visit['结束']) != (reference['ed_start'], reference['ed_end'])):
        raise ValueError(f'{case_id}: ED visit disagrees with its source archive')
    table = archive['原表数据']
    diagnosed, = [row for row in table['ed.diagnosis'] if row['subject_id'] == subject
                  and row['stay_id'] == stay and int(row['seq_num']) == 1]
    dx = reference['ed_primary_icd']
    if diagnosed['icd_code'] != dx['code'] or str(diagnosed['icd_version']) != str(dx['icd_version']):
        raise ValueError(f'{case_id}: first ED diagnosis differs from the linked diagnosis')
    timeline = reference['order_timeline']
    actions = {row['order_id']: (node['offset_seconds'], row) for node in timeline for row in node['actions']}
    headers = [row for row in table['hosp.poe'] if row['poe_id'] in actions
               and row['subject_id'] == subject and row['hadm_id'] == admission]
    if set(actions) != {row['poe_id'] for row in headers}:
        raise ValueError(f'{case_id}: a primary treatment action lacks a source order')
    arrival = datetime.fromisoformat(visit['开始'])
    for row in headers:
        offset, expected = actions[row['poe_id']]
        if ((datetime.fromisoformat(row['ordertime']) - arrival).total_seconds() != offset
                or row['transaction_type'] != expected['transaction']):
            raise ValueError(f'{case_id}: primary treatment action differs from the source order')
    nodes = ed_order_checkpoints(headers, subject=subject, admission=admission,
                                arrival=visit['开始'], departure=visit['结束'])
    if ([item['decision_seconds'] for item in nodes] != [item['offset_seconds'] for item in timeline] or
            nodes != case['his_initial']['time_basis']['ed_order_checkpoints']):
        raise ValueError(f'{case_id}: source, runtime and scoring checkpoints differ')
    return nodes, visit


def archive_case(destination, source, row, reference, current_note, prior_note):
    case_id = row['case_id']
    directory = source / 'patients' / case_id
    archive_path = directory / '原表记录.json'
    archive = read(archive_path)
    case = read(source / row['path'])
    if digest(runtime_case(case)) != reference['runtime_input_hash']:
        raise ValueError(f'{case_id}: the frozen input was altered')
    if (current_note['subject_id'] != str(reference['source_subject_id']) or
            current_note['hadm_id'] != str(reference['source_hadm_id'])):
        raise ValueError(f'{case_id}: admission history differs from its linked encounter')
    nodes, visit = linked_nodes(case, archive, reference)
    facts, provenance = patient_statements(case_id, current_note)
    account = patient_account(case['patient_initial'])
    old = {f['fact_id']: f for f in account['patient_facts'] if not is_triage_reason(f)}
    for fact in facts:
        old.setdefault(fact['fact_id'], fact)
    case['patient_initial'] = {**account, 'patient_facts': list(old.values()),
                               'interview_source': 'patient_attributed_history' if old else 'unrecorded'}
    chart = case['his_initial']
    chart['time_basis']['ed_pharmacy_review'] = POST_SIGNATURE_REVIEW
    view = HistoryChart(runtime_case(case)['his_initial']).initial_view()
    previous_ips = directory / '开诊摘要.json'
    opening = read(previous_ips)
    if opening['opening_summary'] != view['opening_summary']:
        opening['opening_summary'] = view['opening_summary']
    memory = with_medication_memory(case_id, case['patient_initial'], chart)
    known = {fact['topic'] for fact in memory['patient_facts'] if fact['status'] != 'unknown'}
    save(destination / 'patients' / case_id / '开诊摘要.json', opening)
    save(destination / 'patients' / case_id / '预问诊事实.private.json', {
        'case_id': case_id, 'version': VERSION, 'opening_patient_facts': memory['patient_facts'],
        'admission_history_sources': provenance,
        'opening_unknowns': {key: UNKNOWN for key in ('present_illness', 'allergy', 'reconciled_home_medication',
             'prior_discharge_medication') if key not in known},
        'later_patient_memory': 'Update from a released medication reconciliation or a clinician disclosure only',
        'source_note_snapshot': digest(current_note)})
    timed = {section: [{key: value for key, value in report.items() if key in {'source_ref', 'source_kind', 'time'}}
                       for report in records] for section, records in chart.items()
             if isinstance(records, list) and section.startswith('ed_')}
    save(destination / 'patients' / case_id / '原表索引.private.json', {
        'case_id': case_id, 'version': VERSION, 'archive': str(archive_path),
        'archive_sha256': file_sha256(archive_path), 'source_stay': visit,
        'opening_chart': str(previous_ips), 'source_tables': archive['来源'],
        'index_discharge_note': current_note, 'latest_prior_discharge_note': prior_note,
        'report_inventory': timed})
    output = destination / row['path']
    save(output, case)
    reference['runtime_input_hash'] = digest(runtime_case(case))
    return nodes, len(facts), len(memory['patient_facts'])


def ed_library_admissions(ed, excluded):
    with gzip.open(ed / 'edstays.csv.gz', 'rt', newline='') as stream:
        return {(row['subject_id'], row['hadm_id']) for row in csv.DictReader(stream)
                if row['hadm_id'] and row['subject_id'] not in excluded}


def notes_and_library(discharge, ed, references, library):
    excluded = {str(ref['source_subject_id']) for ref in references.values()}
    admitted = ed_library_admissions(ed, excluded)
    notes, prior = {}, {}
    keys = {ref['source_note_id'] for ref in references.values()}
    arrival = {str(ref['source_subject_id']): ref['ed_start'] for ref in references.values()}
    source_hash = sha256()
    csv.field_size_limit(10_000_000)
    with sqlite3.connect(library) as db, discharge.open(newline='') as stream:
        db.executescript('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);'
                         'CREATE VIRTUAL TABLE examples USING fts5(note_id UNINDEXED, '
                         'subject_id UNINDEXED, history, tokenize="unicode61");')
        for row in csv.DictReader(stream):
            source_hash.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
            sid = row['subject_id']
            if sid in excluded:
                if row['note_id'] in keys:
                    notes[row['note_id']] = row
                if (row['storetime'] and row['charttime'] and max(row['storetime'], row['charttime']) < arrival[sid]
                        and (sid not in prior or row['storetime'] > prior[sid]['storetime'])):
                    prior[sid] = row
                continue
            if (sid, row['hadm_id']) in admitted:
                sections = [row['text'][start:end].strip()[:1600] for title, start, end in clinical_note_sections(row['text'])
                            if title in {'chief complaint', 'history of present illness'}]
                if sections:
                    db.execute('INSERT INTO examples VALUES (?, ?, ?)', (row['note_id'], sid, '\n'.join(sections)[:2400]))
        if notes.keys() != keys:
            raise ValueError(f'{len(keys - notes.keys())} source notes are missing')
        count, = db.execute('SELECT COUNT(*) FROM examples').fetchone()
        metadata = {'version': VERSION, 'source': 'MIMIC-IV-ED 2.2 and MIMIC-IV-Note 2.2',
                    'source_logical_sha256': source_hash.hexdigest(), 'example_count': str(count),
                    'excluded_subjects': json.dumps(sorted(excluded)), 'case_scope': 'prior ED-associated admission histories'}
        db.executemany('INSERT INTO metadata VALUES (?, ?)', metadata.items())
    return notes, prior, metadata


def prepare(source, discharge, ed, output):
    manifest = read(source / 'manifest.json')
    references = read(source / 'reference.private.json')
    rows = manifest['cases']
    if (len(rows) != 100 or len({row['patient_key'] for row in rows}) != 100
            or Counter(row['group'] for row in rows) != dict.fromkeys({row['group'] for row in rows}, 20)
            or references.keys() != {row['case_id'] for row in rows}
            or len({ref['source_subject_id'] for ref in references.values()}) != 100):
        raise ValueError('Expected the frozen five groups of twenty distinct ED patients')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        destination = Path(temp) / 'prepared'
        destination.mkdir(mode=0o700)
        notes, prior, metadata = notes_and_library(discharge, ed, references, destination / 'history.private.sqlite')
        print(f"Historical corpus prepared with {metadata['example_count']} eligible source notes", flush=True)
        counts, all_nodes = [], {}
        for row in rows:
            case_id = row['case_id']
            ref = references[case_id]
            nodes, facts, opening_facts = archive_case(destination, source, row, ref,
                  notes[ref['source_note_id']], prior.get(str(ref['source_subject_id'])))
            counts.append({'case_id': case_id, 'group': row['group'], 'nodes': len(nodes),
                           'intake_hpi_facts': facts, 'opening_patient_facts': opening_facts})
            all_nodes[case_id] = nodes
        for name in ('catalog.json', 'formulary_directory.json', 'products.sql'):
            shutil.copyfile(source / name, destination / name)
        save(destination / 'manifest.json', manifest)
        save(destination / 'reference.private.json', references)
        save(destination / '主病开方节点.json', all_nodes)
        save(destination / '病例核验.json', {'version': VERSION, 'cohort': str(source),
             'cases': counts, 'primary_checkpoints': sum(len(x) for x in all_nodes.values()),
             'history_library': {k: v for k, v in metadata.items() if k != 'excluded_subjects'},
             'history_library_sha256': file_sha256(destination / 'history.private.sqlite')})
        destination.rename(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('source', 'discharge', 'ed', 'output'):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.source.resolve(), args.discharge.resolve(), args.ed.resolve(), args.output.resolve())
