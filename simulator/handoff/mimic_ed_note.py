"""Project source-note facts into the ED intake and prescribing views."""
import re

from .ed_workup import source_ref


NOTE_SOURCE = 'MIMIC-IV-Note admission history, retrospectively documented'
SECTION = re.compile(
    r'(?im)^[ \t]*(history of present illness|past medical history|social history|family history|'
    r'physical exam(?:ination)?|admission (?:physical )?exam|on admission|'
    r'discharge (?:physical )?exam|pertinent results|brief hospital course|hospital course|'
    r'medications on admission|discharge medications|discharge disposition|discharge diagnosis|'
    r'discharge condition|discharge instructions|follow[ -]?up instructions|allergies|'
    r'chief complaint|major surgical or invasive procedure)[ \t]*(?::|(?=\n[= -]*\n))'
)
ED_ACTIVITY = re.compile(
    r'(?im)^\s*(?:[-*#]\s*)?(?:initial (?:ED |ER )?vitals|triage vitals|vitals in (?:the )?(?:ED|ER)|'
    r'(?:ED|ER|emergency department) course|received|medications? given|meds given)\b|'
    r'\b(?:ED|ER|emergency department) course\b|'
    r'\b(?:in|at) (?:the )?(?:ED|ER|emergency department)\b|'
    r'\b(?:on|upon) (?:ED |ER )?arrival\b|'
    r'\b(?:in|on) the (?:intensive care unit|floor|ward)\b'
)
POST_ED = re.compile(
    r'(?im)^[ \t]*(?:[-=]+\s*)?(?:(?:on|at)\s+)?'
    r'(?:discharge|discharge (?:physical )?exam|post[ -]?discharge|hospital day [0-9]+|day of discharge)'
    r'(?=[ \t]*[:=\n]|\s*\Z)'
)
TREATMENT = re.compile(
    r'(?i)\b(?:patient|she|he|pt\.?) (?:was |has been |is being )?(?:given|administered|prescribed|'
    r'received|discharged on|treated with|started on|transferred to|admitted for)\b|'
    r'\b(?:given|received)\s*(?=[:0-9]|IV\b)|\bwas started on\b|'
    r'(?im:^[ \t]*(?:[-*]\s*)?(?:treatment|interventions?|medications? given|meds given|received|'
    r'ED medications|(?:ED|ER) course)\s*:)'
)
PRIOR = re.compile(
    r'(?i)\b(?:in the past|(?:day|week|month|year)s? ago|last (?:week|month|year)|'
    r'recent(?:ly)?|prior to (?:presentation|arrival)|previous(?:ly)?|outpatient|'
    r'primary care|PCP|home|ambulance|before arrival|after discharge|'
    r'yesterday|at rehab|at urgent care)\b'
)
CURRENT_OUTCOME = re.compile(
    r'(?i)\b(?:(?:is\s+being|was|is)\s+admitted\s+(?:here|for|to\s+(?:the\s+)?(?:oncology|medicine|ICU))|'
    r'admitted\s+with\s+new|was\s+admitted\s+from\s+our\s+ED|'
    r'admitted\s+for\s+(?:management\s+of|HHS|DKA|UTI))\b'
)
ECG = re.compile(r'(?i)\b(?:12[- ]lead\s+)?(?:EKG|ECG|electrocardiogram)\b')
EKG_RESULT = re.compile(
    r'(?i)\b(?:sinus|NSR|paced|tachycardi\w*|bradycardi\w*|atrial|AF(?:ib)?|ventricular|'
    r'axis|ischemi\w*|RBBB|LBBB|LAFB|LVH|ST[- ](?:T|segment|changes|elevat\w*|depress\w*)|'
    r'peaked|QTc?|intervals|QRS|poor R|normal|(?:without|no)\s+(?:any\s+)?changes)\b'
)
NEXT_FINDING = re.compile(
    r'(?im)^[ \t]*(?:[-+*]\s*)?(?:CXR|chest (?:x[ -]?ray|radiograph)|CT(?:A)?\b|'
    r'MRI|ultrasound|echo(?:cardiogram)?\b|labs?|meds?|vitals?|imaging|microbiology|'
    r'admission|discharge|clinical course)(?:\b|\s*:)'
)
OXYGEN = re.compile(
    r'(?i)\b(?:(?:O2\s*(?:sat(?:uration)?|delivery)|s[ap]o2|sat(?:uration)?)\s*[:=]?\s*)?'
    r'\d{2,3}\s*%\s*(?:(?:on|with)\s+)?'
    r'(?:room air\b|RA\b|\d+(?:\.\d+)?\s*(?:L(?:/min|PM)?|lit(?:ers|res))\s*'
    r'(?:NC\b|nasal cannula\b|O2\b|oxygen\b)?)'
)
PEAK_FLOW = re.compile(
    r'(?i)\bpeak (?:expiratory )?flows?(?:\s+rate)?\s*(?:of|was|were|:)?\s*'
    r'\d{2,3}(?:[ \t]*(?:->|→|to|/)[ \t]*\d{2,3})?(?:[ \t]*L/min)?'
)


