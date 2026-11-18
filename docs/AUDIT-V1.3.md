# V1.3 审计记录（预算 / 并发 / 重试 / 重复触发 / 回滚）

审计时间：2026-11-19 01:35–01:45（北京时间）
审计对象：`SuolunPro/90scale-ai-ops-` 全部工作流与 `ops/` 模块

## 一、预算控制

| 检查点 | 结论 | 证据 |
| --- | --- | --- |
| 月度硬上限 | ✅ 生效 | `config.budget.cap_cny=10`，`budget.summary()` 超过即 `exhausted` |
| 正常巡检是否调用 AI | ✅ 不调用 | triage job 仅在 FAIL/WARN 且 `--force` 时运行 |
| 去重 | ✅ 生效 | 同一 signature 在 FAIL 45 分钟 / WARN 120 分钟内不重复调用（云端实测：17:26 巡检跳过诊断） |
| 单日上限 | ✅ 生效 | `max_calls_per_day=12` |
| 账本并发写 | ⚠️→✅ 已修 | 旧实现读-改-写会互相覆盖；新增 `merge_entries()` + `commit_with_merge()`（冲突重读→并集→重提交）+ job 级 `deepseek-budget-lock` |
| 记账准确性 | ✅ 实测 | 4 条记录 ¥0.024576，合并后历史记录无丢失 |

## 二、并发锁

| 锁 | 粒度 | 作用 |
| --- | --- | --- |
| `90scale-ai-ops-inspect` | 工作流级 | 巡检不重叠 |
| `90scale-ai-ops-tasks` | 工作流级 | 工单执行器不重叠 |
| `deepseek-budget-lock` | job 级 | 跨工作流串行化 AI 调用与账本写入 |
| `90scale-ai-ops-recover` | 工作流级 | 同一时刻只允许一个恢复操作 |

残留风险：GitHub 的 `concurrency` 在 runner 分配前生效，不会出现两个同名组同时跑；但**跨仓库**操作（dispatch 到 engine/site）没有分布式锁，靠“每计划每小时上限”兜底。

## 三、失败重试

| 位置 | 策略 |
| --- | --- |
| HTTP 拉取 | `common.http_get` 默认 2 次重试 + 退避 |
| 仓库写入 | `ghcommit.commit_files` 409 重试 2 次；`commit_with_merge` 4 次重读合并 |
| 工单执行 | 失败不自动重试，需 `--requeue`，且 `max_attempts=2` |
| 恢复动作 | 不自动重试；同计划每小时上限 1–2 次 |

原则：**不无限重试**，避免把上游故障放大成雪崩。

## 四、重复触发保护

| 场景 | 机制 |
| --- | --- |
| 同一工单被执行两次 | `queued→claimed` 状态机；claim 失败即拒绝 |
| 同一故障重复诊断 | signature + 冷却窗口 |
| 恢复动作刷屏 | 计划级 `max_per_hour` + 审计日志 |
| 定时器重复触发 | 巡检只读、幂等；triage 有去重；persist 有内容比对 |

已知事故（V1.2）：本地部署误把 `tasks/*.json` 覆盖云端状态，导致 001/002/003 被重复执行（报告 RPT-20261119-008/009/010）。修复：`deploy.py` 默认跳过 `tasks/ reports/ state/`。报告按要求保留作为审计记录。

## 五、回滚

| 能力 | 状态 |
| --- | --- |
| `state/` 文件回滚到历史提交 | ✅ `ops/rollback.py --list` / `--to <sha>`，生成新提交而不是强推 |
| 账本回滚 | 同上 |
| 代码回滚 | 走 `git revert` / PR，不在本系统自动执行 |
| 未验证项 | 尚未在云端做过一次真实回滚演练（需要一次人为的状态变更） |

## 六、本轮发现并修复的问题

1. **停售保护反向限制**：窗口内一律禁止恢复 → 改为按计划类别判定；引擎/同步类允许窗口内恢复，代码类禁止。
2. **rerun 豁免**：`rerun_failed_run` 曾绕过停售保护 → 取消豁免（现由 plan 的 `allow_in_sale` 控制）。
3. **拒绝不写审计** → 现在允许/拒绝都写 `state/recovery/history.jsonl`。
4. **持久化盲区**：单项检查状态变化（如 `engine_runs: skip→ok`）不会触发提交 → 增加 `fingerprint` 比对 + 首次升级提交。
5. **部署覆盖运行态** → `deploy.py` 跳过 `tasks/`、`reports/`、`state/`。

## 七、剩余风险

- 定时触发依赖外部调度器（见 `docs/SCHEDULER.md`），本仓库 cron 已实测不可靠。
- `ENGINE4_REPAIR_TOKEN` 的**写**权限未验证（不得擅自触发主引擎）。
- 回滚未做云端演练。
