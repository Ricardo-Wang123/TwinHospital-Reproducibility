"""Clinical closure at ED departure, verified against the same OpenHIS encounter."""
import json
import re

DESIGN = 'emergency-v1'
DISPOSITIONS = ('admit', 'discharge', 'transfer', 'observation', 'other')
ENDPOINT = 'signed_ed_note_signed_medication_orders_and_documented_disposition'
WINDOW_END = 'signed_ed_note_readback'
WINDOW_CONSTRAINTS = ('patient_encounter_linkage', 'queue_linkage_before_consultation',
                      'locked_note_immutability', 'authorized_clinician_signatures', 'documented_ed_disposition')

WORK_POLICY = {'completion': ENDPOINT,
               'clinical_work': 'in closed-loop runs, the same physician resumes in renewable work windows without a clinical round limit',
               'unresolved_clinical_work': 'retained in the assigned denominator',
               'no_reference_in_clinical_loop': True}

NOTE_HEADINGS = ('Subjective', 'Objective', 'Assessment', 'Plan')
NOTE_HEADING_ERROR = 'Use the four ED note headings on separate lines in order. A section may state that its facts are undocumented.'
NOTE_RECONCILIATION_POLICY = 'authored_ed_plan_v1'
NOTE_MEDICATION_LABEL = 'Final ED orders'
NOTE_DISPOSITION_LABEL = 'ED disposition'

PRIOR_REVIEW_SECTIONS = ('encounter_headers', 'diagnoses', 'historical_orders', 'historical_administrations',
                         'historical_observations', 'historical_outpatient_measurements', 'historical_procedures',
                         'historical_microbiology', 'historical_dispenses')
PRESENTATION_SECTIONS = ('demographics', 'problem_items', 'allergy_record', 'current_use_record',
                         'current_observations', 'ed_workup', 'ed_diagnostic_reports')
REPORT_SECTIONS = {'presentation': 'ed_diagnostic_reports', 'reassessment': 'ed_reassessment_reports',
                   'closing': 'ed_closing_reports'}
WORKUP_SECTIONS = {'presentation': 'ed_workup', 'reassessment': 'ed_reassessment_workup',
                   'closing': 'ed_closing_workup'}
SOURCE_ED_ADMINISTRATION = 'recorded_ed_medication_administration'


def hide_index_ed_choices(chart):
    """Leave index-visit actions and hindsight outside the agent's clinical record."""
    source_actions = {SOURCE_ED_ADMINISTRATION, 'recorded_ed_medication_order', 'investigation_order'}
    sections = (*WORKUP_SECTIONS.values(), *REPORT_SECTIONS.values(), 'ed_lab_reports', 'current_observations')
    for section in sections:
        if section not in chart:
            continue
        chart[section] = [
            row for row in chart[section] if row.get('kind') not in source_actions
            and not (section == REPORT_SECTIONS['closing']
                     and (row.get('time') or {}).get('event_time_known') is False
                     and (row.get('source_kind') or '').endswith('retrospectively documented'))
        ]


PRESCRIBING_CHECK_POLICY = 'source_review_before_each_complete_submission_v1'
PRESCRIBING_CHECKS = {
    'allergies': 'What drug allergies or reactions are documented, and is their current status confirmed?',
    'medicines': 'Which medicines were prescribed previously, reported in current use, or given before arrival?',
}


def named_prescribing_checks(response, *, include_prose=True):
    checks = list(response.get('outstanding_checks') or [])
    if include_prose:
        checks.extend((response.get('medication_reconciliation') or {}).get('unresolved') or [])
    flags = response.get('flags') or []
    checks.extend(flags)
    if response.get('decision') in ('flag', 'reject') and not (flags or checks):
        checks.append(response.get('reason'))
    prose = [response.get('plan'), response.get('reason'), *(response.get('issues') or [])]
    for item in prose if include_prose and not response.get('check_results') else ():
        for sentence in re.split(r'(?<=[.!?。！？])\s+|(?<=[。！？])', item if isinstance(item, str) else ''):
            if re.search(r'\b(?:clarif\w*|verify|ascertain|reconcile)\b|'
                         r'\b(?:confirm|check)\b.{0,65}\bbefore\b|待核|需核|须核|待查|需查|须查|需确认|须确认', sentence, re.I):
                checks.append(sentence.strip())
    return list(dict.fromkeys(' '.join(q.split()) for q in checks if isinstance(q, str) and q.strip()))


def evidence_text(value):
    if isinstance(value, dict):
        for part in value.values():
            yield from evidence_text(part)
    elif isinstance(value, (list, tuple)):
        for part in value:
            yield from evidence_text(part)
    elif isinstance(value, str) and value.strip():
        yield ' '.join(value.split()).casefold()
    elif type(value) in (int, float):
        yield str(value)


def review_source_fields(row):
    return {key: value for key, value in row.items()
            if key not in {'source_kind', 'status', 'kind', 'available_at',
                           'encounter_ref', 'source_encounter_ref', 'source_refs'}
            and not key.startswith('time.')}


def review_source_values(observation):
    if observation['action'] == 'prepare_consultation':
        handoff = observation['handoff']
        yield from evidence_text([handoff['text'], [fact.get('value') for fact in handoff['facts']]])
        yield from evidence_text(observation.get('triage_record', {}).get('recorded_reasons', ()))
        return
    for table in observation['tables']:
        if observation.get('required_evidence_section') not in (None, table['section']):
            continue
        for row in table_records(table):
            if observation.get('required_evidence_section') == 'allergy_record':
                yield from evidence_text([row.get('patient_report.value'), [
                    [item['name'], item.get('reaction')] for item in row.get('items', [])]])
            else:
                yield from evidence_text(review_source_fields(row))


