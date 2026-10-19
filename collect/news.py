"""뉴스 헤드라인: Google News RSS (키 불필요). 제목·언론사·날짜·링크만 저장(본문 저장 안 함).
- 회사당 1요청, 요청 간격 1.2초. 최근 90일만 보관.
- 짧은/일반명사형 회사명은 오탐이 많아 "회사명" 코스닥 으로 검색어를 보강하고, 제목에 회사명이 포함된 기사만 남긴다.
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from common import UA, connect, log, now

RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
PAUSE = 1.2
KEEP_DAYS = 90
RESUME_DAYS = 3      # 최근 N일 내 성공한 회사는 건너뜀(중단된 실행 이어하기)


def _norm(s):
    return re.sub(r"\s|\(주\)|주식회사|㈜", "", s or "").lower()


def fetch_news(name: str, strict: bool):
    q = f'"{name}"' + (" 코스닥" if strict else "")
    r = requests.get(RSS.format(q=quote(q)), headers=UA, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        src = it.findtext("source") or ""
        pub = it.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        except Exception:  # noqa
            dt = None
        # 제목 끝 " - 언론사" 제거
        title = re.sub(r"\s+-\s+[^-]{1,30}$", "", title)
        out.append({"title": title, "url": link, "source": src, "dt": dt})
    return out


def collect_company(con, c):
    name = c["corp_name"]
    strict = len(_norm(name)) <= 3 or name.isascii()           # 짧거나 영문이면 보강 검색
    items = fetch_news(name, strict)
    key = _norm(name)
    cut = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    n = 0
    for it in items:
        if not it["dt"] or it["dt"] < cut:
            continue
        if key not in _norm(it["title"]):
            continue
        con.execute("INSERT OR IGNORE INTO news (corp_code, dt, title, source, url, fetched_at) VALUES (?,?,?,?,?,?)",
                    (c["corp_code"], it["dt"], it["title"], it["source"], it["url"], now()))
        n += 1
    con.execute("DELETE FROM news WHERE corp_code=? AND dt < ?", (c["corp_code"], cut))
    con.commit()
    return n


def run(corp_codes=None):
    con = connect()
    cs = [dict(r) for r in con.execute("SELECT corp_code, corp_name FROM company ORDER BY corp_name")]
    if corp_codes:
        cs = [c for c in cs if c["corp_code"] in corp_codes]
    done = {r[0] for r in con.execute("SELECT corp_code FROM fetch_log WHERE job='news' AND status='ok' AND at >= date('now', ?)", (f"-{RESUME_DAYS} days",))}
    if done:
        cs = [c for c in cs if c["corp_code"] not in done]
        print(f"[news] 최근 {RESUME_DAYS}일 내 완료 {len(done)}사 제외 → {len(cs)}사", flush=True)
    tot = 0
    for i, c in enumerate(cs, 1):
        try:
            n = collect_company(con, c)
            tot += n
            log(con, c["corp_code"], "news", "ok", n)
        except Exception as e:  # noqa
            log(con, c["corp_code"], "news", "fail", e)
        if i % 100 == 0:
            print(f"[news] {i}/{len(cs)}, 저장 {tot}", flush=True)
        time.sleep(PAUSE)
    log(con, "-", "news", "ok", f"{len(cs)} companies / {tot} items")
    return con


if __name__ == "__main__":
    print(fetch_news("에코프로", False)[:3])
