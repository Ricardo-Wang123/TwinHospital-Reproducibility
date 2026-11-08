# 已核对的结果

`verified_paid_results_170.json` 是 170 条已缴费运行的汇总，对应论文现有表格。来源是同一次完整就诊的交接、原始处方审核、文书、业务读回和角色活动记录。170 条运行来自 10 名来源患者。`batch_status.json` 保留启动、缴费和中断的分母，供续跑使用。

- 处方数据保留首轮决定和规则适用范围。误拦率以可判定且已作出决定的规则阴性订单为分母。规则阳性和被拦截的参考订单为空时，相应召回率、精确率与 F1 保持未定义。
- 单项模拟工时按任务边界和角色统计，整次就诊工时另存。首轮交接完成、文书返工和首稿接受来自对应事件。问诊结束至文书锁定、排队至缴费的耗时来自运行时钟。
- 缴费组按验证过的完成状态选入；不能用该组估计全体任务的完成率。未并入扩展测试、故障注入结果或其他批次。

有权限的运行者可在原始归档所在机器复算：

```sh
.venv/bin/python -m results.summarize_paid_runs \
  --batch /private/path/to/frozen_batch \
  --audit /private/path/to/native_audit.json \
  --case-manifest /private/path/to/frozen_case_manifest.json \
  --output /private/path/to/verified_paid_results_170.json
```

脚本核对运行文件与审计哈希，使用冻结病例清单计算规则范围内的处方参考，并从实际交付检查和活动记录计算汇总。仓库保存按配置汇总的结果。
