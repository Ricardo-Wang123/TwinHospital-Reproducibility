# Clinical records and exchange formats

TwinHospital supports published historical records from **MIMIC-IV on FHIR 2.1**. The release maps MIMIC-IV 2.2 and MIMIC-IV-ED 2.2 to HL7 FHIR R4 (4.0.1). Its implementation guide is version 1.3.0. Each simulation has its own encounter and signed outputs. The index ED study builds its opening charts from the ED and hospital CSV tables. [Dataset and table-level mapping](https://physionet.org/content/mimic-iv-fhir/2.1/) · [Mapping paper](https://doi.org/10.1093/jamia/ocad002) · [Versioned conversion SQL](https://github.com/kind-lab/mimic-fhir/tree/v2.1.0) · [BibTeX](MIMIC_FHIR.bib)

| Record | Published representation | TwinHospital use |
| --- | --- | --- |
| Patient, admissions, transfers, diagnoses and procedures | Patient, Encounter, Location, Condition and Procedure. ICD codes retain the source version. | The index ED arrival sets the cutoff for earlier completed encounters. Native diagnosis rank, when supplied, preserves within-admission coding order. [MIMIC hospital schema](https://mimic.mit.edu/docs/iv/modules/hosp/) |
| Laboratory and microbiology results | Observation and Specimen. Laboratory sampling and result release have separate times. | Queries return results available by the cutoff. A lab without an admission ID can still belong to the patient. [MIMIC FHIR mapping](https://physionet.org/content/mimic-iv-fhir/2.1/#description) |
| Historical drug orders, pharmacy records and administrations | Medication and MedicationRequest from prescriptions and orders, MedicationDispense from pharmacy, and MedicationAdministration from eMAR or ICU input events. | Orders, pharmacy records and administrations can be queried separately. An order alone cannot establish current use. [Medication profiles](https://physionet.org/content/mimic-iv-fhir/2.1/#description) |
| ICU and emergency care | ICU stays, charted measurements and interventions have profiles. ED stays, triage, vital signs and reported medication use have separate profiles. | These records are available when their source module is supplied. The physician's hospital-history view currently projects hospital encounters, diagnoses, procedures, labs, microbiology and medication records. [MIMIC-IV-ED](https://physionet.org/content/mimic-iv-ed/2.2/) |
| Historical free text and outpatient measurements | MIMIC-IV-Note releases discharge summaries and radiology reports. Native outpatient measurements appear in the hospital OMR table. | A scenario can retain native OMR readings as a separately labelled source. The simulated outpatient note uses the encounter's own conversation and clinical decisions. [MIMIC-IV-Note](https://physionet.org/content/mimic-iv-note/2.2/) · [OMR](https://mimic.mit.edu/docs/iv/modules/hosp/omr.html) |
| New outpatient prescription and signed note | HL7 R4 defines MedicationRequest and clinical documents as a document Bundle whose first resource is a Composition. | Each signed order item becomes a MedicationRequest. The final note, patient, visit, physician and linked prescriptions form one document, reconstructed from matching OpenHIS receipts. [MedicationRequest](https://hl7.org/fhir/R4/medicationrequest.html) · [Clinical documents](https://hl7.org/fhir/R4/documents.html) |


The ED cohort links the local hospital, ICU and ED releases. The patient archive uses a FHIR R4 collection Bundle. Its time-restricted opening chart is built from the native tables. Its structured opening chart is transported by the study's FHIR R4 operation. Its simulated final document contains a Composition, an ED Encounter with disposition and the signed MedicationRequest resources. The published MIMIC-IV-on-FHIR input route below applies when its separate FHIR distribution is supplied.

## Three records for one encounter

| When | Record | Fields to read or complete |
| --- | --- | --- |
| Before the doctor sees the patient | Opening patient summary | **Problems currently followed** — condition and evidence status. **Allergies and intolerances** — substance, reaction, chart status and any patient report. **Medication summary** — product, reported use, dose and route where recorded, and reconciliation status. Historical discharge diagnoses retain their admission and coding order. A previous working assessment retains its date and source. Historical investigations remain available alongside the summary. |
| When the physician prescribes | Prescription | Patient, encounter, prescriber, medication, directions (dose, unit, route, frequency and course when specified), dispensing quantity when recorded, order date, intent and status. Each signed item is one R4 MedicationRequest. Items from one prescription share a group identifier. |
| At the end of the encounter | Signed ED progress note | **Subjective** — reason for visit and the patient's history. **Objective** — available examination findings and test results, with their dates. **Assessment** — the physician's assessment or working diagnosis. **Plan** — medication decisions, investigations and follow-up. The ED note also records the treating physician’s disposition. The document links the patient, encounter, author, time and signed prescriptions. |

The physician receives the first record at `hospital_history.initial_view.opening_summary`. Its three core sections follow IPS 2.0.1 and use the corresponding LOINC codes. Empty sections preserve the source status. An undocumented allergy history, a patient's denial and a verified absence are distinct. A discharge code alone establishes neither current disease activity nor resolution. This is an IPS-based clinician display. The HL7 guide separately specifies the complete exchange document. [IPS sections](https://hl7.org/fhir/uv/ips/2.0.1/en/Structure-of-the-International-Patient-Summary.html) · [Complete IPS example](https://hl7.org/fhir/uv/ips/2.0.1/en/Bundle-bundle-minimal.html)

For charts with an explicit problem list, `his_initial.problem_list` accepts a source status, an optional `as_of_offset_hours`, and `items` with a display, status, source and optional coding. Summary entries carry references to the underlying chart. The display includes up to eight items per core section and three recent discharge diagnoses, subject to the chart's byte limits. Each section reports its eligible count and points to the full records.

All three documentation conditions request the same four headings. Their inputs remain specific to the condition: physician authorship, physician dictation, or the encounter transcript with a physician addendum. These headings are a study choice drawn from the US Progress Note profile. The signed R4 Composition preserves the physician-approved text. Formal exchange against the US profile requires its additional constraints. [MedicationRequest and examples](https://hl7.org/fhir/R4/medicationrequest.html) · [HL7 C-CDA on FHIR Progress Note](https://hl7.org/fhir/us/ccda/StructureDefinition-Progress-Note.html)

## Import a history

Run from the simulator root. Use either the credentialed, published profile NDJSON directory or the output of the authors' pinned SQL for a licensed source cohort. The source index is the private `provenance.json` produced with the original historical starter. It links the scenario's patient and anchor to the public mapping. Set `--source-timezone` to the timezone used to interpret MIMIC timestamps in the conversion (default UTC).

```sh
python -m handoff.mimic_fhir --resources "$MIMIC_FHIR" --case "$SCENARIO" --terminology "$MIMIC_IG_PACKAGE" \
  --source-index "$HISTORY/provenance.json" --hosp "$MIMIC_HOSP" --output "$NEW_CASE"
```

Set `MIMIC_IG_PACKAGE` to the extracted `package/` folder of the [MIMIC implementation guide](https://mimic.mit.edu/fhir/mimic/package.tgz). Native ICD wording is retained in the concept text when the guide uses a different display for the same code. The version 2 case stores a patient-scoped FHIR collection in `his_initial.fhir_history`. A private source index supplies native record availability, diagnosis rank and medication names where needed. The physician reads a dated chart view with paged queries. Missing evidence and constructed visit facts remain distinguishable. The patient and evaluator receive their own inputs. Eligibility rules, handoffs, working time and settlement are study definitions. The legacy outpatient workflow verifies payment. The ED workflow verifies the signed orders, locked note and documented disposition.

## Export a signed encounter

The FHIR gateway imports the historical collection as a resource parameter of the project-defined `$th-history-fhir-import` operation. It persists the history in the encounter's local study store. The physician's chart index and page queries run against that store. The ED route imports the source-structured chart via `$th-history-ed-chart-import`. Native OpenHIS persists the signed orders and locked ED note.

```sh
python -m handoff.fhir_visit --run "$RUN_JSON" --receipts "$NATIVE_JOURNAL" \
  --case "$CASE_JSON" --output "$SIGNED_DOCUMENT"
```

The export preserves the signed note text, product identifiers and prescriber. Each document represents the instant of note attestation. Run identifiers, timing estimates and payment receipts remain in the study record. Use the document to inspect what the simulated clinician signed. Its structure alone does not establish clinical appropriateness.
