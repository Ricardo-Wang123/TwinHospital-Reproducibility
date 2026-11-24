# TwinHospital

TwinHospital is a hospital simulation framework that combines virtual hospital modeling and multi-agent simulation with the data flows of a running hospital information system (HIS). The emergency department (ED) study follows each selected visit in linked MIMIC-IV and MIMIC-IV-ED records through prescribing, signed documentation and the recorded ED disposition. CAMEL coordinates the clinical roles and an isolated OpenHIS instance stores their signed outputs.

## Structure

- **HIS data flow.** Case preparation supplies the patient and chart views that initialize an encounter. OpenHIS records the transactions generated during the encounter.
- **Hospital simulation.** The patient, physician, and pharmacist use their permitted views of the case. The simulator passes the resulting reports, dialogue, and decisions between stages and submits hospital operations to OpenHIS.
- **Optimization and evaluation.** The experiment varies AI assistance across clinical stages. The audit connects each assignment's trace to its OpenHIS records, and the report groups the measures by workflow condition.

## Tools and subpackages

| Subpackage | Contents |
| --- | --- |
| [data/](data/) | Case preparation, input formats, and instructions for obtaining the source data. |
| [simulator/](simulator/) | [CAMEL roles](simulator/agents/), AI tools, configuration templates, and experiment commands. |
| [openhis/](openhis/) | Hospital runtime, clinical catalogues, deployment, and interfaces. |
| [study/](study/) | Workflow conditions and matched comparison design. |
| [results/](results/) | Outcome measures and report generation. |

## Emergency department visit

1. Select the index ED visit and freeze three separate views. The patient view contains source-reported facts, the physician starts with prior history and triage, and the evaluator holds the documented primary-condition regimen and disposition. During consultation, the physician can request recorded results from the same ED stay.
2. Register the encounter. Cross the three consultation conditions with the three prescribing and three documentation conditions. Each configuration gets its own encounter and role memory.
3. After reviewing the eligible chart evidence, the treating physician signs one nonempty prescription selected from the hospital formulary. The pharmacist reviews it. An approval can carry monitoring advice. An explicit return goes to the same physician for revision within the same decision.
4. The same physician chooses the ED disposition, reviews the clinical note and signs it. Completion requires matching readbacks of the prescription and locked note, including the disposition, for this encounter.
5. After closure, compare the final pharmacy-approved ingredient set with the documented regimen for the primary ED condition. Compare the signed disposition with the source disposition. Report encounter completion and historical agreement separately.

[Data and source boundaries](data/README.md) · [Workflow conditions](study/README.md) · [Run the ED comparison](simulator/README.md#run-an-experiment) · [Outcomes](results/README.md)

## Workflow comparison

The `emergency-v1` design crosses three choices at each of the consultation, prescribing, and documentation stages. The full comparison assigns **27 encounters per source case**. For a given case, the encounters share its starting inputs and model settings. Each encounter has fresh agent contexts and separate OpenHIS records.

| Stage | Conditions |
| --- | --- |
| Consultation | Direct physician interview, AI intake report read before consultation, or AI intake report read during consultation. |
| Prescribing | Physician decision, AI proposal for physician review, or physician proposal with AI feedback. |
| Documentation | Physician-written note, AI draft from physician dictation, or AI draft from the encounter dialogue and physician addendum. |

To compare a choice at one stage, pair encounters from the same case with the other two stages held fixed. The comparison examines recorded clinical artifacts, modeled workload, and verified encounter completion.

## Prepare your own dataset

1. **Author one case JSON per source patient.** Record the case identity, partition, and provenance. Define what the patient can report and what the clinician can see in the chart. For an index ED encounter, build the physician’s opening chart from evidence available at that point in the visit.
2. **Define the formulary and evaluation references.** Map medications and diagnoses to the OpenHIS catalogues. Freeze the independent ED outcome reference separately from the agent inputs before running the comparison.
3. **Pack and run the dataset.** List the case files in a manifest and freeze the inputs. Configure the role and tool models, run the assigned encounters, and generate the report after auditing the OpenHIS records. [Run an experiment](simulator/README.md#run-an-experiment)

## Study dataset

The [case preparation guide](data/README.md#index-ed-encounter) describes the hospital tables, time cutoff, opening summary and private outcome reference.

## Reproducibility and review

The software archive contains the simulator, OpenHIS deployment assets, case preparation, reporting and review tooling. Use the fictional example for an initial installation check. For a credentialed study, obtain the source data through PhysioNet and prepare the cohort in the authorized environment. The primary-condition prescription and disposition in the source serve as historical comparisons. Clinical judgments about alternatives can be collected independently using the clinician review protocol.

[Run the fictional example](simulator/README.md#fictional-example) · [Review the implementation](study/REVIEW.md) · [Data access](data/README.md)
