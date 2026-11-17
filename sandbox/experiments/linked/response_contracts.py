"""Role/action JSON contracts and a bounded, format-only correction policy."""
from copy import deepcopy
import json
import re
from ..visibility import assert_visible

POLICY = {"version": "role_action_contracts_v29_explicit_pending_disposition", "max_schema_corrections": 1,
    "max_syntax_corrections": 1,
    "note_revision_evidence": "current_text_and_visible_source_excerpt_v1_no_noop_revision",
    "lossless_structure_normalization": "declared_fields_content_to_root_v2",
    "prescription_bearing_consultation_requires_declared_structured_diagnosis_when_catalogue_present": True,
    "eligible": "parsed_shape_or_visible_output_boundary_error_or_completed_response_JSON_syntax_or_duplicate_key_error",
    "max_invalid_response_bytes": 40000,
    "syntax_correction_requires_finish_reason": "stop",
    "syntax_and_shape_corrections_share_one_attempt": False,
    "output_boundary_uses_same_single_correction_attempt": True,
    "syntax_correction_extracts_or_selects_json_fragments": False,
    "duplicate_key_value_selected": False,
    "malformed_response_reinjected_into_role_context": False,
    "same_role_model_and_clinical_input": True,
    "clinical_reject_defer_no_rx_retried": False,
    "invalid_finalization_decision_with_empty_items": "one_format_correction_preserving_no_dispensing",
    "network_budget_truncation_or_semantic_parser_exceptions_retried_here": False,
    "automatic_clinical_field_inference": False}


class ResponseBoundaryError(ValueError):
    """A parsed model output failed the unchanged visible-data boundary."""
    def __init__(self, response, reason):
        super().__init__('model output violates visible-data schema: ' + reason)
        self.response = deepcopy(response)
        self.boundary_reason = reason


def assert_response_visible(value):
    try:
        assert_visible(value)
    except ValueError as exc:
        raise ResponseBoundaryError(value, str(exc)) from None

