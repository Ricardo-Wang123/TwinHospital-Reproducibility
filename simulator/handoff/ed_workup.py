"""Dated, pre-prescribing chart events for a source-linked emergency visit."""
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import re

from sandbox.experiments.linked.emergency import SOURCE_ED_ADMINISTRATION, hide_index_ed_choices


VITALS = {
    'temperature': ('Temperature', '°F'), 'heartrate': ('Heart rate', 'beats/min'),
    'resprate': ('Respiratory rate', 'breaths/min'), 'o2sat': ('Oxygen saturation', '%'),
    'sbp': ('Systolic blood pressure', 'mmHg'), 'dbp': ('Diastolic blood pressure', 'mmHg'),
    'rhythm': ('Rhythm', ''), 'pain': ('Pain', ''),
}


def source_ref(case_id, table, ordinal):
    return 'src_' + sha256(f'{case_id}/{table}/{ordinal}'.encode()).hexdigest()[:24]


_QUALITATIVE = re.compile(
    r'(?:pos(?:itive)?|neg(?:ative)?|trace|tr|sm(?:all)?|mod(?:erate)?|lg|large|'
    r'none|few|rare|occ(?:asional)?|many|normal|abnormal|clear|hazy|cloudy|turbid|'
    r'straw|yellow|amber|red|brown|orange|colorless|detected|not detected|'
    r'(?:(?:[<>]=?|less than|greater than)\s*)?[+-]?\d+(?:\.\d+)?'
    r'(?:\s*[-–]\s*\d+(?:\.\d+)?)?\+?)\s*[*.]*', re.IGNORECASE)


def lab_fields(row):
    """Preserve a posted lab value or its source comment when the value field is empty."""
    raw, numeric = row.get('value'), row.get('valuenum')
    if (raw is not None and str(raw).strip(' _\t\r\n')) or numeric not in (None, ''):
        return {'value_raw': raw, 'numeric_value_raw': numeric}
    comment = (row.get('comments') or '').strip()
    if not comment.strip(' _\t\r\n'):
        return None
    fields = {'value_raw': '', 'numeric_value_raw': numeric, 'value_source_column': 'comments'}
    if _QUALITATIVE.fullmatch(comment):
        fields['value_raw'] = comment
    else:
        fields['lab_comment_raw'] = comment
    return fields


def ed_order_checkpoints(orders, *, subject, admission, arrival, departure, primary_order_ids=None):
    """Relative seconds for distinct ED medication decisions, with no medication identities."""
    transactions = [(row['ordertime'], row['transaction_type']) for row in orders
                    if row['subject_id'] == subject and row['hadm_id'] == admission
                    and row['order_type'] == 'Medications' and row['transaction_type'] in {'New', 'Change', 'D/C'}
                    and row.get('ordertime') and arrival <= row['ordertime'] < departure
                    and (primary_order_ids is None or row.get('poe_id') in primary_order_ids)]
    new_orders = [when for when, action in transactions if action == 'New']
    if not new_orders:
        raise ValueError('No dated ED medication decision in the source order headers')
    first = min(new_orders)
    start = datetime.fromisoformat(arrival)
    points = []
    for when in sorted({t for t, _ in transactions if t >= first}):
        dt = datetime.fromisoformat(when)
        points.append({'decision_seconds': int((dt-start).total_seconds()),
                       'release_before_seconds': max(0, int((dt.replace(second=0, microsecond=0)-start).total_seconds()))})
    return points


def ed_decision_offsets(orders, *, subject, admission, arrival, departure, primary_order_ids=None):
    points = ed_order_checkpoints(orders, subject=subject, admission=admission, arrival=arrival, departure=departure,
                                  primary_order_ids=primary_order_ids)
    hours = lambda seconds: round(seconds / 3600, 5)
    return {'presentation': hours(points[0]['decision_seconds']), 'reassessment': hours(points[-1]['decision_seconds']),
            'closing': hours((datetime.fromisoformat(departure)-datetime.fromisoformat(arrival)).total_seconds()),
            'presentation_release_before': hours(points[0]['release_before_seconds']),
            'reassessment_release_before': hours(points[-1]['release_before_seconds'])}