def review_measurements(observation):
    for table in observation['tables']:
        for row in table_records(table):
            name, value, unit = (row.get(key) for key in ('name', 'value_raw', 'unit_raw'))
            if all(isinstance(part, str) and part.strip() for part in (name, value, unit)):
                yield ' '.join(f'{name} {value} {unit}'.split())


def normalize_chart_receipt(response, observation):
    page = observation['page_number']
    if response.get('page_number') == str(page):
        response = {**response, 'page_number': page}
    if response.get('evidence_quote'):
        return response
    if 'evidence_cell' in response:
        quote = review_cell_value(response['evidence_cell'], observation)
    else:
        findings = response.get('findings')
        if not isinstance(findings, str):
            return response
        text = ' '.join(findings.split())
        quote = next((measurement for measurement in review_measurements(observation)
                      if re.search(r'(?<!\w)' + re.escape(measurement) + r'(?!\w)', text, re.I)), None)
    return {**response, 'evidence_quote': quote} if quote is not None else response


def review_cell_value(selection, observation):
    if (not isinstance(selection, dict) or set(selection) != {'table', 'row', 'field'}
            or type(selection['table']) is not int or type(selection['row']) is not int
            or not isinstance(selection['field'], str)):
        return None
    tables = observation['tables']
    if not 0 <= selection['table'] < len(tables):
        return None
    records = table_records(tables[selection['table']])
    if not 0 <= selection['row'] < len(records):
        return None
    value = review_source_fields(records[selection['row']]).get(selection['field'])
    if type(value) in (str, float, int) and str(value).strip():
        return str(value)
    return None


def review_quote_matches(quote, observation):
    if not isinstance(quote, str):
        return False
    quoted = ' '.join(quote.split()).casefold()
    if not any(char.isalnum() for char in quoted):
        return False
    if any(quoted == value or (len(quoted) >= 8 and quoted in value)
           for value in review_source_values(observation)):
        return True
    if observation.get('required_evidence_section'):
        return False
    if observation['action'] != 'review_ed_chart_page':
        return False
    if any(quoted == measurement.casefold() for measurement in review_measurements(observation)):
        return True
    literal = quote.strip()
    pipe_row = '|' in literal and not literal.startswith('[')
    try:
        cells = ([part.strip().casefold() for part in literal.split('|')] if pipe_row else
                 json.loads(literal if literal.startswith('[') else '[' + literal + ']'))
    except ValueError:
        return False
    if not isinstance(cells, list) or len(cells) < 2:
        return False
    for table in observation['tables']:
        table_columns = table['columns']
        technical = {'encounter_ref', 'source_encounter_ref', 'source_refs'}
        columns = table_columns[:len(cells)] if pipe_row else table_columns
        clinical_columns = [i for i, key in enumerate(columns)
                            if key not in technical]
        if pipe_row and len(clinical_columns) >= 2 and len(cells) == len(columns) and any(
                all(cells[i] == str(row[i]).strip().casefold() for i in clinical_columns) for row in table['rows']):
            return True
        if pipe_row and any(
                [str(value).strip().casefold() for name, value in zip(table_columns, row, strict=True)
                 if name not in technical and value not in (None, '')] == cells for row in table['rows']):
            return True
        if clinical_columns and len(cells) == len(table['columns']) and any(
                all(cells[i] == row[i] for i in clinical_columns) for row in table['rows']):
            return True
    return False


def review_quote_source(quote, observation, earlier_calls):
    """Return 0 for the current material or the call number of an earlier reviewed source."""
    if review_quote_matches(quote, observation):
        return 0
    if observation.get('required_evidence_section'):
        return None
    for call in reversed(earlier_calls):
        previous = call['observation']
        if (call['role'] == 'doctor' and call['status'] == 'completed' and previous.get('source_quote_required')
                and review_quote_matches(quote, previous)):
            return call['sequence']
    return None


def chart_excerpt_present(quote, tables, *, medication_only=False):
    """Match a multi-field excerpt to one record or a single timed set of measurements."""
    def segments(text):
        return {' '.join(part.strip().casefold().replace(' %', '%').split())
                for part in text.split(';') if part.strip()}

    def phrases(row):
        values = set()
        for field in ('display','status'):
            if isinstance(row.get(field),str):
                values.update(segments(row[field]))
                if field=='status':
                    values.update({'status: '+value for value in segments(row[field])})
        name = row.get('name')
        if name:
            for field in ('value_raw','numeric_value_raw'):
                value = row.get(field)
                if value not in (None,''):
                    values.update(segments(f'{name} {value}'))
                    if row.get('unit_raw'):
                        unit = row['unit_raw']
                        values.update(segments(f'{name} {value} {unit}'))
                        values.update(segments(f'{value} {unit}'))
                        values.update(segments(f'{value}{unit}'))
        return values

    expected = segments(quote)
    if not expected:
        return False
    timed = {}
    for table in tables:
        for row in table_records(table):
            if medication_only and row.get('kind')!='home_medication_history':
                continue
            values = phrases(row)
            if expected <= values:
                return True
            when = row.get('time.event_offset_hours'),row.get('time.recorded_offset_hours')
            if (not medication_only and row.get('name') and all(t is not None for t in when)):
                key = (table['section'],row.get('source_kind'),row.get('fluid'),when)
                timed.setdefault(key,set()).update(values)
    return any(expected <= values for values in timed.values())


