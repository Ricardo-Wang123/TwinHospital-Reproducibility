# Run TwinHospital with your dataset

Use the MAS in this repository, the OpenHIS reader release, and your own cases in the [TwinHospital case format](CASE_FORMAT.md). Each case enters the same outpatient workflow and writes its own encounter, prescription, document, and simulated settlement.

The paper comparison crosses the three choices at each clinical stage on the same input cases. It also produces an Application I–III report and supports five paired operation probes. If you received the transferable experiment kit, start with its `START-HERE.md` and use the included runtime and cases.

## 1. Install the runtime

Use Python 3.12 on macOS or Linux, Docker with Compose v2, and an OpenAI-compatible Chat Completions endpoint. The [reader release](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1) provides the OpenHIS 2.0.5 backend, its source, and an independently constructed sample dataset.

Clone this repository using your GitHub access. Download `openhis-twinhospital-runtime.tar.gz` from the release page to the working directory outside the checkout.

~~~sh
git clone https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility.git
cd TwinHospital-Reproducibility
WORK="$HOME/twinhospital-work"
mkdir -p "$WORK"
~~~

After downloading the archive, extract it and install the Python dependencies. The shell time zone aligns registration and read-back dates with OpenHIS.

~~~sh
tar -xzf "$WORK/openhis-twinhospital-runtime.tar.gz" -C "$WORK"
OPENHIS="$WORK/openhis-reader"
export TZ=Asia/Shanghai

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linked.txt
~~~

The runtime serves OpenHIS at `127.0.0.1:28081` with a separate PostgreSQL database, a virtual institution, a simulated product catalogue, and an empty patient registry. [OpenHIS deployment](../deploy/openhis-experiment/README.md)

## 2. Prepare your input

