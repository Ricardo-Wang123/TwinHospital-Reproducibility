"""Build patient replies and intake reports from facts selected by the roles."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import fcntl
from . import ed_allergy_review as allergies

SHARED_REPORT_POLICY = 'frozen_patient_intake_report_v1'


def shared_report(directory, identity, run_id, collect, *, resume_collection=False):
    """Freeze one report and its elicitation dialogue for the assisted conditions."""
    from .contracts import digest

    folder = Path(directory)
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = digest(identity)
    path = folder / (key + '.json')
    with (folder / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if saved is not None and (saved['identity'] != identity
                or saved['report_hash'] != digest(saved['report'])
                or saved['dialogue_hash'] != digest(saved['dialogue'])):
            raise ValueError('Frozen intake report differs from its case or content hash')
        if saved is None or resume_collection:
            report, dialogue = collect()
            if saved is None:
                saved = {'policy': SHARED_REPORT_POLICY, 'identity': identity, 'source_run_id': run_id,
                         'report': report, 'report_hash': digest(report),
                         'dialogue': dialogue, 'dialogue_hash': digest(dialogue)}
                with NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as temp:
                    json.dump(saved, temp, ensure_ascii=False, allow_nan=False)
                os.replace(temp.name, path)
            elif saved['source_run_id'] == run_id and saved['report'] != report:
                raise ValueError('Replayed intake collection differs from the frozen report')
    return saved


def _select(facts, identifiers):
    if not isinstance(identifiers, list):
        return []
    return [facts[key] for key in dict.fromkeys(key for key in identifiers if isinstance(key, str))
            if key in facts]


def _fact_text(fact):
    value = fact['value']
    if isinstance(value, str):
        return value.strip()
    return f"{fact['topic']} {json.dumps(value, ensure_ascii=False)}"


def patient_answer(observation, response):
    memory = {fact['fact_id']: fact for fact in observation['patient_memory']['patient_facts']}
    identifiers = response.get('expressed_fact_ids')
    selected = list(memory.values()) if observation.get('complete_patient_account') else _select(memory, identifiers)
    allergy_mode = observation.get('allergy_dialogue')
    allergy_fact = next((fact for fact in memory.values() if fact.get('topic') == allergies.PATIENT_TOPIC), None)
    if allergy_mode in {'question', 'reaction', 'volunteer'} and allergy_fact:
        if allergy_mode in {'reaction', 'volunteer'}:
            selected = [allergy_fact]
        elif allergy_fact not in selected:
            selected.append(allergy_fact)
    facts = []
    for fact in selected:
        value = fact.get('value')
        status = fact.get('status', 'reported')
        if status == 'unknown' or value is None or (isinstance(value, str) and not value.strip()):
            status, value = 'unknown', None
        if allergy_mode and fact is allergy_fact:
            value = allergies.patient_statement(fact, allergy_mode)
        facts.append({'fact_id': fact['fact_id'], 'topic': fact.get('topic') or fact.get('field') or fact['fact_id'],
                      'value': value, 'status': status})

    unsupported = isinstance(identifiers, list) and any(
        not isinstance(key, str) or key not in memory for key in identifiers)
    prompted_allergy = allergy_fact and (allergy_mode in {'reaction', 'volunteer'}
        or (allergy_mode == 'question' and not allergies.MEDICATION_HISTORY_QUESTION.search(observation['question'])))
    unanswered = ((response.get('unanswered') is True and not prompted_allergy) or not facts or unsupported
                  or any(fact['status'] == 'unknown' for fact in facts))
    sentences = [_fact_text(fact) for fact in facts if fact['status'] != 'unknown']
    if unanswered:
        sentences.append(observation.get('source_unavailable_reply') or '这部分我不清楚。')
    text = ' '.join(sentence if sentence.endswith(('.', '。', '!', '！', '?', '？'))
                    else sentence + '。' for sentence in sentences)
    return {'action': 'answer', 'text': text, 'expressed_fact_ids': [fact['fact_id'] for fact in facts],
            'source_facts': facts, 'unanswered': unanswered}


def intake_summary(observation, response):
    available = {fact['fact_id']: fact for fact in observation['elicited_facts']}
    identifiers = response.get('fact_ids')
    facts = _select(available, identifiers if isinstance(identifiers, list) else list(available))
    if observation.get('carry_allergy_history'):
        facts.extend(fact for fact in available.values()
                     if fact.get('topic') == allergies.PATIENT_TOPIC and fact not in facts)
    selected = {fact['fact_id']: fact for fact in facts}
    risk_flags = [_fact_text(fact) for fact in _select(selected, response.get('risk_fact_ids'))
                  if fact['status'] != 'unknown']
    unresolved = observation['unanswered_questions']

    demographics = observation.get('demographics', {})
    age, sex = demographics.get('age'), demographics.get('sex')
    lines = [f'{label} {value}' for label, value in [('年龄', age), ('性别', sex)]
             if value not in (None, '')]
    reasons = observation.get('triage_record', {}).get('recorded_reasons', [])
    if reasons:
        lines.append('分诊记录为' + '、'.join(reasons) + '。')
    statements = [_fact_text(fact) for fact in facts if fact['status'] != 'unknown']
    if statements:
        lines.extend(['患者口述', *statements])
    if unresolved or any(fact['status'] == 'unknown' for fact in facts) or not statements:
        lines.append('未从患者口述确认的情况留待核实。')
    content = {'text': '\n'.join(lines), 'facts': facts, 'risk_flags': risk_flags, 'unanswered_questions': unresolved}
    if observation.get('artifact_origin'):
        content['origin'] = observation['artifact_origin']
    return {'action': 'summary', 'content': content}