def ips_documents_check(ips, key):
    section = ips.get({'medicines': 'medications', 'allergies': 'allergies'}.get(key))
    return (section is None or bool(section.get('entries')) or section.get('status') in
            {'none_reported', 'none_known', 'confirmed_no_current_use', 'no_known_allergies'})


def chart_citation_candidates(tables, quote):
    """Find short, already delivered measurements or passages for a returned quotation."""
    attempted = ' '.join(quote.casefold().split())
    keywords = set(re.findall(r'\w+', attempted))
    fragments = re.split(r'\.{3}|…|;|,', quote)
    candidates = list(review_measurements({'tables': tables}))
    for table in tables:
        for row in table_records(table):
            for field in ('display', 'lab_comment_raw'):
                if isinstance(value := row.get(field), str):
                    candidates.extend(re.split(r'(?<=[.!?。！？;])\s+', value))
    ranked = {}
    for candidate in [*fragments, *candidates]:
        candidate = ' '.join(candidate.split())
        if 4 <= len(candidate) <= 420:
            normalized = candidate.casefold()
            shared = keywords.intersection(re.findall(r'\w+', normalized))
            if normalized in attempted or len(shared) > 1:
                ranked[candidate] = (normalized in attempted, len(shared))
    return sorted(ranked, key=ranked.get, reverse=True)


def check_findings(required, findings, sources, sources_consulted, *, allow_question_limits=False):
    """Return the still-open questions. A claimed finding needs an observed source passage."""
    open_checks = {}
    for key, question in required.items():
        row = findings.get(key, {})
        if not isinstance(row, dict):
            open_checks[key] = question
            continue
        status = row.get('status')
        source = row.get('source')
        quote = row.get('quote')
        if ('ips' in sources and isinstance(source, str) and source in {'ips', 'original_chart_unknown'}
                and not key.startswith('allergy_chart_')):
            finding = row.get('finding')
            limits = (status == 'reviewed_with_limits' and isinstance(row.get('decision_basis'), str)
                      and bool(row['decision_basis'].strip()))
            if not isinstance(finding, str) or not finding.strip():
                open_checks[key] = question
                continue
            if source == 'ips':
                if limits or (status == 'verified' and ips_documents_check(sources['ips'], key)):
                    continue
            elif limits and isinstance(quote, str) and any(
                    receipt['status'] == 'no_recorded_result'
                    and receipt['term'].strip().casefold() == quote.strip().casefold()
                    for receipt in sources.get('chart_search_receipts', [])):
                continue
            open_checks[key] = question
            continue
        if status == 'reviewed_with_limits':
            allows_limits = key in PRESCRIBING_CHECKS or (allow_question_limits and not key.startswith('allergy_'))
            if (not allows_limits or key not in sources_consulted
                    or not isinstance(row.get('decision_basis'), str) or not row['decision_basis'].strip()):
                open_checks[key] = question
                continue
        elif status != 'verified':
            open_checks[key] = question
            continue
        if (not isinstance(source, str) or not isinstance(quote, str) or not quote.strip()
                or not isinstance(row.get('finding'), str) or not row['finding'].strip()):
            open_checks[key] = question
            continue
        limited_med_history = (key=='medicines' and status=='reviewed_with_limits' and allow_question_limits
                               and source in {'medication_history','medication_records'})
        ips_patient = 'ips' in sources and source == 'patient'
        if (key == 'allergies' and source != ('allergy_history' if status == 'reviewed_with_limits' else 'current_allergy')
                or key == 'medicines' and not limited_med_history
                and source != ('medication_records' if status == 'reviewed_with_limits' else 'medication_history')) and not ips_patient:
            open_checks[key] = question
            continue
        quote = ' '.join(quote.split()).casefold()
        evidence = sources.get(source)
        chart = sources.get('ed_chart', [])
        if source != 'ed_chart' and not limited_med_history:
            chart = [table for table in chart if table.get('section') == source]
        if evidence is None and chart:
            evidence = chart
        if key.startswith('allergy_chart_'):
            event = sources.get(key, {})
            if (source != key or row.get('verification_status') != event.get('verification_status')):
                open_checks[key] = question
                continue
            evidence = event.get('source_statement') or event.get('substance')
        if status == 'verified' and quote in {'unknown', 'unavailable', 'not documented', 'unverified',
                                              'prior_documentation_only_current_status_unverified'}:
            open_checks[key] = question
            continue
        exact = any(quote == passage or (len(quote) >= 4 and quote in passage)
                    for passage in evidence_text(evidence))
        if not exact and chart and not key.startswith('allergy_chart_'):
            exact = chart_excerpt_present(quote, chart, medication_only=limited_med_history)
        if not exact:
            open_checks[key] = question
    return open_checks


def reported_value(row):
    if row.get('kind') == 'investigation_order' or row.get('source_kind') == 'hosp.poe':
        return False
    if isinstance(row.get('allergy_update'), dict) and row['allergy_update'].get('substance'):
        return True
    for key in ('numeric_value_raw', 'value_raw'):
        value = row.get(key)
        if value is not None and str(value).strip(' _\t\r\n').casefold() not in {
                '', 'n/a', 'nan', 'pending', 'not available', 'not reported', 'not performed',
                'not done', 'test not performed', 'cancelled', 'canceled'}:
            return True
    return False


