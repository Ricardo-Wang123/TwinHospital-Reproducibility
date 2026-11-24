"""Coordinate intake, prescribing and documentation through the applicable encounter endpoint."""
from copy import deepcopy

from sandbox.experiments.linked.contracts import (
    ClinicalDecisionIncomplete,
    WorkflowFailure,
    validate_prescription,
)


def run_visit(execution, conditions):
    execution.register()
    execution.preconsult(conditions["preconsult"])
    resuming = False
    while True:
        try:
            if execution.prescribing_required and execution.ed_prescription_signed:
                signed = {'items': deepcopy(execution.prescription_business_state['items'])}
                approved = execution.finalize_ed_orders(execution.consultation_record, conditions['prescription'], signed)
                break
            consultation = execution.consult(resume=resuming)
            execution.record_care_decision(consultation)
            no_action = (getattr(execution, 'independent_order_actions', False)
                         and consultation.get('ed_prescription_reassessment', {}).get('order_actions') == [])
            if not consultation["prescription"]["items"] and not no_action:
                raise ClinicalDecisionIncomplete("physician issued no prescription; three-task chain incomplete")

            proposed = validate_prescription(consultation["prescription"]) if consultation["prescription"]["items"] else {'items': []}
            prescribing_input = {
                "prescription": deepcopy(proposed),
                "clinical_context": deepcopy(execution.clinical_context()),
            }
            execution.result["stage_inputs"].setdefault("prescription", deepcopy(prescribing_input))
            attempts = execution.result["artifacts"].setdefault("prescription_stage_attempts", [])
            attempts.append(deepcopy(prescribing_input))

            approved = execution.prescription(proposed, conditions["prescription"], revise=True)
            if approved is None:
                raise WorkflowFailure("prescription workflow returned without a terminal disposition")
            if execution.prescribing_required:
                approved = execution.finalize_ed_orders(execution.consultation_record, conditions['prescription'], approved)
            break
        except ClinicalDecisionIncomplete as issue:
            if execution.prescribing_required and execution.ed_prescription_signed:
                execution.event('ed_reassessment_outstanding', reason=str(issue))
                continue
            if execution.resolve_pending_care(issue):
                resuming = True
                continue
            if execution.prescribing_required:
                execution.return_unsigned_prescription(issue)
                resuming = True
                continue
            execution.document_clinical_disposition(conditions["documentation"], issue)
            raise

    consultation = deepcopy(execution.consultation_record)
    transcript = execution.physician_transcript()
    if execution.emergency:
        execution.decide_ed_disposition(transcript, consultation, approved)
    execution.result["stage_inputs"]["documentation"] = {
        "transcript": deepcopy(transcript),
        "handoff": deepcopy(execution.handoff),
        "consultation": deepcopy(consultation),
        "hospital_history": deepcopy(execution.history),
        "current_encounter_measurements": deepcopy(execution.current_measurement_panel),
        "observed_business_state": {"prescription": deepcopy(execution.prescription_business_state)},
        "approved_prescription": deepcopy(approved),
    }
    execution.documentation(conditions["documentation"], transcript, consultation, approved)
    if execution.emergency:
        execution.close_ed_encounter()
    else:
        execution.settle()
