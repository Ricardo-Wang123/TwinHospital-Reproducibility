# TwinHospital 运行与扩跑指南

同一个病例从挂号、预问诊、接诊开方、审方修订、病历签署，到 **OpenHIS 模拟药费缴费并读回**。角色用 `deepseek-v4.1-flash-1017`，AI 工具用 `glm-5.2`；两条路由都关闭推理。支付只在隔离 OpenHIS 记账。

论文当前的 170 次就诊汇总和复算命令见 [results](../results/README.md)。`cohort-170-runtime` 标签固定产生这批结果的运行源文件。后续运行使用主分支及相应的冻结配置。

## 在原实验机换 key 续跑

原实验机已有 `private/recover.local.json`，它指向冻结病例、原生 OpenHIS 实例、既有账本和审计过的恢复清单。在仓库根目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-linked.txt
.venv/bin/python -m handoff.run --config private/recover.local.json check
.venv/bin/python -m handoff.run --config private/recover.local.json set-key
.venv/bin/python -m handoff.run --config private/recover.local.json run --execute
```

`check` 仅读取配置和审计记录。以它返回的 `retained_paid` 和 `unpaid_to_dispatch` 确认本次恢复范围。`set-key` 将密钥保存到本机 `0600` 文件。`run --execute` 先以无临床内容的请求核对两条模型路由，再调度病例。恢复沿用原账本、端点、模型和临床配置，已核验的缴费记录保留。每个批次使用一个恢复调度器。

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

扩跑前先用小批归档完整执行一次论文指标导出。逐项冻结指标定义、评价单位、来源字段和分母，并核对以下证据。

| 指标组 | 扩跑前验收 |
| --- | --- |
| 预问诊和文书质量 | 每个要评分的事实对应参考项、允许来源和适用性。风险召回使用明确的规则信号；仅靠已缴费状态只能核验交付。 |
| 审方 | 保留原处方、初审事件、规则参考和严重性。先检查阳性、阴性、拦截和可判定分母。自然处方样本与操作注错分开。 |
| APP II | 单项活动有角色和任务边界，补问、纠正、首稿与返工有事件，问诊结束和签署共用运行时钟。 |
| APP III | 完整流程和局部操作分别登记分母，并保留请求、网关决定、原生效果和读回。 |

先核对每个拟报告的结果单元能由归档回算，再扩大病例量。没有适用分母时保留未定义状态。

- **接续同一批**：使用 [恢复配置模板](../config/run.recover.example.json)。恢复清单、原批次目录、已归档运行和同版本的 HIS 观察部署是校验凭据；旧路径指针和文件哈希都需要可读。最简单是在原实验机执行。新机没有这些本地文件时，可以启动独立新批次，不能声称续接了旧批次。
- **启动下一批**：复制 [新增组合批次模板](../config/run.factorial.example.json) 到 `private/`，指向事前冻结、与已跑病例不重叠的授权病例清单、同版药品／诊断字典及应用过的对应 SQL fixtures、原生观察和规则配置。冻结新窗口后先 `check`，再 `run --execute`。正式留出设计使用 `mode: formal`，额外提供 `analysis_plan`、`acceptance_batch`、`acceptance_audit`。病例准备代码在 `sandbox/experiments/linked/case_preparation/`，输入清单的 `cases` 行使用 `case_id`、`path`、`patient_key`、`data_kind`、`group`、`split` 及已冻结的文件哈希。
- **核对支付**：用 `.venv/bin/python -m handoff.run --config private/run.local.json status` 看结束状态计数。已关闭的批次用同一入口的 `audit --observe-native` 核验；只在原生环境仍在时使用 `--observe-native`，输出留在本机。逐条原始运行在批次目录，审计、模型调用量和费用分开保留。应以原生读回确认已缴费，不能把模型文本或退出码当作缴费。

仓库只含代码、配置样例和汇总。MIMIC 数据和衍生病例、原始运行记录、账号凭据、OpenHIS 种子或发行包都在获准的本地环境中。对 MIMIC 使用受约束的模型路由时，配置中的处理授权必须覆盖本次账号、端点和上游；详见 [PhysioNet 使用说明](https://physionet.org/news/post/llm-responsible-use/)。更换服务或模型是新的配置，不属于同一批的换 key 续跑。
