"""ED artifact measures and paired comparisons using the retained encounter records."""
from collections import Counter, defaultdict
from itertools import combinations
from statistics import fmean

from results.summarize_paid_runs import heldout_effort_plan, matched_effort_interval
from sandbox.experiments.linked import scenario_design
from sandbox.experiments.linked.contracts import digest


DIAGNOSIS_GROUPS = {
    'ICD-9-CM': {'428': 'heart_failure', '486': 'pneumonia', '5990': 'uti'},
    'ICD-10-CM': {'E11': 'type2_diabetes', 'I50': 'heart_failure', 'J44': 'copd',
                  'J18': 'pneumonia', 'N390': 'uti'},
}
CODE_FAMILIES = {'428', 'E11', 'I50', 'J44', 'J18'}
ELAPSED_METRICS = (
    'registration_to_consult_seconds', 'consultation_to_candidate_elapsed_seconds',
    'prescription_resolution_elapsed_seconds', 'documentation_elapsed_seconds',
    'draft_to_lock_seconds', 'registration_to_signed_note_seconds',
)
STAFF_WORK_METRICS = (
    'physician_actions', 'pharmacist_actions', 'patient_answers_total',
    'modeled_staff_seconds', 'modeled_physician_seconds', 'modeled_pharmacist_seconds',
    'modeled_patient_seconds', 'modeled_report_reading_seconds',
    'modeled_prescribing_seconds', 'modeled_documentation_seconds', 'api_wall_seconds',
)
ARTIFACT_RATES = (
    'initial_diagnosis_group_match', 'draft_signed_unchanged',
    'first_submission_exact', 'first_signature_exact',
    'draft_note_medication_agreement', 'draft_note_disposition_agreement',
    'signed_note_medication_agreement', 'signed_note_disposition_agreement',
)
ACTIVITY_TYPES = {
    'report_reading': 'report_reading', 'prescription_review': 'pharmacist_review',
    'prescription_revision': 'physician_prescription_revision',
    'brief_documentation': 'physician_summary', 'note_supplementation': 'physician_summary',
    'documentation': 'note_production',
}
SENSITIVITY_FRACTION = .5


def diagnosis_group(code, system):
    if not isinstance(code, str) or system not in DIAGNOSIS_GROUPS:
        return None
    code = ''.join(c for c in code.upper() if c.isalnum())
    return next((group for pattern, group in DIAGNOSIS_GROUPS[system].items()
                 if code == pattern or pattern in CODE_FAMILIES and code.startswith(pattern)), None)


def first_submission(run):
    event = scenario_design.first_review_submission(run)
    if event is None:
        return None, None, {}
    calls = run.get('calls', [])
    # The first pharmacist request carries the exact submission-time physician transcript.
    review = next((c for c in calls if c.get('role') == 'pharmacist'
                   and c.get('observation', {}).get('action') == 'review_prescription'), None)
    observation = review['observation'] if review else {}
    candidate = observation.get('prescription') or run.get('artifacts', {}).get('original_prescription')
    if not candidate or digest(candidate) != event.get('prescription_hash'):
        return None, None, {}
    return event, candidate, observation


def first_treatment_dialogue(run):
    """Use the doctor's first position, including a decision with no medication action."""
    for event in run.get('events', []):
        if event.get('event') == 'consultation_completed' and 'physician_dialogue_segments' in event:
            transcript = [s for s in run.get('artifacts', {}).get('transcript', []) if s.get('phase') == 'consultation']
            return transcript[:event['physician_dialogue_segments']]
    signed = run.get('artifacts', {}).get('ed_signed_order_history', [])
    initial = next((e for e in run.get('events', []) if e['event'] == 'ed_initial_order_confirmed'), {})
    sequence = initial.get('physician_call') or (signed[0].get('physician_call') if signed else None)
    call = next((c for c in run.get('calls', []) if c['sequence'] == sequence and c['role'] == 'doctor'), {})
    observation = call.get('observation') or first_submission(run)[2]
    return observation.get('clinical_context', observation).get('conversation')


