# 九十刻度4.0 AI运维日报｜2026-11-28 23:48:03 北京时间

**WARN** · 当前故障 无明确故障 · 风险 sources

| 项目 | 状态 | 最新证据时间（北京时间） | 结果 |
| --- | --- | --- | --- |
| 公开主引擎 | PASS | 2026-11-28 23:48:03 | 今日 31 次，成功 23、失败 8、取消 0、运行或排队 0。最近成功结束 2026-11-28T14:58:31Z [证据](https://github.com/SuolunPro/suolunpro.github.io/actions/runs/46156847305) |
| 在售预测刷新 | SKIP | 2026-11-28 22:58:16 | 销售窗口 CLOSED，公开 main 快照 2026-11-28T14:58:16Z，最近有证据的生产计划点 未知 [证据](https://github.com/SuolunPro/suolunpro.github.io/actions/workflows/public-refresh-audit.yml) |
| 赛事池完整性 | UNKNOWN | 未知 | 2026-11-28 正式站 23 场，待预测 0 场，已取证在售赛号 0 个，体彩官方名单待确认 [证据](https://github.com/SuolunPro/suolunpro.github.io/actions/workflows/public-refresh-audit.yml) |
| 逐场采集与匹配 | WARN | 2026-11-28 22:58:16 | 统计来自正式快照。HTTP 可达性独立记录，资料时效以采集时间为准。 [证据](https://suolunpro.github.io/data/engine4/latest.json) |
| 独立赛果核验及异常赛事 | UNKNOWN | 2026-11-28 23:48:03 | 已核验 24 场；未开赛 9 场；比赛中 0 场；完赛待核验 2 场；确认为完赛且超时 0 场；延期 0 场；取消 0 场；腰斩 0 场；官方作废 0 场；状态待核实 11 场 [证据](https://github.com/SuolunPro/suolunpro.github.io/actions/workflows/supabase-settlement-publisher.yml) |
| 公开 main 与正式站发布 | PASS | 2026-11-28 23:48:03 | latest 一致；sales/2026-11-28 一致 [证据](https://suolunpro.github.io/data/engine4/latest.json) |
| 公开自检自愈 | UNKNOWN | 未知 | 公开审计回执 0 份，恢复证据 0 项 [证据](https://github.com/SuolunPro/suolunpro.github.io/actions/workflows/public-refresh-audit.yml) |
| 旧独立复核归档 | RETIRED | 未知 | 旧日期文件仅供历史查阅，正式赛果沿用独立结算发布器 |
| 三核及试验引擎 | SKIP | 未知 | 研究和影子产物不参与正式预测可用性判定 |
| 公开 FotMob 情报影子采集 | SKIP | 2026-11-28 20:56:21 | 影子产物 23 场，逐场状态 {'failed': 4, 'collected': 19}。 [证据](https://github.com/SuolunPro/suolunpro.github.io/blob/main/data/engine4/intelligence/2026-11-28.json) |
| 双AI工单 | PASS | 2026-11-28 23:48:03 | 待领取 0，处理中 0，累计完成 5。按当前故障建单。 [证据](https://github.com/SuolunPro/90scale-ai-ops-/tree/main/tasks) |
| 私人仓库残留运行与额度 | UNKNOWN | 2026-11-28 23:48:03 | 私人运行记录缺少权限或不可读，实际计费分钟也未核实。 [证据](https://github.com/SuolunPro/90scale-engine/actions) |
| 上游端点可达性 | UNKNOWN | 2026-11-28 23:48:03 | 以下仅为巡检节点的 HTTP 响应，实际采集以逐场正式数据为准。 |

## 逐场覆盖

| 字段 | 可用 / 总数 | 缺失 | 陈旧 | 身份冲突 | 待核验或无效 |
| --- | --- | --- | --- | --- | --- |
| 体彩普通SP | 45/46 (97.8%) | 1 | 0 | 0 | 0 |
| 体彩让球SP | 46/46 (100.0%) | 0 | 0 | 0 | 0 |
| William Hill | 41/46 (89.1%) | 5 | 0 | 0 | 0 |
| 澳门亚盘及水位 | 41/46 (89.1%) | 5 | 0 | 0 | 0 |
| 澳门大小球及水位 | 41/46 (89.1%) | 5 | 0 | 0 | 0 |
| 必发指数 | 21/46 (45.7%) | 25 | 0 | 0 | 0 |
| FotMob赛事匹配 | 46/46 (100.0%) | 0 | 0 | 0 | 0 |
| FotMob xG | 45/46 (97.8%) | 1 | 0 | 0 | 0 |
| 赛事身份核验 | 46/46 (100.0%) | 0 | 0 | 0 | 0 |

必发赛号（销售日 / 编号）

- 缺失 2026-11-28 / 001、2026-11-28 / 002、2026-11-29 / 001、2026-11-29 / 002、2026-11-29 / 003、2026-11-29 / 004、2026-11-29 / 005、2026-11-29 / 006、2026-11-29 / 007、2026-11-29 / 008、2026-11-29 / 009、2026-11-29 / 010、2026-11-29 / 011、2026-11-29 / 012、2026-11-29 / 013、2026-11-29 / 014、2026-11-29 / 015、2026-11-29 / 016、2026-11-29 / 017、2026-11-29 / 018、2026-11-29 / 019、2026-11-29 / 020、2026-11-29 / 021、2026-11-29 / 022、2026-11-29 / 023

- 已停售缺失 · 2026-11-28 / 001、2026-11-28 / 002

## 上游端点探测

- 体彩官方接口 · HTTP 567 · 1834 ms
- 球来了镜像 · HTTP 200 · 1730 ms
- FotMob · HTTP 200 · 559 ms
- 澳客竞彩 · HTTP 405 · 1301 ms

DeepSeek 账本估算 · 当日 ¥0.169108（23 次） · 当月 ¥1.354348（210 次） · 内部预算 ¥10.00。账本记录截至 2026-11-28 16:00:36。
