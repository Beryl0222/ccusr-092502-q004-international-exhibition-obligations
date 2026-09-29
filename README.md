# 国际联展交付责任链

文博机构与海外古城联盟联合展览的服务端交付系统。把分散在备忘录与往来函件中的承诺，
拆成**带责任方、期限、前置证据与可接受替代方案**的可核验单元，并管理借展目录、
状态报告、权利许可、箱件交接、专家邀请与对外发布。

只依赖 Python 3.11+ 标准库，可在单个 Linux 容器中直接运行。

## 核心规则

- **承诺即条件**：每条承诺有唯一责任方、截止时间、前置证据种类清单，以及逐项声明的
  可接受替代证据。前置证据齐备（或经声明的替代方案满足）后，承诺才开放。
- **物件离馆三职责分签**：馆藏（COLLECTION）、保险（INSURANCE）、运输（TRANSPORT）
  三种职责分别确认，**同一责任方不得兼任两岗**，任一岗不可重复确认。
  数字复制品可在箱件上声明某岗“不适用”（须写原因），但保险职责仍须确认。
- **提交人回避**：证据的提交人不得是复核签收人。
- **过期只暂停关联对象**：任一承诺过期，仅暂停其显式关联的箱件与公布版本，
  不波及无关箱件；逾期后补齐证据（含替代方案）可解除暂停，承诺标记为
  `RECOVERED` 保留逾期痕迹。
- **迟到确认不复活失效版本**：协议版本被取代或取消后，针对它的新承诺/确认一律拒绝；
  对方时区迟到的入站回执只登记并标记 `late_and_not_effective`，不产生生效效果。
- **回执按外部编号去重，冲突转人工**：同一 `ack_ref` 内容一致的重发只留丢弃记录；
  内容不一致时**全部**版本进入人工协调案件，系统不按到达先后裁定真假；
  裁决后，与裁决一致的重发仍去重，矛盾内容另开新案。
- **取消可追溯**：取消箱件必须留痕最后实体位置、当前保管人与保险责任方；
  已发布记录不得删除，只能以新版本取代，原发布记录持续可查。
- **事实只追加**：所有变化都是领域事件（聚合内单调 version、全局单调 seq），
  持久化为 JSONL，重启重放即恢复全部状态；更正由后续事件表达，不改写历史。

## 目录

- `contracts/domain.json` — 聚合、状态机、事件名、职责与证据种类目录。
- `src/envelope.py` — 共用事件信封校验（保留自领域边界种子）。
- `src/eventstore.py` — 追加式事件存储（JSONL、版本号、并发锁、重放）。
- `src/domain.py` — 事件折叠投影与全部状态推导（纯函数，无时钟副作用）。
- `src/service.py` — 应用服务：命令校验与业务规则。
- `src/api.py` / `src/app.py` — HTTP/JSON API 与启动入口。
- `scripts/demo.py` — 不依赖网络的一轮借展交付演示。
- `tests/` — 领域规则、事件存储、HTTP 端到端共 30 个测试。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests

# 编译检查
python3 -m compileall -q src tests

# 内存演示（打印责任链）
python3 scripts/demo.py

# 启动 HTTP 服务（默认 data/eventlog.jsonl）
CHAIN_STORE_PATH=data/eventlog.jsonl CHAIN_PORT=8080 python3 -m src.app
```

环境变量：`CHAIN_STORE_PATH`（留空为纯内存）、`CHAIN_HOST`、`CHAIN_PORT`。

## API 概览

路径中的标识既可用内部 id，也可用业务编号（如 `MOU-1`、`CRATE-A`、`OBL-INS`）。
写操作均为 `POST` + JSON；业务拒绝返回 `422`（中文原因），版本冲突返回 `409`。

| 动作 | 路径 |
| --- | --- |
| 起草/生效/取代/取消协议 | `POST /agreements`，`/agreements/{id}/activate|supersede|cancel` |
| 登记承诺 | `POST /obligations` |
| 常规证据签收 | `POST /obligations/{code}/evidence` |
| 替代证据解除 | `POST /obligations/{code}/alternative-evidence` |
| 过期扫描（可传 `now`） | `POST /admin/sweep-expired` |
| 借展目录 | `POST /objects` |
| 集结/装箱/三岗确认 | `POST /crates`，`/crates/{code}/objects`，`/crates/{code}/confirmations/{collection\|insurance\|transport}` |
| 放行/交付/退回/取消 | `POST /crates/{code}/release|deliver|return|cancel` |
| 专家邀请 | `POST /invitations`，`/invitations/{code}/acknowledge|reissue|cancel` |
| 公布版本 | `POST /publications`，`/publications/{code}/approve|publish|supersede|cancel` |
| 外部回执 | `POST /acks`（自动去重/升级冲突） |
| 人工裁决 | `POST /cases/{caseId}/resolve` |
| **责任链导出** | `GET /chain` |
| 只读查询 | `GET /agreements /obligations /crates /objects /publications /invitations /acks /cases /events` |

### 责任链导出说明

`GET /chain` 逐箱给出：

- `status` 与 `why`：**为何可移交**（三岗齐备且门控承诺满足）或为何暂停；
- `role_confirmations`：三岗的确认方、时间、证据编号，或“不适用”原因；
- `gating_obligations`：每条门控承诺的责任方、期限、缺证、替代方案与签收记录，
  并区分协议版本是否仍生效；
- `current_custodian` 与 `custody_ledger`：**当前由谁保管、在何处**，以及完整保管链；
- `cancellation`：取消箱件的最后位置、保管人与保险责任留痕。

### 最小联调序列

```bash
curl -s -XPOST localhost:8080/agreements -d '{"agreement_ref":"MOU-1","title":"联展"}'
# ... activate → obligations → crates → confirmations(三岗)
#     → obligations/{code}/alternative-evidence（替代证据）
#     → crates/{code}/release → deliver
curl -s localhost:8080/chain    # 导出每个箱件为何可移交、当前由谁保管
```

## 设计取舍

- 状态推导集中在 `domain.py` 的事件折叠里，服务层只负责“此刻该不该落事件”，
  因此同一份事件流重放必然得到同一状态，便于审计与联调对齐。
- 暂停/解除对箱件是投影派生（无冗余事件）；公布版本因有审批语义，
  用 `PUBLICATION_HELD` / `PUBLICATION_HOLD_LIFTED` 显式留痕。
- 时间均要求带时区；过期判定以落库的 `OBLIGATION_EXPIRED` 事件为准，
  扫描端点支持显式 `now`，方便跨时区联调复现。
