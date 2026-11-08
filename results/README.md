# 已核对的结果

`verified_paid_results_170.json` 是 170 条已缴费运行的汇总，对应论文现有表格。来源是同一次完整就诊的交接、原始处方审核、文书、业务读回和角色活动记录。170 条运行来自 10 名来源患者。`batch_status.json` 保留启动、缴费和中断的分母，供续跑使用。

- 处方数据是首轮决定的计数。后续修订不会覆盖原决定；没有规则参考标签时，不能由它推算准确率、召回率或误拦率。
- 模拟工时来自冻结的活动权重。排队到缴费的耗时是实际运行时钟的观测，另行统计。两者都不是医务人员真人耗时。
- 缴费组按验证过的完成状态选入；不能用该组估计全体任务的完成率。未并入扩展测试、故障注入结果或其他批次。

有权限的运行者可在原始归档所在机器复算：

```sh
.venv/bin/python -m results.summarize_paid_runs \
  --batch /private/path/to/frozen_batch \
  --audit /private/path/to/native_audit.json \
  --output /private/path/to/verified_paid_results_170.json
```

脚本核对完成运行与审计中的文件哈希一一匹配，再从实际交付检查和活动记录计算。不输出病例、提示词、模型回答、逐例 ID 或来源哈希。
