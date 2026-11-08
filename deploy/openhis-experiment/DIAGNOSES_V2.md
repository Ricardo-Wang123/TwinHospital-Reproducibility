# 开发版诊断目录 v2

更新时间：2026-11-07。所属项目：TwinHospital。

当前目录有 **63 项**，使用本机正式 MIMIC-IV 2.2 包中 `hosp/d_icd_diagnoses.csv.gz` 的 `icd_version=10` 原码及英文原描述。公开字段标为 `ICD-10-CM`，词表版本为 `MIMIC-IV-2.2-source-era-unspecified`；不声称是已核验的 FY2027 或现行医院收费词表。

v2 复用原来的五个原生 ID，并在 **twinhospital-exp** 独立库追加 58 个新定义。完整映射含原生定义 ID，供本地运行。

## 覆盖范围与界限

- 二型糖尿病及肾病、神经病变、视网膜病变、血糖异常等相关细分。
- 心衰的收缩性/舒张性/合并类型，以及急性/慢性和部分相关高血压疾病。
- COPD 加重或感染、肺炎常见分类及相关呼吸问题。
- 尿路感染、膀胱炎、肾盂肾炎等。
- 完成治疗后随访、重复处方就诊、长期用药和部分既往史状态。
- 部分常见共病和症状代码，支持“尚不能确定疾病”的表达。

50 例开发素材的非身份诊断聚合中，属于五类目标病的 ICD-10 源代码共 18 个，本目录覆盖 18/18。这个覆盖统计只帮助补齐可选词表；它不把历史诊断或病例分组交给医生当本次正确答案。

候选现代代码 `N18.30/N18.31/N18.32/R05.9` 不在源字典，列为 pending，未自动替代。另显式纳入源字典确实存在的 `N18.3` 和 `R05`，保留其源年代属性。对每个已加入代码检查“在整个提供的源字典中没有更长前缀子码”；这只证明该源字典中的终端性，**不证明现行全国目录中的叶级有效性或可报销性**。

不在目录的诊断保持 unknown/defer，不能为开方结算选择近似但错误的疾病。目录中存在急性疾病也不意味着患者适合普通门诊处置。病人本次诊断仍须由医生基于本次材料产生并保留 working/confirmed 状态。

参考资料：[MIMIC 诊断字典说明](https://mimic.mit.edu/docs/iv/modules/hosp/d_icd_diagnoses.html)、[MIMIC-IV 2.2](https://physionet.org/content/mimiciv/2.2/)。

## Z09 与 Z76.0

`Z09` 用于完成治疗后的随访检查，不能作为持续管理慢性病的通用标签。`Z76.0` 表示重复处方开具就诊，仅在本次实际就诊目的支持时使用。

这两个含义另行核对了 [FY2027 官方编码指南](https://www.cms.gov/files/document/fy-2027-icd-10-cm-coding-guidelines.pdf) 中的随访及其他就诊原因部分。该核对不等于目录全部 63 项已按 FY2027 逐项验证。

## 重建与安装

```sh
.venv/bin/python deploy/openhis-experiment/build_diagnoses_mimic_v2.py \
  --archive /absolute/private/path/to/mimic-iv-2.2.zip
```

此命令只读诊断字典成员，不读患者、就诊或诊断关系表。生成 `diagnosis_mapping_v2.json`、`diagnosis_metadata_v2.json` 与 `experiment-diagnoses-v2.sql`；生成物留在有授权的本地环境。metadata 保存整个 ZIP、压缩成员和解压 CSV 的 SHA-256。SQL 只有追加；ID 冲突时验证原行，绝不覆盖旧定义。

通过 `manage.py` 在独立 Compose 项目应用 SQL。

下一版临床编排必须按目录返回的 `system=ICD-10-CM` 使用它，并在规格记录映射文件 hash。旧运行快照继续使用旧映射及旧标签，不改写为新版本。v2 不要求医生知道码的年代；来源元数据供审计与论文方法使用。
