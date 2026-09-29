# 银行外币现钞兑换预约与券别核对平台（演示）

零依赖（Python 3 标准库 + 原生前端）、SQLite 持久化的银行外币现钞兑换预约平台。
**客户、证件、冠字号、汇率均为虚构数据**，仅用于业务流程与并发一致性演示。

## 一键运行

```bash
cd fx_cash

# 一条命令：创建预约 → 提交锁库 → 确认快照 → 并发竞争 → 替换再确认 → 交付（差额+假钞）→ 幂等重放 → 权限复核 → 取消/到期释放
python3 main.py demo

# 启动 Web 平台（默认 http://127.0.0.1:8080，可用 PORT / FX_DB 覆盖）
python3 main.py serve

# 运行全部测试（16 项）
python3 main.py test

# 重建正式库 fx.db
python3 main.py reset
```

## 六大页面

| 页面 | 内容 |
|---|---|
| 币种库存 | 网点 × 币种 × 券别的紧凑矩阵（可用 / 锁定 / 总量），紧俏标黄、缺货标红；可新建预约 |
| 预约日历 | 月历标注每笔预约的创建日（钴蓝）与取钞期限（柠檬黄） |
| 汇率版本 | 按币种列出版本、生效日与牌价；下方为全部业务事件流水 |
| 柜面核验 | 预约卡：**取钞期限柠檬黄/橙色突出**；提交锁库、确认、提出替换、客户再确认、取消、扫描到期 |
| 现钞交付 | 模拟点验钞机注入实点数量/假钞疑点；**预约数量 / 实点数量 / 差额并排对照**；幂等键重放 |
| 差额复核 | 待差额/疑点清单与已复核清单；右上角切换 T01/T02 柜员、T03 主管 |

配色：**钴蓝 #0047AB · 银白 #EEF2F6 · 柠檬黄 #FDE047**；金额列固定小数位（本币 2 位、汇率 4/5 位）。

## 核心业务规则

- **预约**：保存币种、券别、数量、取钞网点、有效期；草稿不锁库，**提交时原子锁定可用库存**。
- **汇率快照**：确认时读取当时的汇率版本写入预约（`fx_rate / rate_version_id / cny_amount`），
  此后牌价再变不影响已确认金额；确认日落点不同于预约日则记 `rate_cross_day`（跨日）事件。
- **券别替换**：柜员只能"提出"方案（`replacement_offered`），必须**客户重新确认**后才释放原锁、换锁新券别并刷新汇率快照；客户可拒绝而维持原券别。
- **库存竞争**：所有锁库走 `UPDATE inventory SET locked_qty=locked_qty+? WHERE ... AND total_qty-locked_qty>=?`
  条件更新，包在 `BEGIN IMMEDIATE` 事务中；**两名柜员同时占用同一券别，恰好一笔成功**，另一笔收 409 且不部分占用。
- **取消 / 未到场到期**：客户取消或 `expires_at` 到期后，`expire_due()` 扫描并释放全部 held 锁库。
- **现钞交付**：经模拟点验适配器逐券别点验，按实点出库；可注入**交付差额**与**假钞疑点**并分别留事件。
- **交付幂等**：幂等键（`idem_group`）命中即返回原记录，**订单已 fulfilled 也先于状态校验**，绝不重复扣库。
- **柜员权限**：差额/疑点复核仅 `supervisor`，交付人与复核人不能同一人；**已复核金额柜员不得修改**，复核前可修正。
- 库存不足、券别替换、假钞疑点、汇率跨日、客户取消、交付差额均写入 `events` 业务流水。

## 数据模型（SQLite）

```
currencies / denominations / branches / tellers
inventory(branch, denom, total_qty, locked_qty)     -- CHECK locked<=total
rate_versions(currency, rate, eff_date)             -- 汇率版本
reservations + reservation_lines(original/replacement, replacement_of, customer_accepted)
inventory_locks(reservation, denom, qty, held/released/delivered)
deliveries(booked_qty, counted_qty, diff_qty, suspicious,
           idem_group, delivered_by, reviewed_by)   -- UNIQUE(reservation,denom,idem_group)
events                                              -- 业务事件流水
```

## 模拟适配器（`adapters.py`）

- `FxAdapter`：本地汇率版本读取（确认日取 `eff_date <= 当日` 的最新版）。
- `CashValidatorAdapter`：模拟点验钞机，默认账实相符；通过 `overrides={denom_id:{counted,suspicious}}`
  确定性注入差额与假钞场景，并生成虚构冠字号留痕。

## 代码结构

```
fx_cash/
├── main.py        # demo / serve / test / reset 入口
├── db.py          # Schema、每线程连接 Database、虚构种子数据
├── adapters.py    # 模拟汇率源 + 现钞点验/鉴伪适配器
├── services.py    # 业务服务层（事务、锁库、快照、替换、交付、复核、过期）
├── server.py      # http.server JSON API + 静态托管
├── static/        # index.html / styles.css / app.js（六大页面）
└── tests/         # test_services.py（16 项）
```

## 测试覆盖

提交锁库、库存不足留痕、**券别并发竞争只成一笔**、汇率快照与新牌价隔离、跨日留痕、
替换再确认/拒绝/库存不足失败安全、取消释放、**预约过期释放**、**交付幂等不重复扣库**、
差额与假钞、交付前置状态、**柜员/主管权限与已复核金额锁定**、券别币种一致性。
