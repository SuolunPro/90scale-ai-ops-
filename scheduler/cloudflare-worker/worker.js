// Cloudflare Worker：用 Cron Trigger 每 15 分钟给 GitHub 发一次 repository_dispatch。
// 只持有调度专用 PAT（最小权限），不接触任何业务数据。
//
// 部署：
//   npx wrangler secret put SCHEDULER_PAT
//   npx wrangler deploy
//
// 手动测试：
//   curl https://<worker>.<subdomain>.workers.dev/

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(trigger(env));
  },
  async fetch(request, env) {
    const out = await trigger(env);
    return new Response(JSON.stringify(out, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};

async function trigger(env) {
  const res = await fetch(
    "https://api.github.com/repos/SuolunPro/90scale-ai-ops-/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.SCHEDULER_PAT}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "90scale-external-scheduler",
        "content-type": "application/json",
      },
      body: JSON.stringify({ event_type: "external_tick" }),
    }
  );
  return { status: res.status, at: new Date().toISOString() };
}
