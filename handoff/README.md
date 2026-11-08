# TwinHospital 接手与续跑

| 要做的事 | 从哪里开始 |
| --- | --- |
| 看已经得到的结果 | [170 次已核验就诊和复算入口](../results/README.md) |
| 按论文配置重跑 | [冻结病例包、模型和启动命令](REPRODUCE.md#repeat-the-study-configuration) |
| 提供自己的病例 | [MIMIC 选取规则与病例 JSON 格式](CASE_FORMAT.md) |
| 加入一批新测试患者 | [留出批次的配置与分母](REPRODUCE.md#run-a-held-out-cohort) |

## 新电脑启动

按[新机运行指南](REPRODUCE.md)安装 Python 3.12 依赖并准备同版 OpenHIS。`handoff.inputs configure` 校验病例包、字典和 OpenHIS 文件，生成本机的 `run.local.json`。`provision` 建立隔离 HIS 并按顺序应用对应字典。接手者随后录入自己的 API key，`run --execute` 在发送病例前分别探测两个模型。

患者、护士、医生和药师角色使用 `deepseek-v4.1-flash-1017`，AI 工具使用 `glm-5.2`。两者都关闭推理，温度为 0.2。复跑历史配置时使用仓库中的[同版模型配置](../config/provider.example.json)加上接手者的私有数据处理记录。输入、运行记录、密钥和 OpenHIS 发布包在获准环境中管理。

## 在原实验机换 key 续跑

续跑依赖原批次目录、冻结病例、原生观察记录和审计过的恢复清单。将这些路径填入[恢复模板](../config/run.recover.example.json)，然后在仓库根目录执行：

~~~sh
.venv/bin/python -m handoff.run --config private/recover.local.json check
.venv/bin/python -m handoff.run --config private/recover.local.json set-key
.venv/bin/python -m handoff.run --config private/recover.local.json run --execute
~~~

以预检给出的 `retained_paid` 和 `unpaid_to_dispatch` 核对恢复范围。密钥保存在本机 0600 文件。续跑保留已核验的缴费记录，并使用原模型、规则、字典、观察部署和账本。

如果调度窗口过期，使用原冻结策略作为模板，更新窗口和指定并发后重新预检。

~~~sh
.venv/bin/python -m handoff.freeze_window --template "$PRIOR_WORKFLOW_POLICY" \
  --output private/workflow.local.json --workers 20 --dispatch-hours 72
~~~

## 出结果

闭合批次用 `status` 查看状态计数，用 `audit --observe-native` 核对签发、锁定和缴费。原生观察需在该批的隔离 OpenHIS 仍可读时进行。随后按[结果复算指南](../results/README.md)生成汇总和论文表格。新数据用独立的批次名和结果文件。