def prepare_ed_replay(case, orders, *, subject, admission, arrival, departure,
                      stay=None, medrecon=None, prior_notes=(), prior_encounters=None, primary_order_ids=None):
    from sandbox.experiments.linked.emergency import is_triage_reason, patient_account
    from sandbox.experiments.linked.ed_treatment_review import PROTOCOL
    memory, chart = case['patient_initial'], case['his_initial']
    workup = chart.setdefault('ed_workup', [])
    for fact in memory['patient_facts']:
        if is_triage_reason(fact):
            refs = fact.get('source_refs') or [fact['fact_id']]
            if not any(row.get('source_ref') == refs[0] for row in workup):
                workup.append({'kind': 'triage_reason', 'display': fact['value'], 'source_ref': refs[0],
                               'source_kind': fact['source_kind'], 'status': 'recorded at triage',
                               'time': {'basis': 'triage at ED arrival; exact chart time unspecified'}})
    case['patient_initial'] = patient_account(memory)
    if medrecon is not None:
        if not stay:
            raise ValueError('Medication reconciliation requires the index ED stay')
        index_reconciliations(case, medrecon, subject, stay, arrival, departure)
    linked = {r['encounter_ref'] for r in chart.get('historical_encounters', ())
              if r.get('setting') == 'inpatient' and r['discharge_offset_hours'] < 0}
    observations = chart.setdefault('historical_observations', [])
    seen = {r.get('source_ref') for r in observations}
    for note in prior_notes:
        encounter = (prior_encounters or {}).get(note['hadm_id'])
        if (note['subject_id'] != subject or note['hadm_id'] == admission or encounter not in linked
                or not note.get('charttime') or not note.get('storetime')
                or max(note['charttime'], note['storetime']) >= arrival):
            continue
        for row in prior_note_observations(case['case_id'], note['note_id'], note['text'], note['storetime'],
                                           arrival, encounter, titles={'allergies'}):
            if row['source_ref'] not in seen:
                observations.append(row)
                seen.add(row['source_ref'])
    if any(r.get('name', '').startswith('Prior allergies (') for r in observations):
        allergy = chart.get('allergy_record', {})
        if allergy.get('status') == 'unknown' and allergy.get('source_record_status') == 'not_available_in_source_modules':
            allergy['source_record_status'] = 'prior_documentation_only_current_status_unverified'
    offsets = ed_decision_offsets(orders, subject=subject, admission=admission, arrival=arrival, departure=departure,
                                  primary_order_ids=primary_order_ids)
    stage_ed_results(chart, offsets)
    hide_index_ed_choices(chart)
    chart['time_basis']['ed_order_action_protocol'] = 'independent_order_actions_v1' if primary_order_ids is None else PROTOCOL
    chart['time_basis']['ed_order_checkpoints'] = ed_order_checkpoints(
        orders, subject=subject, admission=admission, arrival=arrival, departure=departure,
        primary_order_ids=primary_order_ids)


def index_reconciliations(case, rows, subject, stay, arrival, departure):
    """Rebuild the dated medication history from this ED stay's reconciliation records."""
    chart = case['his_initial']
    for key in ('ed_workup', 'ed_reassessment_workup', 'ed_closing_workup', 'ed_diagnostic_reports',
                'ed_reassessment_reports', 'ed_closing_reports', 'ed_lab_reports'):
        if key in chart:
            chart[key] = [r for r in chart[key] if r.get('kind') != 'home_medication_history']
    opening = {}
    origin = datetime.fromisoformat(arrival)
    for n, r in enumerate(rows, 1):
        when = r.get('charttime')
        if r['subject_id'] != subject or r['stay_id'] != stay or not when or when >= departure or not r['name'].strip():
            continue
        offset = round((datetime.fromisoformat(when) - origin).total_seconds() / 3600, 5)
        if when <= arrival:
            item = {'name': r['name'], 'source_kind': 'MIMIC-IV-ED ed.medrecon',
                    'current_use_status': 'recorded_at_medication_reconciliation',
                    'time': {'recorded_offset_hours': offset}}
            if r['name'] not in opening or offset > opening[r['name']]['time']['recorded_offset_hours']:
                opening[r['name']] = item
        else:
            chart['ed_workup'].append({'kind': 'home_medication_history', 'display': r['name'],
                'status': 'listed at medication reconciliation; last dose and adherence unreported',
                'source_kind': 'ed.medrecon', 'source_ref': source_ref(case['case_id'], 'ed.medrecon', n),
                'time': {'event_offset_hours': offset, 'recorded_offset_hours': offset, 'basis': 'hours since ED arrival'}})
    med = chart.setdefault('current_use_record', {'status': 'unknown', 'items': []})
    med['items'] = [item for item in med.get('items', ()) if item.get('source_kind') != 'MIMIC-IV-ED ed.medrecon'] + list(opening.values())
    if opening and med.get('status') in ('unknown', 'reconciliation_recorded'):
        med.update(status='reconciliation_recorded', source_record_status='documented_at_opening')


