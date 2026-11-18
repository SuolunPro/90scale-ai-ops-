# 外部免费定时调度方案（解决 GitHub cron 漏跑）

## 一、问题回顾（实测）

| 仓库 | 配置 | 24 小时实际触发 |
| --- | --- | --- |
| 运维仓库 | `*/15 * * * *` | 1 次 |
| 网站仓库 | `*/10 * * * *` | 2 次 |

GitHub 的 `schedule` 在高峰期会被大幅延迟或直接跳过。仓库侧无法修复，只能引入**外部触发器**。

## 二、方案对比（免费档）

| 方案 | 最小间隔 | 免费额度 | 自定义 Header | 成本 | 适用 |
| --- | --- | --- | --- | --- | --- |
| **Google Apps Script** | 5 分钟 | 免费 | ✅ `UrlFetchApp` | 0 | ✅ 推荐首选（无第三方持有 token 风险最小，Google 基础设施稳定） |
| **cron-job.org** | 1 分钟 | 免费（约 50 个任务） | ✅ | 0 | ✅ 次选，界面简单；token 存在第三方 |
| **Cloudflare Worker Cron** | 1 分钟 | 免费档（需确认账号权限） | ✅ Worker Secret | 0 | 适合已有 Cloudflare 账号时 |
| GitHub `schedule` | 5 分钟 | 免费 | — | 0 | ❌ 已实测不可靠 |

> 以上免费额度以各平台 2026 年公开页面为准，部署前请再确认一次。

## 三、触发方式（已内置）

巡检工作流已支持 `repository_dispatch`：

```yaml
on:
  repository_dispatch:
    types: [external_tick]
```

外部调度器只需要一次带鉴权的 HTTP 请求：

```bash
curl -X POST \
  -H "Authorization: Bearer <SCHEDULER_PAT>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/SuolunPro/90scale-ai-ops-/dispatches \
  -d '{"event_type":"external_tick"}'
```

**专用 token 要求**：细粒度 PAT，只勾 `SuolunPro/90scale-ai-ops-`，权限只要 `Contents: Read`
（`repository_dispatch` 需要 Contents 写？—— 实测需要 `Contents: Read and write`）+ `Actions: Read`。
建议单独建一个 `SCHEDULER_PAT`，有效期 90 天，只用于调度，泄漏影响面最小。

## 四、Google Apps Script 版本（推荐）

```javascript
// 脚本属性里存 SCHEDULER_PAT
function tick() {
  const token = PropertiesService.getScriptProperties().getProperty('SCHEDULER_PAT');
  const url = 'https://api.github.com/repos/SuolunPro/90scale-ai-ops-/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ event_type: 'external_tick' }),
    muteHttpExceptions: true,
  });
  console.log(res.getResponseCode(), res.getContentText());
}
```

设置：Apps Script → 触发器 → `tick` → 时间驱动 → 每 5 分钟。

## 五、cron-job.org 版本

1. 新建 cronjob：URL 同上，Method `POST`。
2. Header：`Authorization: Bearer <SCHEDULER_PAT>`、`Accept: application/vnd.github+json`。
3. Body（JSON）：`{"event_type":"external_tick"}`。
4. 执行频率：每 15 分钟。

## 六、Cloudflare Worker 版本

见本目录 `scheduler/cloudflare-worker/`（`worker.js` + `wrangler.toml`）。

```bash
cd scheduler/cloudflare-worker
npx wrangler secret put SCHEDULER_PAT   # 粘贴 token
npx wrangler deploy
```

`wrangler.toml` 已配置 `*/15 * * * *` 的 cron。

## 七、安全与运维

- 调度器只持有 **一个** 权限极小的 PAT，不接触任何业务数据。
- token 轮换：90 天一次；轮换时先加新、再删旧。
- 调度器只负责“敲门”，真正的巡检逻辑仍在 Actions 里，失败可在 Actions 页面追溯。
- 建议保留 GitHub 自带 cron 作为兜底（两者重复触发无副作用：巡检是只读的，triage 有去重与预算闸门）。
