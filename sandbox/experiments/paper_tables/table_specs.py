"""Fixed paper row/column definitions (TwinHospital PDF pages 7–16)."""

VERSION = "paper-tables-2026-10-25.2"
MODULES = ("chief_complaint", "symptom_progression", "past_medical_history", "medication_records")
PATIENT_ITEMS = (
    ("ease_of_communication", "Ease of communication"),
    ("physician_attentiveness", "Physician attentiveness"),
    ("feeling_respected", "Feeling respected"),
    ("overall_satisfaction", "Overall satisfaction"),
    ("willingness_to_use_again", "Willingness to use the workflow again"),
)
CLINICIAN_ITEMS = (
    ("supports_clinical_judgment", "Supports clinical judgment"),
    ("facilitates_patient_physician_communication", "Facilitates patient–physician communication"),
    ("helps_reduce_workload", "Helps reduce workload"),
)
RX_ROWS = (
    ("prescription:pprs_baseline", "Rule-based PPRS (status quo)"),
    ("prescription:sequential:medical", "Rule + AI Sequential (Medical)"),
    ("prescription:sequential:foundation", "Rule + AI Sequential (Foundation)"),
    ("prescription:parallel:medical", "Rule + AI Parallel (Medical)"),
    ("prescription:parallel:foundation", "Rule + AI Parallel (Foundation)"),
)
PRE_ROWS = (
    ("preconsult:nurse_only", "Nurse-only baseline"),
    ("preconsult:ai_only:medical", "AI-only (Medical)"),
    ("preconsult:ai_only:foundation", "AI-only (Foundation)"),
    ("preconsult:ai_nurse_review:medical", "AI + Nurse Review (Medical)"),
    ("preconsult:ai_nurse_review:foundation", "AI + Nurse Review (Foundation)"),
)
NOTE_ROWS = (
    ("documentation:manual_documentation", "Fully manual baseline"),
    ("documentation:realtime:medical", "Real-time Collaborative + Medical Model"),
    ("documentation:realtime:foundation", "Real-time Collaborative + Foundation Model"),
    ("documentation:postvisit:medical", "Post-visit Generation + Medical Model"),
    ("documentation:postvisit:foundation", "Post-visit Generation + Foundation Model"),
)
PRE_EFFORT_ROWS = (
    ("preconsult:nurse_only", "Nurse-only collection"),
    ("preconsult:ai_only:foundation", "Patient-operated AI collection"),
    ("preconsult:ai_nurse_review:foundation", "AI collection with nurse review"),
)
SAFETY_ROWS = (
    ("patient_bill", "Patient–record mismatch"),
    ("missing_encounter", "Missing required linkage"),
    ("locked_note", "Invalid state transition"),
    ("unauthorized_signatory", "Invalid signing authorization"),
    ("duplicate_payment", "Duplicate transaction"),
)
PRE_COLUMNS = (
    ("completeness", "Completeness Rate (%)"), ("omission", "Omission Rate (%)"),
    ("factual_error", "Factual Error Rate (%)"), ("risk_recall", "High-Risk Signal Recall (%)"),
    ("handoff_usability", "Hand-off Usability (%)"),
)
NOTE_COLUMNS = (("accuracy", "Factual Accuracy (%)"), ("unsupported", "Unsupported Content Rate (%)"),
                ("omission", "Key Information Omission Rate (%)"))
PRE_EFFORT_COLUMNS = (
    ("human_time", "Total Human Time per Valid Task (min)"),
    ("nurse_time", "Nurse Time per Valid Task (min)"),
    ("physician_time", "Physician Time per Valid Task (min)"),
    ("redundant_time", "Redundant Inquiry Time (min/case)"),
    ("first_pass", "First-pass Completion (%)"),
)
NOTE_EFFORT_COLUMNS = (
    ("human_time", "Total Human Time per Valid Note (min)"),
    ("physician_time", "Attending Physician Time (min)"),
    ("staff_time", "Documentation Staff Time (min)"),
    ("rework_rounds", "Draft Rework Rounds"), ("rework_time", "Draft Rework Time (min)"),
    ("turnaround", "Note Turnaround Time (min)"), ("first_draft", "First-draft Acceptance (%)"),
)


def spec(number):
    """Return display headers and stable identifiers without adding CSV columns."""
    if number == 1:
        return "Task", (("prescription", "Prescription review"), ("preconsult", "Pre-consultation"),
                        ("documentation", "Medical note documentation")), (
                            ("anchors", "Real anchor cases"), ("tests", "Synthetic cases paired with hospital"))
    if number == 2:
        return "Evaluation set", (("end_to_end", "End-to-end execution"),
                                  ("local_injection", "Controlled error injection")), (("cases", "Cases"),)
    if number == 3:
        return "Evaluation group", (("clinician", "Clinicians"), ("patient", "Patients")), (("participants", "Participants"),)
    if number == 4:
        return "Configuration", RX_ROWS, (("pass", "Pass Rate (%)"), ("severe_miss", "Severe Miss (%)"),
            ("fpr", "FPR (%)"), ("recall", "Recall (%)"), ("precision", "Precision (%)"), ("f1", "F1 (%)"))
    if number in (5, 6):
        return "Configuration", PRE_ROWS, PRE_COLUMNS
    if number == 7:
        return "Configuration", PRE_ROWS, tuple(zip(MODULES, ("Chief Complaint (%)", "Symptom Progression (%)",
            "Past Medical History (%)", "Medication Records (%)")))
    if number in (8, 9):
        return "Configuration", NOTE_ROWS, NOTE_COLUMNS
    if number in (10, 11):
        return "Workflow", PRE_EFFORT_ROWS, PRE_EFFORT_COLUMNS
    if number in (12, 13):
        rows = ((NOTE_ROWS[0][0], "Fully manual documentation"),) + NOTE_ROWS[1:]
        return "Configuration", rows, NOTE_EFFORT_COLUMNS
    if number == 14:
        return "Configuration", (("end_to_end", "TwinHospital agents + sandbox"),), (
            ("cases", "Cases"), ("correct", "Correct Completion (%)"), ("unsafe", "Unsafe Execution (%)"),
            ("timeout", "Timeout (%)"), ("mean", "Mean Completion Time (s)"), ("p95", "P95 Completion Time (s)"))
    if number == 15:
        return "Error Category", SAFETY_ROWS, (("prevention", "Sandbox Prevention n (%)"),
            ("downstream", "Downstream Safe Handling (n)"), ("unsafe", "Unsafe Execution (n)"),
            ("unresolved", "Unresolved (n)"), ("false_blocks", "False Blocks n (%)"))
    if number == 16:
        return "Dimension", PATIENT_ITEMS, (("existing", "Existing Workflow Mean"), ("human_ai", "Human–AI Workflow Mean"))
    if number == 17:
        return "Item", tuple((item + ":" + workflow, label) for item, label in CLINICIAN_ITEMS
                             for workflow in ("existing", "human_ai")), (
            ("workflow", "Workflow"), ("score_1", "Strongly Disagree (%)"), ("score_2", "Disagree (%)"),
            ("score_3", "Neutral (%)"), ("score_4", "Agree (%)"), ("score_5", "Strongly Agree (%)"), ("mean", "Mean"))
    raise ValueError(f"unknown paper table {number}")
