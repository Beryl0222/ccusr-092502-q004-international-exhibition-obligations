# 国际联展交付责任链

服务端交付系统：把文博机构与海外古城联盟之间分散在备忘录与往来函件中的承诺，
拆成**有责任方、期限、前置证据与可接受替代方案**的条款，并管理借展目录、
状态报告、权利许可、箱件交接、专家邀请与对外发布。所有业务事实以追加事件
表达，最终可导出一条完整责任链，说明每个箱件为何可移交、当前由谁保管。

## 核心规则

- **协议版本**：登记、取代、取消。新版本不会复活旧版本；对已失效版本
  新设承诺、接受承诺、签署放行一律拒绝。
- **承诺（Obligation）**：责任方、职责（馆藏/保险/运输）、期限、前置
  证据承诺、可接受替代方案。承诺须先被对方接受（可凭外部回执）才能提交证据。
- **职责分离**：证据提交人**不得复核自己提交的证据**；复核通过才视为
  承诺满足；驳回后须重新提交证据。
- **三职责放行**：箱件离馆必须由**馆藏、保险、运输**三个职责分别确认，
  缺一个确认都不可放行；每个职责绑定该职责承诺，承诺未满足不能签署。
- **过期只暂停关联对象**：任一承诺过期，只暂停与其关联的箱件和公布版本，
  其他箱件（如已交付箱件）不受影响；按登记的替代方案补证并复核通过后，
  仅解除与该承诺关联的暂停。
- **回执按外部编号去重**：同编号同内容幂等；同编号内容冲突转人工协调，
  首收内容保留为准候选，**到达顺序不决定真假**，协调员可裁决
  `FIRST` / `LATEST` / `MANUAL`。冲突未决的回执不能用于承诺接受。
- **跨时区迟到**：回执携带业务时间（必须有时区），迟到确认照常登记但永不
  改变版本状态，指向失效版本的回执不能确认新版本下的承诺。
- **取消可追溯**：版本取消后箱件与公布版本追加暂停；在途箱件的实际交接、
  位置与保险责任仍可继续登记；从未放行的箱件标记 `CANCELLED`，全部记录保留。

## 目录

- `contracts/domain.json` — 聚合、事件、职责与状态枚举。
- `src/envelope.py` — 事件信封基础校验（字段、时区时间、版本号）。
- `src/domain.py` — 事件目录、只读投影与 `ResponsibilityChainService` 业务规则。
- `src/storage.py` — append-only 存储：`InMemoryEventStore` 与 `JsonlEventStore`（每行一个事件，可重启重放）。
- `src/api.py` — 仅标准库的 JSON HTTP 适配层；路由逻辑为纯函数 `dispatch`。
- `scripts/demo.py` — 端到端联调剧情（固定时钟，覆盖上述全部规则）。
- `data/sample.json` — 基础信封校验样例。
- `data/demo_journal.jsonl` / `data/chain_export.json` — 演示生成的事件日志与责任链导出样例。

## 运行

```bash
python3 -m unittest discover -s tests       # 53 个测试
python3 -m compileall -q src tests scripts  # 编译检查
python3 scripts/demo.py                     # 端到端联调剧情
python3 -m src.api --port 8080 \
  --journal data/runtime.jsonl              # 启动服务（留空 --journal 则仅内存）
```

## HTTP API（联调）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /versions` | 登记协议版本（`supersedes` 可同时取代旧版本） |
| `POST /versions/{ref}/cancel` | 取消版本（暂停其箱件与公布，不删记录） |
| `POST /versions/{ref}/closure` | 结项核对，快照当时箱件保管状态 |
| `POST /obligations` | 定义承诺（责任方/期限/前置/替代方案/关联箱件） |
| `POST /obligations/{code}/accept` | 接受承诺（可附 `confirming_ack_ref`） |
| `POST /obligations/{code}/evidence` | 提交证据（过期后须带已登记 `alternative_code`） |
| `POST /obligations/{code}/evidence/review` | 复核证据（提交人不得复核自己） |
| `POST /crates` | 登记箱件并绑定三职责承诺闸门 |
| `POST /crates/{code}/link` | 追加关联承诺（过期暂停的影响范围） |
| `POST /crates/{code}/gates/sign` | 馆藏/保险/运输某一职责签署确认 |
| `POST /crates/{code}/release` | 三职责齐全且无暂停时放行离馆 |
| `POST /crates/{code}/handovers` | 箱件交接（接收方、位置、保险责任方、是否终交） |
| `POST /acks` | 登记外部回执（按 `external_ref` 去重/转人工） |
| `POST /acks/{ref}/resolve` | 人工协调冲突回执 |
| `POST /publications` | 批准对外公布素材（可绑定承诺） |
| `POST /maintenance/sweep-expirations` | 到期巡检（各写命令也会自动巡检） |
| `GET /chain` | 导出责任链 |
| `GET /events` | 查看原始追加事件 |

时间字段一律使用带时区的 ISO 8601（如 `2026-09-20T09:00:00+08:00`），
跨时区消息以事件业务时间裁决。

最小联调示例：

```bash
curl -s localhost:8080/health
curl -s -X POST localhost:8080/versions -H 'Content-Type: application/json' \
  -d '{"version_ref":"V1","title":"联展备忘录"}'
curl -s localhost:8080/chain | python3 -m json.tool
```

## 责任链导出说明

`GET /chain` 中每个箱件包含：

- `status`：`LISTED / READY / RELEASED / IN_TRANSIT / DELIVERED / SUSPENDED / CANCELLED`；
- `releasable` 与 `blocking_reasons`：**为何可以/不可以移交**，逐职责列出
  缺失确认及承诺当前状态（未接受/证据未复核/已过期须替代履行）；
- `gates`：三职责各自绑定的承诺、通过的证据与复核人、签署人与签署时间；
- `current_custodian` / `current_location` / `insurance_party`：**当前由谁
  在哪里保管、保险责任在谁**；
- `custody_chain`：自馆藏原点起每次交接的完整链路；
- `suspensions`：当前仍生效的暂停原因（承诺过期 / 版本取消）。

## 事件溯源约定

- 事件一经追加不得原地修改；状态更正一律由后续事件表达（驳回、过期、
  暂停、恢复、取消、协调裁决都是追加事件）。
- 每个聚合的 `version` 从 1 单调递增；JSONL 日志加载时校验连续性，
  检测到缺口直接报错，防止半写或串号日志被静默接受。
- 当前投影由事件流纯函数重建（`build_projection`），重启后从日志恢复，
  导出结果与重启前一致（由 `tests/test_api.py` 的重启用例保证）。
