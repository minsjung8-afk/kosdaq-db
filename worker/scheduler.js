/**
 * kosdaq-db 스케줄러 (Cloudflare Worker Cron)
 * GitHub Actions의 schedule 트리거가 수 시간 지연·누락되는 문제를 피하기 위해, 정시에 workflow_dispatch로 collect.yml을 대신 호출한다.
 *
 * 크론 트리거는 1개만 사용(무료 플랜: 계정당 5개 제한) — 10분마다 깨어나 '지금 시각(KST)'에 해당하는 job이 있으면 디스패치하고,
 * 사이트 '회사 추가' 신청 대기열(KV ADDQ)이 있으면 GitHub 작업이 비어 있을 때만 등록 작업을 한 번에 실행한다(2026-10-26).
 *
 * 설정(비밀값은 wrangler secret으로만 입력, 코드·채팅에 쓰지 않음):
 *   GH_TOKEN  : GitHub fine-grained PAT — 저장소 minsjung8-afk/kosdaq-db, 권한 "Actions: Read and write"만.
 *   RUN_KEY   : 수동 실행(POST /run)·회사 추가 신청(POST /enqueue) 인증용 임의 문자열
 *   GH_REPO / GH_REF : vars (wrangler.toml)
 *   ADDQ      : KV namespace 바인딩(회사 추가 신청 대기열)
 *
 * 왜 대기열인가: collect.yml은 '실행 1 + 대기 1' 구조라 새 디스패치가 대기 중이던 작업(정기 수집·chain 다음 단계)을 취소한다.
 * 신청을 KV에 쌓아 두고 '실행·대기 작업이 모두 없을 때'에만 디스패치하면 서로 취소하지 않는다.
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
  { hh: 5,  mm: 30, dom: [17], mon: [5, 8, 11], job: "fin" }, // 5·8·11월 17일 05:30  분기·반기보고서 제출기한 직후 재무 재수집
  { hh: 5,  mm: 0,  dom: [5],  mon: [4],   job: "biz" },      // 4/5 05:00  사업내용·감사의견·주주(사업보고서)
];   // 2026-10-27: GitHub 자체 schedule(collect.yml)을 없애고 이 표가 유일한 정기 실행 출처
// GitHub 자체 schedule(collect.yml on.schedule) cron → job (실행 목록에서 작업 종류 추정용)
const GH_CRON = { "0 21 * * 0-4": "daily", "30 21 * * 0-4": "subst", "30 23 * * 0-4": "daily", "10 15 * * *": "catchup",
  "30 20 2 * *": "fin", "30 20 17 5,8,11 *": "fin", "30 20 1,15 * *": "rating", "0 19 * * 1,4": "news", "0 20 5 4 *": "biz" };
const DEFAULT_MIN = { daily: 40, subst: 10, catchup: 170, fin: 120, rating: 30, news: 30, chain: 170, site: 12, unlisted_add: 20, kpi: 30,
  da: 15, da_notes: 60, coverage: 8, review: 60, quarters: 60, biz: 90, mezz: 30, summarize: 60, audit_fs: 120, notes: 90, price: 20 };
const GUARD_MIN = 15;          // 정기 작업 15분 전부터는 회사 등록을 시작하지 않음(아침 시세·공시 반영 지연 방지)
const KST = 9 * 3600e3;

function pick(nowUtc) {
  const k = new Date(nowUtc.getTime() + KST);
  const hh = k.getUTCHours(), mm = k.getUTCMinutes(), dow = k.getUTCDay(), dom = k.getUTCDate(), mon = k.getUTCMonth() + 1;
  return TABLE.filter(t => t.hh === hh && t.mm === mm && (!t.dow || t.dow.includes(dow)) && (!t.dom || t.dom.includes(dom)) && (!t.mon || t.mon.includes(mon))).map(t => t.job);
}
const kst = ms => new Date(ms + KST).toISOString().replace("T", " ").slice(0, 16);
const hm = ms => new Date(ms + KST).toISOString().slice(11, 16);

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.GH_REPO}${path}`, {
    ...init,
    headers: { "Authorization": `Bearer ${env.GH_TOKEN}`, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "kosdaq-db-scheduler", "Content-Type": "application/json", ...(init.headers || {}) },
  });
}

async function dispatch(env, job, args = "") {
  const r = await gh(env, "/actions/workflows/collect.yml/dispatches", { method: "POST", body: JSON.stringify({ ref: env.GH_REF || "main", inputs: { job, args } }) });
  const ok = r.status === 204;
  console.log(`[scheduler] ${job} ${args} → ${r.status}${ok ? "" : " " + (await r.text()).slice(0, 200)}`);
  return ok;
}

// 실행 목록의 제목(run-name: "<job> <args>" 또는 "schedule <cron>")에서 작업 종류
function jobOf(run) {
  const t = (run.display_title || "").trim();
  if (run.event === "schedule") return GH_CRON[t.replace(/^schedule\s+/, "")] || "daily";
  const j = t.split(/\s+/)[0];
  return DEFAULT_MIN[j] !== undefined || /^[a-z_]+$/.test(j) ? j : "?";
}

// 최근 완료 실행으로 작업별 소요시간(중앙값, 분) — 표본 없으면 기본값
async function runState(env) {
  const r = await gh(env, "/actions/workflows/collect.yml/runs?per_page=60");
  const runs = r.ok ? (await r.json()).workflow_runs || [] : [];
  const dur = {};
  for (const x of runs) {
    if (x.status !== "completed" || x.conclusion !== "success" || !x.run_started_at) continue;
    const m = (Date.parse(x.updated_at) - Date.parse(x.run_started_at)) / 60000;
    if (m > 1) (dur[jobOf(x)] ||= []).push(m);
  }
  const med = j => { const a = (dur[j] || []).sort((p, q) => p - q); return a.length ? a[Math.floor(a.length / 2)] : (DEFAULT_MIN[j] || 30); };
  const active = runs.filter(x => ["in_progress", "queued", "pending", "waiting", "requested"].includes(x.status) && jobOf(x) !== "unlisted");
  const running = active.find(x => x.status === "in_progress");
  const waiting = active.filter(x => x !== running);
  return { ok: r.ok, running, waiting, med };
}

// chain 소요시간: 제목의 --steps 단계별 소요시간 합(+ 준비·배포 8분), 상한 170분(넘으면 다음 실행으로 이어짐)
function chainMin(run, med) {
  const m = (run.display_title || "").match(/--steps\s+(.*)$/);
  if (!m) return med("chain");
  const steps = m[1].match(/"[^"]*"|\S+/g) || [];
  const sum = steps.reduce((a, x) => a + med(x.replace(/"/g, "").split(":")[0]), 0);
  return Math.min(170, sum + 8);
}

// 다음 정기 작업(start 이후) — {t, job}
function nextScheduled(fromMs) {
  for (let i = 0; i <= 7 * 24 * 6; i++) {
    const t = Math.ceil(fromMs / 600000) * 600000 + i * 600000;
    const j = pick(new Date(t));
    if (j.length) return { t, job: j[0] };
  }
  return null;
}

// 반영 예상 시각 계산: 지금 실행 중 → 대기 중 → (그 사이 정기 작업) → 다음 점검(10분 단위) → 등록 소요
function estimate(st, now, nCodes) {
  const notes = [];
  let t = now;
  if (st.running) {
    const j = jobOf(st.running), s = Date.parse(st.running.run_started_at || st.running.created_at);
    let end = s + (j === "chain" ? chainMin(st.running, st.med) : st.med(j)) * 60000;
    if (end < now + 5 * 60000) { end = now + 5 * 60000; }
    notes.push(`실행 중: ${j} (${hm(s)} 시작, ${hm(end)}경 종료 예상)`);
    if (j === "chain" || j === "catchup") notes.push("연속 작업이라 단계가 이어지면 더 늦어질 수 있음");
    t = end;
  }
  for (const w of st.waiting) { const j = jobOf(w); t += (j === "chain" ? chainMin(w, st.med) : st.med(j)) * 60000; notes.push(`대기 중: ${j} (약 ${Math.round(st.med(j))}분)`); }
  for (let guard = 0; guard < 6; guard++) {                       // 그 사이(또는 직후 15분 안)에 시작될 정기 작업
    const ns = nextScheduled(now + 1);
    if (!ns || ns.t > t + GUARD_MIN * 60000) break;
    if (ns.t > now) { t = Math.max(t, ns.t) + st.med(ns.job) * 60000; notes.push(`정기 작업: ${ns.job} ${hm(ns.t)} (약 ${Math.round(st.med(ns.job))}분)`); now = ns.t; }
    else break;
  }
  const start = Math.ceil((t + 60000) / 600000) * 600000;           // Worker 점검 주기(10분)
  const per = st.med("unlisted_add");
  const done = start + (per + Math.max(0, nCodes - 1) * 3) * 60000;  // 회사가 많으면 1사당 약 3분 추가
  return { start, done, notes };
}

async function loadQ(env) {
  const q = JSON.parse((await env.ADDQ.get("q")) || "[]");
  const inflight = JSON.parse((await env.ADDQ.get("inflight")) || "null");
  return { q, inflight };
}

async function status(env, nAdd = 0) {
  const now = Date.now();
  const { q, inflight } = await loadQ(env);
  const st = await runState(env);
  const out = { now_kst: kst(now), queue: q.map(x => ({ corp_code: x.cc, name: x.nm, at: kst(x.at) })), inflight: null, eta: null, notes: [] };
  if (inflight) {
    out.inflight = { codes: inflight.codes, names: inflight.names, started_kst: kst(inflight.at), done_kst: kst(inflight.at + st.med("unlisted_add") * 60000) };
  }
  const n = q.length + nAdd;
  const e = estimate(st, now, Math.max(n, 1));           // 대기열이 비어 있어도 '지금 신청하면' 예상 시각을 보여 줌
  out.eta = { start_kst: kst(e.start), done_kst: kst(e.done), start: hm(e.start), done: hm(e.done), if_new: !n };
  out.notes = e.notes;
  return out;
}

// 대기열 처리: GitHub 작업이 비어 있고 정기 작업이 15분 안에 없으면 한 번에 디스패치
// 정기 작업 디스패치(2026-10-27): GitHub 동시실행은 '실행 1 + 대기 1'이라 새 대기가 기존 대기를 취소한다.
// 10-27 새벽 catchup이 늦게 돌던 중 06:00 daily(시세)가 대기 → 06:30 subst가 그 대기를 밀어내 10-26 시세가 빠졌다.
// 판단 순서: ① 같은 작업이 이미 대기 중 → 건너뜀 ② subst인데 daily가 대기 중 → 건너뜀(daily가 대용가 포함)
//           ③ 다른 작업이 대기 중이고 새 작업이 그것을 포괄하지 못함 → KV 'deferred'에 보관, 10분마다 빈 시간에 디스패치
//           ④ 그 외(대기 없음, 또는 대기가 subst이고 새 작업이 daily) → 바로 디스패치
async function dispatchScheduled(env, jobs) {
  const st = await runState(env);
  const w = st.waiting.map(jobOf);
  for (const j of jobs) {
    if (w.includes(j) || (j === "subst" && w.includes("daily"))) { console.log(`[scheduler] ${j} 건너뜀(대기 중: ${w.join(",")})`); continue; }
    if (w.length && !(j === "daily" && w.every(x => x === "subst")) && env.ADDQ) {
      const d = JSON.parse((await env.ADDQ.get("deferred")) || "[]");
      if (!d.includes(j)) d.push(j);
      await env.ADDQ.put("deferred", JSON.stringify(d));
      console.log(`[scheduler] ${j} 보류(대기 중: ${w.join(",")}) → 빈 시간에 실행`); continue;
    }
    if (await dispatch(env, j)) w.push(j);
  }
}

async function drain(env) {
  if (!env.ADDQ) return;
  const dfr = JSON.parse((await env.ADDQ.get("deferred")) || "[]");
  if (dfr.length) {                                   // 보류된 정기 작업 먼저(대기가 비었을 때만)
    const s0 = await runState(env);
    if (s0.ok && !s0.waiting.length) {
      if (await dispatch(env, dfr[0])) await env.ADDQ.put("deferred", JSON.stringify(dfr.slice(1)));
    }
    return;
  }
  const { q, inflight } = await loadQ(env);
  const now = Date.now();
  const st = await runState(env);
  if (inflight && !st.running && !st.waiting.length && now - inflight.at > 15 * 60000) await env.ADDQ.delete("inflight");   // 등록 실행 끝남
  if (!q.length || !st.ok || st.running || st.waiting.length) return;
  const ns = nextScheduled(now + 1);
  if (ns && ns.t - now < GUARD_MIN * 60000) return;
  const codes = [...new Set(q.map(x => x.cc))];
  if (await dispatch(env, "unlisted_add", "--type 수동 --codes " + codes.join(" "))) {
    await env.ADDQ.put("inflight", JSON.stringify({ codes, names: q.map(x => x.nm), at: now }));
    await env.ADDQ.put("q", "[]");
  }
}

const json = (o, s = 200) => new Response(JSON.stringify(o, null, 1), { status: s, headers: { "content-type": "application/json; charset=utf-8" } });

export default {
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    const jobs = pick(now);
    if (jobs.length) { ctx.waitUntil(dispatchScheduled(env, jobs)); return; }   // 정기 작업 시각엔 대기열을 건드리지 않음
    ctx.waitUntil(drain(env));
  },
  // GET /            → 스케줄표·다음 실행 예정(점검용)
  // GET /queue       → 회사 추가 대기열·반영 예상 시각
  // GET|POST /visit {new} → 방문자수(오늘·누적)
  // POST /enqueue {corp_code, name}  (헤더 X-Auth: RUN_KEY) → 대기열에 넣고 반영 예상 시각 반환
  // POST /run?job=daily&args=...  (헤더 X-Auth: RUN_KEY)  → 즉시 디스패치
  async fetch(req, env) {
    const u = new URL(req.url);
    const authed = env.RUN_KEY && req.headers.get("X-Auth") === env.RUN_KEY;
    if (req.method === "POST" && u.pathname === "/run") {
      if (!authed) return new Response("forbidden", { status: 403 });
      const ok = await dispatch(env, u.searchParams.get("job") || "price", u.searchParams.get("args") || "");
      return new Response(ok ? "dispatched" : "failed", { status: ok ? 200 : 502 });
    }
    if (u.pathname === "/queue") {
      if (!env.ADDQ) return json({ error: "ADDQ 미설정" }, 500);
      return json(await status(env));
    }
    if (u.pathname === "/visit") {                                     // 방문자수: 브라우저당 하루 1회만 집계(사이트가 new 여부 판단)
      if (!env.ADDQ) return json({ error: "ADDQ 미설정" }, 500);
      const day = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
      let isNew = false;
      if (req.method === "POST") { try { isNew = !!(await req.json()).new; } catch (e) { /* noop */ } }
      const [t, a] = await Promise.all([env.ADDQ.get("v:" + day), env.ADDQ.get("v:total")]);
      let today = +t || 0, total = +a || 0;
      if (isNew) {
        today++; total++;
        await Promise.all([env.ADDQ.put("v:" + day, String(today), { expirationTtl: 40 * 86400 }), env.ADDQ.put("v:total", String(total))]);
      }
      return json({ day, today, total });
    }
    if (req.method === "POST" && u.pathname === "/enqueue") {
      if (!authed) return json({ ok: false, msg: "비밀번호가 틀립니다" }, 403);
      if (!env.ADDQ) return json({ ok: false, msg: "대기열(ADDQ) 미설정" }, 500);
      let b = {}; try { b = await req.json(); } catch (e) { /* noop */ }
      const cc = String(b.corp_code || "").trim(), nm = String(b.name || "").slice(0, 60);
      if (!/^\d{8}$/.test(cc)) return json({ ok: false, msg: "고유번호(8자리)가 올바르지 않습니다" }, 400);
      const { q, inflight } = await loadQ(env);
      if (inflight && inflight.codes.includes(cc)) return json({ ok: true, msg: "이미 등록 실행 중입니다", ...(await status(env)) });
      if (!q.some(x => x.cc === cc)) { q.push({ cc, nm, at: Date.now() }); await env.ADDQ.put("q", JSON.stringify(q)); }
      const s = await status(env);
      const pos = q.findIndex(x => x.cc === cc) + 1;
      return json({ ok: true, msg: `대기열 ${pos}번째로 접수 — ${s.eta ? s.eta.done + "경 반영 예상" : "곧 반영"}`, ...s });
    }
    const now = new Date();
    const next = [];
    for (let i = 1; i <= 7 * 24 * 6 && next.length < 5; i++) {       // 10분 단위로 1주일 훑어 다음 5건
      const t = new Date(Math.floor(now.getTime() / 600000) * 600000 + i * 600000);
      for (const j of pick(t)) next.push({ kst: kst(t.getTime()), job: j });
    }
    return json({ repo: env.GH_REPO, ref: env.GH_REF, now_kst: kst(now.getTime()), schedule_kst: TABLE, next });
  },
};