ROLE_ACTIONS = {
    "patient": ("answer",), "nurse": ("collect", "review_summary"),
    "preconsult_ai": ("collect",), "doctor": ("verify_handoff", "consult", "resolve_diagnosis_coding", "resolve_pending_care", "assess_pending_care", "prepare_consultation", "assess", "finalize_prescription", "write_brief", "dictate_addendum", "revise_prescription", "finalize_care_plan", "document", "review_note", "finalize_note"),
    "pharmacist": ("review_prescription", "reconcile_medications"), "rx_ai": ("review_prescription", "draft_prescription", "advise_prescription"),
    "senior_pharmacist": ("adjudicate",), "scribe": ("document",),
}
CONTRACTS = {
    "assess_pending_care": {"response_actions": ["ask", "read_history", "assessment_completed"], "required_response": "Complete the named work as the SAME treating physician: {action:'ask',question:string} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string,encounter_key?:string} OR {action:'assessment_completed',assessment:string,plan:string,resolved_issues:[string],unresolved_issues:[string],evidence_basis:string}. Perform the clinical assessment now; do not merely recommend that another doctor assess it. Use the actual current observations, available history, patient answers and prior completed assessments. Distinguish a clinical judgment from a newly measured fact. An unknown remains unknown; state whether it actually prevents each treatment option rather than repeatedly requesting an unavailable fact. Do not prescribe, approve an order or invent test results here. The selected prescribing workflow runs after your assessment."},
    "resolve_pending_care": {"response_actions": ["ask", "read_history", "request_medication_reconciliation", "resume_assessment", "confirm_no_prescription", "request_investigation"], "required_response": "Choose the next concrete action as the same treating physician: {action:'ask',question:string} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string,encounter_key?:string} OR {action:'request_medication_reconciliation',issues:[string],reason:string} OR {action:'resume_assessment',work:string,reason:string} OR {action:'confirm_no_prescription',reason:string} OR {action:'request_investigation',issues:[string],reason:string}. resume_assessment assigns a specific unfinished clinical assessment to YOU now, using available evidence or the existing question/chart-query budgets; it is not a request for another doctor or a new assessment agent. Ask the patient or request actual records when needed. Request pharmacist reconciliation only for specific missing/conflicting medication facts, not for a clinical treatment decision you have not made. Confirm no prescription only when that is your reasoned decision. Request genuinely unavailable investigations explicitly; never invent their results, a medicine or approval to complete payment. Do not include a prescription here: resumed prescribing must follow the selected experiment condition."},
    "resolve_diagnosis_coding": {"response_actions": ["coded_diagnoses"], "required_response": "{action:'coded_diagnoses',diagnoses:[{code:string,display:string,system:string,primary:boolean,certainty:'working'|'confirmed'}],reason:string}. Resolve the coding of your existing clinical assessment using the verified source entries in lookup_feedback.coding_candidates. These are candidates, not an automatic crosswalk. Compare the displayed concepts and explicitly select the hospital's code/system when they describe your existing current diagnosis. Correcting your current encounter's code-system label is allowed; it does not rewrite the original historical record's label. Preserve the number/order, primary flags, certainty, assessment, treatment, prescription and patient facts. If no returned entry preserves the existing clinical meaning, retain the unresolved diagnosis and explain why."},
    "answer": {"response_actions": ["answer"], "required_response": "{action:'answer',text:string,expressed_fact_ids:[memory IDs]}"},
    "collect": {"response_actions": ["ask", "summary"], "required_response": "{action:'ask',question:string} OR {action:'summary',content:{text:string,facts:[{fact_id,topic,value,status}],risk_flags:[string]}}"},
    "prepare_consultation": {"response_actions": ["report_read"], "required_response": "{action:'report_read',content:{text:string},questions_to_verify:[string]}; read the supplied report, distinguish reported facts and uncertainty, and record your consultation preparation without claiming to have asked these questions"},
    "assess": {"response_actions": ["ask", "assessment_complete", "read_history"], "required_response": "{action:'ask',question:string,question_purpose:'diagnostic'} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string,encounter_key?:string} OR {action:'assessment_complete',assessment:string,plan:string,diagnoses:[{code,display,system,primary:boolean,certainty:'working'|'confirmed'}],medication_reconciliation:{status:'complete_declared_episode'|'partial'|'not_assessed',basis:string,unresolved:[string]},allergy_assessment:{status:'none_known'|'allergy_reported'|'unable_to_ascertain',basis:string}}; complete the clinical assessment before the AI medication proposal. plan records care goals and unresolved checks, not a new drug selection or regimen hidden in prose. Preserve existing medication history and reconciliation; do not draft the new prescription at this step"},
    "draft_prescription": {"response_actions": ["prescription_draft"], "required_response": "{action:'prescription_draft',prescription:{items:[{drug_code,dose,dose_unit,route,frequency,quantity,quantity_unit,device?:string,administration_instructions?:string,intended_course?:{start_offset_hours:number,duration_days:number,basis:string}}]},reason:string}; propose treatment from the visible assessment and formulary; an empty items list with an explicit reason may represent no appropriate dispensing; you cannot sign or approve a prescription"},
    "advise_prescription": {"response_actions": ["prescription_advice"], "required_response": "{action:'prescription_advice',suggested_prescription:{items:[{drug_code,dose,dose_unit,route,frequency,quantity,quantity_unit,device?:string,administration_instructions?:string,intended_course?:{start_offset_hours:number,duration_days:number,basis:string}}]},issues:[string],reason:string}; assess the physician's unchanged initial prescription and suggest a supported revision or retain it; you cannot sign or approve it"},
    "finalize_prescription": {"response_actions": ["prescription_finalized", "ask", "read_history", "defer"], "allowed_decisions": ["accept", "revise", "no_prescription", "defer"], "information_actions": ["ask", "read_history"], "required_response": "{action:'prescription_finalized',decision:'accept'|'revise'|'no_prescription'|'defer',prescription:{items:[...]},reason:string} OR {action:'ask',question:string,question_purpose:'prescription_clarification'} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string}; accept returns the supplied candidate unchanged, revise returns your revised order, no_prescription returns items=[] for your actual no-dispensing decision, and defer returns items=[] when clinical work remains pending. An explicit action='defer' with decision='defer', prescription.items=[], and reason is also accepted as the same pending disposition. defer authorizes neither signing nor dispensing and returns the unfinished work to you. Questions use action=ask with question, never decision=ask. Judge the AI suggestion independently using only visible clinical evidence"},
    "write_brief": {"response_actions": ["clinical_brief"], "required_response": "{action:'clinical_brief',content:{text:string,source_sequences:[integer]}}. As the treating physician, dictate a concise postencounter summary for AI note drafting. text represents an accurately transcribed spoken summary, not a polished medical note. Select and summarize the history, observed findings, assessment and final care plan from the supplied sources. Preserve uncertainty. Do not copy the full dialogue or add unobserved findings."},
    "dictate_addendum": {"response_actions": ["clinical_addendum"], "required_response": "{action:'clinical_addendum',content:{text:string,source_sequences:[integer]}}. As the SAME treating physician, supplement the recorded encounter with relevant examination findings or final clinical decisions not already spoken in the dialogue. text represents accurate transcription of this postencounter dictation. Use only the supplied observations and completed care records; distinguish assessment from measured findings. Do not repeat the whole history or invent an examination. If no supplement is needed, return text:'' and source_sequences:[], while retaining the content object."},
    "review_summary": {"response_actions": ["ask", "summary"], "required_response": "{action:'ask',question:string} OR {action:'summary',content:{text:string,facts:[{fact_id,topic,value,status}],risk_flags:[string]}}"},
    "verify_handoff": {"response_actions": ["ask", "summary", "read_history"], "required_response": "{action:'ask',question:string,question_purpose:'handoff_verification'|'handoff_supplementation'|'handoff_correction'} OR {action:'summary',content:{text:string,facts:[],risk_flags:[]}} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string,encounter_key?:string}"},
    "consult": {"response_actions": ["ask", "consultation_complete", "read_history"], "required_response": "{action:'ask',question:string,question_purpose:'diagnostic'} OR {action:'consultation_complete',assessment:string,plan:string,diagnoses:[{code,display,system:exact catalogue system,primary:boolean,certainty:'working'|'confirmed'}],prescription:{items:[{drug_code,dose,dose_unit,route,frequency,quantity,quantity_unit,device?:string,administration_instructions?:string,intended_course?:{start_offset_hours:number,duration_days:number,basis:string}}]},medication_reconciliation:{status:'complete_declared_episode'|'partial'|'not_assessed',basis:string,unresolved:[string]},allergy_assessment:{status:'none_known'|'allergy_reported'|'unable_to_ascertain',basis:string},no_prescription_reason?:string} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string,encounter_key?:string}; intended_course records an explicitly chosen new course on the chart's relative-hour clock, not actual ingestion or a duration guessed from quantity; omit it when undecided. An explicit no-Rx decision may have items=[] and undetermined diagnoses=[]; these assessments document this physician's actual judgment, not an independent reference label"},
    "revise_prescription": {"allowed_decisions": ["revise", "retain_rejected", "defer"], "information_actions":["read_history","ask"],
        "required_response": ("{decision:'revise',prescription:{items:[...]},reason:string} OR "
            "{decision:'retain_rejected',reason:string} OR {decision:'defer',reason:string} OR "
            "{action:'read_history',section:string,offset:integer,limit:integer,term?:string} OR "
            "{action:'ask',question:string,question_purpose:'prescription_clarification'}. "
            "Reads and questions share the remaining consultation budgets. A revision can withhold specific "
            "items and submit independently justified items for another review. Explain any withheld items "
            "and follow-up in reason. Use defer if the whole order remains unresolved. Preserve missing facts.")},
    "review_prescription": {"response_actions": ["review"], "allowed_decisions": ["approve", "flag", "reject"], "required_response": "{action:'review',decision:'approve'|'flag'|'reject',flags:[string],reason:string}"},
    "adjudicate": {"response_actions": ["adjudication"], "allowed_decisions": ["approve", "reject", "defer"], "required_response": "{action:'adjudication',decision:'approve'|'reject'|'defer',reason:string}; flag is not a final disposition; unresolved uncertainty may be explicitly deferred"},
    "reconcile_medications": {"response_actions": ["ask", "read_history", "reconciliation_complete"], "required_response": "{action:'ask',question:string} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string} OR {action:'reconciliation_complete',content:{text:string},reconciled_medications:[{name:string,status:'reported_current'|'historical_only'|'uncertain',evidence_basis:string}],allergy_assessment:{status:'none_known'|'allergy_reported'|'unable_to_ascertain',basis:string},resolved_issues:[string],unresolved_issues:[string]}; unresolved_issues contains only missing/conflicting patient medication or allergy information, not a pending physician decision, unsigned prescription, unpaid bill or unrelated investigation. This is a pharmacist assessment from visible evidence, not an approval, new source fact or completed test; use the declared remaining service question/query budgets"},
    "finalize_care_plan": {"response_actions": ["care_plan"], "required_response": "{action:'care_plan',disposition:'no_medication'|'further_assessment'|'in_hospital_review',reason:string,withheld_treatment:string,next_steps:[{action:string,responsible_role:string,timing:string}],patient_instructions:string,unresolved_information:[string]}; this records unfinished work inside this virtual hospital; do not transfer outside or claim workflow success without signed prescription, locked note and verified medication payment. Planned tests are not results."},
    "document": {"response_actions": ["draft"], "required_response": "{action:'draft',content:{text:string,source_sequences:[integer]}}; return the actual note text, never a review decision or acceptance of previous_draft"},
    "finalize_note": {"allowed_decisions": ["accept", "revise", "reject"], "required_response": "{decision:'accept'|'reject',reason:string} OR {decision:'revise',reason:string,content:{text:string,source_sequences:[integer]},finalized:boolean}. Review the CURRENT note against the actual encounter sources. Accept authorizes signing the current text. Revise returns the full corrected text; finalized=true explicitly authorizes signing that corrected version after its saved content is read back. Use finalized=false when further clinical work is needed. Explain your judgment; manual JSON quotations of source records are not required. Preserve unknowns and do not invent findings or completed actions."},
    "review_note": {"allowed_decisions": ["accept", "revise", "reject"], "required_response": "{decision:'accept'|'reject',reason:string} OR {decision:'revise',reason:string,revision_evidence:{issue_type:'incorrect_text'|'unsupported_claim'|'omission',current_text_quote:string,source_field:string,source_excerpt:string},content:{text:string,source_sequences:[integer]}}. A revision must change the current content.text. For incorrect_text or unsupported_claim, quote exact words from CURRENT content.text, not from consultation, transcript or an older version. An omission uses an empty current_text_quote. source_field names an available path rooted at transcript, hospital_history, handoff, current_encounter_measurements, allowed_source_scope, observed_business_state, clinical_disposition, clinical_addendum, consultation, or approved_prescription (for example hospital_history.queried_pages[0].page.records[0].record.actual_use). Copy the actual path from the supplied observation. A relative path is accepted only if exactly one of these roots contains it. source_excerpt quotes that selected field exactly; only unsupported_claim may use an empty source_excerpt to describe absence of support. Correct the cited issue in the returned full text. If the old issue is already resolved, judge the current note afresh; do not invent another issue or approve merely to end the loop."},
}


