"""대용가(KRX) 수집: KRX 정보데이터시스템 '주식 대용가'(화면 MDCSTAT04001, 전종목·전체시장) 1콜 → subst_price 테이블 + price_summary.subst_price 갱신.
- 대용가는 거래소가 전일 종가 기준으로 산출해 당일 적용(MK_DATE=산출기준일, TRD_DD=적용일). 매일 07:00 daily에서 호출(시세 다음).
- 조회일(trdDd)에 자료가 없으면(휴장·미산출) 최대 7일 거슬러 올라가 가장 최근 자료를 쓴다.
  python run.py subst [--start YYYY-MM-DD]   # start 지정 시 그 날짜 기준 1회 조회
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, log, now, to_num  # noqa: E402

URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
BLD = "dbms/MDC/STAT/standard/MDCSTAT04001"
HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
       "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC020102050701",
       "Origin": "https://data.krx.co.kr", "X-Requested-With": "XMLHttpRequest",
       "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Accept": "application/json, text/javascript, */*; q=0.01"}


def fetch(trd_dd: str):
    """trd_dd: YYYYMMDD → [{stock_code, sb_prc, mk_date, prev_close}]"""
    s = requests.Session()
    s.get(HDR["Referer"], headers={"User-Agent": HDR["User-Agent"]}, timeout=30)          # 세션 쿠키
    r = s.post(URL, data={"bld": BLD, "locale": "ko_KR", "searchType": "1", "mktId": "ALL", "trdDd": trd_dd, "csvxls_isNo": "false"},
               headers=HDR, timeout=60)
    r.raise_for_status()
    js = r.json()
    out = []
    for x in js.get("block1", []):
        v = to_num(x.get("SB_PRC"))
        if x.get("ISU_SRT_CD") and v:
            out.append({"stock_code": x["ISU_SRT_CD"], "sb_prc": v, "trd_dd": (x.get("TRD_DD") or "").replace("/", "-"),
                        "mk_date": (x.get("MK_DATE") or "").replace("/", "-"), "prev_close": to_num(x.get("PREVDD_CLSPRC"))})
    return out


def run(start=None, con=None):
    con = con or connect()
    con.execute("""CREATE TABLE IF NOT EXISTS subst_price (stock_code TEXT, trd_dd TEXT, sb_prc REAL, mk_date TEXT, prev_close REAL, fetched_at TEXT,
                   PRIMARY KEY (stock_code, trd_dd))""")
    try:
        con.execute("ALTER TABLE price_summary ADD COLUMN subst_price REAL")
    except Exception:  # noqa
        pass
    try:
        con.execute("ALTER TABLE price_summary ADD COLUMN subst_dd TEXT")
    except Exception:  # noqa
        pass
    d0 = date.fromisoformat(start) if start else date.today()
    rows, used = [], None
    # 0) 국내에서 받아 R2에 올린 파일(subst/latest.json → 워크플로가 subst_latest.json으로 내려줌)이 있으면 우선 사용 — 러너(해외 IP)는 KRX가 400으로 막음
    import json, os
    lf = os.environ.get("SUBST_FILE", "subst_latest.json")
    if os.path.exists(lf):
        try:
            js = json.load(open(lf, encoding="utf-8"))
            rows = js.get("rows") or []
            used = js.get("trd_dd") or (rows[0]["trd_dd"] if rows else None)
            print(f"[subst] R2 파일 사용: 기준 {used} · {len(rows):,}종목 (수집 {js.get('fetched_at')})", flush=True)
        except Exception as e:  # noqa
            print(f"[subst] 로컬 파일 읽기 실패: {e}", flush=True); rows = []
    for back in range(0, 8):
        if rows:
            break
        d = d0 - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            rows = fetch(d.strftime("%Y%m%d"))
        except Exception as e:  # noqa
            log(con, "-", "subst", "fail", f"{d} {e}")
            print(f"[subst] {d} 조회 실패: {e}", flush=True)
            continue
        if rows:
            used = d; break
    if not rows:
        print("[subst] 대용가 자료 없음(최근 7일) — 건너뜀", flush=True)
        return 0
    con.executemany("INSERT OR REPLACE INTO subst_price VALUES (?,?,?,?,?,?)",
                    [(r["stock_code"], r["trd_dd"], r["sb_prc"], r["mk_date"], r["prev_close"], now()) for r in rows])
    # price_summary(최신 asof 행)에 반영 — 사이트는 price_summary.subst_price를 읽는다
    n = 0
    for r in rows:
        cur = con.execute("UPDATE price_summary SET subst_price=?, subst_dd=? WHERE stock_code=? AND asof=(SELECT MAX(asof) FROM price_summary WHERE stock_code=?)",
                          (r["sb_prc"], r["trd_dd"], r["stock_code"], r["stock_code"]))
        n += cur.rowcount
    con.commit()
    log(con, "-", "subst", "ok", f"{used} {len(rows)} rows")
    print(f"[subst] 대용가 {used} 기준 {len(rows):,}종목 수집 · price_summary 반영 {n:,}", flush=True)
    return len(rows)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
