# TwinHospital 运行与扩跑指南

同一个病例从挂号、预问诊、接诊开方、审方修订、病历签署，到 **OpenHIS 模拟药费缴费并读回**。角色用 `deepseek-v4.1-flash-1017`，AI 工具用 `glm-5.2`；两条路由都关闭推理。支付只在隔离 OpenHIS 记账。

当前结果在 [results](../results/README.md)：170 条已缴费，原定 270 条中还有 100 条可续跑。论文中的描述性结果来自这 170 条。代码快照来自 `ab7c5fb`；产生这批结果的原始运行版本为 `654dd39`。

## 在原实验机换 key 续跑

原实验机已有 `private/recover.local.json`，它指向冻结病例、原生 OpenHIS 实例、既有账本和审计过的恢复清单。在仓库根目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linked.txt
.venv/bin/python -m handoff.run --config private/recover.local.json check
.venv/bin/python -m handoff.run --config private/recover.local.json set-key
.venv/bin/python -m handoff.run --config private/recover.local.json run --execute
```

`check` 不调用模型、不写 HIS；原批次应报告 `retained_paid: 170`、`unpaid_to_dispatch: 100`。`set-key` 不回显，保存到本机 `0600` 文件。`run --execute` 先分别用无临床内容请求检查两条模型路由，均成功才启动病例。旧账本保留过往成本；确认服务恢复后才解除供应商停发。用相同端点、模型和临床配置，已缴费 170 条不会重派。不要并行运行另一份恢复调度器。

如果原冻结调度窗口过期，用 [工作窗口模板](../config/workflow.template.json) 冻结新窗口，将 `workflow_policy` 改为新文件。病例、模型、规则、字典及原生观测文件仍需通过原哈希核对：

```sh
.venv/bin/python -m handoff.freeze_window --template config/workflow.template.json \
  --output private/workflow.local.json --workers 20 --dispatch-hours 72
```

## 在新电脑启动

需要 Python 3.12、Docker Compose v2 或兼容版本，以及团队授权的同版 OpenHIS 发布包（`release/health-app.jar`、`openhis-seed/`、`tools/runtime/`）。先用独立虚构病例核对环境。下列命令在仓库根目录运行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linked.txt
export TH_OPENHIS_ASSETS=/absolute/path/to/authorized/openhis
.venv/bin/python deploy/openhis-experiment/manage.py up
.venv/bin/python deploy/openhis-experiment/manage.py provision
.venv/bin/python deploy/openhis-experiment/smoke.py --count 1
```

网关绑定 `127.0.0.1:28081`；数据库不对宿主机开放。`smoke.py` 无模型调用，成功时输出 `native_complete paid`。不要使用其他医院实例或共享正式 HIS。若当前机器已有这个 Compose 项目，请在它原部署目录运行或改用一台新实验机。

让 CAMEL 角色和 AI 工具也参与一次独立虚构病例运行：

```sh
mkdir -m 700 -p private
cp config/provider.example.json private/provider.local.json
cp config/run.synthetic.example.json private/run.local.json
.venv/bin/python -m handoff.prepare_synthetic --output private/synthetic
.venv/bin/python -m handoff.run --config private/run.local.json check
.venv/bin/python -m handoff.run --config private/run.local.json set-key
.venv/bin/python -m handoff.run --config private/run.local.json run --execute
```

示例价格是原批次的记账参数，应核对当前账号费率；计费日志是保守估算，供应商账单为准。新运行默认限额为 1,000 元；获授权不限额时在私有配置中设 `"uncapped": true`。已存在的账本不会因换 key 丢失原成本。

## 跑后续病例、出结果

- **接续同一批**：使用 [恢复配置模板](../config/run.recover.example.json)。恢复清单、原批次目录、已归档运行和同版本的 HIS 观察部署是校验凭据；旧路径指针和文件哈希都需要可读。最简单是在原实验机执行。新机没有这些本地文件时，可以启动独立新批次，不能声称续接了旧批次。
- **启动下一批**：复制 [新增组合批次模板](../config/run.factorial.example.json) 到 `private/`，指向事前冻结、与已跑病例不重叠的授权病例清单、同版药品／诊断字典及应用过的对应 SQL fixtures、原生观察和规则配置。冻结新窗口后先 `check`，再 `run --execute`。正式留出设计使用 `mode: formal`，额外提供 `analysis_plan`、`acceptance_batch`、`acceptance_audit`。病例准备代码在 `sandbox/experiments/linked/case_preparation/`，输入清单的 `cases` 行使用 `case_id`、`path`、`patient_key`、`data_kind`、`group`、`split` 及已冻结的文件哈希。
- **核对支付**：用 `.venv/bin/python -m handoff.run --config private/run.local.json status` 看结束状态计数。已关闭的批次用同一入口的 `audit --observe-native` 核验；只在原生环境仍在时使用 `--observe-native`，输出留在本机。逐条原始运行在批次目录，审计、模型调用量和费用分开保留。应以原生读回确认已缴费，不能把模型文本或退出码当作缴费。

仓库只含代码、配置样例和汇总。MIMIC 数据和衍生病例、原始运行记录、账号凭据、OpenHIS 种子或发行包都在获准的本地环境中。对 MIMIC 使用受约束的模型路由时，配置中的处理授权必须覆盖本次账号、端点和上游；详见 [PhysioNet 使用说明](https://physionet.org/news/post/llm-responsible-use/)。更换服务或模型是新的配置，不属于同一批的换 key 续跑。