def note_comparisons(run, stages):
    if not (stages.get('locked_ed_note_with_disposition') and stages.get('reviewed_signed_prescription')):
        return {}
    from sandbox.experiments.linked.emergency import note_order_disposition_check

    artifacts = run['artifacts']
    codes = {item['drug_code'] for item in artifacts['final_prescription']['items']}
    disposition = artifacts['ed_disposition']['code']
    versions = artifacts.get('note_versions') or []
    first = (versions[0].get('content') or {}).get('text') if versions else None
    signed = artifacts['attested_note']['content']['text']
    return {name: note_order_disposition_check(text, codes, disposition)
            for name, text in [('draft', first), ('signed', signed)] if text}


def artifact_measures(run, stages, reference, group, *, note_checks=None):
    artifacts = run.get('artifacts', {})
    attempts = artifacts.get('prescribing_attempts', [])
    result = dict.fromkeys((*ARTIFACT_RATES, 'physician_turns_before_first_decision', 'physician_turns_after_report'))
    source_code = (reference or {}).get('ed_primary_icd')
    if source_code and attempts and isinstance(attempts[0].get('assessment'), dict):
        source_group = diagnosis_group(source_code['code'], 'ICD-' + str(source_code['icd_version']) + '-CM')
        if source_group != group:
            raise ValueError('The held-out ED diagnosis and enrollment stratum differ.')
        diagnoses = attempts[0]['assessment'].get('diagnoses')
        primary = [d for d in diagnoses if isinstance(d, dict) and d.get('primary') is True] if isinstance(diagnoses, list) else []
        result['initial_diagnosis_group_match'] = (
            len(primary) == 1 and diagnosis_group(primary[0].get('code'), primary[0].get('system')) == source_group)

    conversation = first_treatment_dialogue(run)
    if isinstance(conversation, list):
        conversation = [s for s in conversation if s.get('phase') == 'consultation']
        doctors = [s.get('speaker_role') == 'doctor' for s in conversation]
        result['physician_turns_before_first_decision'] = sum(doctors)
        read = next((e for e in run.get('events', []) if e['event'] == 'preconsult_report_read'), {})
        before = read.get('physician_dialogue_segments')
        if type(before) is int and 0 <= before <= len(conversation):
            result['physician_turns_after_report'] = sum(doctors[before:])

    versions = artifacts.get('note_versions') or []
    first = (versions[0].get('content') or {}).get('text') if versions else None
    signed = ((artifacts.get('attested_note') or {}).get('content') or {}).get('text')
    if stages.get('locked_ed_note_with_disposition') and first and signed:
        result['draft_signed_unchanged'] = first.split() == signed.split()
    if note_checks is None:
        note_checks = note_comparisons(run, stages)
    for stage, check in note_checks.items():
        for field, status in check.items():
            result[f'{stage}_note_{field}_agreement'] = (
                None if status == 'unrecorded' and not run.get('ed_note_reconciliation_policy')
                else status == 'match')
    return result


def _sent_record_refs(value):
    """Identify actual chart records inside a physician observation."""
    if isinstance(value, dict):
        if isinstance(ref := value.get('record_ref'), str) and isinstance(value.get('record'), dict):
            yield ref
        for item in value.values():
            yield from _sent_record_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _sent_record_refs(item)


