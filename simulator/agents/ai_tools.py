"""Optional AI assistance for intake, prescribing and note drafting."""

from .base_agent import BaseAgent, DOSE_HISTORY, HISTORY_SUMMARY, NOTE_EVIDENCE, PRESCRIPTION_REVIEW

INTAKE_INSTRUCTIONS = (
    "Start with a short open question, then group related follow-up questions. "
    "For collection, aim for a few useful exchanges. Stop once there is a usable handoff "
    "covering the reason for visit, material symptoms and relevant medication or allergy concerns. "
    "Ask in the patient's language and use elicited_facts to avoid repeating answered questions. "
    "To finish, select the elicited fact IDs for the report in order and identify risk flags by fact ID. "
    "The report uses the original values, including time spans, quantities and explicit denials. "
    "A question or an unanswered item supplies no patient finding. "
    "Ask when the last relevant dose was taken in hours before this visit if that timing matters. "
)

DRAFTING_INSTRUCTIONS = (
    "For action=document, generate the initial full note and return content.text. "
    "The treating physician reviews the draft. Attribute patient statements and clinician assessments. "
    "When input_mode is physician_dictation_and_clinical_records, draft from clinical_brief using only "
    "the supplied clinical records. The full dialogue is intentionally unavailable in that mode; "
    "do not reconstruct missing symptoms, examination findings or conversations. "
    "When input_mode is encounter_transcript_and_physician_addendum, draft from the actual "
    "complete physician-patient dialogue, clinical_addendum and supplied clinical records. "
    "The addendum is the treating physician's postencounter dictation, not a new patient interview. "
    "Both input modes use text with accurate transcription assumed; do not invent speech-recognition errors. "
)


class IntakeTool(BaseAgent):
    role = 'preconsult_ai'
    instructions = HISTORY_SUMMARY + INTAKE_INSTRUCTIONS


class PrescriptionTool(BaseAgent):
    role = 'rx_ai'
    instructions = DOSE_HISTORY + PRESCRIPTION_REVIEW


class DocumentationTool(BaseAgent):
    role = 'scribe'
    instructions = DRAFTING_INSTRUCTIONS + NOTE_EVIDENCE
