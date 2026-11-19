# Cloudflare Workers 免费定时调度（已备好，待部署）

用 Cloudflare 的 Cron Trigger 每 15 分钟给 GitHub 发一次 `repository_dispatch`，
绕过 GitHub 自身 cron 的限流（实测：本仓库 9 小时只触发 1 次）。

## 免费额度（2026-11 官方 Limits 页核实）

| 项目 | Workers Free |
| --- | --- |
| Cron Triggers / 账号 | **5 个** ✅ |
| 请求数 | 100,000/天 |
| Cron 触发 CPU 时间 | 10 ms（本 Worker 只做一次 fetch，远低于此） |
| Cron 触发最长时长 | 15 分钟 |

结论：**免费档支持 Cron Trigger，够用。**

## 部署步骤

```bash
cd scheduler/cloudflare-worker
npm i -g wrangler            # 或 npx wrangler
npx wrangler login           # 浏览器授权
npx wrangler secret put SCHEDULER_PAT   # 粘贴调度专用 PAT
npx wrangler deploy
```

`wrangler.toml` 已写好 `*/15 * * * *`，部署后立即生效。

## 调度专用 PAT 要求

- 细粒度 token，仅选 `SuolunPro/90scale-ai-ops-`
- 权限：**Contents: Read and write**（`repository_dispatch` 需要）+ **Actions: Read**
- 有效期 90 天，单独命名为 `SCHEDULER_PAT`，不与运维主 token 混用

## 验证

1. 浏览器访问 Worker 域名（`fetch` handler 会立即触发一次并返回 JSON）：
   ```json
   { "status": 204, "at": "..." }
   ```
2. 到仓库 Actions 看是否出现 `repository_dispatch` 触发的“15分钟巡检”。

## 回滚

`npx wrangler delete 90scale-external-scheduler`，或注释 `wrangler.toml` 里的 `[triggers]` 后重新部署。

## 成本

0 元。每日 96 次请求，远低于 10 万次额度。
