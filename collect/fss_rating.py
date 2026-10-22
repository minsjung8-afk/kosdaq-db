"""금감원 신용평가서 공시(fss.or.kr) → 회사별 신용등급 이력.
- 공개 페이지, 서버 렌더 표, GET 파라미터. robots.txt 제한 없음(2026-10-19 확인). 요청 간격 0.7초.
- 평가대상회사명 검색은 부분일치이므로 결과를 정규화된 회사명으로 다시 거른다.
- 대부분의 코스닥사는 공시 등급이 없다 → rating 테이블에 행 없음 = '유효등급 없음'.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from common import UA, connect, log, now

URL = "https://www.fss.or.kr/fss/job/creditRatingReport/list.do"
PAUSE = 0.7
WORKERS = 3          # FSS 응답이 회사당 4~5초라 3개 동시 조회(총 1시간 내). 과도한 부하 방지 위해 3 유지
RESUME_DAYS = 10     # 최근 N일 내 성공 로그가 있는 회사는 건너뜀(중단된 실행 이어하기)
LONG_TYPES = ("기업신용평가", "회사채", "무보증사채", "공모사채", "사모사채", "신주인수권부사채", "전환사채")   # 장기등급 성격


def norm(name: str) -> str:
    n = re.sub(r"\(주\)|주식회사|㈜|\s+", "", name or "")
    return n.lower()


def search_name(c) -> str:
    """검색어: DART 정식명에서 (주)·주식회사 제거, 없으면 KRX 약식명"""
    full = c.get("corp_name_full") or ""
    s = re.sub(r"\(주\)|주식회사|㈜", "", full).strip()
    return s or c["corp_name"]


def fetch(name: str, years=3, page=1, unit=100) -> str:
    today = date.today()
    params = {"menuNo": "200411", "pageIndex": page, "pageUnit": unit, "searchCompany": "", "searchAssGbn": "", "searchTargetCoNm": name,
              "searchTargetTypeKor": "", "searchCnd": "1", "sdate": (today - timedelta(days=365 * years)).isoformat(),
              "edate": today.isoformat()}
    r = requests.get(URL + "?" + urlencode(params), headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def parse(html: str):
    """열: 번호, 평가회사, 평가대상회사, 평가대상종류, 평가구분, 공시일자, 평가등급, 등급감시, 등급전망, 첨부"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        cells = tr.find_all("td")
        tds = [td.get_text(" ", strip=True) for td in cells]
        if len(tds) < 9 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tds[5]):
            continue
        a = cells[9].find("a") if len(cells) > 9 else None
        href = a.get("href") if a else None
        if href and href.startswith("/"):
            href = "https://www.fss.or.kr" + href
        out.append({"agency": tds[1], "target": tds[2], "ttype": tds[3], "kind": tds[4], "disc_dt": tds[5],
                    "grade": tds[6], "watch": tds[7], "outlook": tds[8], "file_url": href})
    m = re.search(r"전체\s*(\d+)\s*건", soup.get_text(" "))
    return out, (int(m.group(1)) if m else len(out))


def _all_pages(term, years, max_pages):
    rows, page = [], 1
    while True:
        r, total = parse(fetch(term, years, page))
        rows += r
        if len(rows) >= total or not r or page >= max_pages:
            return rows, total
        page += 1
        time.sleep(PAUSE)


def fetch_company_rows(c, years=3):
    """네트워크만: 회사 1곳의 공시 행(페이지당 100행). 스레드에서 호출.
    금감원 검색은 부분일치라 '한화'로 찾으면 계열사 포함 수천 건 → 평가대상명 표기 그대로 '(주)한화'·'한화(주)'로 먼저 좁혀 찾고,
    둘 다 없을 때만 회사명 자체로 찾는다(최대 30페이지=3,000행). 최종 거르기는 store_rows의 완전일치."""
    name = search_name(c)
    key = norm(name)
    rows = []
    for term in (f"(주){name}", f"{name}(주)"):
        r, _ = _all_pages(term, years, 10)
        rows += [x for x in r if norm(x["target"]) == key]
        time.sleep(PAUSE)
    if not rows:
        r, _ = _all_pages(name, years, 30)
        rows = r
    seen, out = set(), []
    for x in rows:                                    # 두 검색어 결과 중복 제거
        k = (x["agency"], x["ttype"], x["disc_dt"], x["grade"], x["target"])
        if k not in seen:
            seen.add(k); out.append(x)
    return out


def _ensure_target(con):
    try:
        con.execute("ALTER TABLE credit_rating ADD COLUMN target TEXT")
    except Exception:  # noqa
        pass


