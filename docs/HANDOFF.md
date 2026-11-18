# GPT ↔ DeepSeek 任务交接协议

目标：两个模型（以及人）之间**不靠聊天上下文**交接，而是靠仓库里的三个目录。任何一方重启、
换会话、换账号，都能从文件恢复现场。

## 一、三个目录

| 目录 | 谁写 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| `tasks/` | 人 / GPT | 工单：要做什么、验收标准、证据链接 | 长期保留 |
| `reports/` | DeepSeek / 巡检任务 | 结论报告：JSON（机器读）+ Markdown（人读） | 长期保留 |
| `state/` | 自动化 | 运行时状态：最近巡检、预算账本、诊断结果 | 滚动覆盖 |

## 二、状态机

```
queued ──claim──> claimed ──done──> done
                        └──fail──> failed
                        └──blocked──> blocked（等人授权/等数据）
```

- 只有 `queued` 能被领取；重复领取会报错，避免两个执行者同时动手。
- 第一阶段 `scope` 只允许 `read-only`。任何“写生产、改算法、改历史数据”的工单会被
  `validate_task` 直接拒绝。

## 三、工单格式

```json
{
  "id": "TASK-20261118-001",
  "title": "确认澳客 405 是否只是反爬",
  "created_by": "gpt",
  "created_at": "2026-11-18T06:30:00+00:00",
  "priority": "P1",
  "scope": "read-only",
  "status": "queued",
  "assignee": "",
  "sla_minutes": 60,
  "evidence": ["state/latest.json#sources", "reports/RPT-20261118-002.md"],
  "acceptance": "给出结论 + 证据 + 是否需要改采集策略",
  "notes": "",
  "links": []
}
```

## 四、命令行

```bash
# GPT 建单
python ops/handoff.py new --by gpt --priority P1 \
  --title "确认澳客 405 是否只是反爬" \
  --evidence "state/latest.json#sources" \
  --acceptance "给出结论与是否换源的建议"

# DeepSeek 领取并完成
python ops/handoff.py claim TASK-20261118-001 --by deepseek
python ops/handoff.py done TASK-20261118-001 --by deepseek --verdict OK \
  --summary "405 为 UA/频率反爬，非站点下线" \
  --finding "带完整浏览器头仍 405" \
  --evidence "state/latest.json#sources" \
  --next "改用 football-data.org 作为赛果核验源"

# 看板 / 校验
python ops/handoff.py board
python ops/handoff.py validate
```

## 五、与巡检系统的关系

- 巡检只产出 `state/latest.json` 和异常诊断（`state/triage/latest.json`）。
- 巡检**不自动**创建工单，避免噪声；人/GPT 看完报告后决定是否开单。
- 每条 DeepSeek 诊断都会记录 token 与费用，写进 `state/budget/YYYY-MM.json`，
  可在 `reports/` 里追溯“这次判断花了多少钱”。

## 六、红线

- `tasks/`、`reports/`、`state/` 都是公开仓库内容：**禁止**写密钥、token、会员数据、私人仓库源码。
- 工单里的命令只能是只读命令；执行者不得“顺手修一下”。
- 报告必须引用真实证据（文件路径 + 行号/字段），不得编造运行结果。
