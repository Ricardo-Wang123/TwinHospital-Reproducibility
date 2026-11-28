"""Evaluate recorded ED outcomes after the clinical encounter has closed."""
from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import median, quantiles

from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.ed_order_actions import INDICATION_POLICY, indication_scope
from sandbox.experiments.linked.ed_treatment_review import PROTOCOL as PRIMARY_DECISION_PROTOCOL
from sandbox.experiments.linked.emergency import DESIGN, observed_clinical_stages, verified_closure
from sandbox.experiments.linked import scenario_design
from sandbox.experiments.linked.linked_acceptance import BASELINE_LABEL, attempt_label
from sandbox.experiments.linked.contracts import linked_config_id
from sandbox.experiments.linked.workflow_events import trace_counts
from . import ed_metrics, order_actions


def read(path):
    return Path(path).read_text(encoding='utf-8')


# Naming equivalences for the retrospective medication-name endpoint.
LABELS={
    'ipratropium+albuterol':'albuterol-ipratropium',
    'fluticasone propionate+salmeterol':'fluticasone-salmeterol',
    'trimethoprim+sulfamethoxazole':'sulfamethoxazole-trimethoprim',
    'sulfameth/trimethoprim ds':'sulfamethoxazole-trimethoprim', 'salbutamol':'albuterol', 'albuterol sulfate':'albuterol', 'humalog':'insulin lispro','lantus':'insulin glargine',
    'insulin (regular) for hyperkalemia':'insulin human regular',
    'heparin sodium':'heparin','heparin (hemodialysis)':'heparin',
    'ipratropium-albuterol':'albuterol-ipratropium',
    'ipratropium bromide':'ipratropium','ciprofloxacin hcl':'ciprofloxacin',
    'metoprolol xl (toprol xl)':'metoprolol','metoprolol succinate':'metoprolol','metoprolol tartrate':'metoprolol',
    'hydromorphone (dilaudid)':'hydromorphone','metronidazole (flagyl)':'metronidazole','tramadol (ultram)':'tramadol',
    'nitrofurantoin monohyd (macrobid)':'nitrofurantoin'}


@lru_cache(maxsize=32768)
def drug_name(value):
    name=' '.join(value.strip().casefold().replace('_',' ').split())
    # Strip single dose/route markings only when attached to known named products.
    fixed=[
        (r'^(?:albuterol-ipratropium|ipratropium-albuterol)(?: neb)?$', 'albuterol-ipratropium'),
        (r'^heparin flush(?: \([^()]*\))?$', 'heparin'),
        (r'^lidocaine (?:viscous |jelly )?\d+(?:\.\d+)?%(?: patch| \(urojet\))?$', 'lidocaine'),
        (r'^fluticasone-salmeterol(?: diskus)? \(\d+/\d+\)$','fluticasone-salmeterol'),
        (r'^(hydrocodone-acetaminophen|buprenorphine-naloxone)(?: tablet)? \(\d+mg-\d+mg\)$',None)]
    for pattern,repl in fixed:
        found=re.fullmatch(pattern,name)
        if found:return repl or found[1]
    name=re.sub(r'\s+\((?:sustained|extended|delayed|immediate) release\)$','',name)
    name=re.sub(r'\s+(?:iv|po|neb soln|neb|inhaler|tablet|capsule|ec|er|odt|sl|xr|sr|xl)\s*$', '', name)
    name=re.sub(r'\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|%)\s*(?:tablet|capsule|vial|neb soln)?\s*$', '', name)
    return LABELS.get(name,name).strip()


PRIMARY_SCOPE = 'retrospective_ed_primary_condition_ingredient_reference'
LEGACY_SCOPE = 'encounter_drug_names'

# Used for native directory products that lack the structured ingredient field.
_NATIVE_INGREDIENTS = {
    'albuterol': ('proair hfa', 'ventolin hfa', 'albuterol inhaler (proair)'),
    'azithromycin': ('zithromax z-pak',),
    'doxycycline': ('doxycycline hyclate', 'doxycycline monohydrate'),
    'furosemide': ('lasix', 'furosemide (latex free)', 'furosemide-heart failure'),
    'glimepiride': ('amaryl',),
    'glipizide': ('glucotrol', 'glucotrol xl', 'glipizide ir'),
    'insulin glargine': ('lantus solostar', 'lantus u-100 insulin', 'toujeo solostar', 'insulin glargine (cvicu protocol)'),
    'insulin lispro': ('humalog insulin', 'humalog kwikpen', 'humalog u-100 insulin'),
    'insulin human regular': ('insulin (regular)', 'insulin (regular', 'insulin regular',
        'insulin - regular', 'insulin - insulin regular',
        'insulin human regular 100 units / 100 ml 0.9% sodium chloride',
        'humulin r', 'humulin-r insulin (u-500)', 'humulin r u-500 concentrated',
        'novolin r', 'novolin r regular u-100 insuln', 'insulin reg human (novolin r)',
        '*nf* insulin reg human (novolin r)', 'insulin regular human (u-500)',
        'insulin regular human (u-500) (for insulin pump)'),
    'ipratropium': ('ipratropium bromide mdi', 'atrovent hfa'),
    'lisinopril': ('zestril', 'prinivil'),
    'losartan': ('losartan potassium', 'cozaar'),
    'metformin': ('metformin (glucophage)', 'metformin xr (glucophage xr)', 'glucophage', 'glucophage xr'),
    'methylprednisolone': ('medrol', 'solu-medrol', 'methylprednisolone acetate', 'methylprednisolone sodium succ'),
    'metoprolol': ('toprol xl', 'lopressor', 'metoprolol tartrate suspension'),
    'nitrofurantoin': ('macrobid', 'macrodantin', 'nitrofurantoin (macrodantin)', 'nitrofurantoin macrocrystal',
        'nitrofurantoin macrocrystals', 'nitrofurantoin monohyd/m-cryst'),
    'prednisone': ('prednisone (self med)', 'prednisone intensol'),
    'vancomycin': ('vancocin', 'vancomycin oral liquid'),
}
_NATIVE_COMBINATIONS = {
    ('albuterol', 'ipratropium'): ('albuterol-ipratropium', 'ipratropium-albuterol inhalation spray',
        'combivent', 'combivent respimat', 'duoneb'),
    ('fluticasone propionate', 'salmeterol'): ('fluticasone-salmeterol', 'advair diskus', 'advair hfa'),
    ('sulfamethoxazole', 'trimethoprim'): ('sulfamethoxazole-trimethoprim', 'bactrim', 'bactrim ds'),
    ('insulin lispro', 'insulin lispro protamine'): ('insulin lispro 75/25', 'insulin lispro protam-lispro',
        'humalog mix 75-25', 'humalog mix 75-25 kwikpen'),
    ('insulin human regular', 'insulin human nph'): ('insulin human 70/30', 'novolin 70/30', 'humulin 70/30'),
    ('glyburide', 'metformin'): ('glyburide/metformin',),
    ('glipizide', 'metformin'): ('glipizide/metformin',),
    ('sitagliptin', 'metformin'): ('sitagliptin-metformin', 'janumet', 'janumet xr'),
    ('hydrochlorothiazide', 'losartan'): ('losartan-hydrochlorothiazide', 'losartan/hctz', 'hyzaar'),
    ('hydrochlorothiazide', 'lisinopril'): ('lisinopril-hydrochlorothiazide', 'lisinopril/hctz', 'zestoretic'),
    ('ciprofloxacin', 'dexamethasone'): ('ciprofloxacin-dexamethasone', 'ciprodex'),
}
_NATIVE_NAMES = {name: (ingredient,) for ingredient, names in _NATIVE_INGREDIENTS.items() for name in names}
_NATIVE_NAMES.update({name: ingredients for ingredients, names in _NATIVE_COMBINATIONS.items() for name in names})