def prescribing_evidence(run, physician_call):
    """Bind the released chart and advisory payload to the response that was signed."""
    calls = {call['sequence']: call for call in run['calls'] if call['sequence'] <= physician_call}
    signed = calls[physician_call]
    boundary = next(event['sequence'] for event in run['events']
                    if event['event'] == 'role_call' and event['call_sequence'] == physician_call)
    available, delivered, read = set(), set(), set()
    inventories, receipts, chart_searches, formulary_searches = [], [], [], []
    on_demand = bool(run.get('artifacts', {}).get('physician_opening_ips'))
    complete_inventory = not on_demand
    physician_observations = [call['observation'] for call in calls.values() if call['role'] == 'doctor'
                              and ('response' in call or (call.get('response_parse') or {}).get('finish_reason'))]
    sent_refs = {ref for observation in physician_observations for ref in _sent_record_refs(observation)}
    visible_codes = {product['drug_code'] for observation in physician_observations
                     for product in observation.get('formulary', [])}

    for event in run['events']:
        if event['sequence'] >= boundary:
            break
        kind = event['event']
        if kind == 'opening_ips_read':
            call = calls.get(event['physician_call'])
            if (call and call['role'] == 'doctor' and call.get('response', {}).get('action') == 'report_read'
                    and digest(call['observation'].get('opening_summary')) == event['summary_hash']):
                ref = ('opening_ips', event['summary_hash'])
                delivered.add(ref)
                read.add(ref)
                receipts.append({'event_sequence': event['sequence'], 'physician_call': call['sequence'],
                                 'source': 'ips'})
        elif on_demand and kind == 'history_lookup' and event.get('request', {}).get('term'):
            returned = set(event.get('returned_refs', []))
            shown = returned & sent_refs
            delivered.update((event['section'], ref) for ref in shown)
            chart_searches.append({'event_sequence': event['sequence'], 'section': event['section'],
                                   'term': event['request']['term'], 'status': event['result_status'],
                                   'returned_refs': sorted(returned), 'delivered_refs': sorted(shown),
                                   'returned_without_delivery': sorted(returned - shown),
                                   'accepted_into_context': event.get('accepted_into_context', False)})
        elif kind == 'formulary_searched' and event.get('role') == 'doctor':
            formulary_searches.extend({'query': query, 'returned_product_codes': codes}
                                      for query, codes in zip(event['queries'], event['returned'], strict=True))
        elif kind == 'ed_source_inventory_requested':
            inventories.append(event['sequence'])
            sources = event.get('sources')
            if sources is None:
                complete_inventory = False
            else:
                available.update((section, ref) for section, refs in sources.items() for ref in refs)
        elif kind in {'ed_chart_page_prepared', 'ed_chart_page_reviewed', 'preconsult_report_read'}:
            reviewed = kind != 'ed_chart_page_prepared'
            call = calls.get(event.get('physician_call') if reviewed else event['first_call'])
            if not call or call['role'] != 'doctor':
                continue
            observed = call['observation']
            if kind == 'preconsult_report_read':
                key, expected = 'handoff', event['report_hash']
                refs = {('intake_summary', expected)}
            else:
                key, expected = 'tables', event['content_hash']
                refs = {(part['section'], ref) for part in event['sources'] for ref in part['refs']}
            response_received = ('response' in call or (call.get('response_parse') or {}).get('finish_reason'))
            if key not in observed or digest(observed[key]) != expected or not response_received:
                continue
            delivered.update(refs)
            if reviewed and event.get('evidence_quote') and event.get('evidence_source_call') in calls:
                read.update(refs)
                receipts.append({'event_sequence': event['sequence'], 'physician_call': call['sequence'],
                                 'evidence_source_call': event['evidence_source_call']})
    return {
        'inventory_events': inventories,
        'reading_receipts': receipts,
        **({'chart_searches': chart_searches} if on_demand else {}),
        'available_rows': len(available) if complete_inventory and inventories else None,
        'delivered_rows': len(delivered),
        'read_rows': len(read),
        'available_without_delivery': sorted(available - delivered) if complete_inventory and inventories else None,
        'delivered_without_receipt': sorted(delivered - read),
        'visible_product_codes': sorted(visible_codes),
        'formulary_searches': formulary_searches,
        'advisory_prescription': signed['observation'].get('medication_advice'),
        'advisory_reason': signed['observation'].get('current_advice_reason'),
        'advisory_outstanding_checks': signed['observation'].get('current_advice_outstanding_checks'),
        'presigning_findings': signed['response'].get('check_results'),
    }


