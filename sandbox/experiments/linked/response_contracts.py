"""Role/action JSON contracts and a bounded, format-only correction policy."""
from copy import deepcopy
import json
import re
from ..visibility import assert_visible

POLICY = {"version": "role_action_contracts_v17_output_boundary_correction", "max_schema_corrections": 1,
    "note_revision_evidence": "current_text_and_visible_source_excerpt_v1_no_noop_revision",
    "lossless_structure_normalization": "reconciliation_fields_content_to_root_v1",
    "prescription_bearing_consultation_requires_declared_structured_diagnosis_when_catalogue_present": True,
    "eligible": "parsed_shape_or_visible_output_boundary_error_or_completed_response_JSON_syntax_or_duplicate_key_error",
    "max_invalid_response_bytes": 40000,
    "syntax_correction_requires_finish_reason": "stop",
    "syntax_and_shape_corrections_share_one_attempt": True,
    "output_boundary_uses_same_single_correction_attempt": True,
    "syntax_correction_extracts_or_selects_json_fragments": False,
    "duplicate_key_value_selected": False,
    "malformed_response_reinjected_into_role_context": False,
    "same_role_model_and_clinical_input": True,
    "clinical_reject_defer_no_rx_retried": False,
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
    "preconsult_ai": ("collect",), "doctor": ("verify_handoff", "consult", "revise_prescription", "finalize_care_plan", "document", "review_note"),
    "pharmacist": ("review_prescription", "reconcile_medications"), "rx_ai": ("review_prescription",),
    "senior_pharmacist": ("adjudicate",), "scribe": ("document",),
}
CONTRACTS = {
    "answer": {"response_actions": ["answer"], "required_response": "{action:'answer',text:string,expressed_fact_ids:[memory IDs]}"},
    "collect": {"response_actions": ["ask", "summary"], "required_response": "{action:'ask',question:string} OR {action:'summary',content:{text:string,facts:[{fact_id,topic,value,status}],risk_flags:[string]}}"},
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
    "reconcile_medications": {"response_actions": ["ask", "read_history", "reconciliation_complete"], "required_response": "{action:'ask',question:string} OR {action:'read_history',section:string,offset:integer,limit:integer,term?:string} OR {action:'reconciliation_complete',content:{text:string},reconciled_medications:[{name:string,status:'reported_current'|'historical_only'|'uncertain',evidence_basis:string}],allergy_assessment:{status:'none_known'|'allergy_reported'|'unable_to_ascertain',basis:string},resolved_issues:[string],unresolved_issues:[string]}; this is a pharmacist assessment from visible evidence, not an approval, new source fact or completed test; use the declared remaining service question/query budgets"},
    "finalize_care_plan": {"response_actions": ["care_plan"], "required_response": "{action:'care_plan',disposition:'no_medication'|'further_assessment'|'in_hospital_review',reason:string,withheld_treatment:string,next_steps:[{action:string,responsible_role:string,timing:string}],patient_instructions:string,unresolved_information:[string]}; this records unfinished work inside this virtual hospital; do not transfer outside or claim workflow success without signed prescription, locked note and verified medication payment. Planned tests are not results."},
    "document": {"response_actions": ["draft"], "required_response": "{action:'draft',content:{text:string,source_sequences:[integer]}}; return the actual note text, never a review decision or acceptance of previous_draft"},
    "review_note": {"allowed_decisions": ["accept", "revise", "reject"], "required_response": "{decision:'accept'|'reject',reason:string} OR {decision:'revise',reason:string,revision_evidence:{issue_type:'incorrect_text'|'unsupported_claim'|'omission',current_text_quote:string,source_field:string,source_excerpt:string},content:{text:string,source_sequences:[integer]}}. A revision must change the current content.text. For incorrect_text or unsupported_claim, quote exact words from CURRENT content.text, not from consultation, transcript or an older version. An omission uses an empty current_text_quote. source_field names an available path rooted at transcript, hospital_history, handoff, current_encounter_measurements, allowed_source_scope, observed_business_state, clinical_disposition, consultation, or approved_prescription (for example hospital_history.queried_pages[0].page.records[0].record.actual_use). Copy the actual path from the supplied observation. A relative path is accepted only if exactly one of these roots contains it. source_excerpt quotes that selected field exactly; only unsupported_claim may use an empty source_excerpt to describe absence of support. Correct the cited issue in the returned full text. If the old issue is already resolved, judge the current note afresh; do not invent another issue or approve merely to end the loop."},
}


NOTE_SOURCE_FIELDS = frozenset({'transcript', 'hospital_history', 'handoff',
    'current_encounter_measurements', 'allowed_source_scope', 'observed_business_state',
    'clinical_disposition', 'consultation', 'approved_prescription'})


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
            errors.append('source_excerpt does not occur in the selected visible source field.')
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
    if action == "answer" and not text_field(response, "text"):
        errors.append("answer requires nonempty text")
    if response_action == "ask" and not text_field(response, "question"):
        errors.append("ask requires a nonempty question")
    needs_content = action == "document" or response_action == "summary" or (action == "review_note" and response.get("decision") == "revise")
    if needs_content and not text_field(response.get("content"), "text"):
        errors.append("content must be an object with nonempty text")
    if response_action == "read_history" and not text_field(response, "section"):
        errors.append("read_history requires a section string")
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
    if response_action=='consultation_complete':
        for field,states in [('medication_reconciliation',{'complete_declared_episode','partial','not_assessed'}),
                             ('allergy_assessment',{'none_known','allergy_reported','unable_to_ascertain'})]:
            # Old frozen fixtures may omit the new field. Omission remains an
            # absent assessment; the runtime never fills it as complete/negative.
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


def schema_correction_allowed(response):
    if not isinstance(response, dict):
        return False
    if response.get("decision") in {"reject", "defer", "retain_rejected"}:
        return False
    prescription = response.get("prescription")
    if isinstance(prescription, dict) and prescription.get("items") == []:
        return False
    return True


def normalize_structure(role, action, response):
    """Accept one unambiguous alternate container layout, never infer a value.

    Every named field must be present inside content and absent at the root.
    No extra keys, partial moves or conflicting copies are accepted. The caller
    retains both representations and the move list in its execution evidence.
    """
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