NOTE_SOURCE_FIELDS = frozenset({'transcript', 'hospital_history', 'handoff',
    'current_encounter_measurements', 'allowed_source_scope', 'observed_business_state',
    'clinical_disposition', 'clinical_addendum', 'consultation', 'approved_prescription'})


def visible_note_source(observation, field):
    """Resolve an exact supplied path, optionally relative to one source root.

    Relative paths are accepted only when exactly one permitted root contains
    that path. This does not search arbitrary descendants or other observations.
    """
    if not isinstance(field, str) or len(field) > 500:
        return None
    parts = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[[0-9]+\]))*', field)
    if not parts:
        return None
    tokens = [parts[1]] + [key if key else int(index)
        for key, index in re.findall(r'\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]', field[len(parts[1]):])]

    def resolve(value, keys):
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, IndexError, TypeError):
            return None

    if parts[1] in NOTE_SOURCE_FIELDS:
        return resolve(observation, tokens)
    candidates = [source for root in sorted(NOTE_SOURCE_FIELDS)
                  if (source := resolve(observation.get(root), tokens)) is not None]
    return candidates[0] if len(candidates) == 1 else None


def note_revision_errors(observation, response):
    """Check evidence bindings and actual editing, never infer clinical approval."""
    if response.get('decision') != 'revise':
        return []
    current = (observation.get('content') or {}).get('text')
    revised = (response.get('content') or {}).get('text')
    errors = []
    if isinstance(current, str) and isinstance(revised, str) and current.split() == revised.split():
        errors.append('A revise decision must change the current note text; identical or whitespace-only text is not a revision. Reassess the current version without assuming it must be accepted.')
    evidence = response.get('revision_evidence')
    if not isinstance(evidence, dict):
        return errors + ['A revise decision requires revision_evidence bound to the CURRENT note and a supplied visible source.']
    kind, quote = evidence.get('issue_type'), evidence.get('current_text_quote')
    if kind not in {'incorrect_text', 'unsupported_claim', 'omission'}:
        errors.append('revision_evidence.issue_type is invalid.')
    elif kind == 'omission':
        if quote != '':
            errors.append('An omission uses an empty current_text_quote and a visible source excerpt.')
    elif not isinstance(quote, str) or not quote or not isinstance(current, str) or quote not in current:
        errors.append('current_text_quote is not an exact nonempty substring of CURRENT content.text; source records and previous note versions are not the review target.')
    field, excerpt = evidence.get('source_field'), evidence.get('source_excerpt')
    source = visible_note_source(observation, field)
    if source is None:
        errors.append('source_field must name an available supplied source without future information. Use a fully qualified path such as hospital_history.queried_pages[0].page.records[0].record; a relative path is accepted only under one unambiguous permitted source root.')
    elif not isinstance(excerpt, str) or (not excerpt and kind != 'unsupported_claim'):
        errors.append('A nonempty source_excerpt is required except for unsupported_claim.')
    elif excerpt:
        def nodes(value):
            yield value
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
            for item in children:
                yield from nodes(item)
        values = list(nodes(source))
        rendered = [text for value in values for text in
                    ([value] if isinstance(value, str) else []) +
                    [json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=False, separators=(',', ':'))]]
        matched = any(excerpt in value for value in rendered)
        if not matched:
            try:
                literal = json.loads(excerpt)
                canonical = json.dumps(literal, ensure_ascii=False, sort_keys=True)
                matched = any(json.dumps(value, ensure_ascii=False, sort_keys=True) == canonical for value in values)
            except (ValueError, TypeError):
                pass
        if not matched:
            errors.append('source_excerpt does not occur in the selected visible source field. Copy a literal substring or the exact JSON value from that field; paraphrases belong in the explanation, not in source_excerpt.')
    return errors