def sensitivity_components(run):
    parts = Counter()
    for entry in run.get('activities', []):
        name = ACTIVITY_TYPES.get(entry['activity_type'])
        if name and entry['role'] in ('doctor', 'pharmacist'):
            parts[name] += entry['active_seconds']
    return dict(parts)


def paired_contrasts(rows, settings, *, with_reference):
    """Match within patient and other task conditions, then weight source patients equally."""
    plan = heldout_effort_plan()
    groups = {row['case_id']: row['group'] for row in rows}
    outputs = []
    for task, workflows in scenario_design.WORKFLOWS.items():
        blocks = defaultdict(dict)
        for row in rows:
            conditions = settings[row['config_id']]
            other = tuple(conditions[t]['workflow'] for t in scenario_design.WORKFLOWS if t != task)
            measures = {**row.get('workflow_measures', {}), **row.get('artifact_measures', {}),
                        'verified_closure': row['status'] == 'completed',
                        'closure_with_exact_regimen': bool(row.get('exact_drug_set')) if with_reference else None,
                        'signed_course_exact': row.get('exact_drug_set'),
                        'final_primary_state_agreement': row.get('final_primary_state_agreement'),
                        'order_trajectory_agreement': row.get('order_trajectory_agreement')}
            blocks[row['case_id'], other][conditions[task]['workflow']] = measures, row.get('staff_activity_seconds', {})
        metrics = ('physician_actions', 'pharmacist_actions', 'patient_answers_total', 'modeled_staff_seconds',
                   *ELAPSED_METRICS, 'verified_closure', 'closure_with_exact_regimen')
        task_metrics = {
                'preconsult': ('initial_diagnosis_group_match', 'physician_turns_before_first_decision',
                               'physician_turns_after_report'),
                'prescription': ('first_signature_exact', 'signed_course_exact', 'final_primary_state_agreement',
                                 'order_trajectory_agreement', 'first_pharmacist_return'),
                'documentation': ('draft_signed_unchanged', 'note_revision_count', 'signed_note_medication_agreement',
                                  'signed_note_disposition_agreement'),
        }
        for comparator, condition in combinations(workflows, 2):
            pairs = [(case, block[comparator], block[condition]) for (case, _), block in blocks.items()
                     if comparator in block and condition in block]
            if not pairs:
                continue
            estimates = {}
            for metric in (*metrics, *task_metrics[task]):
                differences = defaultdict(list)
                components = defaultdict(list)
                for case, (before, before_parts), (after, after_parts) in pairs:
                    a, b = before.get(metric), after.get(metric)
                    if a is None or b is None:
                        continue
                    differences[case].append(b - a)
                    if metric == 'modeled_staff_seconds':
                        components[case].append({k: after_parts.get(k, 0) - before_parts.get(k, 0)
                                                for k in set(ACTIVITY_TYPES.values())})
                means = {case: fmean(values) for case, values in differences.items()}
                estimate = fmean(means.values()) if means else None
                matched = sum(map(len, differences.values()))
                interval = matched_effort_interval(means, groups, plan)
                estimates[metric] = {'mean_difference': estimate, 'eligible_patients': len(means),
                                     'matched_blocks': matched, 'missing_blocks': len(pairs) - matched,
                                     **interval}
                if components:
                    mean_components = {k: fmean(fmean(v[k] for v in values) for values in components.values())
                                       for k in sorted(set(ACTIVITY_TYPES.values()))}
                    margin = SENSITIVITY_FRACTION * sum(abs(v) for v in mean_components.values())
                    low, high = estimate - margin, estimate + margin
                    estimates[metric]['weight_sensitivity'] = {
                        'multipliers': [1 - SENSITIVITY_FRACTION, 1 + SENSITIVITY_FRACTION],
                        'mean_difference_range_seconds': [low, high],
                        'direction_reverses': low < 0 < high}
            outputs.append({'task': task, 'comparator': comparator, 'condition': condition, 'planned_blocks': len(pairs),
                            'metrics': estimates})
    return {'bootstrap': plan['bootstrap'], 'contrasts': outputs}
