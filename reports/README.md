# reports/ 结论报告目录

每完成一个工单，生成一对文件：

- `RPT-YYYYMMDD-NNN.json`：机器可读（结论、发现、证据、下一步）
- `RPT-YYYYMMDD-NNN.md`：人可读摘要

规则：

- 结论 `verdict` 用 `OK` / `WARN` / `FAIL`。
- `evidence` 必须写真实路径或链接，禁止“我检查过了”这类无证据结论。
- 报告里不得出现密钥、token、会员信息。
