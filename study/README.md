# Clinical workflow design

TwinHospital crosses three consultation conditions, three prescribing conditions and three documentation conditions. The full design assigns every source case to each of the 27 combinations. In every assignment, the same treating physician follows the patient from consultation through the final note. Each assignment has a fresh encounter and separate role contexts.

## Consultation

| Condition | Workflow |
| --- | --- |
| A1 · Direct consultation | The physician interviews the patient directly. |
| A2 · Report before consultation | The patient completes AI intake. The physician reads the report before starting the consultation. |
| A3 · Report during consultation | The patient completes AI intake. After the standard opening exchange, the physician chooses a follow-up question, hears the patient's answer, then reads the report. |

Both assisted conditions use the standard opening followed by a physician-selected question. The intake report is available at that choice in A2 and is released after the answer in A3.

## Prescribing

| Condition | Workflow |
| --- | --- |
| B1 · Physician decision | The physician selects a nonempty prescription after reviewing the eligible evidence and signs it. |
| B2 · AI draft | AI proposes a regimen using the available evidence. The treating physician selects and signs the prescription. |
| B3 · AI feedback | The physician proposes a regimen for AI review, considers the feedback, and signs the prescription. |

All three conditions use one prescribing decision and the same hospital formulary. The pharmacist reviews every signed version. The same physician resolves the concerns on each return, validates the complete prescription, and signs it again. OpenHIS links the signatures and reviews and verifies the current approved regimen. [Condition definitions](../simulator/sandbox/experiments/linked/scenario_design.py)

## Documentation

| Condition | Workflow |
| --- | --- |
| C1 · Physician writing | The physician writes and signs the note. |
| C2 · Physician dictation | After prescribing, the physician dictates a summary of the consultation. AI drafts the note from that summary and the permitted structured records. The physician edits and signs. |
| C3 · Encounter transcript | AI drafts the note from the complete physician–patient dialogue, the same permitted records and any exam findings or decisions the physician adds after the consultation. The physician edits and signs. |

### Documentation inputs

The documentation tool in C2 receives only the physician's dictation and the permitted chart fields. C3 gives the tool the complete consultation transcript and the same chart fields. The physician can add an examination finding or final decision that was not spoken aloud. The addendum can be empty. Both workflows model accurate transcription using text.

The simulator records the physician's dictation (`clinical_brief`, workflow `brief_to_note`) or addendum (`clinical_addendum`, workflow `postvisit`), the source policy (`documentation_source_policy`), draft versions and the signed note. Dictation, supplementation, review and editing contribute to physician workload.

Gładysz et al. (2026, workflows 3–4) describe clinician dictation and full-encounter recording as separate workflows. Palm et al. (2025) evaluate the quality of initial clinical notes.

## Matched comparisons

One assignment is one source case under one three-stage configuration. Stage comparisons pair the same source case and hold the other two conditions fixed. For example, compare A2–B3–C1 with A3–B3–C1 to see how the timing of the intake report changes the physician's questions and any later rework.

In the ED design, the physician records the disposition after the approved order is signed and before finalizing the note. Completion requires the same encounter's signed medication orders and locked ED note with the physician's disposition. After closure, the evaluator measures coverage of the documented primary-condition ingredients and agreement with the observed ED destination. All configurations share the same source results. Workload includes physician lookups and excludes specimen processing and report turnaround. Same-hospital admission ends at the ED handoff. [Reported outcomes](../results/README.md)
