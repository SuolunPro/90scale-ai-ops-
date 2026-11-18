# state/ 运行时状态（自动生成，勿手改）

| 路径 | 内容 |
| --- | --- |
| `state/latest.json` | 最近一次巡检的完整结果（含每项检查的明细） |
| `state/triage/latest.json` | 最近一次 DeepSeek 诊断结果 |
| `state/budget/YYYY-MM.json` | 当月 AI 预算账本（每次调用的 token 与费用） |

- 这些文件由工作流自动提交，用于跨工作流持久化（Actions 运行器是无状态的）。
- 账本是**月度硬上限**的依据：达到 `config/ops.json -> budget.cap_cny` 后不再调用 AI。
- 不要在 `state/` 放任何密钥或个人信息。
