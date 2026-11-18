# 九十刻度 · AI 智能运维中心 V1.0

「九十刻度 4.0」的只读自动巡检与异常诊断系统。全部运行在 GitHub Actions 上，
不需要额外服务器，电脑关机后仍按计划执行。

- 正式网站：https://suolunpro.github.io
- 本仓库：`SuolunPro/90scale-ai-ops-`（运维专用，不含预测算法与会员数据）
- 运行方式：每 15 分钟只读巡检（cron）+ 手动触发；仅当判定为 `WARN` / `FAIL` 时才调用 DeepSeek
- 月度预算：10 元人民币，硬上限，用尽即停止 AI 调用

## 一、它做什么

| 检查项 | 内容 | 判定依据 |
| --- | --- | --- |
| `site_home` | 正式网站首页可访问、关键标记完整 | HTTP 200 + `vip-code`、`./data/engine4/` 标记 |
| `site_data_sync` | 线上数据与仓库提交是否一致 | 比对 Pages 实际响应体的 git blob SHA 与仓库同名文件 SHA |
| `engine_fresh` | 预测主引擎当日是否产出快照 | `latest.json` 的 `sale_day` 是否为今天（北京时间）+ 快照年龄 |
| `engine_runs` | 引擎计划点是否漏跑（需 `ENGINE4_READ_TOKEN`） | 与 `config.engine.schedule_bj` 计划点比对，缺 1 个即 WARN |
| `pool_coverage` | 当日赛事池是否完整 | 场次数、`metrics.total`、缺失 `prediction` 的场次 |
| `settlement` | 赛果核验与结算是否超时 | 已结束销售日仍有 `pending` 且开赛超过阈值（读线上已发布数据） |
| `independent_review` | 独立复核数据新鲜度 | `data/independent-review/` 最新文件日期 |
| `sources` | 采集链路可用性（体彩/球来了/FotMob/澳客/LOTA） | 每条链路 HTTP 状态、体积、耗时 |
| `site_workflows` | 网站仓库 Actions 健康 | 近 2 小时失败运行数、是否完全停摆 |
| `legacy_sale_status` | 旧版销售日状态文件 | 仅记录，不告警 |

所有检查都是 HTTP GET + 公开/只读 API，**不写入任何生产系统**。

### 已停用的链路（巡检不会告警、不会触发）

按运维决策，私人引擎的旧结算链路已主动停用，**正式赛果以 Supabase 为准**：

- `engine4-settlement.yml`
- `engine4-verify-results.yml`
- `engine4-result-backfill.yml`

它们记录在 `config/ops.json -> engine.retired_workflows`，巡检不会把它们当作异常，
也不会 dispatch 它们。**不得恢复。**

## 二、目录结构

```
.github/workflows/
  ai-ops-inspect.yml   每 15 分钟巡检 + 异常时 DeepSeek 诊断 + 状态持久化
  ai-ops-tasks.yml     只读工单执行器（cron 10,40 + 手动；支持 requeue）
  ai-ops-recover.yml   受控恢复（仅手动；需授权串；避停售窗口）
  ai-ops-daily.yml     每日 00:10（北京时间）汇总一行到 logs/daily.jsonl
  ai-ops-selftest.yml  离线单元测试与配置校验
config/ops.json        阈值、探测源、预算、引擎计划点、executor/recovery 开关
ops/common.py          HTTP、时区、哈希、结果聚合
ops/checks.py          10 个只读检查
ops/inspector.py       巡检入口（写 state/latest.json + Job Summary）
ops/persist.py         把 state/latest.json 提交回仓库（变化时或每小时一次）
ops/task_runner.py     云端只读工单执行器（白名单 action，无任意命令）
ops/recovery.py        受控恢复：授权串 + 停售窗口 + 每小时频率限制
ops/pr.py              代码修复走独立 PR（分支 + 提交 + 开 PR）
ops/rollback.py        state 文件回滚到历史提交
ops/budget.py          月度预算账本与硬停止
ops/triage.py          DeepSeek 诊断（预算闸门、去重、只读）
ops/handoff.py         GPT ↔ DeepSeek 任务交接（tasks/reports/state）
ops/daily.py           每日摘要
ops/ghcommit.py        通过 GitHub API 提交状态文件（单文件 / 单次多文件）
ops/selftest.py        离线测试
tasks/                 待办工单（人或 GPT 创建，含 action/params）
reports/               DeepSeek / 执行器结论报告
state/                 运行时状态：latest.json、triage、budget、executor、recovery
docs/HANDOFF.md        GPT ↔ DeepSeek 交接协议
docs/RUNBOOK.md        逐项排查手册 + 授权清单
docs/DATA-SOURCES.md   采集链路与备用渠道研究
```

