"""일일 델타 수집: DART 전체 공시목록(회사 미지정, 날짜 범위)을 1회 훑어 심사 관점 수시공시만 반영.
- 전환청구권행사 / 신주인수권행사 / 교환청구권행사 / 사채 조기상환 → mezz_event (회차·금액) → 사이트에서 잔액 차감
- 전환가액의 조정 → mezz_event(kind=리픽싱, 신규 전환가)
- 주식등의 대량보유상황보고서 → 해당 회사 majorstock 재조회
- 최대주주변경·담보제공·소송·횡령·관리종목·감사보고서 등 → disclosure_event
호출량: 목록 ~30~60콜/일 + 본문 ~20~60콜/일 (DART 한도의 1% 수준)
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, dart_get, log, now, to_num  # noqa: E402
from business import EVENT_FLAGS, _strip, fetch_document_xml, fetch_major_reports  # noqa: E402

MEZZ_KW = {"전환청구권행사": "전환", "신주인수권행사": "행사", "교환청구권행사": "교환", "조기상환청구권행사": "상환",
           "사채조기상환": "상환", "전환가액의조정": "리픽싱", "행사가액의조정": "리픽싱", "교환가액의조정": "리픽싱"}
NUM = r"([0-9][0-9,]*)"


def _amt(text, *labels):
    """'권면총액 ... 1,000,000,000' 식 라벨 뒤 첫 숫자(원)"""
    for lb in labels:
        m = re.search(lb + r"[^0-9]{0,40}" + NUM, text)
        if m:
            v = to_num(m.group(1))
            if v:
                return v
    return None


def parse_mezz_event(xml_text, kind):
    t = re.sub(r"\s+", " ", _strip(xml_text))
    rnd = None
    m = re.search(r"제\s*(\d+)\s*회", t)
    if m:
        rnd = int(m.group(1))
    out = {"round": rnd, "amount": None, "price": None, "shares": None, "dt": None}
    if kind in ("전환", "행사", "교환"):
        out["amount"] = _amt(t, "청구금액", "행사금액", "권면총액", "전환청구금액", "행사총액")
        out["shares"] = _amt(t, "발행할 주식수", "전환주식수", "주식수")
        out["price"] = _amt(t, "전환가액", "행사가액", "교환가액")
    elif kind == "상환":
        out["amount"] = _amt(t, "상환금액", "조기상환금액", "권면총액")
    elif kind == "리픽싱":
        out["price"] = _amt(t, "조정후", "조정 후", "변경후", "변경 후")
    md = re.search(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", t)
    if md:
        out["dt"] = f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
    return out


def run(days=None, bgn=None, end=None):
    con = connect()
    con.execute("""CREATE TABLE IF NOT EXISTS mezz_event (corp_code TEXT, rcept_no TEXT PRIMARY KEY, rcept_dt TEXT, kind TEXT,
                   round INTEGER, amount REAL, shares REAL, price REAL, report_nm TEXT, fetched_at TEXT)""")
    cmap = {r["corp_code"]: r["stock_code"] for r in con.execute("SELECT corp_code, stock_code FROM company")}
    if not bgn:
        last = con.execute("SELECT MAX(msg) FROM fetch_log WHERE job='delta' AND status='ok'").fetchone()[0]
        if last and not days:
            bgn = (date.fromisoformat(last) + timedelta(days=1)).strftime("%Y%m%d")
        else:
            bgn = (date.today() - timedelta(days=days or 3)).strftime("%Y%m%d")
    end = end or date.today().strftime("%Y%m%d")
    if bgn > end:
        print(f"[delta] 최신 상태 ({bgn} > {end})")
        return
    page, n_list, n_mezz, n_major, n_evt = 1, 0, 0, 0, 0
    while True:
        js = dart_get("list", bgn_de=bgn, end_de=end, page_no=page, page_count=100)
        rows = js.get("list", [])
        for r in rows:
            cc = r.get("corp_code")
            if cc not in cmap:
                continue
            nm = (r.get("report_nm") or "").replace(" ", "")
            n_list += 1
            kind = next((v for k, v in MEZZ_KW.items() if k in nm), None)
            if kind:
                if con.execute("SELECT 1 FROM mezz_event WHERE rcept_no=?", (r["rcept_no"],)).fetchone():
                    continue
                try:
                    ev = parse_mezz_event(fetch_document_xml(r["rcept_no"]), kind)
                except Exception as e:  # noqa
                    log(con, cc, "delta_mezz", "fail", f"{r['rcept_no']} {e}")
                    ev = {"round": None, "amount": None, "price": None, "shares": None, "dt": None}
                con.execute("INSERT OR REPLACE INTO mezz_event VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cc, r["rcept_no"], r["rcept_dt"], kind, ev["round"], ev["amount"], ev["shares"], ev["price"], r["report_nm"], now()))
                n_mezz += 1
            elif "대량보유상황보고서" in nm:
                try:
                    fetch_major_reports(con, cc)
                    n_major += 1
                except Exception as e:  # noqa
                    log(con, cc, "delta_major", "fail", e)
            flag = next((v for k, v in EVENT_FLAGS.items() if k in r.get("report_nm", "")), None)
            if flag:
                con.execute("INSERT OR REPLACE INTO disclosure_event VALUES (?,?,?,?,?)",
                            (cc, r["rcept_no"], r["rcept_dt"], r["report_nm"], flag))
                n_evt += 1
        con.commit()
        if page >= int(js.get("total_page") or 1) or not rows:
            break
        page += 1
    log(con, "-", "delta", "ok", date.today().isoformat())
    print(f"[delta] {bgn}~{end}: 목록 {page}p, 대상회사 공시 {n_list}건 · 메자닌 이벤트 {n_mezz} · 5%보고 {n_major} · 이벤트 {n_evt}", flush=True)


if __name__ == "__main__":
    run(days=int(sys.argv[1]) if len(sys.argv) > 1 else None)