def index_administrations(case_id, rows, details, *, subject, admission, arrival, departure, cutoff):
    """Index source ED administrations for the local historical evaluation."""
    if not admission:
        return
    origin = datetime.fromisoformat(arrival)
    components = defaultdict(list)
    for detail in details:
        if detail['subject_id'] == subject:
            components[(detail['emar_id'], detail['emar_seq'])].append(detail)
    for ordinal, row in enumerate(rows, 1):
        if row['subject_id'] != subject or row['hadm_id'] != admission:
            continue
        event, stored = row.get('charttime'), row.get('storetime')
        medication, status = ((row.get(k) or '').strip() for k in ('medication', 'event_txt'))
        if not (event and stored and arrival <= event < departure and medication and status):
            continue
        event_hours = round((datetime.fromisoformat(event) - origin).total_seconds() / 3600, 5)
        recorded_hours = round((datetime.fromisoformat(max(event, stored)) - origin).total_seconds() / 3600, 5)
        if recorded_hours >= cutoff:
            continue
        measurements = [{'name': 'Documented dose component', 'value': detail['dose_given'],
                         'unit': detail['dose_given_unit']}
                        for detail in components[(row['emar_id'], row['emar_seq'])]
                        if detail.get('dose_given') and detail.get('dose_given_unit')]
        yield {'kind': SOURCE_ED_ADMINISTRATION, 'display': medication, 'status': status,
               'source_kind': 'MIMIC-IV hosp.emar (source ED encounter)',
               'source_ref': source_ref(case_id, 'hosp.emar', ordinal),
               'time': {'event_offset_hours': event_hours, 'recorded_offset_hours': recorded_hours,
                        'basis': 'hours since ED arrival', 'availability_basis': 'later of charttime and storetime'},
               **({'measurements': measurements} if measurements else {})}


def prepare_archived_ed_replay(case, archive, prior_notes=(), *, primary_order_ids=None):
    visits = archive['就诊索引']
    index = next(v for v in visits if v['类型'] == '急诊' and v['编号'] == archive['本次实验急诊'])
    origin = datetime.fromisoformat(index['开始'])
    def offset(t):
        return round((datetime.fromisoformat(t) - origin).total_seconds() / 3600, 5)
    periods = {(v['admission_offset_hours'], v['discharge_offset_hours']): v['encounter_ref']
               for v in case['his_initial']['historical_encounters'] if v['setting'] == 'inpatient'}
    prior = {v['原始就诊号']: periods[(offset(v['开始']), offset(v['结束']))] for v in visits
             if v['类型'] == '住院' and v.get('结束') and v['结束'] < index['开始']}
    rows = archive['原表数据']
    chart = case['his_initial']
    subject = archive['患者登记']['subject_id']
    admission = index['关联住院号']
    arrival = index['开始']
    terms = {r['itemid']: r for r in archive['本患者引用的字典与人员索引']['hosp.d_labitems']}
    source_params = dict(case_id=case['case_id'], subject=subject, admission=admission, arrival=arrival)
    historical = prior_chart_rows(archive, chart, **source_params)['historical_observations']
    current = encounter_reports(rows, terms, (), **source_params, stay=index['原始就诊号'],
                                cutoff=arrival, departure=index['结束'])
    for section, candidates in [('historical_observations', historical), ('ed_diagnostic_reports', current)]:
        sections = (section,) if section == 'historical_observations' else (
            section, 'ed_reassessment_reports', 'ed_closing_reports', 'ed_lab_reports')
        present = {r.get('source_ref') for name in sections for r in chart.get(name, ())}
        chart.setdefault(section, []).extend(r for r in candidates if r['source_ref'] not in present and
            (r.get('value_source_column') == 'comments' or r.get('source_kind') == 'hosp.microbiologyevents'))
    prepare_ed_replay(case, rows['hosp.poe'], subject=archive['患者登记']['subject_id'],
                      admission=index['关联住院号'], stay=index['原始就诊号'], arrival=index['开始'],
                      departure=index['结束'], medrecon=rows['ed.medrecon'], prior_notes=prior_notes,
                      prior_encounters=prior, primary_order_ids=primary_order_ids)