def documented_allergy(row, stage):
    """Keep an attributed chart statement separate from the allergy status at arrival."""
    record = row['record']
    if record.get('kind') == 'investigation_order':
        return None
    detail = record.get('allergy_update')
    title = str(record.get('name') or record.get('kind') or '')
    passage = str(record.get('value_raw') or record.get('display') or '').strip()
    if detail is not None:
        if (not isinstance(detail.get('substance'), str) or not detail['substance'].strip()
                or detail.get('verification_status') not in {'confirmed', 'unconfirmed', 'refuted', 'entered-in-error'}):
            raise ValueError('A documented allergy update requires a named substance and source verification status')
        status = detail['verification_status']
    else:
        # A laboratory value alone carries no drug allergy interpretation.
        pattern = r'anaphylaxis (?:to|after)|allergic reaction (?:to|after)|allergic to|drug allergy to|药物过敏反应|诊断.{0,12}药物过敏|对.{1,24}过敏'
        if not re.search(r'drug allergy (?:assessment|evaluation)|药物过敏评估', title, re.I):
            excerpts = [s.strip() for s in re.split(r'(?<=[.!?。！？])\s+', passage) if re.search(pattern, s, re.I)]
            if not excerpts:
                return None
            passage = ' '.join(excerpts)
        if not passage:
            return None
        status = 'source_statement'
    return {'record_ref': row['record_ref'], 'source':record.get('source_kind') or title,
            'first_visible_at':stage, 'recorded_offset_hours':(record.get('time') or {}).get('recorded_offset_hours'),
            'source_statement':passage, **(detail or {}), 'verification_status':status}


def table_records(table):
    return [{**table['common'], **dict(zip(table['columns'], values))} for values in table['rows']]


def reviewed_prior_medications(sections):
    """Index the latest reviewed record per source section and exact medication name."""
    medication_fields = {'historical_orders': ('drug_name_raw', 'drug_name'),
                         'historical_administrations': ('medication',), 'historical_dispenses': ('medication',)}
    index = {}
    for section, rows in sections:
        if section not in medication_fields:
            continue
        entries = index.setdefault(section, {})
        for row in rows:
            record = row['record']
            name = next((record[key] for key in medication_fields[section]
                         if isinstance(record.get(key), str) and record[key].strip()), None)
            if name is None:
                continue
            offset = (record.get('time') or {}).get('event_offset_hours')
            offset = offset if type(offset) in (int, float) else None
            key = name.strip().casefold()
            previous = entries.get(key)
            if previous is None or (offset is not None and (previous[1] is None or offset > previous[1])):
                entries[key] = [name, offset, record.get('event_status_raw') or record.get('status'), row['record_ref']]
    if not any(index.values()):
        return {}
    return {'columns': ['medication', 'event_hours_from_ed_arrival', 'source_status', 'chart_record_ref'],
            'by_chart_section': {section: [entries[key] for key in sorted(entries)]
                                 for section, entries in index.items() if entries},
            'scope': 'Prior encounter records. Read the originating section by medication name for details. '
                     'Current use, adherence and most recent dose require current encounter evidence.'}


def is_triage_reason(fact):
    return (fact.get('reporter') == 'triage' or
            fact.get('source_kind', '').casefold().endswith('ed.triage.chiefcomplaint'))


def patient_account(memory):
    facts = [fact for fact in memory['patient_facts'] if not is_triage_reason(fact)]
    account = {**memory, 'patient_facts': facts}
    if len(facts) != len(memory['patient_facts']):
        account['interview_source'] = 'patient_attributed_history' if facts else 'unrecorded'
    return account


def triage_reasons(case):
    values = [row['display'] for row in case['his_initial'].get('ed_workup', ())
              if row.get('kind') == 'triage_reason']
    values.extend(f['value'] for f in case['patient_initial']['patient_facts'] if is_triage_reason(f))
    return list(dict.fromkeys(values))


def chart_review_pages(sections, max_bytes=18000):
    """Render complete clinical values in bounded tables. Receipt rows stay in display order."""
    def fields(record, prefix=''):
        result = {}
        for key, value in record.items():
            if key in {'source_ref', 'record_id', 'dictionary_source_ref'}:
                continue
            name = prefix + key
            if isinstance(value, dict):
                result.update(fields(value, name + '.'))
            else:
                result[name] = value
        return result

    def table(section, rows):
        values = [fields(row['record']) for row in rows]
        keys = sorted(set().union(*(row.keys() for row in values)))
        common = {k: values[0][k] for k in keys if all(k in row and row[k] == values[0].get(k) for row in values)}
        columns = [k for k in keys if k not in common]
        return {'section': section, 'common': common, 'columns': columns,
                'rows': [[row.get(k) for k in columns] for row in values]}

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())

    def split(section, rows):
        view = table(section, rows)
        if size([view]) > max_bytes:
            if len(rows) == 1:
                raise ValueError('An ED chart record requires clinically complete subrecords: ' + section)
            mid = len(rows) // 2
            yield from split(section, rows[:mid])
            yield from split(section, rows[mid:])
        else:
            yield view, [row['record_ref'] for row in rows]

    tables, receipts = [], []
    for section, rows in sections:
        for start in range(0, len(rows), 128):
            for view, refs in split(section, rows[start:start + 128]):
                if tables and size(tables + [view]) > max_bytes:
                    yield tables, receipts
                    tables, receipts = [], []
                tables.append(view)
                receipts.append({'section': section, 'refs': refs})
    if tables:
        yield tables, receipts


