/**
 * Pages Function — GET /api/queue  (2026-10-26 '회사 추가' 대기열 현황)
 * 스케줄러 Worker의 /queue(대기 회사·등록 실행 중·반영 예상 시각·지금 돌고 있는 작업)를 그대로 전달. 인증 불필요(읽기 전용).
 */
const WORKER_URL = "https://kosdaq-db-scheduler.minsjung8.workers.dev";

export async function onRequestGet() {
  try {
    const r = await fetch(`${WORKER_URL}/queue`);
    return new Response(await r.text(), { status: r.status, headers: { "content-type": "application/json; charset=utf-8", "Cache-Control": "no-store" } });
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 502 });
  }
}