def stage_ed_results(chart, offsets):
    """Partition index-visit results by when the report could be read, not specimen time."""
    first, last, departure = (offsets[k] for k in ('presentation', 'reassessment', 'closing'))
    if not 0 <= first <= last <= departure:
        raise ValueError('ED decision times are out of order')
    first = offsets.get('presentation_release_before', first)
    last = offsets.get('reassessment_release_before', last)
    keys = ('ed_diagnostic_reports', 'ed_reassessment_reports', 'ed_closing_reports')
    phases = ('initial_ed_review', 'ed_reassessment', 'ed_closing')
    def reported(row):
        timing = row.get('time', {})
        return timing.get('recorded_offset_upper_hours', timing.get('recorded_offset_hours'))
    late_histories = ('ed_reassessment_workup', 'ed_closing_workup')
    workup_kinds = {'home_medication_history', 'investigation_order', 'repeat_vitals', SOURCE_ED_ADMINISTRATION}
    workup = [r for k in (*late_histories, *keys) for r in chart.get(k, []) if r.get('kind') in workup_kinds]
    for key in late_histories:
        chart.pop(key, None)
    results = [row for key in keys for row in chart.get(key, []) if row.get('kind') not in workup_kinds]
    included = {row['source_ref'] for row in results if row.get('source_ref')}
    def append_result(row):
        if not row.get('source_ref') or row['source_ref'] not in included:
            results.append(row)
            included.add(row.get('source_ref'))
    for row in chart.pop('ed_lab_reports', []):
        append_result(row)
    for key in ('current_observations', 'ed_workup'):
        kept = []
        for row in chart.get(key, []):
            when = reported(row)
            if row.get('kind') in workup_kinds:
                workup.append(row)
                continue
            is_result = (key == 'current_observations' and 'triage' not in row.get('source_kind', '').lower()
                         or row.get('kind') in {'ed_diagnostic_finding', 'repeat_vitals'})
            if (when is not None and when >= first) or (when is None and is_result):
                measurements = ', '.join(f"{m['name']} {m['value']} {m.get('unit', '')}".strip()
                                         for m in row.get('measurements', []))
                append_result({**row, 'name': row.get('name') or row.get('display', ''),
                               'value_raw': row.get('value_raw') or row.get('value') or measurements or row.get('display', ''),
                               'unit_raw': row.get('unit_raw') or row.get('unit', '')})
            else:
                kept.append(row)
        if key in chart:
            chart[key] = kept
    chart.update({key: [] for key in keys})
    for row in workup:
        when = reported(row)
        if when is not None and not 0 <= when <= departure:
            continue
        section = 'ed_workup' if when is not None and when < first else (
            'ed_reassessment_workup' if when is not None and when < last else 'ed_closing_workup')
        chart.setdefault(section, []).append({k: row[k] for k in
            ('kind','display','status','source_kind','source_ref','time','measurements') if k in row})
    chart['time_basis']['ed_decision_offsets_hours'] = dict(offsets)
    for row in results:
        known = reported(row)
        if known is not None and not 0 <= known <= departure:
            continue
        phase = 2 if known is None or known >= last else (0 if known < first else 1)
        chart[keys[phase]].append({**row, 'available_at': phases[phase]})