def store_rows(con, c, rows, replace=False):
    """평가대상회사명이 정확히 같은 행만(정규화 후). 예전 '부분일치' 허용 때문에 CJ ← CJ푸드빌·CJ대한통운, SK ← SK하이닉스 등
    계열사 등급이 섞이던 문제(2026-10-22) → 완전일치로 변경. replace=True면 그 회사의 기존 행을 지우고 다시 넣음."""
    _ensure_target(con)
    keys = {norm(search_name(c)), norm(c.get("corp_name"))} - {""}
    ok = [r for r in rows if norm(r["target"]) in keys]
    if replace:
        con.execute("DELETE FROM credit_rating WHERE corp_code=?", (c["corp_code"],))
    for r in ok:
        con.execute("""INSERT OR REPLACE INTO credit_rating (corp_code, agency, target_type, eval_kind, disc_dt, grade, watch, outlook, fetched_at, file_url, target)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (c["corp_code"], r["agency"], r["ttype"], r["kind"], r["disc_dt"], r["grade"], r["watch"], r["outlook"], now(), r["file_url"], r["target"]))
    con.commit()
    return len(ok)


def fix_mismatch(con=None, years=3):
    """기존 등급 보유사만 다시 조회해 완전일치 행으로 교체 → 바뀐 회사 목록 출력 + 잘못된 평가서로 만든 요약(rating_summary) 삭제"""
    con = con or connect()
    _ensure_target(con)
    cs = [dict(r) for r in con.execute("""SELECT c.corp_code, c.corp_name, c.corp_name_full FROM company c
                                          WHERE c.corp_code IN (SELECT DISTINCT corp_code FROM credit_rating)""")]
    before = {r[0]: r[1] for r in con.execute("SELECT corp_code, COUNT(*) FROM credit_rating GROUP BY corp_code")}
    print(f"[rating-fix] 등급 보유 {len(cs)}사 재조회(완전일치)", flush=True)
    changed = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [(c, ex.submit(fetch_company_rows, c, years)) for c in cs]
        for i, (c, f) in enumerate(futs, 1):
            try:
                n = store_rows(con, c, f.result(), replace=True)
                if n != before.get(c["corp_code"], 0):
                    changed.append((c["corp_name"], before.get(c["corp_code"], 0), n))
            except Exception as e:  # noqa
                log(con, c["corp_code"], "rating_fix", "fail", e)
            if i % 50 == 0:
                print(f"[rating-fix] {i}/{len(cs)} · 변경 {len(changed)}사", flush=True)
    # 요약의 근거 평가서가 더 이상 그 회사 등급 목록에 없으면 삭제(다음 요약 실행에서 올바른 평가서로 재생성)
    try:
        bad = [r[0] for r in con.execute("""SELECT s.corp_code FROM rating_summary s WHERE s.file_url IS NOT NULL AND NOT EXISTS
                     (SELECT 1 FROM credit_rating r WHERE r.corp_code=s.corp_code AND r.file_url=s.file_url)""")]
        for cc in bad:
            con.execute("DELETE FROM rating_summary WHERE corp_code=?", (cc,))
        con.commit()
        print(f"[rating-fix] 잘못된 평가서 기반 요약 삭제 {len(bad)}사", flush=True)
    except Exception as e:  # noqa
        print("[rating-fix] 요약 정리 실패:", e)
    print(f"[rating-fix] 등급 행 수가 바뀐 회사 {len(changed)}사:", flush=True)
    for nm, a, b in sorted(changed, key=lambda x: x[1] - x[2], reverse=True):
        print(f"  {nm}: {a} → {b}건", flush=True)
    log(con, "-", "rating_fix", "ok", f"{len(cs)} checked / {len(changed)} changed")
    return changed


def collect_company(con, c, years=3):
    return store_rows(con, c, fetch_company_rows(c, years))


def run(corp_codes=None, years=3, resume=True):
    con = connect()
    q = "SELECT corp_code, corp_name, corp_name_full FROM company ORDER BY corp_name"
    cs = [dict(r) for r in con.execute(q)]
    if corp_codes:
        cs = [c for c in cs if c["corp_code"] in corp_codes]
    if resume:
        done = {r[0] for r in con.execute("SELECT corp_code FROM fetch_log WHERE job='rating' AND status='ok' AND at >= date('now', ?)", (f"-{RESUME_DAYS} days",))}
        if done:
            cs = [c for c in cs if c["corp_code"] not in done]
            print(f"[rating] 최근 {RESUME_DAYS}일 내 완료 {len(done)}사 제외 → {len(cs)}사", flush=True)
    hit = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [(c, ex.submit(fetch_company_rows, c, years)) for c in cs]   # 네트워크는 병렬, DB 쓰기는 순차
        for i, (c, f) in enumerate(futs, 1):
            try:
                n = store_rows(con, c, f.result())
                log(con, c["corp_code"], "rating", "ok", n)
                if n:
                    hit += 1
            except Exception as e:  # noqa
                log(con, c["corp_code"], "rating", "fail", e)
            if i % 100 == 0:
                print(f"[rating] {i}/{len(cs)}, 등급 보유 {hit}", flush=True)
    log(con, "-", "rating", "ok", f"{len(cs)} checked / {hit} rated")
    return con


if __name__ == "__main__":
    rows, total = parse(fetch("에스케이해운"))
    print(total, rows[:3])
