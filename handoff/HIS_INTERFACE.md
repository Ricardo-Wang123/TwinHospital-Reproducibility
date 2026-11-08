# Hospital interface and encounter state

TwinHospital coordinates the agents, role views, workflow rules, and evidence. The isolated OpenHIS 2.0.5 backend executes the hospital operations and commits their records to its database. The archived cohort used the HTTP application endpoints shipped with the OpenHIS workbench. The adapter authenticates to OpenHIS with the assigned role, submits the request, and reads the resulting business state. Setup and independent audit access to the database are separate from the execution path.

Each source case can initialize independent encounter replicas. All configurations start with the same source history, constructed visit, formulary, and visibility rules. Every replica gets fresh agent contexts and native patient, encounter, and business identifiers. The saved handoff becomes a physician input after retrieval. A review or encoding issue returns the affected version to the responsible role. A signed order supplies the charge items used in simulated settlement. The trace connects each transition to the artifact or state that the next step uses. This defines the outpatient workflow twin. [Case format](CASE_FORMAT.md) · [Execution protocol](../deploy/openhis-experiment/PROTOCOL.md)

The queue, imported chart, dialogue, handoff, and review decisions live in TwinHospital. The native encounter, diagnoses, encodable orders, locked notes, and cash records live in OpenHIS. Frontend-only form checks and interaction behavior require a separate browser evaluation. The experiment uses local simulated cash and the declared operational constraints.

Figure 1 shows the role-scoped request to OpenHIS and the verified state read back into TwinHospital as separate arrows. [Vector figure source](../docs/figures/draw_linked_overview.py)

## FHIR R4 workflow operations

For new batches, `--his-interface fhir-r4` uses FHIR R4's HTTP operation protocol. A loopback bridge scoped to each assignment receives `application/fhir+json` at `POST [base]/$th-…`. `GET [base]/metadata` returns the `CapabilityStatement`. `GET [base]/OperationDefinition/{id}` describes the parameter names, types, and cardinalities for each operation. Requests carry `Parameters` with logical `Patient` and `Encounter` references and identifiers for the case, assignment, actor, and target objects. A JSON `Attachment` carries each structured artifact where specified. The response is a `Parameters` receipt with the observed read-back, or an `OperationOutcome` on rejection or error. Transport uncertainty requires reconciliation before a retry. [HL7 FHIR R4 operations](https://hl7.org/fhir/R4/operations.html) · [Bridge implementation](../sandbox/experiments/linked/fhir_his.py)

| Workflow step | FHIR R4 project operations | OpenHIS action and read-back |
| --- | --- | --- |
| Registration and reception | `$th-registration-create`, `$th-encounter-begin-consultation` | Create the patient and registration, then start reception and verify the bound encounter and physician. |
| Diagnosis | `$th-diagnosis-catalogue`, `$th-consultation-save` | Read the hospital diagnosis catalogue, save selected diagnoses, and verify the resulting diagnosis records. The consultation narrative and transcript stay with the simulator. |
| Prescription | `$th-order-save`, `$th-order-attest` | Save mapped medication lines, sign under the registered physician, and verify the native request identifiers and signed state. |
| Documentation | `$th-note-save`, `$th-note-revise`, `$th-note-attest` | Save and retrieve the document, check its version binding, then submit and verify the locked content. |
| Simulated settlement | `$th-payment-prepare`, `$th-payment-commit` | Read the signed order's charge items, prepare the bill, commit local cash, and check the payment identifier and amount. |

These named operations are TwinHospital's workflow contract over FHIR R4. Each operation delegates to the existing authorization, identity, version, idempotency, and read-back checks. The vendor connector translates to OpenHIS application requests. A connector for another HIS needs equivalent business actions, code mappings, role permissions, and receipts. This repository implements the OpenHIS connector. The read-only FHIR exporter supplied with OpenHIS is a separate service.

## Run and compare

Pass `--his-interface fhir-r4` when configuring a **fresh** comparison or engineering batch using the current checkout. The selected bridge version, code digest, and operation-definition digest are frozen in that batch's specification. An existing batch keeps its recorded interface. The `cohort-170-runtime` tag and the version 1 reader kit use the original native path. [Run commands](REPRODUCE.md#4-run-a-paper-comparison)

With the isolated [OpenHIS reader runtime](../deploy/openhis-experiment/README.md) running and provisioned, compare both interfaces without a model endpoint. Run from the repository root and choose a fresh output directory.

~~~sh
.venv/bin/python -m sandbox.experiments.linked.verify_his_interfaces \
  --output "$WORK/his-interface-check"
cmp "$WORK/his-interface-check/aggregate.json" results/his_interface_checks.json
~~~

The check uses five fixed fictional case scripts on each path. Each assignment gets its own patient, encounter, and payment identifiers. Independent read-back checks the saved handoff, native diagnosis, signed order, locked note, and linked medication payment. It also checks native state after a rejected unsigned charge, an attempted revision of a locked note, and a duplicate cash request. The [aggregate](../results/his_interface_checks.json) records five matching pairs and ten completed encounters. The detailed `summary.json` and checkpoint files stay in the local output directory. This is a transport check on the same OpenHIS implementation, separate from model-driven outcomes.

To check the FHIR messages exchanged by those scripts, pass `--fhir-schema` with the official HL7 R4 JSON Schema archive. The verifier checks discovery metadata, requests, successful receipts, and error responses. It also checks the input and output parameter names, types, and cardinalities against the definitions returned by the bridge. The aggregate covers all eleven advertised operations across 70 requests and their responses. [HL7 R4 schema and validation scope](https://hl7.org/fhir/R4/validation.html) · [Aggregate](../results/fhir_r4_wire_checks.json)

~~~sh
curl --fail --location https://hl7.org/fhir/R4/fhir.schema.json.zip -o "$WORK/fhir.schema.json.zip"
.venv/bin/python -m sandbox.experiments.linked.verify_his_interfaces \
  --fhir-schema "$WORK/fhir.schema.json.zip" --output "$WORK/his-interface-schema-check"
cmp "$WORK/his-interface-schema-check/fhir_schema.json" results/fhir_r4_wire_checks.json
~~~

For further model experiments, retain the three application definitions, the 27 crossed configurations, and the five existing operation probes. Freeze the source-disjoint inputs, tool and role models, rule catalogue, interface, and scoring before launch. Record all assigned outcomes. For completed encounters, trace which handoffs reached verification, which prescription candidates were revised before signing, and which accepted notes and payments persisted. Report model-driven outcomes separately from the transport comparison.