def presentation_from_source(case_id, note_id, text, selections):
    """Verify attributed, pretreatment HPI excerpts selected during case preparation."""
    header = re.search(r'(?im)^history of present illness:[ \t]*$', text)
    if header is None:
        raise ValueError('Source has no identified history of present illness')
    end = re.search(r'(?im)^(?:past medical history|social history|physical exam|hospital course|'
                    r'medications on admission|discharge medications|discharge diagnosis):', text[header.end():])
    stop = header.end() + end.start() if end else len(text)
    hpi = text[header.end():stop]
    facts, observations, provenance = [], [], []
    for kind, quote in selections:
        if kind not in {'patient_report', 'patient_denial', 'ed_collateral_history', 'ed_physical_examination',
                        'ed_diagnostic_finding'}:
            raise ValueError('Unknown presentation role')
        if not quote.strip():
            raise ValueError('Empty presentation excerpt')
        pattern = r'\s+'.join(re.escape(word) for word in quote.split())
        matches = list(re.finditer(pattern, hpi))
        if len(matches) != 1:
            raise ValueError('Presentation excerpt must resolve uniquely in this HPI')
        start, finish = (header.end() + n for n in matches[0].span())
        ref = source_ref(case_id, 'admission_presentation', f'{note_id}/{start}/{finish}')
        actual = text[start:finish]
        quoted = ' '.join(actual.split())
        source = 'MIMIC-IV-Note admission history, retrospectively documented'
        provenance.append({'source_ref': ref, 'note_id': note_id, 'section': 'History of Present Illness',
                           'kind': kind, 'start': start, 'end': finish, 'verbatim': actual})
        if kind.startswith('patient_'):
            facts.append({'fact_id': 'fact-' + ref[4:], 'topic': 'present_illness',
                          'field': 'symptom_history', 'value': quoted,
                          'status': 'denied' if kind == 'patient_denial' else 'reported',
                          'source_kind': source, 'source_refs': [ref]})
        else:
            observations.append({'kind': kind, 'display': quoted, 'status': 'documented',
                                 'source_kind': source, 'source_ref': ref,
                                 'time': {'basis': 'pre-arrival collateral or initial ED examination, '
                                          'reconstructed at consultation; exact chart time unavailable',
                                          'event_time_known': False}})
    if len({row['source_ref'] for row in provenance}) != len(provenance):
        raise ValueError('Duplicate presentation excerpt')
    return facts, observations, provenance


_NOTE_SECTION = re.compile(r'(?im)^[ \t]*(allergies|chief complaint|major surgical or invasive procedure|'
    r'history of present illness|past medical history|social history|family history|physical exam|'
    r'pertinent results|brief hospital course|hospital course|medications on admission|discharge medications|'
    r'discharge disposition|discharge diagnosis|discharge condition|discharge instructions|follow[ -]?up instructions|'
    r'service|attending|facility)[ \t]*:[ \t]*')
_CLINICAL_SECTIONS = frozenset({'allergies', 'chief complaint', 'major surgical or invasive procedure',
    'history of present illness', 'past medical history', 'social history', 'family history', 'physical exam',
    'pertinent results', 'brief hospital course', 'hospital course', 'medications on admission',
    'discharge medications', 'discharge diagnosis', 'discharge condition'})


def clinical_note_sections(text):
    """Keep the author's section boundaries and offsets for dated clinical source material."""
    markers = list(_NOTE_SECTION.finditer(text))
    for i, match in enumerate(markers):
        title = match[1].lower()
        if title not in _CLINICAL_SECTIONS:
            continue
        stop = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        if text[match.end():stop].strip():
            yield title, match.end(), stop


def prior_note_observations(case_id, note_id, text, recorded, arrival, encounter_ref, *, titles=None):
    offset = (datetime.fromisoformat(recorded) - datetime.fromisoformat(arrival)).total_seconds() / 3600
    if offset >= 0:
        raise ValueError('A prior clinical note must be available before arrival')
    for title, start, end in clinical_note_sections(text):
        if titles is not None and title not in titles:
            continue
        parts = list(range(start, end, 1400))
        for i, pos in enumerate(parts, 1):
            excerpt = text[pos:min(end, pos + 1400)]
            yield {'name': f'Prior {title} ({i}/{len(parts)})', 'value_raw': title + ': ' + excerpt,
                   'source_ref': source_ref(case_id, 'prior_note', f'{note_id}/{pos}'),
                   'source_encounter_ref': encounter_ref, 'source_kind': 'MIMIC-IV-Note prior discharge',
                   'time': {'recorded_offset_hours': round(offset, 5), 'available_by_source_cutoff': True}}


