# Run TwinHospital

Use Python 3.12 on macOS or Linux, Docker with Compose v2, and an isolated OpenHIS release. Run commands from the repository root. Set `WORK` to a private absolute directory **outside** the checkout and `OPENHIS` to the authorized release directory.

~~~sh
WORK="$HOME/twinhospital-private"
OPENHIS="/absolute/path/to/authorized/openhis"
mkdir -m 700 -p "$WORK"
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linked.txt
.venv/bin/python -m pip check
~~~

The release contains `release/health-app.jar`, `openhis-seed/`, and `tools/runtime/`. The bundle records release checksums. The default deployment uses `127.0.0.1:28081`, an isolated PostgreSQL volume, and local simulated cash payments.

## Offline regression checks

The two suites cover the workflow contracts, case preparation, result processing, and compatibility with the legacy runner. The legacy smoke tests also use Jinja2.

~~~sh
.venv/bin/python -m pip install Jinja2==3.1.6
PYTHONPATH=tests:tests/linked .venv/bin/python -m unittest discover -s tests
PYTHONPATH=tests:tests/linked .venv/bin/python -m unittest discover -s tests/linked
~~~

These checks use local fixtures and test doubles. The fictional encounter below separately checks an isolated OpenHIS instance.

## Fictional encounter

This path checks the setup before using clinical inputs. Generate one independent fictional case, configure the launcher, and start the isolated HIS.

~~~sh
.venv/bin/python -m handoff.prepare_synthetic --output "$WORK/example"
.venv/bin/python -m handoff.inputs pack \
  --case "$WORK/example/case.json" --assets "$OPENHIS" \
  --output "$WORK/fictional_bundle"
.venv/bin/python -m handoff.inputs configure \
  --bundle "$WORK/fictional_bundle" --assets "$OPENHIS" \
  --batch fictional_01 --output "$WORK/fictional_run"

RUN="$WORK/fictional_run/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python deploy/openhis-experiment/smoke.py --count 1
~~~

The native smoke should report `native_complete paid`. It performs no model calls. The default configuration uses the study endpoint. To use your own endpoint, pass the provider profile described in the next section to `configure`. For a model-backed check, enter a key with access to the configured model IDs and run:

~~~sh
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" probe
.venv/bin/python -m handoff.run --config "$RUN" run --execute
.venv/bin/python -m handoff.run --config "$RUN" status
~~~

The `probe` command sends nonclinical text once per distinct configured model ID and stores the responses in a local receipt. It can run before OpenHIS provisioning. The launch action performs this check immediately before dispatch.

## Use your own model endpoint

For a new run, copy the [custom provider template](../config/provider.custom.example.json) to a private file and set the fields below. The example describes a no-charge loopback server. For a remote service use its HTTPS API base URL.

| Setting | Value |
| --- | --- |
| `endpoint`, `credential_scope.endpoint` | The same base URL in both fields, ending before `/chat/completions`. |
| `role_model`, `tool_models.tool_a` | Model IDs accepted and returned by the endpoint. Use the same ID in both fields to share a model across agents and tools. |
| `generation_policy.models` | One entry per distinct model ID. Set the supported token-limit field, context and completion sizes, reasoning mode, and request parameters. |
| `pricing_cny_per_million_tokens`, `pricing_evidence` | Input and output rates for each model, including reasoning where billed, and the source for those rates. |

The endpoint must return a Chat Completions envelope with the requested model ID, an assistant message, completion status, and prompt/completion token counts. Workflow prompts specify the JSON object and action fields for each role. Use a compatible adapter if the underlying service exposes a different API. The case schema, role-specific information, OpenHIS checks, and payment readback stay the same. The batch records the selected endpoint, models, and generation settings.

~~~sh
PROVIDER="$WORK/provider.local.json"
CASE_BUNDLE="$WORK/fictional_bundle"
cp -n config/provider.custom.example.json "$PROVIDER"
# Edit the private provider profile using the settings above.
.venv/bin/python -m handoff.inputs configure \
  --bundle "$CASE_BUNDLE" --assets "$OPENHIS" --provider "$PROVIDER" \
  --batch own_model_01 --output "$WORK/own_model_run"
RUN="$WORK/own_model_run/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" run --execute
.venv/bin/python -m handoff.run --config "$RUN" status
~~~

This command runs the baseline chain once per case. For your own cases, point `CASE_BUNDLE` to a verified input bundle. For restricted cases, pass `--data-policy "$PROCESSING_RECORD"` for the chosen endpoint when configuring. Enter your own credential at the hidden `set-key` prompt. It is stored in a separate local `0600` file. Share the credential-free template and keep the private run directory with its operator.

For a crossed evaluation, use the held-out commands below with `--provider "$PROVIDER"`. Keep the resulting cohort and metrics separate from the paper's archived run. A compatible model can enter the workflow. Its outputs still have to pass the same artifact and native settlement checks.

## Repeat the study configuration

### Inputs

| Item | Obtain or prepare |
| --- | --- |
| Runtime code | The tag `cohort-170-runtime` preserves the engine used for the published encounters. The current branch supplies the setup and audit commands. |
| Frozen case bundle | Use the approved study bundle with `bundle.json`, the portable `manifest.json`, 10 runtime case files, the native product and diagnosis mappings, the clinical rule catalogue, the fixture SQL in installation order, and the workflow policy. See [case construction](CASE_FORMAT.md). |
| Matching OpenHIS | Use the authorized matching release. Provisioning installs the case bundle's native fixtures into the isolated experiment instance. |
| Model access | One account key must have access to `deepseek-v4.1-flash-1017` and `glm-5.2` at the configured endpoint. Use the [same provider profile](../config/provider.example.json) for the same API route and request settings. |
| Processing record | For MIMIC-derived cases, supply a private endpoint-specific `data_policy` record covering authorized access, zero retention, no training, no human review, and caching. Use your approved local provider configuration or pass the record with `--data-policy`. |

