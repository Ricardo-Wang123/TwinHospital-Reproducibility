# TwinHospital 接手与续跑

| 要做的事 | 从哪里开始 |
| --- | --- |
| 用自己的病例和模型开新批次 | [准备数据并启动](REPRODUCE.md) |
| 下载 OpenHIS 与五类虚构样例 | [GitHub Release](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1) |
| 看已经得到的结果 | [170 次已核验就诊和复算入口](../results/README.md) |
| 按论文配置重跑 | [冻结病例包、模型和启动命令](REPRODUCE.md#repeat-the-study-configuration) |
| 提供自己的病例 | [MIMIC 选取规则与病例 JSON 格式](CASE_FORMAT.md) |
| 加入一批新测试患者 | [留出批次的配置与分母](REPRODUCE.md#run-a-held-out-cohort) |

## 新电脑启动

按运行指南安装 Python 3.12 和 Docker，并解压 GitHub 上的 OpenHIS 读者版。用自己的病例 JSON 或示例数据准备输入包，再由 `handoff.inputs configure` 生成 `run.local.json`。`provision` 启动隔离医院并装入字典，用 `set-key` 在本机输入 API key，再启动批次。[新机运行指南](REPRODUCE.md)

新批次可用自选的兼容接口，角色和工具可以共用一个模型。论文这一批的角色模型为 `deepseek-v4.1-flash-1017`，工具模型为 `glm-5.2`，两者都关闭推理，温度为 0.2。复跑论文配置时使用[同版模型配置](../config/provider.example.json)和自己的数据处理记录。

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