def prior_chart_rows(book, chart, *, case_id, subject, admission, arrival):
    """Index dated prior findings and administration records for physician-initiated lookups."""
    origin = datetime.fromisoformat(arrival)
    def offset(stamp):
        return round((datetime.fromisoformat(stamp) - origin).total_seconds() / 3600, 5)

    periods = {(v['setting'], v['admission_offset_hours'], v['discharge_offset_hours']): v['encounter_ref']
               for v in chart['historical_encounters']}
    ed, hosp = {}, {}
    for visit in book['就诊索引']:
        if visit['类型'] not in ('急诊', '住院') or not visit['结束'] or visit['结束'] >= arrival:
            continue
        setting = 'emergency' if visit['类型'] == '急诊' else 'inpatient'
        key = periods[(setting, offset(visit['开始']), offset(visit['结束']))]
        (ed if setting == 'emergency' else hosp)[visit['原始就诊号']] = key
    rows = book['原表数据']
    terms = {r['itemid']: r for r in book['本患者引用的字典与人员索引']['hosp.d_labitems']}
    observations, administrations = [], []
    for n, r in enumerate(rows['hosp.labevents'], 1):
        sampled, available = r['charttime'], r['storetime']
        if (r['subject_id'] != subject or r['hadm_id'] == admission or not sampled or not available
                or not sampled <= available < arrival):
            continue
        values = lab_fields(r)
        if values is None:
            continue
        term = terms[r['itemid']]
        item = {'source_ref': source_ref(case_id, 'hosp.labevents', n), 'source_kind': 'MIMIC-IV hosp.labevents',
                'name': term['label'], 'display': term['label'], 'fluid': term['fluid'],
                **values, 'unit_raw': r['valueuom'],
                'time': {'event_offset_hours': offset(sampled), 'recorded_offset_hours': offset(available),
                         'available_by_source_cutoff': True}}
        if r['hadm_id'] in hosp:
            item['source_encounter_ref'] = hosp[r['hadm_id']]
        observations.append(item)
    for n, r in enumerate(rows['ed.triage'], 1):
        if r['subject_id'] == subject and r['stay_id'] in ed and r['chiefcomplaint'].strip():
            observations.append({'source_ref': source_ref(case_id, 'ed.triage', n),
                'source_kind': 'MIMIC-IV-ED ed.triage', 'source_encounter_ref': ed[r['stay_id']],
                'name': 'Prior ED chief complaint', 'value_raw': r['chiefcomplaint']})
    for n, r in enumerate(rows['hosp.emar'], 1):
        when, recorded = r['charttime'], r['storetime']
        if (r['subject_id'] == subject and r['hadm_id'] in hosp and r['medication'].strip()
                and when and recorded and when < arrival and recorded < arrival):
            administrations.append({'source_ref': source_ref(case_id, 'hosp.emar', n),
                'source_kind': 'MIMIC-IV hosp.emar', 'source_encounter_ref': hosp[r['hadm_id']],
                'medication': r['medication'], 'event_status_raw': r['event_txt'],
                'administration_is_not_current_use': True,
                'time': {'event_offset_hours': offset(when), 'recorded_offset_hours': offset(recorded)}})
    return {'historical_observations': observations, 'historical_administrations': administrations}