## 三、本地运行

```bash
python ops/selftest.py                                   # 离线测试，不需要密钥
python ops/inspector.py --config config/ops.json --out state/latest.json
python ops/handoff.py board                              # 任务看板
```

## 四、预算控制逻辑

1. `config/ops.json -> budget.cap_cny = 10.0`，账本按月存在 `state/budget/YYYY-MM.json`，
   由工作流通过 API 提交，跨运行持久化。
2. 每次调用前检查：本月已用金额、保留额（0.3 元）、当日调用次数上限（12 次）、
   同一故障签名的冷却窗口（FAIL 45 分钟 / WARN 120 分钟）。
3. 巡检结果为 `OK` 时**绝不调用** DeepSeek（正常巡检 0 成本）。
4. 按 token 计费写入账本，`deepseek-chat` 单价按输入 2 元 / 输出 8 元每百万 token 估算。
5. 达到 10 元后本月不再调用，巡检继续运行（纯脚本，0 成本）。

## 五、受控恢复（写生产必须人工授权）

`ai-ops-recover.yml` 只能手动触发，四道闸门缺一不可：

1. **授权串**：`confirm` 必须等于 `YES-I-APPROVE`，否则预检直接 REFUSED（已在云端实测）。
2. **停售保护**：北京时间 08:45–22:45 拒绝触发写生产的恢复动作（`rerun_failed_run` 除外）。
3. **频率限制**：同一小时内最多 `recovery.max_per_hour` 次（默认 2）。
4. **审计**：每次尝试都写 `state/recovery/history.jsonl`（操作者、工单号、原因、结果）。

动作白名单：`dispatch_engine4`、`dispatch_site_sync`、`rerun_failed_run`。
代码修复**不走恢复通道**，必须用 `ops/pr.py` 建独立 PR 审核。

## 六、安全边界

- 第一阶段只读：不修改预测算法、不写历史冻结数据、不碰会员数据、不改生产仓库。
- 不新增任何 Supabase 调用与额度消耗（`settlement` 读的是网站已发布的静态数据）。
- 本仓库是公开仓库，**禁止**写入任何密钥、token、私人仓库源码、会员信息。
  `DEEPSEEK_API_KEY`、`ENGINE4_READ_TOKEN` 只以 GitHub Secret 形式存在，不落盘、不打印。
- 自动修复需要人工授权；`triage.py` 只输出诊断和修复建议，不执行修复。

## 七、部署步骤（需一次授权）

1. 运维专用细粒度 PAT（见 `docs/RUNBOOK.md` 的“授权清单”）。
2. 推送 `ops/`、`config/`、`tasks/`、`reports/`、`state/`、`.github/workflows/ai-ops-*.yml`、
   `README.md`、`docs/` 到本仓库。
3. 确认 Secret：`DEEPSEEK_API_KEY`（必需）、`ENGINE4_READ_TOKEN`（可选，用于 `engine_runs`）。
4. 手动触发一次 `ai-ops-inspect.yml`，检查 Job Summary 与 artifact。
5. 等一个 15 分钟周期，确认 `schedule` 触发。

## 八、已知限制

- **GitHub 定时器被严重限流（实测）**：本仓库 9 小时内只收到 1 次 `schedule` 触发（应约 36 次）；
  网站仓库 `*/10` 的同步任务 24 小时只触发 2 次。结论：cron 只能当“兜底”，
  不能当精确计时器；需要准点时要手动 dispatch。已在 `state/latest.json` 的 `generated_at` 留痕。
- 自动开 PR 需要 token 具备 **Pull requests: Read and write**；当前 token 只有 Contents/Actions/Workflows，
  所以 `ops/pr.py` 会创建分支并给出 compare 链接，由人点一下开 PR。
- 私人预测引擎的内容只能通过公开产物与只读 token 间接判断。
- 本机位于中国大陆网络时，`supabase.co`、`raw.githubusercontent.com` 可能连接被重置，
  这些检查必须以 Actions 运行结果为准。
