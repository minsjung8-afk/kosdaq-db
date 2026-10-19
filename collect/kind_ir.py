"""KIND IR자료실 수집(선택). 회사가 공개한 기업설명회 자료 PDF 링크 → report_meta(source=KIND_IR).
클라우드 IP는 bot 차단되므로 PC에서 실행. 커버리지는 자발적 공시사 한정."""
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup  # pip install beautifulsoup4

from common import UA, connect, log

URL = "https://kind.krx.co.kr/corpgeneral/irschedule.do"
MKT = {"ALL": "", "KOSPI": "1", "KOSDAQ": "2", "KONEX": "6"}  # 실제 값은 폼 소스에서 확인


def fetch_page(market="KOSDAQ", frm=None, to=None, page=1):
    to = to or date.today()
    frm = frm or (to - timedelta(days=90))
    data = {"method": "searchIRScheduleSub", "gubun": "iRMaterials", "currentPageSize": "100", "pageIndex": str(page),
            "marketType": MKT[market], "fromDate": frm.strftime("%Y-%m-%d"), "toDate": to.strftime("%Y-%m-%d")}
    r = requests.post(URL, data=data, headers={**UA, "Referer": URL + "?method=searchIRScheduleMain&gubun=iRMaterials"}, timeout=30)
    r.raise_for_status()
    return r.text


def parse(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tr.find("a", href=re.compile(r"\.pdf|download|file", re.I))
        name = tds[1].get_text(strip=True)
        dt = tds[2].get_text(strip=True)
        title = tds[3].get_text(strip=True)
        href = a["href"] if a else None
        if href and href.startswith("/"):
            href = "https://kind.krx.co.kr" + href
        code_a = tds[1].find("a")
        m = re.search(r"\d{6}", (code_a.get("onclick") or code_a.get("href") or "") if code_a else "")
        out.append({"corp_name": name, "stock_code": m.group(0) if m else None, "pub_dt": dt.replace("-", ""),
                    "title": title, "url": href})
    return out


def run(market="KOSDAQ", days=365):
    con = connect()
    name2code = {r[0]: r[1] for r in con.execute("SELECT corp_name, stock_code FROM company")}
    to = date.today()
    frm = to - timedelta(days=days)
    page, n = 1, 0
    while True:
        rows = parse(fetch_page(market, frm, to, page))
        if not rows:
            break
        for r in rows:
            sc = r["stock_code"] or name2code.get(r["corp_name"])
            if not sc or not r["url"]:
                continue
            con.execute("INSERT OR IGNORE INTO report_meta (stock_code, source, title, publisher, pub_dt, url) VALUES (?,?,?,?,?,?)",
                        (sc, "KIND_IR", r["title"], r["corp_name"], r["pub_dt"], r["url"]))
            n += 1
        con.commit()
        page += 1
        if page > 50:
            break
    log(con, "-", "kind_ir", "ok", f"{n} rows")
    return con


if __name__ == "__main__":
    print(parse(fetch_page())[:3])
