"""주가(KRX API): 날짜별 전종목 일봉 적재 + 담보가치 판단용 요약(price_summary)
시총·발행주식수는 KRX 일별시세에 포함되므로 DART 주식총수 호출이 필요 없다."""
import math
from datetime import date, timedelta

from common import connect, log
from krx import daily_all, trading_days


def fetch_prices_range(con, start: date, end: date, market="KOSDAQ", only_codes=None):
    """start~end 거래일마다 전종목 1호출. 이미 적재된 날짜는 건너뜀. 반환: 호출 수"""
    have = {r[0] for r in con.execute("SELECT DISTINCT dt FROM price_daily")}
    n = 0
    for d in trading_days(start, end):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if iso in have:
            continue
        rows = daily_all(market, d)
        n += 1
        if not rows:
            continue  # 휴장일
        if only_codes:
            rows = [r for r in rows if r["stock_code"] in only_codes]
        con.executemany("INSERT OR REPLACE INTO price_daily VALUES (?,?,?,?,?,?,?)",
                        [(r["stock_code"], r["dt"], r["open"], r["high"], r["low"], r["close"],
                          int(r["volume"] or 0)) for r in rows])
        # 최신 발행주식수·시총은 price_summary 산출 시 사용하도록 임시 테이블에 보관
        con.executemany("INSERT OR REPLACE INTO _krx_latest VALUES (?,?,?,?)",
                        [(r["stock_code"], r["dt"], r["shares"], r["mcap"]) for r in rows])
        con.commit()
    return n


def build_summary(con, stock_code, shares_out=None, asof=None):
    rows = [dict(r) for r in con.execute(
        "SELECT dt, close, volume FROM price_daily WHERE stock_code=? ORDER BY dt", (stock_code,))]
    if not rows:
        return None
    asof = asof or rows[-1]["dt"]
    rows = [r for r in rows if r["dt"] <= asof]
    last = rows[-1]
    d0 = date.fromisoformat(asof)
    if shares_out is None:
        k = con.execute("SELECT shares FROM _krx_latest WHERE stock_code=? ORDER BY dt DESC LIMIT 1", (stock_code,)).fetchone()
        shares_out = k[0] if k else None

    def close_at(days):
        cut = (d0 - timedelta(days=days)).isoformat()
        prev = [r for r in rows if r["dt"] <= cut]
        return prev[-1]["close"] if prev else None

    def chg(days):
        c = close_at(days)
        return round((last["close"] / c - 1) * 100, 1) if c else None

    y1 = [r for r in rows if r["dt"] >= (d0 - timedelta(days=365)).isoformat()]
    m3 = [r for r in rows if r["dt"] >= (d0 - timedelta(days=91)).isoformat()]
    d60 = rows[-61:]
    rets = [math.log(d60[i]["close"] / d60[i - 1]["close"]) for i in range(1, len(d60))
            if d60[i - 1]["close"] and d60[i]["close"]]
    vol = None
    if len(rets) > 5:
        mu = sum(rets) / len(rets)
        sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        vol = round(sd * math.sqrt(252) * 100, 1)
    avg_vol = sum(r["volume"] for r in m3) / len(m3) if m3 else None
    avg_val = sum(r["volume"] * r["close"] for r in m3) / len(m3) if m3 else None
    mcap = last["close"] * shares_out if shares_out else None
    con.execute("INSERT OR REPLACE INTO price_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stock_code, asof, last["close"], shares_out, mcap,
                 max(r["close"] for r in y1) if y1 else None, min(r["close"] for r in y1) if y1 else None,
                 chg(30), chg(91), chg(365), avg_vol, avg_val, vol))
    con.commit()
    return asof


def run(pairs, bsns_year=None, start="2025-01-01", market="KOSDAQ"):
    """pairs: [(corp_code, stock_code)]. 시세는 전종목으로 받되 요약은 pairs만."""
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS _krx_latest (stock_code TEXT, dt TEXT, shares REAL, mcap REAL, PRIMARY KEY(stock_code, dt))")
    try:
        n = fetch_prices_range(con, date.fromisoformat(start), date.today(), market)
        log(con, "-", "price_fetch", "ok", f"{n} calls")
    except Exception as e:  # noqa
        log(con, "-", "price_fetch", "fail", e)
    for cc, sc in pairs:
        try:
            build_summary(con, sc)
            log(con, cc, "price", "ok")
        except Exception as e:  # noqa
            log(con, cc, "price", "fail", e)
    return con
