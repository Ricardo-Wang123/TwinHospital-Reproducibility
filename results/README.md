# Verified Results

The manuscript snapshot reports 170 verified paid encounters. [Encounter metrics](verified_paid_results_170.json), [operation replay results](operation_replay_10.json), and [generated LaTeX](acl_results_170.tex) provide the corresponding table values. Recomputing them from traces requires the authorized local archive.

`verified_paid_results_170.json` 是 170 条已缴费运行的汇总，对应论文现有表格。来源是同一次完整就诊的交接、原始处方审核、文书、业务读回和角色活动记录。170 条运行来自 10 名来源患者。`batch_status.json` 保留启动、缴费和中断的分母，供续跑使用。

- 处方数据分别记录规则、AI 和药师发出的标记或拒绝，药师复核，以及首轮处置。原方和后续复审分别统计，每轮各来源均可计入。同一轮出现多来源告警时，在合计中只计一次。全程另统计出现告警的就诊数。误拦率以可判定且已作出决定的规则阴性订单为分母。
- 单项模拟工时按任务边界和角色统计，整次就诊工时另存。首轮交接完成、文书返工和首稿接受来自对应事件。问诊结束至文书锁定、排队至缴费的耗时来自运行时钟。
- 交接后续处理以通过交付核验的交接为分母。诊中稿是否可用取同一轨迹中问诊结束前的中间稿接受事件。
- 缴费组以终态和事务核验为纳入条件。汇总按首方直接签发、首轮审方未通过、首轮批准但原生编码不通过三条路径分组。故障探测另用故障请求和合规对照作为分母。
- 工时敏感性使用同一批活动记录，对采集与撰写、核对与返工分别取 0.5、1、2 倍权重。表格报告辅助配置相对同任务基线的差值范围。
- 配对工时比较固定来源病史和另外两项任务配置，仅纳入两侧都有合格产物的组合。在各病史内平均配对差值，再等权汇总病史。

有权限的运行者可在原始归档所在机器复算：

```sh
.venv/bin/python -m results.summarize_paid_runs \
  --batch /private/path/to/frozen_batch \
  --audit /private/path/to/native_audit.json \
  --case-manifest /private/path/to/frozen_case_manifest.json \
  --clinical-catalogue /private/path/to/frozen_product_catalogue.json \
  --output /private/path/to/verified_paid_results_170.json
```

脚本核对运行文件与审计哈希，使用冻结病例清单计算规则范围内的处方参考，并从实际交付检查和活动记录计算汇总。仓库保存按配置汇总的结果。

论文摘要、正文数字和结果表共用一个生成文件。按同一设计扩跑后，先用上面的命令重算任务指标，再运行：

~~~sh
.venv/bin/python -m results.render_linked_manuscript \
  --metrics /private/path/to/linked_metrics.json \
  --workflow-audit /private/path/to/workflow_audit.json \
  --human-review /private/path/to/human_review.json \
  --probes /private/path/to/operation_replay/summary.json \
  --output /path/to/paper/acl_results.tex
~~~

工作流审计使用包含逐运行条目的审计 JSON。人工复核 JSON 的 `cohort_sha256` 取任务汇总中的 `paid_cohort_sha256`，`reviewed` 填实际复核份数。如果已有逐例判定，`outcomes` 填 `appropriate`、`inappropriate` 和 `indeterminate` 三个计数，三者之和须等于 `reviewed`。只掌握覆盖范围时，`outcomes` 填 `null`。原生操作探测的汇总可以独立更新，它和完整就诊各有自己的分母。

将生成的 `acl_results.tex` 放进同一版论文源码，加入经作者核验的 `references_verified.bib`，在正文和附录的对应位置插入引用键，再编译 `twinhospital_acl.tex`。数值文件会一起更新样本数、摘要、分组结果、复审告警、人工复核和安全探测结果。
