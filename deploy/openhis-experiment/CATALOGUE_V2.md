# TwinHospital 药品候选目录 V2

供 TwinHospital 实验实现与论文复核使用。更新：2026-11-07。

`catalogue_metadata_v2.json` 保留完整产品身份、强度基准、剂型、组成与每项 DailyMed 标签链接；`medication_catalogue.get_formulary()` 返回给角色模型的精简候选。不同疾病队列共用同一目录，没有按病种指定答案，也没有预填剂量、频次、数量或疗程。

默认 14 项：二甲双胍、呋塞米、沙丁胺醇 HFA、阿莫西林、呋喃妥因一水合物/大晶体、赖诺普利、螺内酯、恩格列净、噻托溴铵吸入粉末、泼尼松、阿奇霉素、TMP/SMX、华法林、对乙酰氨基酚。标签核验的范围是成分、剂型、规格与给药途径；临床适用性与六类错误的独立评审仍单独进行。

`catalog_mapping_v2.json` 仅含这 14 项到独立 OpenHIS 实验库的 SIM 映射。原有 `catalog_mapping.json` 与原生 V1 行均保留。新增行使用新主键和 `V2-SIM-*` 业务编码，价格统一为实验假设的每发药单位 1 元，库存为模拟库存，`official_verified_flag=false`，不冒充真实采购产品。

`catalog_mapping_v2_extensions.json` 另含头孢曲松粉针、0.9%氯化钠、5%葡萄糖、乳酸林格液，以及仅供特定错误候选使用的沙库巴曲/缬沙坦。默认门诊目录不包含这些项。IV 产品尚未实现原生输注医嘱与溶媒的关联，目录存在不代表完整输注能力已验证。

## 单位与未知项

- 剂量单位和发药单位分开保存。例如吸入剂是 `puff` 与 `inhaler`，口服单成分通常是 `mg` 与 `tablet/capsule`。
- TMP/SMX 以 `tablet` 开剂量并保留两个组分的强度；不接受含义不明的单个 mg 数字。
- 噻托溴铵胶囊是吸入粉末，需要匹配的吸入设备，不能因包装叫 capsule 就变成口服。
- 90 μg albuterol base/actuation 不与旧 100 μg 沙丁胺醇自动对应；盐与活性部分的强度基准不明时保持 unknown。
- 原生剂量换算字段存在显示与库存换算约定不一致，V2 不启用该转换；模型必须分别提供明确剂量与发药数量。
- `normalize_medication()` 返回独立注释，不修改原始记录。MIMIC 的 NDC/GSN/内部药品码不会被猜测成 SIM 编码；剂型/缓释类型或规格不明时保留原文并标 unknown。历史开过药不推断当前正在服用。

## 构建与安装

`build_catalogue_v2.py` 生成版本化 fixture，`manage.py catalog-v2` 只对 `twinhospital-exp` 应用。

每项产品的 DailyMed 链接位于 `catalogue_metadata_v2.json` 的 `source_references`。
