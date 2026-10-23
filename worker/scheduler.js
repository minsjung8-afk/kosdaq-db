/**
 * kosdaq-db 스케줄러 (Cloudflare Worker Cron)
 * GitHub Actions의 schedule 트리거가 수 시간 지연·누락되는 문제를 피하기 위해, 정시에 workflow_dispatch로 collect.yml을 대신 호출한다.
 *
 * 설정(비밀값은 대시보드/wrangler secret으로만 입력, 코드·채팅에 쓰지 않음):
 *   GH_TOKEN  : GitHub fine-grained PAT — 저장소 minsjung8-afk/kosdaq-db, 권한 "Actions: Read and write"만. (wrangler secret put GH_TOKEN)
 *   GH_REPO   : "minsjung8-afk/kosdaq-db" (vars)
 *   GH_REF    : "main" (vars)
 *
 * 크론은 UTC 기준(wrangler.toml). 요일은 Cloudflare 규칙 1=일요일 … 7=토요일 (GitHub cron의 0=일요일과 다름). 아래 표의 KST ↔ UTC 매핑으로 job을 고른다.
 *   21:00 UTC 일~목(1-5) = 06:00 KST 월~금  daily   (시세+대용가+델타)
 *   21:30 UTC 일~목 = 06:30 KST        subst   (대용가 재조회)
 *   23:30 UTC 일~목 = 08:30 KST        daily   (06:00 보완)
 *   15:10 UTC 매일  = 00:10 KST        catchup (미수집분)
 *   20:30 UTC 2일   = 05:30 KST 2일    fin
 *   20:30 UTC 1·15일= 05:30 KST        rating
 *   19:00 UTC 월·목(2,5) = 04:00 KST 화·금  news
 */
const TABLE = [
  { cron: "0 21 * * 1-5",  job: "daily" },
  { cron: "30 21 * * 1-5", job: "subst" },
  { cron: "30 23 * * 1-5", job: "daily" },
  { cron: "10 15 * * *",   job: "catchup" },
  { cron: "30 20 2 * *",   job: "fin" },
  { cron: "30 20 1,15 * *", job: "rating" },
  { cron: "0 19 * * 2,5",  job: "news" },
];

async function dispatch(env, job, args = "") {
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/collect.yml/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "kosdaq-db-scheduler",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs: { job, args } }),
  });
  const ok = r.status === 204;
  console.log(`[scheduler] ${job} ${args} → ${r.status}${ok ? "" : " " + (await r.text()).slice(0, 200)}`);
  return ok;
}

export default {
  // Cron Trigger: event.cron 은 wrangler.toml에 적은 문자열 그대로 들어온다
  async scheduled(event, env, ctx) {
    const hit = TABLE.find(t => t.cron === event.cron);
    if (!hit) { console.log("[scheduler] 매핑 없음:", event.cron); return; }
    ctx.waitUntil(dispatch(env, hit.job));
  },
  // 수동 확인용: GET /  → 매핑표, POST /run?job=daily&args=... (헤더 X-Auth: RUN_KEY 일치 시) → 즉시 디스패치
  async fetch(req, env) {
    const u = new URL(req.url);
    if (req.method === "POST" && u.pathname === "/run") {
      if (!env.RUN_KEY || req.headers.get("X-Auth") !== env.RUN_KEY) return new Response("forbidden", { status: 403 });
      const ok = await dispatch(env, u.searchParams.get("job") || "price", u.searchParams.get("args") || "");
      return new Response(ok ? "dispatched" : "failed", { status: ok ? 200 : 502 });
    }
    return new Response(JSON.stringify({ repo: env.GH_REPO, ref: env.GH_REF, table: TABLE }, null, 1), { headers: { "content-type": "application/json" } });
  },
};