@lru_cache(maxsize=32768)
def native_ingredients(label):
    raw = ' '.join(label.casefold().split())
    name = drug_name(raw)
    without_strength = re.sub(r'\s*\(\d[0-9./ %a-z-]*\)$', '', name)
    resolved = _NATIVE_NAMES.get(raw) or _NATIVE_NAMES.get(name) or _NATIVE_NAMES.get(without_strength)
    if not resolved:
        resolved = (name,)
    return frozenset(resolved)


def product_ingredients(product):
    declared = product.get('ingredient')
    if declared:
        return frozenset(drug_name(part) for part in declared.split('+'))
    return native_ingredients(product['name'])


def reference_scope(reference):
    scope = reference.get('scope', LEGACY_SCOPE)
    if scope not in (PRIMARY_SCOPE, LEGACY_SCOPE):
        raise ValueError(f'Unknown medication reference scope: {scope}')
    return scope


def primary_regimen(reference):
    groups = {}
    for k in ('continued', 'added'):
        values = reference[k]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f'Invalid {k} ingredient list')
        groups[k] = {drug_name(v) for v in values}
    if not (groups['continued'] or groups['added']) or groups['continued'] & groups['added']:
        raise ValueError('The primary regimen must be nonempty and its two groups must be disjoint')
    return groups


def common_scope(references):
    scopes = {reference_scope(ref) for ref in references}
    if len(scopes) != 1:
        raise ValueError('An evaluation batch must use one medication reference scope')
    return scopes.pop()


class MedicationIndex:
    def __init__(self, formulary, scope):
        self.scope = scope
        self.by_code = {}
        for product in formulary:
            code = product['drug_code']
            if code in self.by_code:
                raise ValueError(f'Duplicate formulary product code: {code}')
            names = (product_ingredients(product) if scope == PRIMARY_SCOPE else
                     frozenset((drug_name(product.get('ingredient') or product['name']),)))
            if not names or '' in names:
                raise ValueError(f'Missing medication identity for {code}')
            self.by_code[code] = names
        self.available = frozenset().union(*self.by_code.values())


def catalogue_coverage(references, formulary):
    scope = common_scope(references.values())
    idx = MedicationIndex(formulary, scope)
    names = [set.union(*primary_regimen(r).values()) if scope == PRIMARY_SCOPE else
             {drug_name(n) for n in r['drug_names']} for r in references.values()]
    union = set().union(*names)
    return {'scope': scope, 'method': 'reference ingredients representable in the frozen directory'
            if scope == PRIMARY_SCOPE else 'recorded drug names representable under the frozen matcher',
            'products': len(idx.by_code), 'distinct_reference_names': len(union),
            'distinct_names_mapped': len(union & idx.available),
            'fully_mappable_cases': sum(row <= idx.available for row in names), 'cases': len(names),
            'reference_patient_drug_pairs': sum(len(row) for row in names),
            'mapped_patient_drug_pairs': sum(len(row & idx.available) for row in names),
            'unmapped_reference_names': sorted(union - idx.available)}


