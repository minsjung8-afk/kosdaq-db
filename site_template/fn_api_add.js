/**
 * Pages Function — POST /api/add {corp_code, name, key}  (2026-10-26 '회사 추가' 기능)
 * 바로 실행하지 않고 스케줄러 Worker의 신청 대기열(/enqueue)에 넣는다. Worker가 GitHub 작업이 비어 있을 때 한꺼번에 등록을 실행하므로
 * 정기 수집·chain 대기 작업을 취소하지 않는다. 응답에 반영 예상 시각(eta)과 근거(notes)를 담는다.
 * 입력한 관리자 비밀번호(key)는 Worker 인증(X-Auth = RUN_KEY)에 그대로 넘기며 이 함수에는 비밀값을 저장하지 않는다.
 */
const WORKER_URL = "https://kosdaq-db-scheduler.minsjung8.workers.dev";

export async function onRequestPost({ request }) {
  let j = {};
  try { j = await request.json(); } catch (e) { /* noop */ }
  const cc = String(j.corp_code || "").trim(), key = String(j.key || ""), name = String(j.name || "");
  if (!/^\d{8}$/.test(cc)) return Response.json({ ok: false, msg: "고유번호(8자리)가 올바르지 않습니다" }, { status: 400 });
  if (!key) return Response.json({ ok: false, msg: "관리자 비밀번호를 입력하세요" }, { status: 400 });
  const r = await fetch(`${WORKER_URL}/enqueue`, { method: "POST", headers: { "X-Auth": key, "Content-Type": "application/json" }, body: JSON.stringify({ corp_code: cc, name }) });
  if (r.status === 403) return Response.json({ ok: false, msg: "비밀번호가 틀립니다" }, { status: 403 });
  let o = {};
  try { o = await r.json(); } catch (e) { o = { ok: false, msg: "대기열 응답 오류(" + r.status + ")" }; }
  return Response.json(o, { status: r.ok ? 200 : 502 });
}
