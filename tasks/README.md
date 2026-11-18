# tasks/ 工单目录

人 / GPT 在这里创建只读工单，DeepSeek 领取并产出报告到 `reports/`。

- 命名：`TASK-YYYYMMDD-NNN.json`
- 状态：`queued` → `claimed` → `done` / `failed` / `blocked`
- 第一阶段 `scope` 只能是 `read-only`

用命令创建，不要手写（手写容易漏字段）：

```bash
python ops/handoff.py new --by gpt --priority P1 --title "..." --acceptance "..."
python ops/handoff.py board
```

格式说明见 `docs/HANDOFF.md`。
