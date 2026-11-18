# 运维手册（RUNBOOK）

## 一、授权清单

部署需要一次写权限。推荐方式：细粒度 PAT（Fine-grained token），只给一个仓库、只给必要权限。

**步骤**

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token
2. Resource owner：`SuolunPro`
3. Repository access：Only select repositories → `SuolunPro/90scale-ai-ops-`
4. Permissions（Repository permissions）：
   - **Contents**：Read and write（提交 state / 工作流文件）
   - **Workflows**：Read and write（修改 `.github/workflows/*` 必须）
   - **Actions**：Read and write（手动触发、读取运行记录）
   - **Metadata**：Read-only（自动带出）
5. Expiration：7 天（够完成部署与验收，之后可吊销）

**不要**把 token 贴在聊天里。请在本机新建文件：

```
C:\Users\87722\Documents\deepseek-harness\default-workspace\.secrets\gh_token.txt
```

文件内容只有一行 token。我会从文件读取到环境变量，绝不打印、不写入仓库、不写日志。

**第二阶段（可选，用于直接检查私人引擎是否漏跑）**

同一 PAT 增加仓库 `SuolunPro/90scale-engine` 的 **Contents: Read-only**，
或单独建一个只读 token 存到 `.secrets\engine_token.txt`。

---

## 二、告警分级

| 级别 | 含义 | 响应 |
| --- | --- | --- |
| FAIL / critical | 用户可见故障（网站挂、数据没同步、结算超时） | 立即处理，DeepSeek 自动诊断 |
| WARN | 可能恶化（备用源不可用、Pages 延迟、复核滞后） | 当日处理，DeepSeek 去重调用 |
| OK / skip | 正常，或检查条件不满足 | 无需处理 |

巡检每 15 分钟一次。同一故障签名在 45 分钟（FAIL）/ 120 分钟（WARN）内不重复调用 AI，避免烧预算。

---

## 三、逐项排查手册

### site_home

- 现象：首页非 200，或缺 `vip-code`、`./data/engine4/` 标记。
- 只读排查：
  ```bash
  curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' https://suolunpro.github.io/
  curl -sS https://suolunpro.github.io/ | grep -c 'vip-code'
  ```
- 常见根因：Pages 构建失败（deploy 步骤报错）、`index.html` 被误改、仓库 Pages 设置被关。
- 修复建议：看网站仓库 Actions 的 `pages build and deployment`；回滚到上一个正常提交。
- 需要授权：写生产仓库 → 需人工确认。

### site_data_sync

- 现象：线上 `latest.json` 与仓库文件内容不一致（SHA 不同）。
- 只读排查：
  ```bash
  curl -sS https://suolunpro.github.io/data/engine4/latest.json | git hash-object --stdin
  gh api repos/SuolunPro/suolunpro.github.io/contents/data/engine4/latest.json --jq .sha
  ```
- 常见根因：Pages 部署队列延迟（< 10 分钟属正常）、构建失败、CDN 缓存。
- 修复建议：先等 10 分钟；仍不一致则查看 Pages 构建日志，必要时空提交触发重建。
- 需要授权：触发重建/改生产 → 需人工确认。

### engine_fresh

- 现象：销售窗口内 `sale_day` 不是今天，或快照年龄超过阈值（窗口内 180/360 分钟）。
- 只读排查：
  ```bash
  curl -sS https://suolunpro.github.io/data/engine4/latest.json | python -m json.tool | head -20
  ```
- 常见根因：私人引擎的 cron 未触发（Actions 停用 / 额度 / 上游数据源全挂）、官方接口 567 等异常。
- 修复建议：看 `90scale-engine` 的 `engine4.yml` 运行记录；必要时手动 dispatch 一次。
- 需要授权：写私人引擎 → 需人工确认。

### pool_coverage

- 现象：当日场次为空、`metrics.total` 与实际不符、部分场次缺 `prediction`。
- 只读排查：同上读取 `latest.json`，检查 `matches[].prediction` 是否为空。
- 常见根因：官方全池未开售、镜像源缺场、上游字段变更。
- 修复建议：先确认官方接口当前是否真的没有可售场次（避免把“无赛日”当故障）。

