# OpenHIS runtime and deployment

TwinHospital uses OpenHIS 2.0.5 to register outpatient visits, store signed orders and notes, and record simulated cash payments. The `outpatient-v2` run guide uses the TwinHospital FHIR R4 operation bridge. The bridge translates each request into an OpenHIS business operation and returns the recorded state.

For the ED design, the same hospital services store the clinical objects. The study's ED setting and physician-selected disposition are preserved in the signed document and in the exported FHIR encounter. The native registration service creates an ambulatory OpenHIS registration. A ward transfer order and final medication charge are outside the study endpoint.

## Bundled assets

| Asset | Contents |
| --- | --- |
| `openhis-twinhospital-runtime.tar.gz` | Java 17 backend, PostgreSQL seed and local provisioning files. Extracts to `openhis-reader/`. |
| `openhis-backend-source-2.0.5.tar.gz` | Java and Maven source corresponding to the packaged backend, with upstream notices. |
| [Deployment configuration](../simulator/deploy/openhis-experiment/compose.yaml) | Docker Compose services for the isolated backend, database and gateway. |

The two archives are included in the software download under this directory.

## Start OpenHIS

The experiment launcher sets `TH_OPENHIS_ASSETS` and provisions the hospital. For direct deployment, set `OPENHIS` to the extracted `openhis-reader/` directory and run the commands below from `simulator/`. [Run an experiment](../simulator/README.md#run-an-experiment)

```sh
export TH_OPENHIS_ASSETS="$OPENHIS"
python3 deploy/openhis-experiment/manage.py up
python3 deploy/openhis-experiment/manage.py provision
python3 deploy/openhis-experiment/manage.py status
```

The default gateway address is `127.0.0.1:28081`. If the default TwinHospital deployment is already running, use a separate checkout and set an unused project name and port before configuring, starting, or auditing the new run.

~~~sh
export TH_OPENHIS_PROJECT=twinhospital-exp-smoke-reader
export TH_OPENHIS_PORT=28082
~~~

## Database and clinical dictionaries

The startup database uses the matching OpenHIS schema, software terminology, frequencies, access roles, and settings. The virtual institution, registration service, self-pay contract, document type, and pharmacy routing are defined in [reader-bootstrap.sql](../simulator/deploy/openhis-experiment/reader-bootstrap.sql). Provisioning adds local role accounts, a rolling schedule, and the simulated formulary. Patients, encounters, prescriptions, notes, and transactions start empty. [Schedule](../simulator/deploy/openhis-experiment/reader-schedule.sql) · [Formulary fixture](../simulator/deploy/openhis-experiment/experiment-fixtures.sql)

The reader runtime includes five simulated products and a diagnosis catalogue. Product entries distinguish dose units from dispensing units. Prices and stock belong to the simulated institution. Inputs that add products or diagnoses also supply matching catalogue entries and provisioning fixtures. [Product mappings](../simulator/deploy/openhis-experiment/catalog_mapping.json) · [Diagnosis mappings](../simulator/deploy/openhis-experiment/diagnosis_mapping.json)

OpenHIS creates new patient and encounter identifiers for each assignment. [Encounter records and read-back](#encounter-records) · [OpenHIS connector](../simulator/sandbox/experiments/linked/native_his.py)

## Hospital interfaces

TwinHospital maintains the queue, imported chart, dialogue, handoff, and review decisions. The adapter authenticates with the assigned role, submits hospital operations, and reads back the native encounter, diagnoses, orders, notes, and payments from OpenHIS. Provisioning and independent audits access the database separately from workflow requests. Browser interactions and form checks require a browser evaluation.

Each assignment starts from a frozen case with a fresh agent context and fresh identifiers for the patient, encounter, and business records. The trace connects the retrieved handoff to the consultation, review or encoding feedback to prescription revisions, and the signed order to the charges used in simulated settlement. [Case format](../data/README.md)

The run guide sets `--his-interface fhir-r4` for `outpatient-v2`. Choose `--his-interface wst-846` when configuring a batch to use the SOAP profile. [Batch configuration](../simulator/README.md#run-the-comparison)

### FHIR R4 workflow operations

The FHIR R4 bridge receives `application/fhir+json` at `POST [base]/$th-…` on an assignment-scoped loopback endpoint. `GET [base]/metadata` returns the `CapabilityStatement`. `GET [base]/OperationDefinition/{id}` describes each operation's parameters and cardinalities. Requests carry `Parameters` with logical `Patient` and `Encounter` references and identifiers for the case, assignment, actor, and target objects. Historical FHIR collections enter as a `Bundle` resource parameter on the project-defined `$th-history-fhir-import` operation. The encounter study store supplies the physician’s chart pages. Other structured workflow artifacts use JSON `Attachment` fields. The response is a `Parameters` receipt with the read-back or an `OperationOutcome` on rejection or error. Reconcile transport uncertainty before retrying. [Bridge implementation](../simulator/sandbox/experiments/linked/fhir_his.py)

| Workflow step | FHIR R4 project operations | OpenHIS action and read-back |
| --- | --- | --- |
| Registration and reception | `$th-registration-create`, `$th-encounter-begin-consultation` | Create the patient and registration, then start reception and verify the bound encounter and physician. |
| Diagnosis | `$th-diagnosis-catalogue`, `$th-consultation-save` | Read the hospital diagnosis catalogue, save selected diagnoses, and verify the resulting diagnosis records. The consultation narrative and transcript stay with the simulator. |
| Prescription | `$th-order-save`, `$th-order-attest` | Save mapped medication lines, sign under the registered physician, and verify the native request identifiers and signed state. |
| Documentation | `$th-note-save`, `$th-note-revise`, `$th-note-attest` | Save and retrieve the document, check its version binding, then submit and verify the locked content. |
| Simulated settlement | `$th-payment-prepare`, `$th-payment-commit` | Read the signed order's charge items, prepare the bill, commit local cash, and check the payment identifier and amount. |

These are TwinHospital workflow operations over FHIR R4. The adapter checks authorization, identity, version, idempotency, and read-back, then translates the operations into OpenHIS application requests. The read-only FHIR exporter supplied with OpenHIS runs as a separate service. A connector for another HIS needs equivalent operations, mappings, role permissions, and receipts.

### WS/T service profile

The WS/T profile uses a SOAP-only route. It sends `HIPMessageServer(action, message)` two string arguments over SOAP 1.2 and uses the same persistent simulator services for the queue, imported chart, handoff, and staff registry. [WS/T implementation](../simulator/sandbox/experiments/linked/wst_workflow.py)

| Operation | Service definition | Scope |
| --- | --- | --- |
| Patient registration | `PatientInfoRegister`, WS/T 846.2-2024 | Required patient fields follow Table 2. A sender patient identifier maps to a fresh OpenHIS identifier. The registration name and institution are experimental identities. Encounter creation and local registration-fee bookkeeping use project extensions. |
| Medication order creation | `OrderInfoAdd`, WS/T 846.8-2024 | Required order, patient, encounter, and dose fields follow Table 2. The hospital drug catalogue owns product codes. Fields without a verified national code mapping use named extensions. |
| Reception, diagnosis lookup and saving, order attestation, note versions and signing, simulated settlement | `TH_…` project services in `urn:twinhospital:workflow-extension:1` | These project extensions use the transport and string-extension form of WS/T 846.1. |

The batch specification freezes the profile. Wire records identify the national service or project extension used for each operation.

### Paired interface comparisons

The main comparison uses FHIR R4. For an optional transport comparison, run WS/T on the same cases, dictionaries, clinical configurations, and model settings. Give each transport its own batch, ledger, model evidence, and OpenHIS records. Match by source case and clinical configuration and report the assigned encounters and verified payments for each transport. [Paired protocol analysis](../simulator/tools/compare_protocol_runs.py)

### Encounter records

| Operation | Recorded evidence |
| --- | --- |
| Registration | OpenHIS returns the patient and encounter identifiers. TwinHospital records the queue entry against that encounter. |
| Consultation | The adapter confirms the attending physician and reception state. The physician selects diagnoses from the available catalogue. |
| Prescribing | The physician signs a nonempty, deduplicated prescription. The pharmacist reviews every version, and the original physician signs any revision within the same decision. The adapter reads back the superseded and current versions, the states of withdrawn medication lines, and the linked pharmacy reviews. |
| Documentation | The physician submits the final note. The adapter retrieves the locked version and checks its encounter binding. |
| Settlement | The cashier operation retrieves the medication charges attached to the signed order, records local cash settlement, and checks the payment and amount. |

## Build the backend from source

For a source build, use JDK 17 and Maven from the extracted backend source directory. The resulting JAR is `whale-health/health-app/target/health-app-2.0.5.jar`.

~~~sh
mvn -B -ntp -pl whale-health/health-app -am -DskipTests package
~~~

## Licenses

The OpenHIS package supplies the GNU Lesser General Public License v3 and the GNU General Public License v3 distribution text. [LICENSE](LICENSE) · [LICENSE-distribution](LICENSE-distribution)