def contract_for(role, action):
    if action not in ROLE_ACTIONS.get(role, ()):
        raise ValueError("action is not assigned to role: " + str(role) + "/" + str(action))
    return {"policy_version": POLICY["version"], "request_action": action, **deepcopy(CONTRACTS[action])}


def shape_errors(role, action, response):
    contract = contract_for(role, action)
    if not isinstance(response, dict):
        return ["response must be a JSON object"]
    errors = []
    response_action = response.get("action")
    # Review decisions are unambiguous without an action key, but an explicit
    # wrong action remains invalid. Generative actions require their action key.
    allowed_actions = contract.get("response_actions")
    if allowed_actions and (response_action is not None or "allowed_decisions" not in contract):
        if response_action not in allowed_actions:
            errors.append("action must be one of " + ",".join(allowed_actions))
    if (contract.get("allowed_decisions") and response_action not in contract.get('information_actions',[])
            and response.get("decision") not in contract["allowed_decisions"]):
        errors.append("decision must be one of " + ",".join(contract["allowed_decisions"]))
    def text_field(obj, name):
        return isinstance(obj, dict) and isinstance(obj.get(name), str) and bool(obj[name].strip())
    if action == 'resolve_pending_care':
        if response_action not in {'ask','read_history'} and not text_field(response,'reason'):
            errors.append('Pending care routing requires the physician reason')
        if response_action == 'resume_assessment' and not text_field(response,'work'):
            errors.append('The physician must name the actual clinical assessment to perform')
        if response_action in {'request_medication_reconciliation','request_investigation'}:
            issues=response.get('issues')
            if not isinstance(issues,list) or not issues or any(not isinstance(s,str) or not s.strip() for s in issues):
                errors.append('A service request requires specific nonempty issues')
        if 'prescription' in response:
            errors.append('Care routing cannot bypass the assigned prescribing condition')
    if action == 'assess_pending_care':
        if 'prescription' in response:
            errors.append('Focused assessment cannot bypass the assigned prescribing condition')
        if response_action == 'assessment_completed':
            for field in ('assessment', 'plan', 'evidence_basis'):
                if not text_field(response, field):
                    errors.append('Focused assessment requires ' + field)
            for field in ('resolved_issues', 'unresolved_issues'):
                if not isinstance(response.get(field), list) or any(not isinstance(s, str) for s in response[field]):
                    errors.append('Focused assessment requires a string list: ' + field)
    if action == 'resolve_diagnosis_coding':
        diagnoses=response.get('diagnoses')
        if (not isinstance(diagnoses,list) or not diagnoses or any(not isinstance(d,dict)
                or not all(text_field(d,k) for k in ('code','display','system'))
                or type(d.get('primary')) is not bool or d.get('certainty') not in {'working','confirmed'}
                for d in diagnoses)):
            errors.append('Coding resolution requires explicit structured diagnoses')
        elif sum(d['primary'] for d in diagnoses)!=1:
            errors.append('Coding resolution requires exactly one primary diagnosis')
        if not text_field(response,'reason'):
            errors.append('Coding resolution requires a reason')
    if action == "answer" and not text_field(response, "text"):
        errors.append("answer requires nonempty text")
    if response_action == "ask" and not text_field(response, "question"):
        errors.append("ask requires a nonempty question")
    if action == 'revise_prescription' and response.get('decision') == 'revise' and not text_field(response, 'reason'):
        errors.append('A prescription revision requires reason explaining how the review was addressed.')
    needs_content = action in {"document", "prepare_consultation", "write_brief"} or response_action == "summary" or (action in {"review_note", "finalize_note"} and response.get("decision") == "revise")
    if needs_content and not text_field(response.get("content"), "text"):
        errors.append("content must be an object with nonempty text")
    if action == 'finalize_note':
        if not text_field(response, 'reason'):
            errors.append('Note finalization requires the physician rationale.')
        if response.get('decision') == 'revise' and type(response.get('finalized')) is not bool:
            errors.append('A corrected note requires an explicit finalized boolean.')
    if response_action == "read_history" and not text_field(response, "section"):
        errors.append("read_history requires a section string")
    if action == 'prepare_consultation' and (not isinstance(response.get('questions_to_verify'), list)
            or any(not isinstance(x, str) for x in response['questions_to_verify'])):
        errors.append('questions_to_verify must be a list of strings')
    if response_action == 'assessment_complete':
        if not all(text_field(response, field) for field in ('assessment', 'plan')):
            errors.append('assessment_complete requires assessment and plan')
        if not isinstance(response.get('diagnoses'), list):
            errors.append('assessment_complete requires an explicit diagnoses list')
        if 'prescription' in response:
            errors.append('assessment_complete precedes prescribing and must not contain a prescription')
    deferred_finalization = action == 'finalize_prescription' and response_action == 'defer'
    finalization_result = action == 'finalize_prescription' and response_action not in {'ask', 'read_history'}
    if deferred_finalization and response.get('decision') != 'defer':
        errors.append('An explicit defer action requires decision=defer')
    if response_action in {'prescription_draft', 'prescription_advice', 'prescription_finalized'} or finalization_result:
        key = 'suggested_prescription' if response_action == 'prescription_advice' else 'prescription'
        rx = response.get(key)
        if not isinstance(rx, dict) or not isinstance(rx.get('items'), list):
            errors.append(key + ' must contain an explicit items list')
        elif any(not isinstance(item, dict) or any(item.get(field) in (None, '') for field in
                 ('drug_code', 'dose', 'dose_unit', 'route', 'frequency', 'quantity', 'quantity_unit'))
                 for item in rx['items']):
            errors.append('Each medication item requires its explicit product, dose, unit, route, frequency and quantity fields')
        if not text_field(response, 'reason'):
            errors.append(('prescription finalization' if finalization_result else response_action) + ' requires a reason')
        if finalization_result and isinstance(rx, dict):
            if response.get('decision') in {'no_prescription', 'defer'} and rx.get('items') != []:
                errors.append('no_prescription or defer requires items=[]')
            elif response.get('decision') not in {'no_prescription', 'defer'} and rx.get('items') == []:
                errors.append('an empty final prescription requires decision=no_prescription or defer')
        if response_action == 'prescription_advice' and (not isinstance(response.get('issues'), list)
                or any(not isinstance(x, str) for x in response['issues'])):
            errors.append('prescription advice requires issues as a list of strings')
    note_content = response.get('content') if isinstance(response.get('content'), dict) else {}
    if action == 'dictate_addendum' and not isinstance(note_content.get('text'), str):
        errors.append('clinical_addendum.content.text must be a string; empty means no supplement needed')
    if action in {'write_brief', 'dictate_addendum'}:
        sequences = note_content.get('source_sequences')
        if not isinstance(sequences, list) or any(type(n) is not int for n in sequences):
            errors.append(action + '.content.source_sequences must be a list of integers')
    if action == 'finalize_care_plan':
        if response.get('disposition') not in {'no_medication','further_assessment','in_hospital_review'}:
            errors.append('care_plan requires an explicit non-dispensing disposition')
        for field in ('reason','withheld_treatment','patient_instructions'):
            if not text_field(response,field): errors.append('care_plan requires '+field)
        steps = response.get('next_steps')
        if not isinstance(steps,list) or not steps or any(not all(text_field(step,f) for f in
                ('action','responsible_role','timing')) for step in steps):
            errors.append('next_steps requires explicit action, responsible_role and timing')
        unresolved = response.get('unresolved_information')
        if not isinstance(unresolved,list) or any(not isinstance(s,str) for s in unresolved):
            errors.append('unresolved_information must be an explicit list')
    if action=='reconcile_medications' and response_action=='reconciliation_complete':
        if not text_field(response.get('content'),'text'):
            errors.append('reconciliation requires explicit content.text')
        meds=response.get('reconciled_medications')
        if not isinstance(meds,list) or any(not text_field(m,'name') or not text_field(m,'evidence_basis')
                or m.get('status') not in {'reported_current','historical_only','uncertain'} for m in meds):
            errors.append('reconciled_medications requires names, evidence and current/historical/uncertain status')
        allergy=response.get('allergy_assessment')
        if not text_field(allergy,'basis') or allergy.get('status') not in {'none_known','allergy_reported','unable_to_ascertain'}:
            errors.append('reconciliation allergy assessment requires a status and basis')
        for field in ('resolved_issues','unresolved_issues'):
            if not isinstance(response.get(field),list) or any(not isinstance(v,str) for v in response[field]):
                errors.append(field+' must be an explicit list')
    if response_action == "consultation_complete" or (action == "revise_prescription" and response.get("decision") == "revise"):
        rx = response.get("prescription")
        if not isinstance(rx, dict) or not isinstance(rx.get("items"), list):
            errors.append("prescription must be an object with an items list")
    if response_action in {'consultation_complete', 'assessment_complete', 'prescription_finalized'}:
        for field,states in [('medication_reconciliation',{'complete_declared_episode','partial','not_assessed'}),
                             ('allergy_assessment',{'none_known','allergy_reported','unable_to_ascertain'})]:
            # Old frozen fixtures may omit the new field. Omission remains an
            # absent assessment; the runtime never fills it as complete/negative.
            if response_action == 'assessment_complete' and field not in response:
                errors.append('assessment_complete requires an explicit ' + field)
            if field in response:
                assessment=response[field]
                if not isinstance(assessment,dict) or assessment.get('status') not in states or not text_field(assessment,'basis'):
                    errors.append(field+' requires an explicit allowed status and nonempty basis')
                elif field=='medication_reconciliation' and (not isinstance(assessment.get('unresolved'),list)
                        or not all(isinstance(x,str) for x in assessment['unresolved'])):
                    errors.append('medication_reconciliation.unresolved must be a list of strings')
    # Drug/diagnosis choices, dose correctness, source truth and reviewer decisions
    # belong to clinical/business evaluation; this layer never changes them.
    return errors