def encounter_order_activity(events, patient_id, encounter_id):
    """Return the last verified state of each order version from this simulated visit."""
    orders = {}
    uncertain = set()
    for event in events:
        op = event.get('operation')
        if (event.get('event') != 'business_operation' or op not in ('order.save', 'order.attest')
                or event.get('patient_id') != patient_id or event.get('encounter_id') != encounter_id):
            continue
        response = event['response']
        if not response.get('readback_verified'):
            if response.get('native_effect') is not False:
                uncertain.add(event.get('request_id'))
            continue
        data = response.get('data') or {}
        order_id = data.get('order_id')
        if not order_id or not data.get('status'):
            uncertain.add(event.get('request_id'))
            continue
        rows = response.get('readback')
        if isinstance(rows, list):
            products = [r['adviceDisplayName'] for r in rows if r.get('adviceDisplayName')]
        else:
            products = [item.get('drug_code') for item in (data.get('items') or []) if item.get('drug_code')]
        orders[order_id] = {'status': data['status'], 'products': products or orders.get(order_id, {}).get('products', [])}
    return {'query_section': 'current_ed_medication_activity',
            'order_versions': list(orders.values()), 'unverified_order_operations': len(uncertain),
            'source': 'HIS order receipts for this simulated ED encounter up to this decision',
            'scope': 'This view lists simulated orders. Original ED administration statuses available by this '
                     'decision are in the ED chart workup. Use them as treatment history. Reconciled home '
                     'medicines and prehospital treatments have separate chart sources.'}


def workup_view(rows):
    """Display recent entries per record type; all entries remain available by chart query."""
    groups = {}
    for row in rows:
        entry = row['record']
        group = groups.setdefault(entry['kind'], {'total': 0, 'recent': []})
        group['total'] += 1
        if len(group['recent']) < 20:
            detail = {'entry': entry['display']}
            if 'event_offset_hours' in entry['time']:
                detail['hours_since_arrival'] = entry['time']['event_offset_hours']
            else:
                detail['time_basis'] = entry['time']['basis']
            if 'measurements' in entry:
                detail['measurements'] = entry['measurements']
            group['recent'].append(detail)
    return {'groups': groups, 'query_section': 'ed_workup',
            'scope': 'Charted during this ED visit and available before prescribing. Listed home medicines '
                     'do not establish ingestion or an ED medication order. Investigation orders establish '
                     'orders only. Repeat vitals are displayed with their recorded times.'}


def lab_report_view(rows, phase):
    """Only the current phase's released values enter the clinician's chart."""
    panels = {}
    for row in rows:
        record = row['record']
        if record['available_at'] != phase:
            continue
        time = record['time']
        key = (time['event_offset_hours'], time['recorded_offset_hours'], record['fluid'], record['status'])
        panel = panels.setdefault(key, {'sample_hour': key[0], 'report_hour': key[1], 'specimen_type': key[2],
                                        'linkage': key[3], 'results': []})
        numeric = record.get('numeric_value_raw')
        value = numeric if numeric not in (None, '') else record['value_raw']
        finding = {'test': record['name'], 'value': value, 'unit': record['unit_raw'], 'flag': record['flag_raw']}
        if record.get('value_source_column'):
            finding['value_source_column'] = record['value_source_column']
        if record.get('lab_comment_raw'):
            finding['lab_comment_raw'] = record['lab_comment_raw']
        panel['results'].append(finding)
    return sorted(panels.values(), key=lambda item: item['report_hour'])


def soap_sections(text):
    """Return the four documented sections when the signed text follows the study template."""
    if not isinstance(text, str):
        return {}
    aliases = {'subjective': 'Subjective', '主观': 'Subjective', '主观资料': 'Subjective',
               'objective': 'Objective', '客观': 'Objective', '客观资料': 'Objective',
               'assessment': 'Assessment', '评估': 'Assessment',
               'plan': 'Plan', '计划': 'Plan', '诊疗计划': 'Plan'}
    blocks = {}
    current = None
    for line in text.splitlines():
        line = line.strip().strip('# ').replace('**', '').strip()
        parts = re.split(r'[:：]', line, maxsplit=1)
        heading = aliases.get(parts[0].strip().casefold())
        if heading:
            if heading in blocks:
                return {}
            current = heading
            blocks[current] = [parts[1].strip()] if len(parts) == 2 else []
        elif line and current:
            blocks[current].append(line)
    result = {name: '\n'.join(lines).strip() for name, lines in blocks.items()}
    return result if tuple(result) == NOTE_HEADINGS and all(result.values()) else {}


def render_soap_sections(sections):
    """Render the author's section text without changing any clinical content."""
    if (not isinstance(sections, dict) or set(sections) != set(NOTE_HEADINGS)
            or any(not isinstance(text, str) or not text.strip() for text in sections.values())):
        raise ValueError(NOTE_HEADING_ERROR)
    return '\n'.join(f'{heading}: {sections[heading]}' for heading in NOTE_HEADINGS)


