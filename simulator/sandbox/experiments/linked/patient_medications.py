"""Medication facts the simulated ED patient can recall."""

import re

from .contracts import digest


RECALL_WINDOW_HOURS = 90 * 24
HOME_MEDICATION = 'reconciled_home_medication'
PRIOR_MEDICATION = 'prior_discharge_medication'
MEDRECON = 'MIMIC-IV-ED ed.medrecon'
PRIOR_DISCHARGE = 'MIMIC-IV-Note prior discharge'


def _fact(case_id, topic, text, source_kind, source_refs):
    return {'fact_id': 'med-' + digest([case_id, topic, text])[:24], 'topic': topic,
            'value': text, 'status': 'reported', 'source_kind': source_kind,
            'source_refs': source_refs}


def _home_medications(case_id, chart, released_workup):
    entries = [row for row in chart.get('current_use_record', {}).get('items', [])
               if row.get('source_kind') == MEDRECON]
    entries.extend(row for row in released_workup
                   if row.get('kind') == 'home_medication_history' and row.get('source_kind') == 'ed.medrecon')
    names = {}
    for row in entries:
        name = ' '.join((row.get('name') or row.get('display') or '').split())
        if name:
            names.setdefault(name.casefold(), (name, row.get('source_ref')))
    return [_fact(case_id, HOME_MEDICATION, f'我来急诊前的用药清单里有 {name}。', MEDRECON, [ref] if ref else [])
            for name, ref in names.values()]


def _prior_medications(case_id, chart):
    encounters = [row for row in chart.get('historical_encounters', [])
                  if type(row.get('discharge_offset_hours')) in (int, float) and row['discharge_offset_hours'] < 0]
    if not encounters:
        return []
    latest = max(encounters, key=lambda row: row['discharge_offset_hours'])
    if latest['discharge_offset_hours'] < -RECALL_WINDOW_HOURS:
        return []
    rows = [row for row in chart.get('historical_observations', [])
            if row.get('source_encounter_ref') == latest['encounter_ref']
            and row.get('name') == 'Prior discharge medication list'
            and row.get('source_kind') == PRIOR_DISCHARGE
            and isinstance(row.get('value_raw'), str) and row['value_raw'].strip()
            and type(row.get('time', {}).get('recorded_offset_hours')) in (int, float)
            and row['time']['recorded_offset_hours'] <= 0]
    if not rows:
        return []
    recorded = max(row['time']['recorded_offset_hours'] for row in rows)
    sections = [row for row in rows if row['time']['recorded_offset_hours'] == recorded]
    text = ''.join(row['value_raw'] for row in sections)
    parts = re.split(r'(?m)^[ \t]*\d+[.)][ \t]+', text)
    items = parts[1:] if len(parts) > 1 else parts
    refs = [row['source_ref'] for row in sections if row.get('source_ref')]
    return [_fact(case_id, PRIOR_MEDICATION, '我上次出院药单上写的是 ' + ' '.join(item.split()), PRIOR_DISCHARGE, refs)
            for item in dict.fromkeys(items) if any(char.isalpha() for char in item)]


def with_medication_memory(case_id, memory, chart, *, released_workup=()):
    facts = {fact['fact_id']: fact for fact in memory['patient_facts']}
    for fact in _home_medications(case_id, chart, released_workup) + _prior_medications(case_id, chart):
        facts.setdefault(fact['fact_id'], fact)
    return {**memory, 'patient_facts': list(facts.values())}
