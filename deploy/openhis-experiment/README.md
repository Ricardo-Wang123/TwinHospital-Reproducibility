# 独立 OpenHIS 实验实例

本目录运行当前 whale/Java17 OpenHIS 发布包。只管理隔离 Compose 项目 `twinhospital-exp`；与现有 HIS 项目、账户、数据库和端口分离。用 `TH_OPENHIS_ASSETS` 指向已授权的发布目录，软件和种子数据库按只读方式挂载。见[接手指南](../../README.md)；续跑已有批次时使用其原部署目录，确保观测文件哈希不变。

```sh
python3 deploy/openhis-experiment/manage.py up
python3 deploy/openhis-experiment/manage.py provision
python3 deploy/openhis-experiment/manage.py status
```

`up` 首次自动创建 0600 的 `runtime/secrets.env`，`provision` 在独立数据库建立实验角色及当天排班，并应用 `experiment-fixtures.sql`。不输出凭据。`stop` 只停止本项目；未提供删除数据库或重置功能。不要运行桌面原有启动、停止或重置脚本。

后端与 PostgreSQL 仅连接 Docker internal 网络，没有对外网络连接，也不映射数据库端口。Nginx 网关在 `127.0.0.1:28081` 暴露 `/api/app/` 和健康检查。药品医保上传、医保、银联及电子票据模块关闭；仅 `CASH` 本地账务记录。没有实际支付。

## 与 MAS 接口

```python
import json
from sandbox.experiments.linked.native_his import NativeOpenHISPort
port = NativeOpenHISPort(
    state_dir="output/example/native",
    catalog_mapping=json.load(open("deploy/openhis-experiment/catalog_mapping.json")),
)
result = port.execute(operation, payload, context)
```

默认凭据位置为本目录 `runtime/accounts/credentials.json`，必须 0600。每例每条件使用独立 `run_id` 和 `state_dir`。`context` 包含 `case_id/run_id/role`，挂号后加入实际 `patient_id/encounter_id`。首轮顺序执行；多个进程共享同一 state 目录有文件锁，同一逻辑请求用稳定 `idempotency_key`。不允许更改键对应的请求体，不盲重试无法确定结果的写请求。

- 原生业务：建档、挂号、医生接诊、诊断、处方及签发、文书版本与锁定、预结算、现金结算与读回。
- `diagnosis.catalogue` 读出实验目录；医生必须根据本次交接与问诊返回 `diagnoses:[{code,display,system,primary,certainty}]`。不从病例类别填诊断。`consultation.save` 将这些诊断写入原生 HIS；对话本体仍保存在持久化 sidecar，并明确字段来源。
- `history.import/read`、`handoff.save/read` 是持久化 runtime sidecar，`native_effect=false`；不声称原生 HIS 历史导入。
- 文书签署是医生账号提交后 `LOCKED` 应用状态；不是 CA 或电子签名。原生处方签发读回 `ACTIVE`、处方号及签发时间；源服务读模型的 `signedById` 为空，不用其冒称已验证签名人。
- 角色路由、患者绑定、幂等及文书本地版本检查属于 runtime gateway。返回 `protection_source`，不得当作原生服务本身的安全能力。原生拒绝与网关拒绝需要分开统计。

## 数据和字典边界

源种子有既存临床记录，禁止将其查询结果送给模型。适配器只为本轮虚构输入创建新对象，不采用既存病人；医院身份、生日、电话均为登记占位，不代表来源临床信息，实际年龄必须使用病例自己的来源字段。每条注册记录留有此说明。

五种 `SIM-*` 药品是独立建立的工程目录，保持各自成分、规格、剂型；价格和库存完全模拟，未验证真实药品产品注册或收费。剂量与开药数量分别保留，例如 `500 mg` 与 `14 tablet`。本版原生显示组件对 `dose_unit_ratio` 使用除法，库存换算组件用乘法，语义冲突；工程目录把该自动换算设空，禁止以剂量单位出库计费。处方仍逐字段核对剂量、数量、单位、途径、频次、患者和就诊，结算用明确数量单位。此限制不能作为真实药品安全验证的结论。

新增的五条诊断目录是本地实验术语记录，未注册医保映射；它们提供可选择标签，不是病例真值。若医生输出目录外诊断则保留失败，不能替换为最近类别。