def compare(evidence, reference, formulary, *, run=None):
    scope = reference_scope(reference)
    idx = formulary if isinstance(formulary, MedicationIndex) else MedicationIndex(formulary, scope)
    if idx.scope != scope:
        raise ValueError('Catalogue and reference scopes differ')
    groups = primary_regimen(reference) if scope == PRIMARY_SCOPE else {}
    expected = set.union(*groups.values()) if groups else {drug_name(n) for n in reference['drug_names']}
    policy = evidence.get('ed_order_indication_policy')
    if policy not in (None, INDICATION_POLICY):
        raise ValueError('Unknown signed medication indication policy')
    by_indication = scope == PRIMARY_SCOPE and policy == INDICATION_POLICY
    code_field = 'primary_drug_codes' if by_indication else 'drug_codes'

    def ingredients(codes):
        return set().union(*(idx.by_code[c] for c in codes))

    states = evidence.get('signed_order_states', [])
    if by_indication and (not states or any(
            'primary_drug_codes' not in state or 'other_drug_codes' not in state for state in states)):
        raise ValueError('Scoring by indication requires the signed prescribing history')
    if groups and (by_indication or 'order_timeline' in reference):
        observed = set().union(*(ingredients(state[code_field]) for state in states))
    else:
        observed = ingredients(evidence[code_field])
    if not expected or '' in expected:
        raise ValueError('Missing medication identity in the matched ED encounter')
    result = {'matched': sorted(expected & observed), 'missing': sorted(expected - observed),
              'missing_available': sorted((expected & idx.available) - observed),
              'outside_catalogue': sorted(expected - idx.available), 'additional': sorted(observed - expected),
              'disposition_match': evidence['disposition'] == reference['disposition']}
    if groups:
        result.update(primary_regimen_complete=expected <= observed, exact_drug_set=expected == observed,
                      primary_ingredients={k: {'matched': sorted(v & observed), 'missing': sorted(v - observed)}
                                           for k, v in groups.items()})
        if by_indication:
            result['other_indication_ingredients'] = sorted(set().union(*(
                ingredients(state['other_drug_codes']) for state in states)))
            result['first_primary_order_seconds'] = next((state['offset_seconds'] for state in states
                                                         if state[code_field]), None)
            result['first_primary_order_checkpoint'] = next((i for i, state in enumerate(states, 1)
                                                             if state[code_field]), None)
        if 'order_timeline' in reference:
            timeline = reference['order_timeline']
            transactions = None
            if reference.get('order_task_protocol') in {'independent_order_actions_v1', PRIMARY_DECISION_PROTOCOL}:
                if any(p['not_assessable'] for p in timeline):
                    raise ValueError('The frozen primary-condition reference must classify every decision')
                named = reference.get('predecessor_comparison') == 'ingredient_and_checkpoint'
                steps = order_actions.source_steps(timeline, named_predecessors=named)
                if any('transactions' not in state for state in states):
                    raise ValueError('Order-action scoring requires read-back of signed transactions')
                if [s['offset_seconds'] for s in states] == [p['offset_seconds'] for p in timeline]:
                    predicted = order_actions.scored_steps(states, None if by_indication else expected, idx,
                                                          named_predecessors=named)
                    transactions = [order_actions.compare_step(ref, actual) for ref, actual in zip(steps, predicted, strict=True)]
            if not timeline or [v['offset_seconds'] for v in states] != [v['offset_seconds'] for v in timeline]:
                raise ValueError('Signed ED decision times differ from the source encounter')
            decisions = []
            for state, source in zip(states, timeline, strict=True):
                actual = ingredients(state[code_field])
                excluded = set(source['not_assessable'])
                active = (actual if by_indication else actual & expected) - excluded
                documented = (set(source['active']) & expected) - excluded
                decisions.append({'offset_seconds': source['offset_seconds'],
                                  'matched': sorted(active & documented), 'missing': sorted(documented - active),
                                  'unexpected_active': sorted(active - documented),
                                  'not_assessable': source['not_assessable'],
                                  'agreement': None if source['not_assessable'] else active == documented})
                if 'treatment_review' in state:
                    decision = decisions[-1]
                    decision['physician_call'] = state['physician_call']
                    options = [{'option': option, **entry} for option, entry in state['treatment_review'].items()]
                    decision['class_decisions'] = [entry for entry in options if entry['decision'] == 'omit']
                    if run is not None:
                        decision['review_evidence'] = ed_metrics.prescribing_evidence(run, state['physician_call'])
                    decision['omission_review'] = {}
                    for ingredient in decision['missing']:
                        named = [entry for entry in state.get('considered_not_ordered', [])
                                 if ingredient in idx.by_code.get(entry['drug_code'], ())]
                        signed_actions = state.get('signed_version_transactions', state['transactions'])
                        prior = [entry for entry in options if any(
                            ingredient in idx.by_code.get(signed_actions[action].get('prior_code'), ())
                            for action in entry.get('action_indices', []))]
                        advised = [entry for entry in options if any(ingredient in idx.by_code.get(code, ())
                                   for code in entry.get('advice_codes', []))]
                        trace = decision.get('review_evidence', {})
                        visible = {code for code in trace.get('visible_product_codes', [])
                                   if ingredient in idx.by_code.get(code, ())}
                        searches = trace.get('formulary_searches', [])
                        returned = {code for search in searches for code in search['returned_product_codes']
                                    if ingredient in idx.by_code.get(code, ())}
                        queried = [search['query'] for search in searches
                                   if ingredient in (idx.by_code.get(search['query']) or native_ingredients(search['query']))]
                        decision['omission_review'][ingredient] = {
                            'named_decisions': named, 'prior_product_decisions': prior, 'advisory_product_decisions': advised,
                            'visible_candidate_product_codes': sorted(visible),
                            'catalogue_trace': {'queried_names_or_codes': sorted(set(queried)),
                                                'returned_product_codes': sorted(returned),
                                                'returned_without_delivery': sorted(returned - visible)},
                            'status': ('ingredient_named' if named else 'prior_product_identified' if prior
                                       else 'advisory_product_identified' if advised
                                       else 'no_ingredient_reason_recorded')}
                elif 'withheld_treatments' in state:
                    reasons = state['withheld_treatments']
                    decisions[-1]['physician_call'] = state['physician_call']
                    decisions[-1]['class_decisions'] = [r for r in reasons if r['level'] == 'class']
                    omissions = {}
                    for ingredient in decisions[-1]['missing']:
                        named = [r for r in reasons if r['level'] == 'ingredient' and drug_name(r['name']) == ingredient]
                        linked = [r for r in reasons if any(ingredient in idx.by_code.get(code, ())
                                                            for code in r.get('prior_codes', []))]
                        omissions[ingredient] = {'named_decisions': named, 'prior_product_decisions': linked,
                                                 'status': 'ingredient_named' if named else (
                                                     'prior_product_identified' if linked else 'no_ingredient_reason_recorded')}
                    decisions[-1]['omission_review'] = omissions
            agreements = [decision['agreement'] for decision in decisions]
            if transactions is not None:
                for decision, counts, state in zip(decisions, transactions, states, strict=True):
                    counts['signed_no_action'] = not state['transactions']
                    if by_indication:
                        counts['signed_no_primary_action'] = counts['physician_none']
                    counts['signed_transactions'] = state['transactions']
                    decision['order_actions'] = counts
                result['order_trajectory_agreement'] = all(s['exact'] for s in transactions)
                result['final_order_action_agreement'] = transactions[-1]['exact']
            result.update(signed_decisions=decisions, final_primary_state_agreement=agreements[-1],
                          final_primary_matched=decisions[-1]['matched'],
                          final_primary_missing=decisions[-1]['missing'],
                          final_primary_additional=decisions[-1]['unexpected_active'],
                          primary_trajectory_agreement=None if None in agreements else all(agreements),
                          course_ingredient_coverage=result['matched'], course_ingredient_missing=result['missing'])
    else:
        result['exact_drug_set'] = expected == observed
    return result