def spans(text):
    markers = list(SECTION.finditer(text))
    return [(mark[1].casefold(), mark.end(),
             markers[n + 1].start() if n + 1 < len(markers) else len(text))
            for n, mark in enumerate(markers)]


def strip_span(text, start, stop):
    block = text[start:stop]
    return start + len(block) - len(block.lstrip()), start + len(block.rstrip())


def sentences(text, start, stop):
    next_sentence = re.compile(r'(?<=[.!?])\s+(?=[A-Z_])|\n[ \t]*\n+')
    for gap in next_sentence.finditer(text, start, stop):
        begin, end = strip_span(text, start, gap.start())
        if begin < end:
            yield begin, end
        start = gap.end()
    begin, end = strip_span(text, start, stop)
    if begin < end:
        yield begin, end


def original_note_findings(text):
    """Select original history and admission findings, retaining source offsets."""
    found = []
    for section, start, stop in spans(text):
        if section == 'history of present illness':
            ed = ED_ACTIVITY.search(text, start, stop)
            if ed:
                visit = text[ed.start():stop]
                # Symptoms explicitly reported on arrival remain patient facts.
                reported = re.match(
                    r'(?is)[^.\n]{0,125}\b(?:report(?:s|ed)|endors(?:es|ed)|complain(?:s|ed)|'
                    r'deni(?:ed|es))\b[^.\n]+', visit)
                if reported and not TREATMENT.search(reported[0]):
                    found.append((section, ed.start(), ed.start() + reported.end(), 'patient'))
            for left, right in sentences(text, start, ed.start() if ed else stop):
                body = text[left:right]
                if not body.strip(' =_\n.\t') or POST_ED.search(body):
                    continue
                outcome = CURRENT_OUTCOME.search(body)
                if outcome:
                    left, right = strip_span(text, left, left + outcome.start())
                    body = text[left:right].rstrip(' ,;')
                    right = left + len(body)
                if (body.strip(' .').casefold() in ('', 'he', 'she', 'the patient', 'patient')
                        or (TREATMENT.search(body) and not PRIOR.search(body))):
                    continue
                # Earlier treatment describes the clinical chart; patient medication recall retains its own policy.
                chart_fact = TREATMENT.search(body) or ECG.search(body)
                found.append((section, left, right, 'prior_history' if chart_fact else 'patient'))
        elif section in ('past medical history', 'social history', 'family history'):
            left, right = strip_span(text, start, stop)
            if text[left:right].strip(' =_\n.\t') and not POST_ED.search(text, start, stop):
                found.append((section, left, right, 'prior_history'))
        elif section.startswith(('physical exam', 'admission exam', 'admission physical exam')) or section == 'on admission':
            later = POST_ED.search(text, start, stop)
            limit = later.start() if later else stop
            left, right = strip_span(text, start, limit)
            if text[left:right].strip(' =_\n.\t') and not TREATMENT.search(text, start, limit):
                found.append((section, left, right, 'admission_exam'))
        if section not in ('history of present illness', 'pertinent results', 'physical exam', 'physical examination',
                            'admission exam', 'admission physical exam', 'on admission'):
            continue
        later = POST_ED.search(text, start, stop)
        if later:
            stop = later.start()
        for test, kind in ((OXYGEN, 'oxygen_context'), (PEAK_FLOW, 'peak_flow')):
            for match in test.finditer(text, start, stop):
                found.append((section, match.start(), match.end(), kind))
        for match in ECG.finditer(text, start, stop):
            previous = re.split(r'[.]\s+|\n\s*\n', text[max(start, match.start() - 36):match.start()])[-1]
            if re.search(r'(?i)\b(?:prior|previous|stress|dobutamine)\b', previous):
                continue
            section_end = NEXT_FINDING.search(text, match.end(), stop)
            limit = min(stop, match.end() + 540, section_end.start() if section_end else stop)
            body = text[match.start():limit]
            following = re.search(r'(?im)(?:\n\s*\n|\n[ \t]*[-+*]|'
                                  r'\b(?:received|treated with|given|started on|was admitted|discharged|was transferred)\b)', body)
            end = match.start() + following.start() if following else limit
            if section == 'history of present illness':
                sentence_end = re.search(r'\.(?=\s+[A-Z_])', text[match.end():end])
                if sentence_end:
                    end = match.end() + sentence_end.end()
            spelling = text[match.start():end]
            if EKG_RESULT.search(spelling) and not TREATMENT.search(spelling):
                found.append((section, match.start(), end, 'ecg'))
    seen = set()
    for section, start, end, kind in sorted(found, key=lambda row: row[1]):
        body = ' '.join(text[start:end].split())
        if not body or (kind, body) in seen:
            continue
        seen.add((kind, body))
        yield section, start, end, kind


