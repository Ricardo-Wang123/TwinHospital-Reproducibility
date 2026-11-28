# Case data

## Index ED encounter

Each source patient contributes one emergency department (ED) stay. Link `ed.edstays`, `ed.triage` and `ed.diagnosis` by patient and ED stay, then link the corresponding hospital admission by patient and admission ID. The unique first-ranked **ED** diagnosis defines the disease group. The triage complaint is required. An earlier eligible stay may be selected when the last ED visit cannot supply the required evidence. [MIMIC-IV-ED 2.2](https://physionet.org/content/mimic-iv-ed/2.2/) · [MIMIC-IV 2.2](https://physionet.org/content/mimiciv/2.2/)

| Record | Source and intended use |
| --- | --- |
| Longitudinal patient record | All patient rows from the local hospital, ICU and ED tables, with the dictionary rows needed to interpret them. A visit index links each row to its ED stay, hospital admission or ICU stay when the source provides a link. Unassigned rows remain at patient level. |
| FHIR archive | Apply the MIMIC-IV on FHIR 2.1 mappings to the source data. Package the patient resources and their referenced shared resources as one R4 `Bundle` with `type: collection`. Native rows preserve source fields outside the published mappings. [MIMIC mapping](https://physionet.org/content/mimic-iv-fhir/2.1/) · [R4 Bundle](https://hl7.org/fhir/R4/bundle.html) |
| Opening view | The triage record supplies the presenting complaint and vital signs. The patient role receives separately attributed statements when available. In this cohort, the complaint is attributed to staff triage. The IPS-based physician summary includes medication reconciliation recorded by arrival and the simulation's fixed longitudinal allergy history. [Opening-summary sections](MIMIC_FHIR.md) |
| Patient medication recall | The patient can recall the documented discharge medication list from the latest prior encounter when it ended within 90 days. The 90-day window is a simulation parameter. Prior prescriptions retain their historical attribution. Medication names documented by arrival are available at opening. A later ED medication reconciliation becomes available to the patient and physician when that chart record is released. The same rules apply across consultation conditions. [MIMIC-IV-ED medication reconciliation](https://mimic.mit.edu/docs/iv/modules/ed/medrecon.html) · [Recall implementation](../simulator/sandbox/experiments/linked/patient_medications.py) |
| Later clinical evidence | All conditions use the same release times. The physician reviews home medication reconciliation as it is charted and investigation findings as they are reported. A result in a comments field retains its value, source column, and posting time. The prescription uses eligible records posted before ED departure. Historical encounters must have ended before arrival. Prior outpatient measurements retain their dates. [ED case preparation](../simulator/handoff/ed_workup.py) |
| Outcome reference | Link medication records and the discharge narrative for the first-ranked ED diagnosis. The evaluator receives the documented final primary-condition ingredient set and ED disposition in a separate file. Each distinct reference ingredient contributes once per encounter. |

The cohort has 100 encounters and 172 patient–ingredient pairs. Its 186 source principal-condition medication transactions are recorded as new orders. Each simulation has one prescribing decision using the available ED evidence. [Single-prescription case preparation](preparation/freeze_single_prescription.py)

The longitudinal archive is retrospective. The MAS opens the case-specific runtime view for the selected visit. The study uses the documented primary-condition regimen and ED destination as historical comparators. [Medication scoring](../simulator/handoff/emergency_results.py)

### Prepare the local study package

Obtain MIMIC-IV, MIMIC-IV-ED and MIMIC-IV-Note through PhysioNet. Keep the native records, extracted cases and evaluator references in the credentialed environment. The repository provides software and an independently fictional example. A shared prepared cohort requires an authorized PhysioNet distribution under the applicable agreement. [MIMIC-IV-Note 2.2](https://physionet.org/content/mimic-iv-note/2.2/) · [PhysioNet data-access policy](https://physionet.org/about/faqs/#download-and-reuse)

Build the time-linked cohort with [source preparation](preparation/freeze_ed_protocol.py) and apply [single-prescription preparation](preparation/freeze_single_prescription.py). The source input to these programs is a locally indexed native archive with linked stay IDs. The reviewed cohort selection and patient-level linkage remain subject to the source access requirements. Use the resulting manifest and the corresponding reference for the experiment.

For source-aligned ED replay, enable `--align-source-notes` during single-prescription preparation. Review the selected patient history and admission findings against the indexed MIMIC note before running. Dated records follow the source table order within the appropriate review phase. Undated admission findings follow their document order after the timed records and before prescribing. The physician receives the first ED diagnosis as the prescribing target and reads the findings as ordinary chart content. Original treatment and disposition remain on the evaluation side. Generate the clinician review materials from the same local cohort.

| Path within the local package | Contents |
| --- | --- |
| `patients/<case_id>/就诊索引.html` | Opening summary and longitudinal encounter index with links to the original table rows. |
| `patients/<case_id>/原表记录.json` · `FHIR资源.json` | Native patient data and the mapped FHIR collection. |
| `patients/<case_id>/开诊摘要.json` | The opening complaint and triage observations, the physician's runtime IPS-based summary, and dated historical evidence. |
| `manifest.json` · `cases/<case_id>.json` | Selected cases and their time-restricted MAS inputs. |
| `formulary_directory.json` · `catalog.json` · `products.sql` | Shared searchable products derived from MIMIC-IV prescriptions, native product mapping and OpenHIS fixture. The same source directory is used in every condition and in clinician review. |
| `history.private.sqlite` | Frozen index of other-patient histories for the assisted conditions. Cohort preparation excludes the study patients. The private input bundle records its checksum. |
| `reference.private.json` | Evaluator-only primary-condition ingredients, continuation/addition status, medication evidence, ED disposition and visit linkage. |

Run the packer from the simulator directory with this manifest, native product map and source product fixture. The packer resolves the shared directory beside the case files. [Pack the cases](../simulator/README.md#pack-the-cases)

## Runtime case JSON

Source-extracted ED cases use a structured version 1 chart and `encounter_setting: emergency`. Imports from the published MIMIC FHIR collections use version 2. [Published clinical formats and import](MIMIC_FHIR.md)

| Top-level field | Contents |
| --- | --- |
| `schema_version` | `twinhospital.linked.case/2` for MIMIC FHIR or `twinhospital.linked.case/1` for an existing chart |
| `case_id`, `patient_key` | Stable, opaque identifiers. Use the same patient key for the same source patient across manifests. A bundle has distinct cases and source patients. |
| `group`, `split`, `data_kind` | One of the five group IDs below, a declared split such as `engineering` or `test`, and explicit origin (`mimic_derived`, `hospital_derived`, or `synthetic_independent`). |
| `patient_initial` | Demographics as perceived by the patient, communication profile, and facts from the patient view. Each fact has a unique `fact_id` and status `reported`, `denied`, or `unknown`. Include a complaint only if the source records one. |
| `his_initial` | Authorized demographics, historical encounters and orders, optional administration, lab, microbiology and outpatient history, current observations, current medication-use and allergy records, and timing basis. |
| `formulary` | Inline products for synthetic cases. Indexed MIMIC ED cases use an empty array and the shared directory. |
| `formulary_directory` | Indexed ED cases carry the version and SHA-256 of the shared directory. During prescribing the actor searches this directory by name, formulation or code. |
| `evaluation`, `provenance` | Evaluator-only reference fields and annotations identifying which facts come from source records and which were constructed. |

Use `type2_diabetes`, `heart_failure`, `copd`, `pneumonia`, or `uti` as the group ID. The group identifies a sampling stratum. The clinician agent chooses a diagnosis from the evidence visible during the visit.

Separate current patient-reported facts, historical chart entries, and current simulated observations. Record a historical prescription in `his_initial.historical_orders` and current use in `his_initial.current_use_record`. Set the allergy status to `unknown` when the source is silent. Record no known medication allergies only when a source explicitly states this, with its date and provenance. Apply newly documented findings at their availability time. The patient agent receives `patient_initial`; other roles query the permitted chart and encounter records. The `evaluation` object is reserved for scoring and is excluded from the runtime case view. [HL7 IPS allergy status](https://www.hl7.org/fhir/uv/ips/STU1.1/CodeSystem-absent-unknown-uv-ips.html)

## Dataset manifest

The manifest is a JSON object with a top-level `cases` array. Each entry includes `path`, `case_id`, `patient_key`, `group`, `split`, and `data_kind`. Resolve `path` relative to the manifest's directory and copy the five metadata fields from the case JSON. Each case and source patient appears once.

The packer computes `file_sha256`, `case_hash`, and `runtime_input_hash` when it freezes the input. For one case, pass the case JSON directly with `--case`. [Pack the cases](../simulator/README.md#pack-the-cases)

For an approved hospital dataset use `hospital_derived`, set `provenance.uses_patient_records` to `true`, and name the source in `provenance.source_dataset`. Supply the endpoint's processing record when configuring the run.

### Fixed allergy history

[Allergy preparation](preparation/prepare_fixed_allergies.py) reads the credentialed Note extracts and locally indexed chart records. Explicit allergens are retained and aliases are merged when the formulary supports the same active ingredient. Source times and text are retained in the private preparation audit. A documented positive history takes precedence over a generic negative entry. The study supplied the longitudinal record at opening, including entries documented after the index visit. The two cases with no usable statement were assigned the declared simulation default. The same fixed facts are recorded in the patient view and physician chart for every condition. Runtime acceptance uses delivery and reading receipts.
