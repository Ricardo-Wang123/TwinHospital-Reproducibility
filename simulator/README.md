# CAMEL simulator

The simulator runs each visit as a sequence of decisions by the patient, physician, and pharmacist. The frozen case determines what each role can see. The hospital adapter submits clinical actions to OpenHIS and reads the resulting records. [Case format](../data/README.md) · [OpenHIS backend and interfaces](../openhis/README.md)

[Run an experiment](#run-an-experiment) · [Configuration templates](config/)

## Clinical roles

| Role | Responsibility |
| --- | --- |
| [Patient](agents/patient_agent.py) | Answers questions from the patient view of the case. |
| [Treating physician](agents/doctor_agent.py) | Interviews the patient, finalizes the prescription, addresses pharmacist feedback, and signs the note. |
| [Pharmacist](agents/pharmacist_agent.py) | Reviews medication decisions signed by the ED physician and returns issues to that physician. |

The encounter runner schedules these roles and their AI tools. The adapter registers the patient and verifies the signed orders and locked chart. The ED episode closes when the physician’s disposition is read back with that chart. [Encounter coordinator](agents/coordinator.py) · [Shared CAMEL session](agents/base_agent.py)

## AI tools

| Tool | Role in the visit |
| --- | --- |
| Intake | Collects history from the patient. A factorial ED batch freezes one report per patient and uses it for both A2 and A3. |
| Prescribing | In B2, proposes a regimen after evidence review. In B3, reviews the physician's proposed regimen. |
| Documentation | Drafts the note from physician dictation in C2 or from the encounter dialogue and physician addendum in C3. |

The selected workflow controls when the physician sees each tool's output. [AI tools](agents/ai_tools.py) · [Conditions and inputs](../study/README.md)

## Encounter sequence

| Stage | Action |
| --- | --- |
| Registration and intake | Create a patient and encounter. The assigned intake condition determines when the physician can read an AI report. |
| Consultation and prescribing | The physician assesses the main problem and reads the eligible chart evidence. Comorbidities, allergies, and prior medication inform selection. The physician then signs a nonempty prescription using the shared formulary. Before every signature, the simulator returns duplicate regimen lines to the original physician for merging with their reasons. The pharmacist reviews the signed version. The physician addresses each returned concern and re-signs the revised prescription at the same decision. |
| Documentation | Each writer receives the original ED reports the treating physician has reviewed. C2 adds the physician’s dictation. C3 adds the encounter dialogue and the physician’s addendum. The treating physician reviews and signs the note. |
| ED disposition and closure | The physician chooses the destination before reviewing the note. Match the signed prescription and locked document to the current patient and encounter and recover the disposition from the signed content. |

During consultation, the physician can inspect dated results, ask the patient about symptoms and medication use, and retrieve historical records. Before prescribing, the physician reads and acknowledges all eligible chart pages. OpenHIS retains prior signed versions, records inactive withdrawn lines, and identifies the current version. The episode uses the final approved version. The historical medication reference is reserved for scoring.

## Source

| Component | Entry point |
| --- | --- |
| Role prompts and model calls | [Clinical agents](agents/) · [Role selection](sandbox/experiments/linked/actors.py) · [CAMEL provider](sandbox/experiments/linked/camel_provider.py) |
| Workflow selection and execution | [Clinical conditions](sandbox/experiments/linked/scenario_design.py) · [Coordinator](agents/coordinator.py) · [Stage implementation](sandbox/experiments/linked/clinical.py) |
| Hospital operations | [FHIR R4 bridge](sandbox/experiments/linked/fhir_his.py) · [OpenHIS connector](sandbox/experiments/linked/native_his.py) |
| Case input and reporting | [Input preparation](handoff/inputs.py) · [Experiment runner](handoff/run.py) · [Report generator](handoff/reader_report.py) |

The source setting is the ED. This OpenHIS distribution uses its ambulatory registration and document services to persist the simulation. Its administrative registration transaction creates the visit identifier. The ED endpoint measures the signed clinical record, orders and destination. Ward admission and medication settlement are outside the measured episode. The legacy `outpatient-v2` configuration retains its paid endpoint.

## Run an experiment

A comparison uses Python 3.12 on macOS or Linux, Docker Compose v2, the OpenHIS 2.0.5 runtime and a model endpoint compatible with Chat Completions. The role model drives the patient, physician and pharmacist. One tool model serves the assisted conditions throughout the comparison.

### Install the runtime

Start in the TwinHospital root directory extracted from the software archive. Keep the same shell open for the remaining steps. [OpenHIS deployment](../openhis/README.md)

```sh
cd simulator
WORK="$HOME/twinhospital-work"
mkdir -p "$WORK"
tar -xzf ../openhis/openhis-twinhospital-runtime.tar.gz -C "$WORK"
OPENHIS="$WORK/openhis-reader"
export TZ=Asia/Shanghai

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### Pack the cases

Set `ED_COHORT` to the prepared single-prescription cohort produced by [case preparation](../data/README.md#index-ed-encounter). Pack its manifest and the OpenHIS fixtures for the same formulary.

```sh
.venv/bin/python -m handoff.inputs pack \
  --manifest "$ED_COHORT/manifest.json" --assets "$OPENHIS" --output "$WORK/inputs" \
  --diagnosis-catalog "$ED_DIAGNOSIS_MAPPING" \
  --catalog "$ED_COHORT/catalog.json" \
  --fixture deploy/openhis-experiment/outpatient_catalogue/base_products.sql \
  --fixture deploy/openhis-experiment/outpatient_catalogue/additional_products.sql \
  --fixture "$ED_FORMULARY_SQL"
```

Set `ED_DIAGNOSIS_MAPPING` and `ED_FORMULARY_SQL` to the matching diagnosis mapping and product fixture from the authorized preparation workspace. For one visit, supply `--case`. Case paths are relative to the manifest. Each output directory holds one frozen input bundle. [Case preparation](../data/README.md#index-ed-encounter)

### Configure the models

Copy the provider template to `$WORK` and edit that copy. [Provider template](config/provider.custom.example.json)

```sh
PROVIDER="$WORK/provider.local.json"
cp -n config/provider.custom.example.json "$PROVIDER"
```

| Setting | Value |
| --- | --- |
| `endpoint`, `credential_scope.endpoint` | Use the same API base in both fields, ending before `/chat/completions`. Use HTTPS for a remote endpoint. |
| `role_model`, `tool_models.tool_a` | Set model IDs accepted and returned by your endpoint. Agents and tools can use the same model. |
| `generation_policy.models` | Provide one entry for each model. Set its supported token-limit field, context and completion sizes, and request parameters. |
| `pricing_cny_per_million_tokens`, `pricing_evidence` | Set the input and output rates, including billed reasoning, and record their source. The template's zero rates are for a no-charge local endpoint. |

The API must return assistant JSON, the model identifier, completion status and token counts. The [Ollama profile](config/provider.ollama.example.json) uses Gemma 4 26B for clinical roles and Gemma 4 12B for AI tools, with 262,144 context tokens and a 32,768-token completion ceiling. Install the specified model variants before using this profile. For restricted datasets, set `ED_DATA_POLICY` to the endpoint's processing record. [Processing record template](config/data_policy.example.json)

### Run the comparison

The comparison below uses `emergency-v1` over FHIR R4. It assigns every source case to all 27 workflow combinations. Use a fresh batch name and output directory for each run. The `configure` command writes `run.local.json` in that directory for the runner's `--config` argument. [Hospital interfaces](../openhis/README.md#hospital-interfaces)

```sh
.venv/bin/python -m handoff.inputs configure \
  --bundle "$WORK/inputs" --assets "$OPENHIS" --provider "$PROVIDER" --data-policy "$ED_DATA_POLICY" \
  --mode comparison --scenario-design emergency-v1 --his-interface fhir-r4 \
  --batch ed_01 --output "$WORK/ed_01"

RUN="$WORK/ed_01/run.local.json"
.venv/bin/python -m handoff.run --config "$RUN" provision
.venv/bin/python -m handoff.run --config "$RUN" set-key
.venv/bin/python -m handoff.run --config "$RUN" check
.venv/bin/python -m handoff.run --config "$RUN" run --execute
.venv/bin/python -m handoff.run --config "$RUN" status
.venv/bin/python -m handoff.run --config "$RUN" audit --observe-native
.venv/bin/python -m handoff.run --config "$RUN" report --reference "$ED_COHORT/reference.private.json"
```

Enter the API key at the hidden prompt. A loopback endpoint without authentication accepts a local placeholder. Keep OpenHIS running through the state audit. The ED summary and private encounter comparisons appear under `$WORK/ed_01/ed_results/`. [Report contents and denominators](../results/README.md)

### Pilot and baseline visits

Adding `--quick` to `handoff.inputs configure` selects three complete workflows per case (`A1–B1–C1`, `A2–B2–C2` and `A3–B3–C3`). The command above selects the full comparison. Set `--workers` at configuration time to select parallelism. [Workflow definitions](../study/README.md)

To run one baseline visit per case, choose `--mode linked` with its own batch name and output directory. Use `provision`, `set-key`, `run --execute`, `status` and `audit --observe-native`. The batch directory includes `batch_summary.json` and an index to each encounter's `run.json`.

### Fictional example

From the simulator directory, after installing the runtime, generate and freeze a fictional fixture:

```sh
.venv/bin/python -m handoff.prepare_synthetic --output "$WORK/fictional"
.venv/bin/python -m handoff.inputs pack \
  --manifest "$WORK/fictional/manifest.json" --assets "$OPENHIS" \
  --output "$WORK/fictional-inputs"
```

These commands check case preparation and input packing without model calls. The outpatient fixture uses the built-in simulated formulary.
