"""한경컨센서스 리포트 수집 (Playwright, headless Chromium)
- robots.txt: /consensus 허용 확인(2026-10-19). 목록 API는 페이지 내부 토큰으로 보호되므로
  API를 직접 호출하지 않고, 실제 페이지를 열어 페이지가 받는 응답(JSON)을 그대로 읽는다.
- 요청 간격을 두고, 이미 받은 리포트(source_id)는 건너뛴다.
- PDF는 개인 PC reports/<종목코드>/ 에 저장. 사이트에는 배포되지 않음.
설치: pip install playwright && playwright install chromium
"""
import json
import re
import time
from pathlib import Path

from common import ROOT, connect, log, now, to_num

BASE = "https://markets.hankyung.com"
REPORT_DIR = ROOT / "reports"
PAUSE = 1.5  # 초


def _safe(s: str) -> str:
    return re.sub(r"[^\w가-힣().\- ]", "_", s)[:80]


def collect_list(period="y1", per_page="200", max_pages=None, headless=True):
    """기업 리포트 목록을 페이지가 받은 JSON 그대로 수집. 반환: dict 리스트(API 원본 필드)"""
    from playwright.sync_api import sync_playwright  # noqa

    rows, seen = [], set()
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=headless)
        except Exception:  # noqa  GitHub 러너: 브라우저 미설치 → 그 자리에서 설치 후 재시도(2026-10-28)
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=False)
            br = p.chromium.launch(headless=headless)
        pg = br.new_page(locale="ko-KR")
        pg.set_default_timeout(30000)

        def on_response(resp):
            if "/api/v2/consensus/search/report" in resp.url and resp.status == 200:
                try:
                    js = resp.json()
                except Exception:  # noqa
                    return
                data = js.get("data") if isinstance(js, dict) else None
                if isinstance(data, dict):           # 2026-10-28: 응답 data가 {"0": {...}} 객체로 바뀐 경우(페이지네이션 응답) → 값 목록
                    data = list(data.values())
                for r in data or []:
                    if not isinstance(r, dict):
                        continue
                    k = r.get("REPORT_IDX")
                    if k not in seen:
                        seen.add(k)
                        rows.append(r)
                on_response.last = js
                on_response.url = resp.url
        on_response.last = None
        on_response.url = None
        pg.on("response", on_response)

        pg.goto(f"{BASE}/consensus", wait_until="networkidle")
        pg.locator("a[href='javascript:;']:text-is('기업')").first.click()
        pg.wait_for_timeout(1500)
        # 2026-10-28: 기간·개수 선택은 select2(jQuery) 드롭다운 — 숨은 <select> 값만 바꾸면 페이지가 모름 → 실제 드롭다운을 열고 항목을 클릭
        PL = {"d7": "최근 7일", "m1": "1개월", "m6": "6개월", "y1": "1년"}

        def pick(sel_idx, text):
            sel = pg.locator("select.form-control").nth(sel_idx)
            box = sel.locator("xpath=following-sibling::span[contains(@class,'select2')][1]")
            box.click()
            pg.locator(".select2-results__option", has_text=text).first.click()
            pg.wait_for_timeout(800)
        try:
            pick(0, PL.get(period, "1개월"))
            pick(pg.locator("select.form-control").count() - 1, f"{per_page}개")
        except Exception as e:  # noqa
            print(f"[hk] 드롭다운 선택 실패: {str(e)[:120]}", flush=True)
        pg.locator(".report-search-field button.btn-primary").first.click()    # 상단 통합검색 버튼이 아닌 컨센서스 검색 버튼
        pg.wait_for_timeout(2500)
        print(f"[hk] 마지막 요청: {(on_response.url or '').split('?')[-1][:160]}", flush=True)

        lp = on_response.last if isinstance(on_response.last, dict) else {}
        last_page = int(lp.get("last_page") or 1)
        print(f"[hk] 목록 {len(rows)}건 수집(1쪽) · 전체 {last_page}쪽", flush=True)
        if max_pages:
            last_page = min(last_page, max_pages)
        import re as _re
        cur_page = lambda: int((_re.search(r"[?&]page=(\d+)", on_response.url or "") or [0, 1])[1])
        page = 1
        while page < last_page:
            page += 1
            pg_box = pg.locator(".report-paging, .paging, .pagination, [class*=paging]")
            pager = pg_box.last if pg_box.count() else pg
            link = pager.locator(f"a:text-is('{page}')")
            if link.count() == 0:              # 쪽 번호가 다음 묶음에 있음 → '다음' 화살표로 묶음 이동 후 다시 찾기(2026-10-28: 11쪽 이후 누락 수정)
                nxt = pager.locator("a:has-text('다음'), a[class*=next], button[class*=next]")
                if nxt.count() == 0:
                    print(f"[hk] {page}쪽 이동 버튼 없음 — 중단", flush=True)
                    break
                nxt.first.click()
                pg.wait_for_timeout(int(PAUSE * 1000))
                if cur_page() == page:
                    continue
                link = pager.locator(f"a:text-is('{page}')")
                if link.count() == 0:
                    print(f"[hk] {page}쪽 링크 없음(현재 {cur_page()}쪽) — 중단", flush=True)
                    break
            link.first.click()
            pg.wait_for_timeout(int(PAUSE * 1000))
        br.close()
    print(f"[hk] 목록 총 {len(rows)}건", flush=True)
    return rows