def preprescribing_events(rows, *, case_id, subject, stay, admission, arrival, cutoff):
    """Use stay-linked ED records and admission-linked diagnostic orders before the cutoff."""
    if not all((subject, stay, admission, arrival, cutoff)) or cutoff < arrival:
        raise ValueError('An identified ED interval is required')
    origin = datetime.fromisoformat(arrival)
    events = []
    for table in ('ed.vitalsign', 'ed.medrecon', 'hosp.poe'):
        for ordinal, row in enumerate(rows.get(table, ()), 1):
            if row['subject_id'] != subject:
                continue
            is_order = table == 'hosp.poe'
            when = row['ordertime' if is_order else 'charttime']
            if not when or not arrival <= when < cutoff:
                continue
            if is_order:
                if (row['hadm_id'] != admission or row['transaction_type'] != 'New'
                        or row['order_type'] not in {'Lab', 'Radiology', 'Respiratory', 'Cardiology'}):
                    continue
                event = {'kind': 'investigation_order', 'display': row['order_subtype'] or row['order_type'],
                         'status': 'ordered; result not reported by this record'}
            else:
                if row['stay_id'] != stay:
                    continue
                if table == 'ed.medrecon':
                    if not row['name'].strip() or when == arrival:
                        continue
                    event = {'kind': 'home_medication_history', 'display': row['name'],
                             'status': 'listed at medication reconciliation; last dose and adherence unreported'}
                else:
                    values = [{'name': name, 'value': row[key], 'unit': unit}
                              for key, (name, unit) in VITALS.items() if row.get(key) not in (None, '')]
                    if not values:
                        continue
                    event = {'kind': 'repeat_vitals', 'display': 'ED vital signs', 'measurements': values,
                             'status': 'recorded'}
            offset = round((datetime.fromisoformat(when) - origin).total_seconds() / 3600, 5)
            events.append({**event, 'source_kind': table, 'source_ref': source_ref(case_id, table, ordinal),
                           'time': {'event_offset_hours': offset, 'recorded_offset_hours': offset,
                                    'basis': 'hours since ED arrival'}})
    return sorted(events, key=lambda item: item['time']['event_offset_hours'])


def collected_lab_reports(rows, dictionary, *, case_id, subject, stay, admission, arrival, cutoff, departure):
    """Recover patient lab reports from specimens already collected before prescribing."""
    if not arrival <= cutoff < departure:
        raise ValueError('Invalid ED collection window')
    origin = datetime.fromisoformat(arrival)
    def hours(stamp):
        return round((datetime.fromisoformat(stamp) - origin).total_seconds() / 3600, 5)

    other_ed = [(r['intime'], r['outtime']) for r in rows['ed.edstays']
                if r['subject_id'] == subject and r['stay_id'] != stay]
    inpatient = [(r['admittime'], r['dischtime']) for r in rows['hosp.admissions'] if r['subject_id'] == subject]
    reports = []
    for number, row in enumerate(rows['hosp.labevents'], 1):
        sampled, available, hadm = row['charttime'], row['storetime'], row['hadm_id']
        if (row['subject_id'] != subject or not sampled or not available
                or not arrival <= sampled < cutoff or not sampled <= available < departure):
            continue
        if hadm:
            if hadm != admission or available < cutoff:
                continue  # Earlier explicitly linked labs already appear in the consultation panel.
            binding = 'same admission identifier'
        else:
            if any(lo <= available and (not hi or sampled < hi) for lo, hi in other_ed + inpatient if lo):
                continue
            binding = 'same patient; sampled during this ED stay only; no source encounter identifier'
        values = lab_fields(row)
        if values is None:
            continue
        term = dictionary[row['itemid']]
        reports.append({'source_ref': source_ref(case_id, 'hosp.labevents', number), 'source_kind': 'hosp.labevents',
                        'name': term['label'], 'fluid': term['fluid'], 'status': binding,
                        **values, 'unit_raw': row['valueuom'],
                        'flag_raw': row['flag'], 'time': {'basis': 'hours since ED arrival',
                            'event_offset_hours': hours(sampled), 'recorded_offset_hours': hours(available)},
                        'available_at': 'initial_prescribing' if available < cutoff else 'ed_reassessment'})
    return sorted(reports, key=lambda record: record['time']['recorded_offset_hours'])