def format_soap_headings(text):
    """Insert line breaks before explicit headings without changing note content."""
    if not isinstance(text, str) or soap_sections(text):
        return text
    headings = list(re.finditer(r'(?<![A-Za-z0-9_])(Subjective|Objective|Assessment|Plan):', text))
    if tuple(heading.group(1) for heading in headings) != NOTE_HEADINGS or text[:headings[0].start()].strip():
        return text
    formatted = text
    for heading in reversed(headings[1:]):
        offset = heading.start()
        if text[offset - 1] != '\n':
            formatted = formatted[:offset] + '\n' + formatted[offset:]
    return formatted if soap_sections(formatted) else text


def note_order_disposition_check(text, codes, disposition):
    """Compare clinician-authored Plan entries to the verified same-visit state."""
    plan = soap_sections(text).get('Plan', '')
    medication_field = '(?:' + re.escape(NOTE_MEDICATION_LABEL) + '|ED medications?)'
    medications = re.findall(r'^\s*' + medication_field + r'\s*[:：]\s*(.+)$', plan, re.I | re.M)
    dispositions = re.findall(r'^\s*' + re.escape(NOTE_DISPOSITION_LABEL) + r'\s*[:：]\s*(.+)$', plan, re.I | re.M)
    recorded = ({value.strip().casefold() for value in medications[0].split('|') if value.strip()}
                if len(medications) == 1 else set())
    explicit_medication_state = bool(recorded)
    if recorded == {'none'}:
        recorded.clear()
    expected = {code.casefold() for code in codes}
    no_orders = re.search(r'(?:^|[.!?。；\n])\s*(?:no (?:ed )?(?:medications?|(?:medication )?orders?) '
                          r'(?:(?:were|was) )?(?:prescribed|issued|ordered)|'
                          r'(?:本次急诊)?(?:未开药|未开具(?:任何)?(?:药物|处方)))'
                          r'(?=[\s.,;。；，]|$)', plan, re.I)
    return {
        'medication': ('conflict' if (codes and no_orders) or len(medications) > 1 else
                       'unrecorded' if not explicit_medication_state else
                       'match' if recorded == expected else 'conflict'),
        'disposition': ('unrecorded' if not dispositions else 'match' if
                        len(dispositions) == 1 and dispositions[0].strip().casefold() == disposition else 'conflict'),
    }


def require(condition, message):
    if not condition:
        from .contracts import WorkflowFailure
        raise WorkflowFailure('ED closure: ' + message)


def receipt(run, operation, field=None, identity=None):
    for event in reversed(run['events']):
        if event.get('event') != 'business_operation' or event.get('operation') != operation:
            continue
        reply = event['response']
        if (reply.get('native_effect') is True and reply.get('readback_verified') is True
                and reply.get('status') in {'executed', 'replayed', 'ok'}
                and (field is None or reply['data'].get(field) == identity)):
            return reply
    require(False, 'missing native readback for ' + operation)



def registered_encounter(run):
    patient, encounter = str(run['patient_id']), str(run['encounter_id'])
    registered = receipt(run, 'registration.create')['data']
    require(str(registered.get('patient_id')) == patient and
            str(registered.get('encounter_id')) == encounter, 'registration binding mismatch')
    return patient, encounter


