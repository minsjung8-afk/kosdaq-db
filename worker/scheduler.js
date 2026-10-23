/**
 * kosdaq-db 스케줄러 (Cloudflare Worker Cron)
 * GitHub Actions의 schedule 트리거가 수 시간 지연·누락되는 문제를 피하기 위해, 정시에 workflow_dispatch로 collect.yml을 대신 호출한다.
 *
 * 크론 트리거는 1개만 사용(무료 플랜: 계정당 5개 제한) — 매시 0·10·30분에 깨어나 '지금 시각(KST)'에 해당하는 job이 있으면 디스패치.
 *
 * 설정(비밀값은 wrangler secret으로만 입력, 코드·채팅에 쓰지 않음):
 *   GH_TOKEN  : GitHub fine-grained PAT — 저장소 minsjung8-afk/kosdaq-db, 권한 "Actions: Read and write"만.
 *   RUN_KEY   : 수동 실행(POST /run) 인증용 임의 문자열
 *   GH_REPO / GH_REF : vars (wrangler.toml)
 */

// KST 기준 스케줄표. dow: 0=일 … 6=토 (JS getUTCDay 기준), dom: 일(day of month)
const TABLE = [
  { hh: 6,  mm: 0,  dow: [1, 2, 3, 4, 5], job: "daily" },    // 월~금 06:00  시세+대용가+공시 델타
  { hh: 6,  mm: 30, dow: [1, 2, 3, 4, 5], job: "subst" },    // 월~금 06:30  대용가 재조회
  { hh: 8,  mm: 30, dow: [1, 2, 3, 4, 5], job: "daily" },    // 월~금 08:30  06:00 보완(미반영분만)
  { hh: 0,  mm: 10,                        job: "catchup" },  // 매일 00:10   미수집분 일괄
  { hh: 5,  mm: 30, dom: [2],              job: "fin" },      // 매월 2일 05:30  연간 재무 갱신
  { hh: 5,  mm: 30, dom: [1, 15],          job: "rating" },   // 매월 1·15일 05:30  신용등급
  { hh: 4,  mm: 0,  dow: [2, 5],           job: "news" },     // 화·금 04:00  뉴스
];

function pick(nowUtc) {
  const k = new Date(nowUtc.getTime() + 9 * 3600 * 1000);   // KST
  const hh = k.getUTCHours(), mm = k.getUTCMinutes(), dow = k.getUTCDay(), dom = k.getUTCDate();
  return TABLE.filter(t => t.hh === hh && t.mm === mm && (!t.dow || t.dow.includes(dow)) && (!t.dom || t.dom.includes(dom))).map(t => t.job);
}

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
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    const jobs = pick(now);
    if (!jobs.length) return;                      // 이 시각엔 할 일 없음(대부분의 깨어남)
    ctx.waitUntil(Promise.all(jobs.map(j => dispatch(env, j))));
  },
  // GET /            → 스케줄표·다음 실행 예정(점검용)
  // POST /run?job=daily&args=...  (헤더 X-Auth: RUN_KEY)  → 즉시 디스패치
  async fetch(req, env) {
    const u = new URL(req.url);
    if (req.method === "POST" && u.pathname === "/run") {
      if (!env.RUN_KEY || req.headers.get("X-Auth") !== env.RUN_KEY) return new Response("forbidden", { status: 403 });
      const ok = await dispatch(env, u.searchParams.get("job") || "price", u.searchParams.get("args") || "");
      return new Response(ok ? "dispatched" : "failed", { status: ok ? 200 : 502 });
    }
    const now = new Date();
    const next = [];
    for (let i = 1; i <= 7 * 24 * 6 && next.length < 5; i++) {       // 10분 단위로 1주일 훑어 다음 5건
      const t = new Date(Math.floor(now.getTime() / 600000) * 600000 + i * 600000);
      for (const j of pick(t)) next.push({ kst: new Date(t.getTime() + 9 * 3600 * 1000).toISOString().replace("T", " ").slice(0, 16), job: j });
    }
    return new Response(JSON.stringify({ repo: env.GH_REPO, ref: env.GH_REF, now_kst: new Date(now.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 16), schedule_kst: TABLE, next }, null, 1),
      { headers: { "content-type": "application/json; charset=utf-8" } });
  },
};