def empty_finalization_format_error(response, action):
    return (action == 'finalize_prescription' and isinstance(response, dict)
            and response.get('action') == 'prescription_finalized'
            and isinstance(response.get('prescription'), dict)
            and response['prescription'].get('items') == []
            and response.get('decision') not in {'accept', 'revise', 'no_prescription',
                                                'defer', 'reject', 'retain_rejected'})


def schema_correction_allowed(response, action=None):
    if not isinstance(response, dict):
        return False
    if empty_finalization_format_error(response, action):
        return True
    if response.get("decision") in {"reject", "defer", "retain_rejected"}:
        return False
    prescription = response.get("prescription")
    if isinstance(prescription, dict) and prescription.get("items") == []:
        return False
    return True


def normalize_structure(role, action, response):
    """Move declared fields without changing values or choosing conflicting copies."""
    if (role == 'doctor' and action == 'prepare_consultation'
            and isinstance(response, dict) and response.get('action') == 'report_read'
            and isinstance(response.get('content'), dict)
            and 'questions_to_verify' in response['content'] and 'questions_to_verify' not in response):
        canonical = deepcopy(response)
        canonical['questions_to_verify'] = canonical['content'].pop('questions_to_verify')
        if not shape_errors(role, action, canonical):
            return canonical, [{'from': '/content/questions_to_verify', 'to': '/questions_to_verify'}]
    fields = ('reconciled_medications', 'allergy_assessment', 'resolved_issues', 'unresolved_issues')
    if (role != 'pharmacist' or action != 'reconcile_medications'
            or not isinstance(response, dict) or set(response) != {'action', 'content'}
            or response.get('action') != 'reconciliation_complete'
            or not isinstance(response.get('content'), dict)
            or set(response['content']) != {'text', *fields}):
        return deepcopy(response), []
    canonical = deepcopy(response)
    for field in fields:
        canonical[field] = canonical['content'].pop(field)
    if shape_errors(role, action, canonical):
        return deepcopy(response), []
    # A reversible field move is stricter than merely passing the schema.
    restored = deepcopy(canonical)
    for field in fields:
        restored['content'][field] = restored.pop(field)
    if restored != response:
        raise ValueError('Response container normalization changed a value')
    return canonical, [{'from': '/content/' + f, 'to': '/' + f} for f in fields]
