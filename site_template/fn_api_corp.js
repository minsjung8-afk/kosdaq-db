/**
 * Pages Function — GET /api/corp?q=회사명  (2026-10-26 '회사 추가' 기능)
 * DART 웹 공시검색(최근 2년, 회사명 부분일치)으로 고유번호 후보를 찾고, 후보별 기업개황(대표자·주소·업종·설립일)을 붙여 돌려준다.
 * 인증키·비밀값 없음(공개 웹페이지 조회만). 동명 법인 구분용으로 기업개황을 반드시 보여 준다.
 */
const DART = "https://dart.fss.or.kr";
const UA = { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36" };
const strip = s => s.replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();

async function overview(cik) {
  const r = await fetch(DART + "/dsae001/selectPopup.ax", {
    method: "POST", body: new URLSearchParams({ selectKey: cik }),
    headers: { ...UA, "X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded" },
  });
  const t = strip(await r.text());
  const g = (k, nxt) => { const m = t.match(new RegExp(k + " (.*?) (" + nxt + ")")); return m ? m[1].replace(" 정보 더보기", "").trim() : ""; };
  return {
    name: g("회사이름", "영문명|공시회사명"), stock: g("종목코드", "대표자명"), ceo: g("대표자명", "법인구분"),
    kind: g("법인구분", "법인등록번호"), addr: g("주소", "홈페이지|IR홈페이지|전화번호"), induty: g("업종명", "설립일"), est: g("설립일", "결산월"),
  };
}

export async function onRequestGet({ request }) {
  const q = (new URL(request.url).searchParams.get("q") || "").trim();
  if (q.length < 2) return Response.json({ error: "회사명을 2자 이상 입력하세요" }, { status: 400 });
  const end = new Date(Date.now() + 9 * 3600e3), bgn = new Date(end.getTime() - 730 * 86400e3);
  const ymd = d => d.toISOString().slice(0, 10).replace(/-/g, "");
  const body = new URLSearchParams({ currentPage: "1", maxResults: "100", maxLinks: "5", sort: "date", series: "desc",
                                      textCrpNm: q, startDate: ymd(bgn), endDate: ymd(end) });
  const r = await fetch(DART + "/dsab007/detailSearch.ax", {
    method: "POST", body, headers: { ...UA, "X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded", "Referer": DART + "/dsab007/main.do" },
  });
  const h = await r.text();
  const seen = new Map();
  for (const m of h.matchAll(/openCorpInfoNew\('(\d{8})'[^>]*>\s*([^<]+)</g)) {
    if (!seen.has(m[1])) seen.set(m[1], m[2].trim());
    if (seen.size >= 8) break;
  }
  const out = [];
  for (const [cik, nm] of seen) {
    let ov = {};
    try { ov = await overview(cik); } catch (e) { /* 개황 실패해도 후보는 보여 줌 */ }
    out.push({ corp_code: cik, name: ov.name || nm, ...ov });
  }
  return Response.json({ q, results: out }, { headers: { "Cache-Control": "no-store" } });
}