def signed_medications(run, patient, encounter):
    from .ed_order_actions import (INDICATION_POLICY, confirmed_checkpoint_transactions, indication_scope, read_actions,
                                   validate_withheld_treatments)
    from .ed_treatment_review import SINGLE_PRESCRIPTION, order_commitment_errors
    from .native_his import VALID_SIGNED_REQUEST_STATES

    artifacts = run['artifacts']
    order = artifacts['prescription_business_state']
    items = artifacts['final_prescription']['items']
    logical = order['logical_order_id']
    draft = receipt(run, 'order.save', 'order_id', logical)
    signed = receipt(run, 'order.attest', 'order_id', logical)
    rows = signed['readback']
    ids = [str(item) for item in signed['data']['native_ids']]
    saved_products = {str(row['requestId']): str(row['productDefinitionId']) for row in draft['readback']}
    signed_products = {str(row['requestId']): str(row['productDefinitionId']) for row in rows}
    signer = str(signed['data'].get('signatory_verification', {}).get('signatory_id') or '')
    history = artifacts.get('ed_signed_order_history')
    states = []
    single = run.get('ed_prescribing_schedule') == SINGLE_PRESCRIPTION
    policy = run.get('ed_order_indication_policy')
    require(policy in (None, INDICATION_POLICY), 'unknown signed medication indication policy')
    if policy:
        require(bool(history), 'missing signed medication indication history')
    if run.get('ed_initial_signature_requires_medication'):
        require(bool(history and history[0]['active']), 'required first medication has no signed order line')
    if history is not None:
        reread = receipt(run, 'order.history')
        pharmacy = artifacts.get('ed_pharmacy_readback')
        if pharmacy is not None:
            require(reread['data'].get('pharmacy_reviews') == pharmacy and set(pharmacy) == {v['order_id'] for v in history},
                    'signed pharmacy review history missing')
        require(bool(history) and reread['data']['versions'] == history and
                reread['readback']['documents'] == [v['document_id'] for v in history] and
                (bool(history[0]['active']) or history[0].get('order_actions') == []),
                'initial order or signed ED decision history missing')
        if single:
            native = reread['readback']
            active_ids = {line['request_id'] for line in history[-1]['active']}
            require(native['current_order_id'] == logical and native['version_states'] == {
                        v['order_id']: ('current' if v['order_id'] == logical else 'superseded') for v in history}
                    and active_ids == {rid for rid, status in native['line_states'].items()
                                       if status in VALID_SIGNED_REQUEST_STATES},
                    'the current prescription differs from the native versions or active line states')
        previous = None
        versions = []
        for current in history:
            confirmation = receipt(run, 'order.attest', 'order_id', current['order_id'])['data']
            require(current['parent_order_id'] == (previous['order_id'] if previous else None)
                    and current['previous_document'] == (previous['document_id'] if previous else None)
                    and current['document_id'] == confirmation['decision_document']
                    and [x['request_id'] for x in current['active']] == confirmation['native_ids']
                    and current['author'] == signer and bool(current['reason']),
                    'ED decision differs from its registered signature or predecessor')
            if single:
                require(bool(current['active']) and current['prescribing_schedule'] == SINGLE_PRESCRIPTION,
                        'a formal single prescription is empty or uses a different workflow')
                if previous:
                    transition = confirmation.get('replacement_readback', {})
                    require(transition.get('current_order_id') == current['order_id']
                            and transition.get('current_document_id') == current['document_id']
                            and transition.get('superseded_order_id') == previous['order_id']
                            and transition.get('superseded_document_id') == previous['document_id']
                            and set(transition['active_line_states']) == {line['request_id'] for line in current['active']}
                            and all(status in VALID_SIGNED_REQUEST_STATES for status in transition['active_line_states'].values())
                            and set(transition['withdrawn_line_states']) == {line['request_id'] for line in current['withdrawn']}
                            and all(status not in VALID_SIGNED_REQUEST_STATES for status in transition['withdrawn_line_states'].values()),
                            'the predecessor and withdrawn medication lines did not read back as superseded')
            if previous and current.get('decision_offset_seconds') is not None:
                same_node = current['decision_offset_seconds'] == previous['decision_offset_seconds']
                returned = pharmacy and pharmacy[previous['order_id']]['decision'] in {'reject', 'defer'}
                require((same_node and returned) or (not returned and
                        current['decision_offset_seconds'] > previous['decision_offset_seconds']),
                        'ED decision times are out of order')
            state = {'offset_seconds': current.get('decision_offset_seconds'),
                     'drug_codes': sorted({x['item']['drug_code'] for x in current['active']})}
            if run.get('ed_signed_treatment_reasons'):
                require('withheld_treatments' in current, 'missing signed treatment reasons')
            if run.get('ed_treatment_review_protocol'):
                require('treatment_review' in current, 'missing signed treatment review')
            reason_field = 'treatment_review' if 'treatment_review' in current else 'withheld_treatments'
            if reason_field in current or 'physician_call' in current:
                if reason_field == 'withheld_treatments':
                    validate_withheld_treatments(current.get(reason_field))
                seq = current.get('physician_call')
                calls = run['calls']
                require(type(seq) is int and 0 < seq <= len(calls), 'missing ED prescribing call')
                call = calls[seq - 1]
                require(call['sequence'] == seq and call['role'] == 'doctor' and call['status'] == 'completed'
                        and call['response'].get(reason_field) == current[reason_field]
                        and call['response'].get('reason') == current['reason']
                        and (call['response'].get('prescription') or {}).get('items')
                            == [line['item'] for line in current['active']],
                        'signed ED reasons differ from the verifying physician response')
                if single:
                    issues = pharmacy[previous['order_id']]['required_followup'] if previous else {}
                    require(current['pharmacy_followup'] == {key: {'issue': issue,
                                'finding': call['response']['check_results'][key]} for key, issue in issues.items()},
                            'the signed revision does not record all pharmacy concerns and their physician findings')
                if reason_field == 'treatment_review':
                    require(not order_commitment_errors(call['observation'], call['response']),
                            'signed ED orders differ from the physician\'s treatment commitments')
                state.update(physician_call=seq)
                state[reason_field] = current[reason_field]
            if policy:
                require('order_actions' in current, 'missing signed ED transactions')
                codes = {'primary': set(), 'other': set()}
                for line in current['active']:
                    item = line['item']
                    codes[indication_scope(item)].add(item['drug_code'])
                state['primary_drug_codes'] = sorted(codes['primary'])
                state['other_drug_codes'] = sorted(codes['other'])
            if 'order_actions' in current:
                state['transactions'] = read_actions(previous, current, by_indication=bool(policy))
            if pharmacy is None:
                states.append(state)
            else:
                outcome = pharmacy[current['order_id']]
                disposition = outcome['decision']
                if disposition == 'not_applicable':
                    require(not current['active'] and not current['order_actions'], 'A signed medication decision lacks pharmacy review')
                else:
                    response = receipt(run, 'order.review', 'order_id', current['order_id'])
                    require(response['data'] == outcome and outcome['signed_decision_document'] == current['document_id'],
                            'The pharmacy review did not read back against this signature')
                versions.append(state)
                if disposition in {'approve', 'not_applicable'}:
                    state = {**state, 'signed_version_transactions': state['transactions'],
                             'transactions': confirmed_checkpoint_transactions(versions)}
                    states.append(state)
                    versions.clear()
            previous = current
        require(not versions, 'A reviewed prescribing checkpoint lacks a resolved decision')
        require(history[-1]['order_id'] == logical and
                [r['item'] for r in history[-1]['active']] == items, 'final ED prescription history differs')
    if run.get('ed_final_signature_requires_active_medication'):
        require(bool(states and states[-1].get('primary_drug_codes')),
                'the final native regimen has no active primary medication')
    if run.get('ed_prescribing_schedule'):
        require(single and len(states) == 1 and pharmacy[logical]['decision'] == 'approve',
                'a single-prescription run must have one resolved prescribing decision')
    require(bool(signer) and order.get('signatory_verified_from_registry') is True and
            order['status'] == signed['data']['status'] == 'signed', 'physician signature missing')
    require((bool(items) or bool(history)) and order['items'] == items and len(items) == len(rows) == len(set(ids))
            and set(ids) == set(signed_products) and saved_products == signed_products
            and all(str(r['patientId']) == patient and str(r['encounterId']) == encounter
                    and str(r.get('authoredById')) == signer for r in rows), 'signed order lines differ')
    reviews = artifacts['prescription_versions']
    empty_confirmation = bool(history) and not items and history[-1].get('order_actions') == []
    review = reviews[-1]['review']['decision']
    require(reviews[-1]['order_id'] == logical and reviews[-1]['prescription'] == artifacts['final_prescription']
            and (review == 'approve' or (empty_confirmation and review == 'not_applicable')), 'final pharmacist approval missing')
    if run.get('clinical_disposition_policy', {}).get('prescribing_checks') == PRESCRIBING_CHECK_POLICY:
        from .contracts import digest
        checks = artifacts.get('ed_prescribing_checks', [])
        require(bool(checks) and not checks[-1]['pending']
                and checks[-1]['order_hash'] == digest(artifacts['final_prescription'])
                and set(checks[-1]['questions']) <= set(checks[-1]['findings']),
                'final prescription differs from the order checked by the treating physician')

    return {'order_id': logical, 'native_request_ids': ids,
            'drug_codes': sorted({item['drug_code'] for item in items}),
            **({'ed_order_indication_policy': policy, 'primary_drug_codes': states[-1]['primary_drug_codes'],
                'other_drug_codes': states[-1]['other_drug_codes']} if policy else {}),
            **({'signed_order_states': states} if history else {})}