The assigned roles and generation settings are in the [frozen execution profile](../results/cohort_170_execution.json). DeepSeek drives all conversational patient and staff agents. GLM drives pre-consultation assistance, the review tool, and note drafting. Registration and simulated settlement are HIS operations. Reasoning is disabled for both models, and temperature is 0.2.

The repository publishes the input description and checksums alongside the verified results. Arrange approved access to the protected study bundle and traces under the MIMIC-IV terms. Obtain MIMIC access under your own account. [PhysioNet data-use and model-processing guidance](https://physionet.org/news/post/llm-responsible-use/)

### Configure and launch

Use a fresh isolated deployment on the default loopback port for the historical runtime. The commands generate local absolute paths from the portable case manifest and verify the case and release hashes. Set `PROCESSING_RECORD` to the locally approved data-processing record. A [blank verification template](../config/data_policy.example.json) shows the fields. The example leaves each verification false.

~~~sh
git worktree add --detach "$WORK/frozen_runtime" cohort-170-runtime
.venv/bin/python -m handoff.inputs configure \
  --bundle "$FROZEN_BUNDLE" --assets "$OPENHIS" \
  --data-policy "$PROCESSING_RECORD" \
  --frozen-runtime "$WORK/frozen_runtime" \
  --mode factorial --batch repeat_01 \
  --output "$WORK/repeat_run"

RUN="$WORK/repeat_run/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" run --execute
~~~

The preflight reports ten inputs, the model IDs, and 270 planned encounters (27 configurations per source history). It verifies the 20-worker setting and 120-minute observation window for this replay. The launcher checks the two model routes with nonclinical messages before dispatch. The observed request, role and native-state traces stay in the local batch directory. Use `status` and `audit --observe-native` after closure. The audit verifies terminal payment, note, and order state.

The published tables describe the 170 audited paid encounters. Replaying the configurations generates fresh model responses. The authorized evidence package reproduces the published table values from the recorded traces on a different host, without model access. [Results and recomputation commands](../results/README.md)

## Run a held-out cohort

Build cases with [the same runtime format](CASE_FORMAT.md). Assign them to `split: "test"` using the same source identity namespace as the development cases. Check disjointness in the restricted source registry before assigning opaque IDs. Include all histories used in development, including prompt and rule tuning, in the exclusion manifest. Use the same clinical catalogue, native mappings, fixture sequence, and workflow policy as the comparison cohort. Freeze the batch before seeing results.

Pack the new manifest by inheriting the study's native settings. Then configure a crossed batch on the current branch:

~~~sh
.venv/bin/python -m handoff.inputs pack \
  --manifest "$NEW_MANIFEST" --inherit-native "$FROZEN_BUNDLE" \
  --assets "$OPENHIS" --output "$NEW_BUNDLE"
.venv/bin/python -m handoff.inputs configure \
  --bundle "$NEW_BUNDLE" --assets "$OPENHIS" \
  --data-policy "$PROCESSING_RECORD" \
  --mode heldout --development-manifest "$DEVELOPMENT_MANIFEST" \
  --patients-per-disease "$K" --batch heldout_01 --workers 1 \
  --output "$WORK/heldout_run"
~~~

Set `K` to the equal number of distinct test patients in each of the five strata. The launcher rejects overlap with the supplied development manifest and creates all 27 assignments per test patient. Each assignment gets one independent clinical attempt. Provider, observation, and transaction safeguards still apply. Use the same `provision`, `check`, `set-key`, `run --execute`, and audit commands. For a separately provisioned instance, use its own directory and batch name.

Report the held-out denominator and terminal outcomes separately from the development snapshot. The result scripts derive each task's counts from the new audit. Independent operation probes use their own denominator. [Result reconstruction](../results/README.md)

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

## Model or environment check fails

Dispatch begins after every configured model ID passes its probe. Check the exact endpoint, model access, and the private probe receipt. Enter a replacement key for the same approved endpoint with `set-key`. Changing the provider endpoint or model IDs creates a distinct execution configuration. The run also records the local transport mode.

An HTTP 401 is a credential rejection. A missing HTTP status points to a failure before an HTTP response, such as DNS, routing, or TLS. The recorded macOS run used a process-bound route to the same HTTPS endpoint. On a machine that needs a specific route, provide a private copy of the provider profile via `configure --provider`. The [route adapter](../sandbox/experiments/linked/network_route.py) accepts an existing physical interface, a confirmed public DNS address, and the original HTTPS hostname, with TLS verification enforced. Keep machine-specific addresses and credentials in the local setup.

The Compose project belongs to the deployment directory that created it. On a host that already runs TwinHospital, use that directory or a separate isolated smoke project on the current branch. A smoke project uses `TH_OPENHIS_PROJECT=twinhospital-exp-smoke-<name>` and a matching `TH_OPENHIS_PORT` between 28082 and 28199. Export both variables for provisioning and execution, and pass the matching `--his-url` to the standalone native smoke script.