def upsert_meta(con, r, only_codes=None):
    sc = (r.get("BUSINESS_CODE") or "").zfill(6)
    if not sc.strip("0") or (only_codes and sc not in only_codes):
        return None
    eps = {}
    for i in (1, 2, 3):
        y, e = r.get(f"STOCK_SETTLEMENT_DAY{i}"), r.get(f"STOCK_EPS{i}")
        if y and e:
            eps[str(y)[:4]] = to_num(e)
    con.execute("""INSERT INTO report_meta (stock_code, source, source_id, title, publisher, analyst, pub_dt, opinion, old_opinion,
        target_price, old_target_price, eps_json, url, pdf_url, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source, source_id) DO UPDATE SET opinion=excluded.opinion, target_price=excluded.target_price""",
                (sc, "HK", str(r["REPORT_IDX"]), r.get("REPORT_TITLE"), r.get("OFFICE_NAME"), r.get("REPORT_WRITER"),
                 (r.get("REPORT_DATE") or "").replace("-", ""), r.get("GRADE_VALUE"), r.get("OLD_GRADE_VALUE"),
                 to_num(r.get("TARGET_STOCK_PRICES")), to_num(r.get("OLD_TARGET_STOCK_PRICES")),
                 json.dumps(eps, ensure_ascii=False) if eps else None,
                 f"{BASE}/consensus/view/{r['REPORT_IDX']}", r.get("REPORT_FILEPATH"), now()))
    return sc


def download_pdfs(con, limit=None):
    """local_path 없는 HK 리포트 PDF를 개인 PC에 저장"""
    import requests  # noqa
    from common import UA  # noqa
    q = "SELECT id, stock_code, pub_dt, publisher, source_id, pdf_url FROM report_meta WHERE source='HK' AND pdf_url IS NOT NULL AND local_path IS NULL ORDER BY pub_dt DESC"
    rows = con.execute(q).fetchall()
    if limit:
        rows = rows[:limit]
    n = 0
    for r in rows:
        d = REPORT_DIR / r["stock_code"]
        d.mkdir(parents=True, exist_ok=True)
        fn = d / f"{r['pub_dt']}_{_safe(r['publisher'] or '')}_{r['source_id']}.pdf"
        try:
            resp = requests.get(r["pdf_url"], headers={**UA, "Referer": f"{BASE}/consensus/view/{r['source_id']}"}, timeout=60)
            if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                fn.write_bytes(resp.content)
                con.execute("UPDATE report_meta SET local_path=? WHERE id=?", (str(fn.relative_to(ROOT)), r["id"]))
                con.commit()
                n += 1
            else:
                log(con, r["stock_code"], "hk_pdf", "fail", f"{resp.status_code} {r['pdf_url']}")
        except Exception as e:  # noqa
            log(con, r["stock_code"], "hk_pdf", "fail", e)
        time.sleep(PAUSE)
    return n


def run(period="y1", max_pages=None, pdf=True, pdf_limit=None, only_db_companies=True):
    con = connect()
    codes = {r[0] for r in con.execute("SELECT stock_code FROM company")} if only_db_companies else None
    rows = collect_list(period=period, max_pages=max_pages)
    n = sum(1 for r in rows if upsert_meta(con, r, codes))
    con.commit()
    log(con, "-", "hk_list", "ok", f"{len(rows)} seen / {n} stored")
    if pdf:
        d = download_pdfs(con, pdf_limit)
        log(con, "-", "hk_pdf", "ok", f"{d} files")
    return con


if __name__ == "__main__":
    run(period="d7", max_pages=1, pdf_limit=3)
