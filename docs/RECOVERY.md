# 受限恢复（V1.3）

## 一、两条通道，彻底分开

| 通道 | 工具 | 做什么 | 能不能改代码 |
| --- | --- | --- | --- |
| 运维恢复 | `ops/recovery.py` + `ai-ops-recover.yml` | 重触发**已存在**的工作流；只产生新产物 | **不能** |
| 代码修复 | `ops/fix_pr.py` | 跑测试 → 独立分支 → 提交 → 开 PR | 只能通过 PR，等人工审核 |

恢复通道里 `class=code` 的计划会被直接拒绝，避免“顺手改一下代码”。

## 二、四类恢复计划

| 计划 | 动作 | 窗口内允许 | 前置条件 | 是否写冻结数据 |
| --- | --- | --- | --- | --- |
| `engine_missed_run` | dispatch `engine4.yml` | ✅ 是 | `engine_runs` 与 `engine_fresh` 均为 warn/fail | ❌ 否 |
| `site_sync_failure` | dispatch `sync-engine4.yml` | ✅ 是 | `site_data_sync` 为 warn/fail | ❌ 否 |
| `collector_failure` | 无（仅报告） | ❌ 否 | `sources` 为 warn/fail | ❌ 否 |
| `code_fix` | 拒绝 | — | — | — |

**前置条件**来自最近一次巡检的 `state/latest.json`：条件不满足时直接拒绝，避免“看到告警就盲目重试”。

## 三、停售保护为什么改成这样

旧规则：08:45–22:45 一律禁止恢复。结果是**最需要救的时段反而救不了**（反向限制）。

新规则按动作性质判定：

- **不改写对外可见数据、只重跑既有流水线**（引擎预测、站点同步）→ 窗口内允许，但仍需授权串 + 频率限制。
- **会改变对外数据的行为** → 窗口内禁止。
- **写已冻结赛事数据** → 无论何时永久禁止（配置 `writes_frozen_data: true` 会被直接拒绝）。
- **代码修改** → 不走本通道，只能 PR。

## 四、冻结数据保护

1. 恢复动作只调用 GitHub 的 `dispatches` / `rerun` API，**不写任何数据文件**。
2. 被触发的工作流本身带冻结保护（引擎只追加新快照；站点同步合并时保留已结算记录）。
3. 配置层硬开关 `writes_frozen_data`，误配也会被拒绝。
4. 审计日志 `state/recovery/history.jsonl` 记录：谁、什么计划、授权串、前置条件、结果。

## 五、操作示例

```bash
# 1) 看有哪些计划
python ops/recovery.py --plan engine_missed_run --list-plans

# 2) 预检（安全，不执行）
python ops/recovery.py --plan engine_missed_run --confirm YES-I-APPROVE \
  --ticket INC-001 --reason "12:12 计划点缺失" --dry-run

# 3) 真执行（需在 GitHub 手动触发，dry_run=false）
```

## 七、代码修复示例

```bash
python ops/fix_pr.py --branch fix/collector-timeout --title "fix: 采集超时重试" \
  --files ops/checks.py --body "说明改动原因"
```

脚本会先跑 `ops/selftest.py`，**测试不通过直接拒绝开 PR**；PR 创建后不会自动合并，
需人工审核。

## 八、变更记录

- **V1.3（2026-11-19）**：恢复通道与代码通道彻底分离；停售保护改为按计划类别判定；
  未定义计划、冻结数据写入、代码类计划一律拒绝；拒绝操作也写审计日志。
  审计细节见 `docs/AUDIT-V1.3.md`。

