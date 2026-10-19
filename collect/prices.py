"""주가(KRX API): 날짜별 전종목 일봉 적재 + 담보가치 판단용 요약(price_summary)
시총·발행주식수는 KRX 일별시세에 포함되므로 DART 주식총수 호출이 필요 없다."""
import math
from datetime import date, timedelta

from common import connect, log
from krx import daily_all, trading_days


def _ensure_fetched_table(con):
    """시장별 적재 완료 일자. 기존(코스닥 단일 시장) 데이터는 KOSDAQ로 간주해 1회 시드."""
    con.execute("CREATE TABLE IF NOT EXISTS _price_fetched (market TEXT, dt TEXT, PRIMARY KEY(market, dt))")
    if not con.execute("SELECT 1 FROM _price_fetched LIMIT 1").fetchone():
        con.execute("INSERT OR IGNORE INTO _price_fetched SELECT 'KOSDAQ', dt FROM price_daily GROUP BY dt")
        con.commit()


def fetch_prices_range(con, start: date, end: date, market="KOSDAQ", only_codes=None):
    """start~end 거래일마다 해당 시장 전종목 1호출. 이미 적재된 (시장, 날짜)는 건너뜀. 반환: 호출 수"""
    _ensure_fetched_table(con)
    have = {r[0] for r in con.execute("SELECT dt FROM _price_fetched WHERE market=?", (market,))}
    n = 0
    for d in trading_days(start, end):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if iso in have:
            continue
        rows = daily_all(market, d)
        n += 1
        con.execute("INSERT OR IGNORE INTO _price_fetched VALUES (?,?)", (market, iso))
        if not rows:
            con.commit()
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


def split_factors(con, stock_code):
    """액면분할·병합 감지: 상장주식수가 하루 사이 2% 이상 바뀌고 주가가 그 역수만큼 움직인 날.
    반환 [(dt, ratio)] — dt 이전 가격은 ratio로 나누고 거래량은 곱한다(수정주가)."""
    try:
        sh = con.execute("SELECT dt, shares FROM _krx_latest WHERE stock_code=? AND shares>0 ORDER BY dt", (stock_code,)).fetchall()
    except Exception:  # noqa  _krx_latest 없음
        return []
    if len(sh) < 2:
        return []
    px = {r[0]: r[1] for r in con.execute("SELECT dt, close FROM price_daily WHERE stock_code=? AND close>0", (stock_code,))}
    out = []
    for i in range(1, len(sh)):
        d0, s0 = sh[i - 1]
        d1, s1 = sh[i]
        r = s1 / s0
        if abs(r - 1) < 0.02 or d0 not in px or d1 not in px:
            continue
        pr = px[d0] / px[d1]                     # 분할이면 주가도 r배 하락
        if abs(pr / r - 1) < 0.25:
            out.append((d1, r))
    return out


def adjusted_rows(con, stock_code, since=None):
    """수정주가 적용 일봉. since: 'YYYY-MM-DD' 이상만."""
    rows = [dict(r) for r in con.execute(
        "SELECT dt, open, high, low, close, volume FROM price_daily WHERE stock_code=? ORDER BY dt", (stock_code,))]
    for dt, r in split_factors(con, stock_code):
        for x in rows:
            if x["dt"] < dt:
                for k in ("open", "high", "low", "close"):
                    if x[k] is not None:
                        x[k] = round(x[k] / r, 2)
                x["volume"] = int((x["volume"] or 0) * r)
    return [x for x in rows if not since or x["dt"] >= since]


def build_summary(con, stock_code, shares_out=None, asof=None):
    rows = adjusted_rows(con, stock_code)
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
    con.execute("""INSERT OR REPLACE INTO price_summary (stock_code, asof, close, shares_out, mcap, high_52w, low_52w,
                chg_1m, chg_3m, chg_1y, avg_vol_3m, avg_value_3m, vol_60d, chg_1w, chg_6m, chg_2y, chg_3y)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (stock_code, asof, last["close"], shares_out, mcap,
                 max(r["close"] for r in y1) if y1 else None, min(r["close"] for r in y1) if y1 else None,
                 chg(30), chg(91), chg(365), avg_vol, avg_val, vol, chg(7), chg(182), chg(730), chg(1095)))
    con.commit()
    return asof


def run(pairs, bsns_year=None, start=None, markets=None):
    """pairs: [(corp_code, stock_code)]. 시세는 시장별 전종목으로 받되 요약은 pairs만.
    markets: None이면 company 테이블에 있는 시장 전부."""
    con = connect()
    start = start or (date.today() - timedelta(days=3 * 365 + 10)).isoformat()   # 3년치(3년 수익률용)
    con.execute("CREATE TABLE IF NOT EXISTS _krx_latest (stock_code TEXT, dt TEXT, shares REAL, mcap REAL, PRIMARY KEY(stock_code, dt))")
    markets = markets or [r[0] for r in con.execute("SELECT DISTINCT market FROM company WHERE market IS NOT NULL")] or ["KOSDAQ"]
    for market in markets:
        try:
            n = fetch_prices_range(con, date.fromisoformat(start), date.today(), market)
            log(con, "-", "price_fetch", "ok", f"{market} {n} calls")
            print(f"[price] {market} {n} calls", flush=True)
        except Exception as e:  # noqa
            log(con, "-", "price_fetch", "fail", f"{market}: {e}")
            print(f"[price] {market} 실패: {e}", flush=True)
    for cc, sc in pairs:
        try:
            build_summary(con, sc)
            log(con, cc, "price", "ok")
        except Exception as e:  # noqa
            log(con, cc, "price", "fail", e)
    return con
