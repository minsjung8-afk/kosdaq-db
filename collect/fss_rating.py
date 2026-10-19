"""금감원 신용평가서 공시(fss.or.kr) → 회사별 신용등급 이력.
- 공개 페이지, 서버 렌더 표, GET 파라미터. robots.txt 제한 없음(2026-10-19 확인). 요청 간격 0.7초.
- 평가대상회사명 검색은 부분일치이므로 결과를 정규화된 회사명으로 다시 거른다.
- 대부분의 코스닥사는 공시 등급이 없다 → rating 테이블에 행 없음 = '유효등급 없음'.
"""
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from common import UA, connect, log, now

URL = "https://www.fss.or.kr/fss/job/creditRatingReport/list.do"
PAUSE = 0.7
LONG_TYPES = ("기업신용평가", "회사채", "무보증사채", "공모사채", "사모사채", "신주인수권부사채", "전환사채")   # 장기등급 성격


def norm(name: str) -> str:
    n = re.sub(r"\(주\)|주식회사|㈜|\s+", "", name or "")
    return n.lower()


def search_name(c) -> str:
    """검색어: DART 정식명에서 (주)·주식회사 제거, 없으면 KRX 약식명"""
    full = c.get("corp_name_full") or ""
    s = re.sub(r"\(주\)|주식회사|㈜", "", full).strip()
    return s or c["corp_name"]


def fetch(name: str, years=3, page=1) -> str:
    today = date.today()
    params = {"menuNo": "200411", "pageIndex": page, "searchCompany": "", "searchAssGbn": "", "searchTargetCoNm": name,
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


def collect_company(con, c, years=3):
    name = search_name(c)
    key = norm(name)
    rows, total = [], 0
    page = 1
    while True:
        r, total = parse(fetch(name, years, page))
        rows += r
        if len(rows) >= total or not r or page >= 10:
            break
        page += 1
        time.sleep(PAUSE)
    n = 0
    for r in rows:
        if norm(r["target"]) != key and key not in norm(r["target"]):
            continue
        con.execute("""INSERT OR REPLACE INTO credit_rating (corp_code, agency, target_type, eval_kind, disc_dt, grade, watch, outlook, fetched_at, file_url)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (c["corp_code"], r["agency"], r["ttype"], r["kind"], r["disc_dt"], r["grade"], r["watch"], r["outlook"], now(), r["file_url"]))
        n += 1
    con.commit()
    return n


def run(corp_codes=None, years=3):
    con = connect()
    q = "SELECT corp_code, corp_name, corp_name_full FROM company ORDER BY corp_name"
    cs = [dict(r) for r in con.execute(q)]
    if corp_codes:
        cs = [c for c in cs if c["corp_code"] in corp_codes]
    hit = 0
    for i, c in enumerate(cs, 1):
        try:
            n = collect_company(con, c, years)
            if n:
                hit += 1
        except Exception as e:  # noqa
            log(con, c["corp_code"], "rating", "fail", e)
        if i % 100 == 0:
            print(f"[rating] {i}/{len(cs)}, 등급 보유 {hit}", flush=True)
        time.sleep(PAUSE)
    log(con, "-", "rating", "ok", f"{len(cs)} checked / {hit} rated")
    return con


if __name__ == "__main__":
    rows, total = parse(fetch("에스케이해운"))
    print(total, rows[:3])
