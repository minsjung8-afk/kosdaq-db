/**
 * Pages Function — POST /api/add {corp_code, key}  (2026-10-26 '회사 추가' 기능)
 * 입력한 관리자 비밀번호(key)를 그대로 스케줄러 Worker의 /run 인증(X-Auth = RUN_KEY)에 넘겨
 * collect.yml을 job=unlisted_add, args="--type 수동 --codes <고유번호>"로 실행한다.
 * 이 함수에는 비밀값을 저장하지 않는다(비밀번호가 틀리면 Worker가 403).
 */
const WORKER_URL = "WORKER_URL_PLACEHOLDER";   // 예: https://kosdaq-db-scheduler.<계정>.workers.dev

export async function onRequestPost({ request }) {
  let j = {};
  try { j = await request.json(); } catch (e) { /* noop */ }
  const cc = String(j.corp_code || "").trim(), key = String(j.key || "");
  if (!/^\d{8}$/.test(cc)) return Response.json({ ok: false, msg: "고유번호(8자리)가 올바르지 않습니다" }, { status: 400 });
  if (!key) return Response.json({ ok: false, msg: "관리자 비밀번호를 입력하세요" }, { status: 400 });
  if (WORKER_URL.includes("PLACEHOLDER")) return Response.json({ ok: false, msg: "Worker 주소 미설정" }, { status: 500 });
  const u = `${WORKER_URL}/run?job=unlisted_add&args=${encodeURIComponent("--type 수동 --codes " + cc)}`;
  const r = await fetch(u, { method: "POST", headers: { "X-Auth": key } });
  if (r.status === 403) return Response.json({ ok: false, msg: "비밀번호가 틀립니다" }, { status: 403 });
  return Response.json({ ok: r.ok, msg: r.ok ? "등록 요청 완료 — 약 10분 뒤(다른 작업이 돌고 있으면 그 뒤) 목록에 나타납니다" : "실행 요청 실패(" + r.status + ")" }, { status: r.ok ? 200 : 502 });
}