def recorded_runs(batch, case_rows):
    paths = {r['case_id']: Path(r['path']) for r in case_rows}
    summary = json.loads(read(batch / 'batch_summary.json'))
    seen = set()
    for result in summary['cases']:
        cid = result['case_id']
        history = result.get('prior_attempts', {})
        records = ((profile, record)
                   for profile, last in result.get('attempts', {}).items()
                   for record in [*history.get(profile, []), last])
        for profile, record in records:
            if not record.get('result_path'):
                continue
            number = record.get('attempt_number', 1)
            if type(number) is not int or number < 1:
                raise ValueError('Invalid attempt number in the batch index')
            label = attempt_label(profile, number)
            path = Path(record['result_path']).resolve()
            if not path.is_relative_to((batch / cid).resolve()) or (cid, label) in seen:
                raise ValueError('Unexpected result path or duplicate assignment')
            seen.add((cid, label))
            run = json.loads(read(path))
            case = json.loads(read(paths[cid]))
            if (run['case_id'] != cid or run.get('scenario_design') != DESIGN
                    or run['input_hash'] != digest(runtime_case(case))
                    or run['run_id'] != batch.name + ':' + cid + ':' + label or run['status'] != record['status']):
                raise ValueError('Result and frozen case/assignment differ')
            yield case, run, path, record


def verify_live(cfg):
    from sandbox.experiments.linked.native_his import NativeOpenHISPort
    observed = 0
    for _, run, path, record in recorded_runs(cfg.batch, json.loads(read(cfg.path('manifest')))['cases']):
        if run['status'] != 'completed' or record.get('invalidated'):
            continue
        native = NativeOpenHISPort(path.parents[2] / 'native', credentials_path=cfg.credentials, base_url=cfg.his_url)
        binding = native._binding(run)
        evidence = verified_closure(run)
        fresh = deepcopy(run)
        order = binding['orders'][evidence['order_id']]
        rows = native._read_valid_signed_order(binding, order) if order['native_ids'] else []
        if 'signed_order_states' in evidence:
            from sandbox.experiments.linked.ed_order_history import read_history
            history = read_history(native, binding, {'patientId': evidence['patient_id'],
                                                     'encounterId': evidence['encounter_id']})
        doc = native._client('doctor').post('/document/getContent', {'documentId': evidence['document_id']})
        for event in fresh['events']:
            if event.get('event') == 'business_operation':
                if event['operation'] == 'note.attest':
                    event['response']['readback'] = doc
                elif event['operation'] == 'order.history' and 'signed_order_states' in evidence:
                    event['response'].update(data=history['data'], readback=history['readback'])
                elif event['operation'] == 'order.attest' and event['response']['data'].get('order_id') == evidence['order_id']:
                    event['response']['readback'] = rows
        if verified_closure(fresh) != evidence:
            raise ValueError('Live signed ED record differs from the run evidence')
        observed += 1
    print(f'Live ED closures verified: {observed}')
    return observed


def within_stage_seconds(run):
    registration = consultation = draft = lock = None
    note_id = (run.get('artifacts', {}).get('attested_note') or {}).get('note_id')
    closed_document = (run.get('artifacts', {}).get('ed_closure') or {}).get('document_id')
    patient, encounter = str(run.get('patient_id') or ''), str(run.get('encounter_id') or '')
    for event in run.get('events', []):
        if event.get('event') != 'business_operation':
            continue
        reply = event.get('response') or {}
        if (not patient or not encounter or reply.get('native_effect') is not True or reply.get('readback_verified') is not True
                or reply.get('status') not in {'executed', 'replayed', 'ok'}):
            continue
        data = reply.get('data') or {}
        record = reply.get('readback') or {}
        op = event.get('operation')
        if op == 'registration.create' and registration is None:
            if str(data.get('encounter_id')) == encounter and str(data.get('patient_id')) == patient:
                registration = event
        elif op == 'encounter.begin_consultation' and consultation is None:
            if (data.get('status') == 'in_progress' and str(data.get('encounter_id')) == encounter and isinstance(record, dict)
                    and str(record.get('patientId')) == patient and str(record.get('encounterId')) == encounter):
                consultation = event
        elif op in {'note.save', 'note.attest'} and isinstance(record, dict):
            if (str(record.get('encounterId')) != encounter or str(record.get('patientId')) != patient
                    or not data.get('document_id') or str(record.get('id')) != str(data['document_id'])):
                continue
            if op == 'note.save' and draft is None and note_id and data.get('version') == 1 and data.get('note_id') == note_id:
                draft = event
            elif (op == 'note.attest' and lock is None and record.get('status') == 'LOCKED'
                    and data.get('status') == 'locked'
                    and str(data['document_id']) == str(closed_document or (
                        draft and draft['response']['data']['document_id']))):
                lock = event
    return {'registration_to_consult_seconds': scenario_design.elapsed_seconds(run, registration, consultation),
            'draft_to_lock_seconds': scenario_design.elapsed_seconds(run, draft, lock),
            'registration_to_signed_note_seconds': scenario_design.elapsed_seconds(run, registration, lock)}