### settlement

- 现象：已结束销售日仍有 `pending`，且最早未结算场次开赛超过 3 小时（WARN）/ 8 小时（FAIL）。
- 只读排查：
  ```bash
  curl -sS https://suolunpro.github.io/data/engine4/sales/index.json
  curl -sS https://suolunpro.github.io/data/engine4/sales/<日期>.json | python -m json.tool | head -40
  ```
- 常见根因：赛果源（Supabase 发布链路 / FotMob）未返回、比分格式变更。
- 修复建议：查网站仓库 `supabase-settlement-publisher` 的运行记录与 Supabase 侧发布状态。
- **红线**：私人引擎的 `engine4-settlement.yml` / `engine4-verify-results.yml` /
  `engine4-result-backfill.yml` 已主动停用，正式赛果以 Supabase 为准；
  不得 dispatch、不得恢复、不得改回旧链路。
- 需要授权：任何写数据的重跑 → 需人工确认。

### engine_runs

- 现象：`config.engine.schedule_bj` 中的计划点在 `lookback_min`（默认 180 分钟）内没有对应运行。
- 前置：需要 Secret `ENGINE4_READ_TOKEN`（只读私人仓库 Actions）；没有则该项 SKIP，不误报。
- 只读排查：
  ```bash
  gh run list -R SuolunPro/90scale-engine -w engine4.yml -L 10
  ```
- 常见根因：GitHub 调度被延迟/跳过（本账号已实测发生）、引擎被手动停用、额度问题。
- 处理：先看 `engine_fresh`（数据是否真的停更）。**数据没停更就不要动**；
  数据确实停更才考虑手动 dispatch 一次 `engine4.yml`，并先取得授权。
- 注意：已停用的三个结算工作流不参与本项判定。

### independent_review

- 现象：`data/independent-review/` 最新文件滞后 3 天（WARN）/ 10 天（FAIL）。
- 只读排查：列目录最新文件名日期。
- 常见根因：复核任务停更、生成工作流失败。
- 修复建议：确认复核链路是否仍在运行；若产品已下线该功能，把阈值调大或改为 info。

### sources

- 现象：`required=true` 的源不可用 → FAIL；备用源不可用 → WARN。
- 只读排查：
  ```bash
  curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' 'https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?channel=c&poolCode=had,hhad,crs,ttg,hafu'
  curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' 'https://qiulaile.vip/api/matches?matchType=0&mode=full'
  ```
- 已知：澳客 `/jingcai/<日期>/` 在当前出口 IP 返回 405（反爬）；这是常见现象，属 WARN。
- 修复建议：换 UA / 加 Referer / 降低频率；或改用备用渠道（见 `docs/DATA-SOURCES.md`）。

### site_workflows

- 现象：近 2 小时内网站仓库有失败运行，或完全没有运行（调度停摆）。
- 只读排查：
  ```bash
  gh run list -R SuolunPro/suolunpro.github.io -L 10
  ```
- 常见根因：`supabase-settlement-publisher` 等需要 Secret 的工作流失败、并发冲突。
- 修复建议：看失败日志；区分“真实故障”与“可选检查失败”。

---

## 四、回滚

- 运维系统自身故障：把 `.github/workflows/ai-ops-inspect.yml` 的 `schedule` 注释掉，
  或在本仓库 Actions 页面 Disable workflow，生产不受影响（本系统只读）。
- 预算失控：把 `config/ops.json -> budget.cap_cny` 改成 0，重跑即停止所有 AI 调用。

## 五、成本与额度

| 项目 | 用量 | 费用 |
| --- | --- | --- |
| Actions 分钟（公开仓库） | 96 次/天 × 约 1 分钟 | 0 元 |
| DeepSeek 诊断 | 仅异常触发，去重后约 0–10 次/天 | 单次约 0.004–0.01 元 |
| Supabase | 不新增调用 | 0 |
| 服务器 | 不购买 | 0 |

月度硬上限 10 元；按最坏情况（每天 12 次、每次 0.008 元）也只需约 2.9 元/月。