Put one [case JSON](CASE_FORMAT.md#runtime-case-json) per patient under `$WORK/dataset`. For a collection, write a [manifest](CASE_FORMAT.md#dataset-manifest) with the case paths and matching identifiers. The packer resolves relative paths from the manifest's directory.

~~~sh
MANIFEST="$WORK/dataset/manifest.json"
.venv/bin/python -m handoff.inputs pack \
  --manifest "$MANIFEST" --assets "$OPENHIS" --output "$WORK/inputs"
~~~

To start with a single case, replace `--manifest "$MANIFEST"` with `--case "$WORK/dataset/case.json"`. Pass `--case` multiple times to assemble a batch without writing a manifest. If your cases require more terms than the included catalogue, see [product and diagnosis mappings](CASE_FORMAT.md#product-and-diagnosis-mappings).

### Optional fictional examples

Download `twinhospital-fictional-cases.tar.gz` from the [reader release](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1) and extract it into `$WORK`. Set `MANIFEST="$WORK/twinhospital-fictional-cases/manifest.json"` in the command above. The archive includes one editable case for each of the five strata. The generator writes the same files locally.

~~~sh
.venv/bin/python -m handoff.prepare_synthetic --all-groups \
  --output "$WORK/twinhospital-fictional-cases"
~~~

Use `--case "$WORK/twinhospital-fictional-cases/cases/type2_diabetes.json"` to start with one example. When you edit a case, pack the edited file or update its manifest metadata as described in the case guide.

## 3. Configure your model

Copy the [provider template](../config/provider.custom.example.json) to a local file.

~~~sh
PROVIDER="$WORK/provider.local.json"
cp -n config/provider.custom.example.json "$PROVIDER"
~~~

| Setting | Value |
| --- | --- |
| `endpoint`, `credential_scope.endpoint` | Use the same API base in both fields, ending before `/chat/completions`. Use HTTPS for a remote endpoint. |
| `role_model`, `tool_models.tool_a` | Set model IDs accepted and returned by your endpoint. Agents and tools can use the same model. |
| `generation_policy.models` | Provide one entry for each model. Set its supported token-limit field, context and completion sizes, and request parameters. |
| `pricing_cny_per_million_tokens`, `pricing_evidence` | Set the input and output rates, including billed reasoning, and record their source. The template's zero rates are for a no-charge local endpoint. |

The runner reads assistant JSON, the model identifier, completion status, and token counts from the Chat Completions response. For restricted datasets, supply the endpoint-specific local processing record using `--data-policy`. [Processing record template](../config/data_policy.example.json)

## 4. Run a paper comparison

Start with `--quick` for a pilot. It schedules three fresh encounters per source case and exercises every choice at least once.

| Intake | Prescription review | Documentation |
| --- | --- | --- |
| Nurse | Rules with pharmacist routing | Physician |
| AI alone | Sequential | During consultation |
| AI with nurse review | Parallel | After consultation |

Remove `--quick` to assign every case to all 27 combinations. The full crossing supports matched contrasts that hold the other two stages fixed. Each assignment has its own agent context, encounter, and native records. Comparisons use one worker by default. Set `--workers` when configuring a batch to change parallelism.

For a fresh batch, add `--his-interface fhir-r4` to `configure` to use the FHIR R4 operation bridge. The historical cohort instructions below use the original native path. [HIS protocol and verification](HIS_INTERFACE.md)

~~~sh
.venv/bin/python -m handoff.inputs configure \
  --bundle "$WORK/inputs" --assets "$OPENHIS" --provider "$PROVIDER" \
  --mode comparison --quick --batch my_cohort_01 --output "$WORK/my_cohort_01"

RUN="$WORK/my_cohort_01/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" run --execute
.venv/bin/python -m handoff.run --config "$RUN" status
.venv/bin/python -m handoff.run --config "$RUN" audit --observe-native
.venv/bin/python -m handoff.run --config "$RUN" operations
.venv/bin/python -m handoff.run --config "$RUN" report
~~~

Enter the API key in the hidden prompt. For a loopback endpoint that accepts requests without authentication, enter a local placeholder. Use a fresh batch name and output directory for each comparison. Keep the isolated OpenHIS instance running through the audit and operation checks.

| Paper application | What to inspect in `reader_results/REPORT.md` and `summary.json` |
| --- | --- |
| I · Clinical artifacts | Handoff and note delivery, original prescription decisions, original and follow-up alerts, and the false-positive rate among resolved rule-reference negatives. |
| II · Modeled staff effort | Staff minutes per delivered artifact, supplementation and rework, and elapsed note turnaround. Matched contrasts use cells with the same case and other stage configurations. |
| III · Workflow integrity | Native signed order, locked note, and simulated payment, with planned and verified counts for each combination. The paired operation schedule reports fault and control dispositions separately. |

Configuration selects the first manifest case for the paired operation checks. The replay uses that case's completed baseline artifacts to create fresh OpenHIS objects, with one fault and one valid control for each of five operations. If the baseline is unavailable, the planned requests retain unresolved outcomes. Choose the first manifest row before running if you want a different source.

The run reports every planned assignment. The artifact and effort tables apply the paper's verified-paid observation window. A rule with no covered product or sufficient evidence remains indeterminate. Rates with no eligible denominator appear as a dash. The JSON includes the counts and the exact model identifiers. The case files, traces, and detailed audit stay in the local workspace.

### Run a single baseline visit

Use `--mode linked` for one baseline visit per case. Run `provision`, `set-key`, `run --execute`, `status`, and `audit --observe-native` as above. The batch directory includes `batch_summary.json` and an index to each encounter's `run.json`.

### Compare to the study snapshot

The independent dataset route uses the same workflow configurations. Use the study sections below for the exact historical inputs and model profile. [Study results](../results/README.md)

## Repeat the study configuration

### Inputs

| Item | Obtain or prepare |
| --- | --- |
| Runtime code | The tag `cohort-170-runtime` preserves the engine used for the published encounters. The current branch supplies the setup and audit commands. |
| Frozen case bundle | Use the approved study bundle with `bundle.json`, the portable `manifest.json`, 10 runtime case files, the native product and diagnosis mappings, the clinical rule catalogue, the fixture SQL in installation order, and the workflow policy. See [case construction](CASE_FORMAT.md). |
| Matching OpenHIS | Use the original authorized release identified by the frozen bundle. Provisioning installs the case bundle's native fixtures into the isolated experiment instance. |
| Model access | One account key must have access to `deepseek-v4.1-flash-1017` and `glm-5.2` at the configured endpoint. Use the [same provider profile](../config/provider.example.json) for the same API route and request settings. |
| Processing record | For MIMIC-derived cases, supply a private endpoint-specific `data_policy` record covering authorized access, zero retention, no training, no human review, and caching. Use your approved local provider configuration or pass the record with `--data-policy`. |

The assigned roles and generation settings are in the [frozen execution profile](../results/cohort_170_execution.json). DeepSeek drives all conversational patient and staff agents. GLM drives pre-consultation assistance, the review tool, and note drafting. Registration and simulated settlement are HIS operations. Reasoning is disabled for both models, and temperature is 0.2.

The repository publishes the input description and checksums alongside the verified results. Arrange approved access to the protected study bundle and traces under the MIMIC-IV terms. Obtain MIMIC access under your own account. [PhysioNet data-use and model-processing guidance](https://physionet.org/news/post/llm-responsible-use/)

### Configure and launch

Use a fresh isolated deployment on the default loopback port for the historical runtime. Set `STUDY_OPENHIS` to the extracted original release, `FROZEN_BUNDLE` to the received study bundle, and `PROCESSING_RECORD` to the locally approved data-processing record. The [template](../config/data_policy.example.json) shows the fields.

~~~sh
git worktree add --detach "$WORK/frozen_runtime" cohort-170-runtime
.venv/bin/python -m handoff.inputs configure \
  --bundle "$FROZEN_BUNDLE" --assets "$STUDY_OPENHIS" \
  --data-policy "$PROCESSING_RECORD" \
  --frozen-runtime "$WORK/frozen_runtime" \
  --mode factorial --batch repeat_01 \
  --output "$WORK/repeat_run"

RUN="$WORK/repeat_run/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" run --execute
~~~

The configuration schedules 27 combinations per source history. After the batch closes, use `status` and `audit --observe-native` to collect order, note, and payment records.

The published tables describe the 170 audited paid encounters. Replaying the configurations generates fresh model responses. The authorized evidence package reproduces the published table values from the recorded traces on a different host, without model access. [Results and recomputation commands](../results/README.md)

## Run a held-out cohort

This route extends the study with new MIMIC-IV cases. Build cases with [the same runtime format](CASE_FORMAT.md). Assign them to `split: "test"` using the same source identity namespace as the development cases. Check disjointness in the restricted source registry before assigning opaque IDs. Include all histories used in development, including prompt and rule tuning, in the exclusion manifest. Use the same clinical catalogue, native mappings, fixture sequence, and workflow policy as the comparison cohort. Freeze the batch before seeing results.

Pack the new manifest by inheriting the study's native settings. Then configure a crossed batch on the current branch:

~~~sh
.venv/bin/python -m handoff.inputs pack \
  --manifest "$NEW_MANIFEST" --inherit-native "$FROZEN_BUNDLE" \
  --assets "$STUDY_OPENHIS" --output "$NEW_BUNDLE"
.venv/bin/python -m handoff.inputs configure \
  --bundle "$NEW_BUNDLE" --assets "$STUDY_OPENHIS" \
  --data-policy "$PROCESSING_RECORD" \
  --mode heldout --development-manifest "$DEVELOPMENT_MANIFEST" \
  --patients-per-disease "$K" --batch heldout_01 --workers 1 \
  --output "$WORK/heldout_run"
RUN="$WORK/heldout_run/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" check
~~~

Set `K` to the equal number of distinct test patients in each of the five strata. The runner assigns each patient to all 27 configurations, with one attempt per assignment. For the planned 1,000-patient comparison, set `K=200` and add `--operation-schedule "$LOCAL_SCHEDULE"` to `pack`. This includes the preassigned Application III requests in the input bundle. The 500 requests use 500 patients from the same cohort, with four fault requests and sixteen controls per disease and operation. The batch specification binds both manifests, the replay code, and the baseline configuration used as the replay source. [Operation schedule format](#replay-paired-operations)

The local `check` command reports 27,000 planned encounters and 500 planned operations for a 1,000-patient bundle with the schedule. Use the same `provision`, `set-key`, `run --execute`, and `audit --observe-native` subcommands to collect the held-out outcomes. Set the dispatch window with `--dispatch-hours` when configuring the batch. Each encounter retains the observation window in the inherited workflow policy.

Size the working volume for encounter files, model evidence, native observation files, and the OpenHIS database. On APFS, start this command in a separate terminal once the runner creates `batch.lock`. It compresses indexed terminal attempts and verifies each file's contents, path, and permissions. The receipt stays in the private workspace.

~~~sh
.venv/bin/python -m tools.compress_finished_evidence \
  --batch "$WORK/heldout_run/batches/heldout_01" \
  --receipt "$WORK/heldout_run/evidence_compression.jsonl" --follow
~~~

After the native audit, run the `operations` subcommand if the bundle includes the frozen schedule, then run `report`. The operation replay exercises the shared gateway on fresh OpenHIS objects reconstructed from the baseline assignment. An unavailable baseline leaves its preassigned operation unresolved. The report uses the same Application I and II metrics and keeps all assigned encounters and replay requests in their respective Application III denominators. Its output is `heldout_results/`. [Result reconstruction](../results/README.md)

## Replay paired operations

The source-bound replay exercises the adapter and OpenHIS without model calls. It reconstructs fresh business objects from a completed source encounter. The ten published probe outcomes use a separate development source batch. The evidence bundle provides its aggregate for offline table reconstruction.

For a new paired replay, prepare an authorized cohort manifest and a frozen schedule. Each schedule row copies a cohort case row and adds a unique `local_test_id`, an `operation_category` from the replay module, and a `request_condition` of `single_fault` or `compliant_control`. Both rows of a pair can use the same source case. Use a closed source batch with the corresponding `linked_baseline_anchor` traces, matching catalogues, and its isolated OpenHIS deployment. The manifest and attempt indexes must resolve to local authorized files.

~~~sh
.venv/bin/python -m sandbox.experiments.linked.source_operation_replay \
  --engineering-only --schedule "$REPLAY_SCHEDULE" --cohort "$SOURCE_MANIFEST" \
  --source-batch "$SOURCE_BATCH" --deployment-dir "$SOURCE_DEPLOY" \
  --catalog "$SOURCE_CATALOG" --diagnosis-catalog "$SOURCE_DIAGNOSES" \
  --run-id paired_replay_01 --output "$WORK/paired_replay_01"
~~~

The output contains unit-level read-back in `outcomes.jsonl` and grouped counts in `summary.json`. The schedule keeps unresolved source or execution cases in the denominator. Use the summary corresponding to this schedule when rendering new paper tables. [Result renderer](../results/README.md#regenerate-the-manuscript-tables)