def elapsed_summary(values):
    if not values:
        return {'n': 0, 'median': None, 'q1': None, 'q3': None}
    q1 = q3 = values[0]
    if len(values) > 1:
        q1, _, q3 = quantiles(values, n=4, method='inclusive')
    return {'n': len(values), 'median': median(values), 'q1': q1, 'q3': q3}


def workflow_measures(run, observed):
    activity = run.get('activities', [])
    roles = Counter(a['role'] for a in activity)
    effort = observed['modeled_role_activity_seconds']
    # Incomplete attempts still contribute the work that was observed.
    measures = {k: observed[k] for k in ('patient_answers_total',
        'modeled_report_reading_seconds', 'modeled_prescribing_seconds', 'modeled_documentation_seconds',
        'pharmacist_review_rounds', 'first_pharmacist_return', 'prescription_revision_count', 'note_revision_count',
        'modeled_staff_seconds', 'api_wall_seconds', 'pharmacist_return_rounds', 'pharmacist_initiated_returns',
        'verification_initiated_returns', 'post_return_resubmissions', 'prescription_successor_versions',
        'encoding_only_revisions')}
    measures.update(physician_actions=roles['doctor'], pharmacist_actions=roles['pharmacist'],
                    modeled_physician_seconds=effort.get('doctor', 0),
                    modeled_pharmacist_seconds=effort.get('pharmacist', 0),
                    modeled_patient_seconds=effort.get('patient', 0))
    intervals = observed['elapsed_stage_seconds']
    measures.update({k + '_elapsed_seconds': v for k,v in intervals.items()})
    measures.update(within_stage_seconds(run))
    return measures


def first_signature_comparison(run, reference, index):
    submission, submitted, _ = ed_metrics.first_submission(run)
    timeline = reference.get('order_timeline') or []
    order_history = run['artifacts'].get('ed_signed_order_history') or []
    metrics = {}
    if run.get('ed_pharmacy_review') and timeline and order_history:
        first = order_history[0]
        source = timeline[0]
        if first.get('decision_offset_seconds') != source['offset_seconds'] or source['not_assessable']:
            return metrics
        states = run['artifacts']['ed_closure']['signed_order_states']
        before = {ingredient for row in first['active'] if indication_scope(row['item']) == 'primary'
                  for ingredient in index.by_code[row['item']['drug_code']]}
        after = {ingredient for code in states[0]['primary_drug_codes'] for ingredient in index.by_code[code]}
        metrics['first_signature_exact'] = after == set(source['active'])
        if first['order_id'] in run['artifacts']['ed_pharmacy_readback'] and run['artifacts']['ed_pharmacy_readback'][first['order_id']]['decision'] != 'not_applicable':
            metrics['first_submission_exact'] = before == set(source['active'])
        return metrics
    if not submitted or not timeline or not order_history:
        return metrics
    original = order_history[0]
    source = timeline[0]
    if original.get('decision_offset_seconds') != source['offset_seconds'] or source['not_assessable']:
        return metrics
    order_id = submission.get('order_id') or submission.get('candidate_id')
    first_versions = run['artifacts'].get('prescription_versions') or []
    signed_at = next((i for i, v in enumerate(first_versions) if v['order_id'] == original['order_id']), None)
    submitted_at = next((i for i, v in enumerate(first_versions) if v['order_id'] == order_id), None)
    if submitted_at is None or signed_at is None or submitted_at > signed_at:
        return metrics
    def primary(items):
        try:
            codes = {i['drug_code'] for i in items if indication_scope(i) == 'primary'}
            return set().union(*(index.by_code[c] for c in codes))
        except (KeyError, ValueError):
            return None
    before = primary(submitted['items'])
    after = primary([row['item'] for row in original['active']])
    if before is not None and after is not None:
        recorded = set(source['active'])
        metrics.update(first_submission_exact=before == recorded, first_signature_exact=after == recorded)
    return metrics


