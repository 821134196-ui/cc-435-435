# 银行外币现钞兑换预约与券别核对平台

管理 **币种 / 券别 / 网点库存 / 客户预约 / 汇率版本 / 柜员复核 / 现钞交付 / 取消记录**。
全部客户、证件、金额均为虚构演示数据；仅依赖 Python 3.10+ 标准库（含 SQLite），无需安装任何第三方包。

## 一条命令跑通样例

```bash
python3 run_demo.py            # 重建 data/fxplatform.db，创建预约→锁库→确认→交付→复核 全套样例
python3 run_demo.py --serve    # 样例跑完后直接启动 Web 界面
```

样例覆盖：建预约锁库、库存不足拦截、汇率快照计价、柜面核验、**短款交付 + 幂等重放**、
柜员/主管权限、券别替换客户重新确认、**假钞疑点** 主管处置、**到期释放** 共 10 个场景。

仅启动 Web 服务：

```bash
python3 -m fxplatform.server          # http://127.0.0.1:8000
python3 -m fxplatform.server --help 2>/dev/null || true
```

运行测试：

```bash
python3 -m unittest discover -s tests -v   # 28 个用例
```

## 六个页面

| 页面 | 内容 |
| --- | --- |
| 币种库存 | 币种切换 + 网点 × 券别紧凑矩阵：可用（总量−锁定）、低库存红色预警 |
| 预约日历 | 按预约日分组的预约卡，**柠檬黄徽标突出取钞截止期限**，临期高亮、到期扫描 |
| 汇率版本 | 版本列表（生效中/已替代）、主管发布新版本；确认时快照，历史版本不可改 |
| 柜面核验 | 校验网点、状态、有效期、汇率快照、本预约锁库净额 |
| 现钞交付 | 预约数量 / 实点数量 / 差额实时对照，假钞勾选标记，金额显示冻结 |
| 差额复核 | **预约数量、实点数量、复核数量三栏并排**；仅主管可复核，复核后冻结 |

右上角可切换当前柜员（张三/李四为柜员，王五为主管）与当前柜面网点。

## 业务规则

- **预约即锁库**：创建预约在单个 `BEGIN IMMEDIATE` 事务内完成插入 + 条件更新锁定
  （`total_qty - locked_qty >= ?`）；库存不足整笔回滚，不留任何锁定。
- **券别竞争**：SQLite 写事务串行化 + 条件 UPDATE 兜底，两名柜员同时抢同一券别时
  只允许一笔成功，另一笔收到 `concurrent_lock`，绝不会超锁。
- **汇率快照**：确认时取确认当日 `active` 版本写入预约（`fx_rate_id`）；预约日与确认日
  不同记 **跨日**（`fx_events.cross_day`），以确认日版本为准。之后再发布新版本不影响
  已确认金额。
- **券别替换**：柜员仅能提出方案（`REPLACE_PENDING`），必须客户重新确认；接受时在一个
  事务内释放旧锁、锁定新方案、重新快照当日汇率并重算金额，方案版本 +1。拒绝则维持原方案。
- **现钞交付**：柜员按实点券别核对，短款/多款登记 `diff_records`；重复提交同一预约
  **幂等**返回原交付单，不重复出库。
- **假钞疑点**：点验标记可疑冠字号后暂扣登记、不出库，仅主管可处置；处置后取消交易并
  释放库存。
- **柜员权限**：柜员可建预约/核验/交付，但不能发布汇率、不能复核、不能处置假钞；主管
  不能直接办交付。已复核交付的金额与复核数量**冻结**，任何角色不可再改。
- **取消 / 到期**：客户取消、柜员取消、到期三类分别记入 `cancellation_records`；
  `expire_due()` 扫描超过 `valid_until` 未到场的预约，置 `EXPIRED` 并释放全部锁定。

金额一律 `Decimal`（4 位存储精度，页面固定 2 位展示），绝不用浮点。

## 模拟适配器（可替换为真实系统）

- `services/adapters.py` `FxAdapter`：按币种/日期确定性生成建议牌价，主管确认发布。
- `services/adapters.py` `CashCountAdapter`：本地模拟点钞机会话，支持预置短款/多款/
  假钞冠字号，同一会话重复点验结果稳定（支撑交付幂等）。

## 目录结构

```
fxplatform/
  config.py                 配色、端口、有效期等配置
  db.py                     SQLite schema + 虚构种子数据（5 币种/2 网点/3 柜员）
  server.py                 零依赖 HTTP 服务 + JSON API
  services/
    adapters.py             模拟汇率源 / 模拟点钞机
    inventory.py            库存矩阵、条件锁定、释放、出库
    fx.py                   汇率版本发布、确认日取值
    appointments.py         预约、确认快照、替换重确认、取消/到期
    delivery.py             柜面核验、交付幂等、差额/假钞、主管复核
    errors.py               业务异常（带 HTTP 状态映射）
    moneyutil.py            Decimal 金额与固定小数位展示
  static/                   index.html + app.js + styles.css（原生 JS）
run_demo.py                 单命令样例
tests/                      领域测试 + HTTP 端到端测试（共 28 个）
```

## 主要 API

```
GET  /api/meta /api/inventory /api/calendar /api/fx /api/diffs
POST /api/appointments                      新建预约（锁库）
POST /api/appointments/{code}/confirm       客户确认（汇率快照）
POST /api/appointments/{code}/cancel        客户/柜员取消（释放）
POST /api/appointments/{code}/replacement            提出券别替换
POST /api/appointments/{code}/replacement/decision   客户重新确认
POST /api/verify /api/deliver /api/review   核验 / 交付 / 主管复核
POST /api/fx/publish                        主管发布汇率
POST /api/counterfeit/resolve               主管处置假钞
POST /api/expire-scan                        到期扫描
```

所有写接口通过 `X-Teller` 请求头（或 body.actor）识别柜员身份。
