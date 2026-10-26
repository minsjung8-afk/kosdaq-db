/**
 * Pages Function — POST /api/visit {new:boolean}  (2026-10-26 방문자수)
 * 스케줄러 Worker의 /visit로 전달. new=true면 오늘·누적 +1, false면 조회만. 브라우저당 하루 1회 판단은 사이트(localStorage)가 함.
 */
const WORKER_URL = "https://kosdaq-db-scheduler.minsjung8.workers.dev";

export async function onRequest({ request }) {
  try {
    const init = request.method === "POST" ? { method: "POST", headers: { "content-type": "application/json" }, body: await request.text() } : {};
    const r = await fetch(`${WORKER_URL}/visit`, init);
    return new Response(await r.text(), { status: r.status, headers: { "content-type": "application/json; charset=utf-8", "Cache-Control": "no-store" } });
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 502 });
  }
}