def encounter_reports(rows, dictionary, imaging, *, case_id, subject, stay, admission, arrival, cutoff, departure):
    """Index observed results from the index ED stay for clinician-initiated lookup."""
    if not arrival <= cutoff <= departure:
        raise ValueError('Invalid ED review interval')
    origin = datetime.fromisoformat(arrival)
    reports = []

    def add(table, key, name, specimen, value, unit, sampled, reported, linkage, flag='', numeric='', extras=None):
        if not (sampled and reported and arrival <= sampled <= reported <= departure):
            return
        if (value is None or str(value).strip() in ('', '___')) and numeric in (None, '') and not extras:
            return
        reports.append({'source_ref': source_ref(case_id, table, key), 'source_kind': table,
                        'name': name, 'fluid': specimen, 'value_raw': value, 'numeric_value_raw': numeric, **(extras or {}),
                        'unit_raw': unit, 'flag_raw': flag,
                        'status': linkage, 'available_at': 'initial_ed_review' if reported < cutoff else 'ed_reassessment',
                        'time': {'basis': 'hours since ED arrival',
                                 'event_offset_hours': round((datetime.fromisoformat(sampled) - origin).total_seconds()/3600, 5),
                                 'recorded_offset_hours': round((datetime.fromisoformat(reported) - origin).total_seconds()/3600, 5)}})

    other_visits = [(r['intime'], r['outtime']) for r in rows['ed.edstays']
                    if r['subject_id'] == subject and r['stay_id'] != stay]
    other_visits += [(r['admittime'], r['dischtime']) for r in rows['hosp.admissions']
                     if r['subject_id'] == subject and r['hadm_id'] != admission]
    def encounter_link(row):
        sampled, released = row.get('charttime'), row.get('storetime')
        if row.get('subject_id') != subject or not (sampled and released and arrival <= sampled <= released <= departure):
            return None
        if row.get('hadm_id'):
            return 'same admission and ED interval' if row['hadm_id'] == admission else None
        if not any(lo and lo <= released and (not hi or hi >= sampled) for lo, hi in other_visits):
            return 'same patient and unique ED interval; no admission identifier'
        return None

    for row in rows['hosp.labevents']:
        linkage = encounter_link(row)
        if not linkage:
            continue
        sampled, released = row['charttime'], row['storetime']
        term = dictionary[row['itemid']]
        values = lab_fields(row)
        if values is not None:
            add('hosp.labevents', row['labevent_id'], term['label'], term['fluid'], values['value_raw'],
                row['valueuom'], sampled, released, linkage, row['flag'], values['numeric_value_raw'],
                {k: values[k] for k in ('value_source_column', 'lab_comment_raw') if k in values})

    for row in rows.get('hosp.microbiologyevents', ()):
        linkage = encounter_link(row)
        if not linkage or not row.get('microevent_id') or not row.get('test_name'):
            continue
        def recorded(key):
            value = str(row[key]).strip() if row.get(key) is not None else ''
            return value if value.strip(' _') else ''
        values = [recorded('org_name'), recorded('quantity'), recorded('comments')]
        dilution = recorded('dilution_text') or (recorded('dilution_comparison') + recorded('dilution_value')
                                                  if recorded('dilution_value') else '')
        susceptibility = ' '.join(v for v in (recorded('interpretation'), dilution) if v)
        if susceptibility:
            values.append(' '.join(v for v in (recorded('ab_name'), susceptibility) if v))
        result = ' | '.join(dict.fromkeys(v for v in values if v))
        if result:
            add('hosp.microbiologyevents', row['microevent_id'], row['test_name'], row.get('spec_type_desc', ''),
                result, '', row['charttime'], row['storetime'], linkage)

    for n, row in enumerate(rows['ed.vitalsign']):
        if row['subject_id'] != subject or row['stay_id'] != stay:
            continue
        for key, (name, unit) in VITALS.items():
            add('ed.vitalsign', f'{n}/{key}', name, 'Bedside', row.get(key), unit,
                row['charttime'], row['charttime'], 'same ED stay')

    for row in imaging:
        if row['subject_id'] != subject or row['hadm_id'] not in ('', admission):
            raise ValueError('Imaging report belongs to a different encounter')
        name = row.get('exam_name') or 'Radiology report'
        # Keep long reports readable through the chart's per-record page budget.
        body = row['text'].strip()
        parts = [body[i:i + 1500] for i in range(0, len(body), 1500)]
        for i, part in enumerate(parts, 1):
            display = f'{name} ({i}/{len(parts)})' if len(parts) > 1 else name
            link = 'same admission and ED interval' if row['hadm_id'] else 'same patient and verified ED report'
            add('MIMIC-IV-Note radiology', f'{row["note_id"]}/{i}', display, 'Radiology', part, '',
                row['charttime'], row['storetime'], link)
    return sorted(reports, key=lambda item: item['time']['recorded_offset_hours'])