def signed_note(run, patient, encounter):
    artifacts = run['artifacts']
    note = artifacts['attested_note']
    lock = receipt(run, 'note.attest')
    doc = lock['readback']
    require(doc.get('status') == 'LOCKED' and lock['data'].get('status') == note.get('status') == 'locked'
            and note['version'] == lock['data']['version'] and str(doc['id']) == str(lock['data']['document_id'])
            and str(doc.get('patientId')) == patient and str(doc.get('encounterId')) == encounter,
            'locked document binding mismatch')
    editor = json.loads(doc['content']) if isinstance(doc['content'], str) else doc['content']
    content = editor['content']
    require(len(content) == 1 and len(content[0]['content']) == 1, 'unexpected stored document structure')
    body = json.loads(content[0]['content'][0]['text'])
    disposition = body.get('ed_disposition') or {}
    require(body == note['content'] and bool(body.get('text', '').strip()) and
            disposition == artifacts['ed_disposition'] and disposition.get('code') in DISPOSITIONS
            and bool(disposition.get('reason', '').strip()), 'signed note or physician disposition differs')
    require(bool(soap_sections(body['text'])), 'signed ED note lacks the four documented SOAP sections')
    if run.get('ed_note_reconciliation_policy') == NOTE_RECONCILIATION_POLICY:
        codes = {item['drug_code'] for item in artifacts['final_prescription']['items']}
        check = note_order_disposition_check(body['text'], codes, disposition['code'])
        require(all(status == 'match' for status in check.values()), 'authored plan differs from signed orders or disposition')
    if artifacts.get('ed_signed_order_history'):
        require(body.get('ed_signed_order_history') == artifacts['ed_signed_order_history'],
                'locked ED note differs from the signed prescription decisions')
    if artifacts.get('ed_allergy_updates'):
        require(body.get('ed_allergy_history', {}).get('updates') == artifacts['ed_allergy_updates'],
                'signed note differs from the released allergy history')
    if 'ed_allergy_review' in artifacts:
        require(body.get('ed_allergy_review') == artifacts['ed_allergy_review']['final'],
                'signed note differs from the final physician allergy review')
    return {'document_id': str(doc['id']), 'disposition': disposition['code']}


def verified_closure(run):
    patient, encounter = registered_encounter(run)
    return {'endpoint': ENDPOINT, 'patient_id': patient, 'encounter_id': encounter,
            **signed_medications(run, patient, encounter), **signed_note(run, patient, encounter)}


def observed_clinical_stages(run):
    """Count only same-encounter clinical stages supported by native read-back."""
    from .contracts import WorkflowFailure
    missing = (WorkflowFailure, LookupError, ValueError, TypeError, AttributeError)
    try:
        patient, encounter = registered_encounter(run)
    except missing:
        return {'reviewed_signed_prescription': False, 'locked_ed_note_with_disposition': False}
    result = {}
    for name, evidence in [('reviewed_signed_prescription', signed_medications),
                           ('locked_ed_note_with_disposition', signed_note)]:
        try:
            evidence(run, patient, encounter)
            result[name] = True
        except missing:
            result[name] = False
    return result