def attach_index_note(case, note):
    chart = case['his_initial']
    basis = chart['time_basis']
    basis['ed_review_order'] = 'source_table'
    (decision,) = basis['ed_order_checkpoints']
    cutoff = decision['release_before_seconds']
    facts = case['patient_initial']['patient_facts']
    early = chart['ed_workup']
    late = chart['ed_reassessment_workup']
    original = note['text']
    provenance = []
    labels = {
        'prior_history': '既往资料',
        'admission_exam': '入院查体',
        'oxygen_context': '血氧测量条件',
        'peak_flow': '峰流量',
        'ecg': '心电图',
    }
    for section, start, stop, kind in original_note_findings(original):
        quote = original[start:stop]
        value = ' '.join(quote.split())
        ref = source_ref(case['case_id'], 'index_admission_note', f"{note['note_id']}/{start}/{stop}")
        source = {'source_ref': ref, 'note_id': note['note_id'], 'section': section,
                  'start': start, 'end': stop, 'verbatim': quote, 'kind': kind}
        row = {'kind': 'ed_collateral_history' if kind in ('patient','prior_history') else 'ed_diagnostic_finding',
               'display': value if kind == 'patient' else labels[kind] + ' ' + value,
               'status': 'documented', 'source_kind': NOTE_SOURCE, 'source_ref': ref}
        if kind in ('patient', 'prior_history'):
            row['time'] = {'basis': 'History predating arrival; exact time is not recorded.',
                           'event_time_known': False}
            early.append(row)
            delivery = 'ed_workup'
            if kind == 'patient':
                facts.append({'fact_id': 'fact-' + ref[4:], 'topic': 'present_illness',
                              'field': 'symptom_history', 'value': value, 'status': 'reported',
                              'source_kind': NOTE_SOURCE, 'source_refs': [ref]})
        else:
            row['time'] = {'replay_release_at_seconds': cutoff}
            late.append(row)
            delivery = 'ed_reassessment_workup'
        provenance.append({**source, 'delivery_section': delivery})
    return provenance