def write_report(batch, case_rows, reference_path, output):
    batch = Path(batch)
    reference_bytes = Path(reference_path).read_bytes() if reference_path else None
    gold = json.loads(reference_bytes) if reference_bytes is not None else None
    if gold is not None and not {c['case_id'] for c in case_rows}.issubset(gold):
        raise ValueError('The private evaluation file does not cover the selected cases')
    scope = common_scope(gold[c['case_id']] for c in case_rows) if gold is not None else None
    medication_indices = {}
    spec = json.loads(read(batch / 'specification.json'))
    if spec.get('scenario_design') != DESIGN:
        raise ValueError('Expected a frozen ED specification')
    if reference_bytes is not None and spec.get('reference_sha256') not in (None, sha256(reference_bytes).hexdigest()):
        raise ValueError('Medication reference differs from the frozen ED specification')
    design = spec.get('linked_acceptance') or {}
    base = scenario_design.baselines()
    profiles = {p['label']: p for p in design.get('profiles', [])} or {
        BASELINE_LABEL: {'config_id': linked_config_id(base), 'conditions': {k: v.to_dict() for k, v in base.items()}}}
    cases = {c['case_id']: c for c in case_rows}
    frozen = spec['cases']
    if len({c['case_id'] for c in frozen}) != len(frozen) or any(
            c['case_id'] not in cases or c['group'] != cases[c['case_id']]['group'] for c in frozen):
        raise ValueError('Frozen case list and supplied manifest differ')
    expected = {(c['case_id'], p['config_id']): (c['group'], p['config_id']) for c in frozen for p in profiles.values()}
    planned = len(expected)
    if not planned or design.get('planned_unique_encounters', planned) != planned:
        raise ValueError('Frozen assignment denominator differs')
    selected, attempts, first_attempt_closed = {}, Counter(), set()
    assignment_work, assignment_sensitivity = defaultdict(Counter), defaultdict(Counter)
    assignment_events = defaultdict(dict)
    paired_report_hashes = {}
    attribution = None
    for case, run, _, record in recorded_runs(batch, case_rows):
        label = run['run_id'].rsplit(':', 1)[-1]
        profile_label, _, retry = label.partition('__retry_')
        profile = profiles.get(profile_label)
        assignment = case['case_id'], run['config_id']
        if assignment not in expected or not profile or run['config_id'] != profile['config_id'] or run['conditions'] != profile['conditions']:
            raise ValueError('Encounter does not use its assigned clinical conditions')
        attempt = int(retry) if retry else 1
        row = {'case_id': case['case_id'], 'run_id': run['run_id'], 'group': case['group'],
               'config_id': run['config_id'], 'status': run['status'], 'attempt_number': attempt}
        if record.get('invalidated'):
            row.update(status='invalidated', invalidated=record['invalidated'])
        if scope == PRIMARY_SCOPE:
            basis = run.get('ed_order_indication_policy', 'legacy_unattributed')
            if basis not in ('legacy_unattributed', INDICATION_POLICY):
                raise ValueError('Unrecognized prescribing attribution')
            if attribution is not None and attribution != basis:
                raise ValueError('Report historical orders without signed indications in a separate batch.')
            attribution = basis
        counted = Counter(recorded=1)
        stages = observed_clinical_stages(run)
        counted.update({name: int(found) for name, found in stages.items()})
        row['native_stage_readbacks'] = stages
        assignment_events[assignment][attempt] = trace_counts(run)
        row['resumed_after_failed_format'] = bool(record.get('response_format_repairs'))
        tracked_invalid = {call.get('request_id') for call in run['calls'] if call.get('status') == 'invalid_schema'}
        for repair in record.get('response_format_repairs', []):
            failure_count = len(set(repair['failed_request_ids']) - tracked_invalid)
            assignment_events[assignment][attempt]['model_execution_signals'] += failure_count
            if repair['corrected_request_id'] in {call.get('request_id') for call in run['calls']
                                                  if call.get('status') == 'completed'}:
                assignment_events[assignment][attempt]['model_execution_recoveries'] += failure_count
        if record.get('invalidated'):
            assignment_events[assignment][attempt]['model_execution_signals'] += 1
        observed = scenario_design.measurements(run)
        row['workflow_measures'] = workflow_measures(run, observed)
        assignment_work[assignment].update({k: row['workflow_measures'][k] for k in ed_metrics.STAFF_WORK_METRICS})
        assignment_sensitivity[assignment].update(ed_metrics.sensitivity_components(run))
        assessment_reference = gold.get(case['case_id']) if gold is not None else None
        row['note_consistency'] = ed_metrics.note_comparisons(run, stages)
        row['artifact_measures'] = ed_metrics.artifact_measures(
            run, stages, assessment_reference, case['group'], note_checks=row['note_consistency'])
        returns = observed['pharmacist_return_rounds']
        if returns is not None:
            counted.update(reviewed=1, first_returns=int(observed['first_pharmacist_return']),
                           any_returns=int(returns > 0), returned_rounds=returns)
        if row['status'] == 'completed':
            proof = verified_closure(run)
            checks = scenario_design.execution_checks(run, profile, spec['models'])
            if proof != run['artifacts'].get('ed_closure') or not all(checks.values()):
                raise ValueError('Closed ED run lacks matching evidence')
            if spec.get('paired_intake_report') and profile['conditions']['preconsult']['model_family']:
                shared = run['artifacts'].get('shared_intake')
                if not shared or shared['policy'] != spec['paired_intake_report']:
                    raise ValueError('Assisted consultation lacks the frozen intake report')
                previous = paired_report_hashes.setdefault(assignment[0], shared['report_hash'])
                if previous != shared['report_hash']:
                    raise ValueError('Assisted consultations differ in the report shown to the physician')
                row['paired_intake_report_hash'] = shared['report_hash']
            counted['closed'] = 1
            if attempt == 1 and not row['resumed_after_failed_format']:
                first_attempt_closed.add(assignment)
            counted['closed_after_return'] = int(bool(returns))
            if 'signed_order_states' in proof:
                counted['closed_without_medication'] = int(not any(s['drug_codes'] for s in proof['signed_order_states']))
            if gold is not None:
                if gold[case['case_id']].get('runtime_input_hash') != run['input_hash']:
                    raise ValueError('Reference and acting case belong to different freezes')
                from sandbox.experiments.linked.formulary_search import directory_for_case
                directory = directory_for_case(case, cases[case['case_id']]['path'])
                formulary = directory.products.values() if directory else case['formulary']
                catalogue_key = digest(case.get('formulary_directory') or case['formulary'])
                if catalogue_key not in medication_indices:
                    medication_indices[catalogue_key] = MedicationIndex(formulary, scope)
                score = compare(proof, gold[case['case_id']], medication_indices[catalogue_key], run=run)
                row.update(score)
                if run.get('ed_order_indication_policy') == INDICATION_POLICY:
                    row['artifact_measures'].update(first_signature_comparison(
                        run, assessment_reference, medication_indices[catalogue_key]))
                if 'signed_decisions' in score:
                    row['artifact_measures']['first_signature_exact'] = score['signed_decisions'][0]['agreement']
                if (row['artifact_measures']['first_submission_exact'] is not None
                        and row['artifact_measures']['first_signature_exact'] is not None):
                    counted.update(first_signature_pairs=1, first_signature_pair_delta=(
                        int(row['artifact_measures']['first_signature_exact'])
                        - int(row['artifact_measures']['first_submission_exact'])))
                counted.update(scored=1,
                               disposition_match=int(score['disposition_match']), matched=len(score['matched']),
                               missing=len(score['missing']), missing_available=len(score['missing_available']),
                               outside_catalogue=len(score['outside_catalogue']), additional=len(score['additional']))
                if scope == PRIMARY_SCOPE:
                    if 'other_indication_ingredients' in score:
                        counted['other_indication_ingredients'] += len(score['other_indication_ingredients'])
                        counted['closed_with_primary_medication'] = int(score['first_primary_order_seconds'] is not None)
                        row['workflow_measures']['first_primary_order_checkpoint'] = score['first_primary_order_checkpoint']
                    counted['primary_regimen_complete'] = int(score['primary_regimen_complete'])
                    counted['course_exact'] = int(score['exact_drug_set'])
                    if 'order_trajectory_agreement' in score:
                        counted.update(action_trajectories=1,
                                       action_trajectories_exact=int(score['order_trajectory_agreement']),
                                       final_action_exact=int(score['final_order_action_agreement']))
                    if 'signed_decisions' in score:
                        observable = [d['agreement'] for d in score['signed_decisions'] if d['agreement'] is not None]
                        final = score['final_primary_state_agreement']
                        trajectory = score['primary_trajectory_agreement']
                        counted.update(decisions_evaluable=len(observable), decisions_agree=sum(observable),
                                       final_evaluable=int(final is not None), final_agree=int(final is True),
                                       trajectories_evaluable=int(trajectory is not None),
                                       trajectories_agree=int(trajectory is True))
                        for decision in score['signed_decisions']:
                            if 'omission_review' in decision:
                                counted['omission_checkpoints_with_signed_reasons'] += 1
                                counted['missing_ingredient_checkpoints'] += len(decision['omission_review'])
                                counted['withheld_class_decisions'] += len(decision['class_decisions'])
                                for omission in decision['omission_review'].values():
                                    counted[omission['status']] += 1
                            if 'order_actions' in decision:
                                action = decision['order_actions']
                                counted.update(action_steps=1, action_steps_content_exact=int(action['content_exact']),
                                               linked_steps=int(action['exact'] is not None),
                                               linked_steps_exact=int(action['exact'] is True),
                                               targets_evaluable=action['targets_evaluable'],
                                               targets_matched=action['targets_matched'])
                                for kind, flag in action['by_action'].items():
                                    outcome = ('tp' if flag['physician'] else 'fn') if flag['reference'] else ('fp' if flag['physician'] else 'tn')
                                    counted['action_' + kind + '_' + outcome] += 1
                                for field in ('matched', 'missing', 'additional'):
                                    counted['order_actions_' + field] += sum(row['count'] for row in action[field])
                    for kind, parts in score['primary_ingredients'].items():
                        for outcome, ingredients in parts.items():
                            counted[kind + '_' + outcome] = len(ingredients)
                else:
                    counted['exact_drug_set'] = int(score['exact_drug_set'])
        # At most one outcome per assignment. Use the first verified closure, irrespective of its concordance.
        rank = (not bool(counted['closed']), attempt if counted['closed'] else -attempt)
        for key, value in (row['workflow_measures'] | row['artifact_measures']).items():
            if value is not None and key not in ed_metrics.STAFF_WORK_METRICS:
                counted['sum_' + key] += value
                counted['n_' + key] += 1
        if assignment not in selected or rank < selected[assignment][0]:
            selected[assignment] = (rank, counted, row)
        attempts[assignment] += 1
    totals, groups, details = Counter(), defaultdict(Counter), []
    settings = {p['config_id']: p['conditions'] for p in profiles.values()}
    by_task = defaultdict(Counter)
    by_condition = defaultdict(Counter)
    for assignment, group in expected.items():
        if assignment in selected:
            _, counted, row = selected[assignment]
            row['recorded_attempts'] = attempts[assignment]
            row['attempt_workflow_events'] = assignment_events[assignment]
            event_counts = Counter()
            for signals in assignment_events[assignment].values():
                event_counts.update(signals)
            row['workflow_events'] = dict(event_counts)
            counted.update(event_counts)
            counted['assignments_closed_on_later_attempt'] = int(counted['closed'] and (
                row['attempt_number'] > 1 or row['resumed_after_failed_format']))
            row['workflow_measures'].update(assignment_work[assignment])
            row['staff_activity_seconds'] = dict(assignment_sensitivity[assignment])
            for key, value in assignment_work[assignment].items():
                counted['sum_' + key] += value
                counted['n_' + key] += 1
        else:
            counted = Counter()
            row = {'case_id': assignment[0], 'group': group[0], 'config_id': group[1], 'status': 'unobserved',
                   'recorded_attempts': 0}
        counted.update(planned=1, first_attempt_closed=int(assignment in first_attempt_closed))
        row['first_attempt_closed'] = assignment in first_attempt_closed
        totals.update(counted)
        groups[group].update(counted)
        by_condition[group[1]].update(counted)
        for task, condition in settings[group[1]].items():
            by_task[task, condition['workflow']].update(counted)
        details.append(row)

    durations = {task: {p['conditions'][task]['workflow']: [] for p in profiles.values()}
                 for task in ('preconsult', 'documentation')}
    for row in details:
        for task, metric in (('preconsult', 'registration_to_consult_seconds'), ('documentation', 'draft_to_lock_seconds')):
            value = row.get('workflow_measures', {}).get(metric)
            if value is not None:
                durations[task][settings[row['config_id']][task]['workflow']].append(value)

    def time_summaries(rows):
        return {metric: elapsed_summary([row['workflow_measures'][metric] for row in rows
                                          if row.get('workflow_measures', {}).get(metric) is not None])
                for metric in ed_metrics.ELAPSED_METRICS}

    def measures(values):
        def rate(n, d):
            return n / d if d else None
        m = values['matched']
        means = {k[2:]: {'mean': rate(values['sum_' + k[2:]], n), 'n': n}
                 for k, n in values.items() if k.startswith('n_')}
        counts = {k: v for k, v in values.items() if not k.startswith(('sum_', 'n_'))}
        if scope == PRIMARY_SCOPE:
            medication = {'primary_regimen_complete_rate': rate(values['primary_regimen_complete'], values['scored']),
                          'drug_exact_rate': rate(values['course_exact'], values['scored']),
                          'drug_precision': rate(m, m + values['additional']),
                          'mean_missing_ingredients': rate(values['missing'], values['scored']),
                          'mean_additional_ingredients': rate(values['additional'], values['scored']),
                          'closure_with_exact_regimen_rate': rate(values['course_exact'], values['planned'])}
            if attribution == INDICATION_POLICY:
                medication['additional_primary_ingredients'] = counts.pop('additional', 0)
                medication['closure_with_primary_medication_rate'] = rate(values['closed_with_primary_medication'], values['planned'])
            else:
                medication['additional_unattributed_ingredients'] = counts.pop('additional', 0)
            if 'decisions_evaluable' in values:
                medication.update(primary_state_agreement=rate(values['decisions_agree'], values['decisions_evaluable']),
                                  final_primary_state_agreement=rate(values['final_agree'], values['final_evaluable']),
                                  closure_with_exact_final_regimen_rate=rate(values['final_agree'], values['planned']),
                                  primary_trajectory_agreement=rate(values['trajectories_agree'], values['trajectories_evaluable']))
            if values['action_steps']:
                matched = values['order_actions_matched']
                medication.update(order_trajectory_agreement=rate(values['action_trajectories_exact'], values['action_trajectories']),
                                  final_order_action_agreement=rate(values['final_action_exact'], values['action_trajectories']),
                                  order_action_content_agreement=rate(values['action_steps_content_exact'], values['action_steps']),
                                  order_action_step_agreement=rate(values['linked_steps_exact'], values['linked_steps']),
                                  order_action_recall=rate(matched, matched + values['order_actions_missing']),
                                  order_action_precision=rate(matched, matched + values['order_actions_additional']),
                                  predecessor_agreement=rate(values['targets_matched'], values['targets_evaluable']))
                by_action = {}
                for kind in ('new', 'repeat', 'change', 'stop'):
                    hits = values['action_' + kind + '_tp']
                    positives = hits + values['action_' + kind + '_fn']
                    by_action[kind] = {'reference_positive_decisions': positives,
                                       'sensitivity': rate(hits, positives)}
                medication['order_actions_by_type'] = by_action
            for k in ('continued', 'added'):
                matched = values[k + '_matched']
                denominator = matched + values[k + '_missing']
                medication[k + '_ingredient_recall'] = rate(matched, denominator)
                medication[k + '_reference_ingredients'] = denominator
        else:
            medication = {'drug_precision': rate(m, m + values['additional']),
                          'drug_exact_rate': rate(values['exact_drug_set'], values['scored'])}
        artifacts = {k: {'rate': rate(values['sum_' + k], values['n_' + k]),
                         'successes': values['sum_' + k], 'n': values['n_' + k]} for k in ed_metrics.ARTIFACT_RATES}
        artifacts['first_signature_exact_change'] = {
            'difference': rate(values['first_signature_pair_delta'], values['first_signature_pairs']),
            'n': values['first_signature_pairs']}
        return {**counts, **medication, 'artifact_rates': artifacts,
                'first_attempt_closure_rate': rate(values['first_attempt_closed'], values['planned']),
                'first_return_rate': rate(values['first_returns'], values['reviewed']),
                'any_return_rate': rate(values['any_returns'], values['reviewed']),
                'review_coverage': rate(values['reviewed'], values['planned']),
                'closed_after_return_rate': rate(values['closed_after_return'], values['any_returns']),
                'closure_rate': rate(values['closed'], values['planned']),
                'workflow_measures': means, 'drug_recall': rate(m, m + values['missing']),
                'catalogue_representable_recall': rate(m, m + values['missing_available']),
                'disposition_agreement': rate(values['disposition_match'], values['scored'])}
    report = {'endpoint': DESIGN, 'planned': planned, 'closed': totals['closed'],
              'closure_rate': totals['closed'] / planned, 'reference_supplied': gold is not None,
              'measurement_version': 7,
              'paired_intake_reports_verified': (len(paired_report_hashes) if spec.get('paired_intake_report') else None),
              'evaluation_sha256': {
                  'reference': sha256(reference_bytes).hexdigest() if reference_bytes is not None else None,
                  **{path.name: sha256(path.read_bytes()).hexdigest() for path in
                     (Path(__file__), Path(ed_metrics.__file__), Path(order_actions.__file__),
                      Path(scenario_design.__file__))}},
              'medication_reference_scope': scope,
              'medication_attribution': attribution,
              'omission_attribution': 'signed omitted-product decisions and observed catalogue and chart access',
              'medication_unit': ('distinct active ingredient in the documented primary-condition regimen'
                                  if scope == PRIMARY_SCOPE else 'distinct normalized name per encounter'),
              'totals': measures(totals),
              'source_interview_factual_accuracy': 'unassessed',
              'within_stage_seconds': {task: {name: elapsed_summary(values) for name, values in conditions.items()}
                                       for task, conditions in durations.items()},
              'elapsed_seconds': time_summaries(details),
              'by_configuration': [{'config_id': config, **measures(v)} for config, v in sorted(by_condition.items())],
              'by_task_condition': [{'task': task, 'workflow': condition, **measures(v),
                                     'elapsed_seconds': time_summaries([
                                         row for row in details if settings[row['config_id']][task]['workflow'] == condition])}
                                    for (task, condition), v in sorted(by_task.items())],
              'paired_comparisons': ed_metrics.paired_contrasts(details, settings, with_reference=gold is not None),
              'full_note_clinical_correctness': 'unassessed',
              'by_disease_and_condition': [{'group': g, 'config_id': c, **measures(v)} for (g,c), v in sorted(groups.items())]}
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name, obj in [('summary.json', report), ('encounters.private.json', details)]:
        (output / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
        (output / name).chmod(0o600)
    print(json.dumps(report['totals'] | {'planned': planned, 'closed': totals['closed']}))
