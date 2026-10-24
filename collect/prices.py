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


def fetch_prices_range(con, start: date, end: date, market="KOSDAQ", only_codes=None, newest_first=False, max_calls=None):
    """start~end 거래일마다 해당 시장 전종목 1호출. 이미 적재된 (시장, 날짜)는 건너뜀. 반환: 호출 수
    newest_first: 최근 날짜부터 거슬러 받음(과거 백필 시 3년→5년→10년 순으로 차트가 먼저 길어짐). max_calls: 호출 상한(KRX 일일 한도 대비)"""
    _ensure_fetched_table(con)
    # 복구: 최근 10일 중 '빈 응답'으로 적재완료 표시됐지만 실제 시세가 없는 날짜는 다시 받는다
    #  (07:00 daily가 당일/전일을 KRX 집계 전에 조회 → 빈 응답을 휴장일로 오인해 영구 건너뛰던 문제)
    con.execute("""DELETE FROM _price_fetched WHERE market=? AND dt >= date('now','-10 days')
                   AND dt NOT IN (SELECT DISTINCT dt FROM price_daily WHERE dt >= date('now','-10 days'))""", (market,))
    # 2026-10-22 수정: 예전 백필에서 KRX가 빈 응답(한도·장애)을 준 날짜가 '휴장일'로 굳어져 2021.10~2023.10 등 구간이 영구 누락됨.
    #  → '적재 완료'는 실제 시세 행이 있는 날짜만 인정. 빈 날짜는 _price_empty에 시도 횟수를 두고 2회까지 재시도 후 휴장일로 확정.
    con.execute("CREATE TABLE IF NOT EXISTS _price_empty (market TEXT, dt TEXT, tries INTEGER, PRIMARY KEY(market, dt))")
    # 2026-10-24 수정: '그 시장 소속 회사의 시세가 1행이라도 있는 날짜'를 적재 완료로 보면 이전상장(코넥스→코스닥, 코스닥→코스피) 회사가
    #  옛 시장에서 받은 날짜까지 완료로 오인해 구간이 통째로 빠짐(코스피 2011~2013.10, 코스닥 2013.10~2015 누락의 원인).
    #  → 적재 완료 = _price_fetched 기록(빈 응답은 기록 안 함) 또는 그 날짜 시세 행이 시장 회사 수의 절반 이상인 경우.
    ncomp = con.execute("SELECT COUNT(*) FROM company WHERE market=?", (market,)).fetchone()[0] or 1
    have = {r[0] for r in con.execute("SELECT dt FROM _price_fetched WHERE market=?", (market,))}
    have |= {r[0] for r in con.execute("""SELECT p.dt FROM price_daily p JOIN company c ON c.stock_code=p.stock_code
                                           WHERE c.market=? GROUP BY p.dt HAVING COUNT(*) >= ?""", (market, max(50, ncomp // 2)))}
    empty = {r[0]: r[1] for r in con.execute("SELECT dt, tries FROM _price_empty WHERE market=?", (market,))}
    today = date.today().isoformat()
    n = 0
    days = list(trading_days(start, end))
    if newest_first:
        days.reverse()
    for d in days:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if iso in have or iso > today or empty.get(iso, 0) >= 2:
            continue
        if max_calls and n >= max_calls:
            print(f"[price] {market} 호출 상한 {max_calls} 도달 — 다음 실행에서 이어서", flush=True); break
        rows = daily_all(market, d)
        n += 1
        if not rows:
            if iso < (date.today() - timedelta(days=7)).isoformat():     # 일주일 넘게 지난 빈 날짜: 시도 횟수 +1(2회면 휴장일 확정)
                con.execute("INSERT INTO _price_empty VALUES (?,?,1) ON CONFLICT(market, dt) DO UPDATE SET tries=tries+1", (market, iso))
            con.commit()
            continue  # 휴장일 또는 아직 미집계(최근 7일은 다음 실행에서 재시도)
        con.execute("INSERT OR IGNORE INTO _price_fetched VALUES (?,?)", (market, iso))
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
    for x in rows:                                       # 거래정지일: KRX가 시·고·저=0, 거래량 0으로 줌 → 종가로 평탄화(0까지 내려가는 긴 캔들 방지)
        if x["close"] and not (x["open"] and x["high"] and x["low"]):
            x["open"] = x["high"] = x["low"] = x["close"]; x["volume"] = 0
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
    try:   # 새 asof 행에도 최신 대용가를 이어 붙임(INSERT OR REPLACE가 대용가 열을 비우는 문제 방지)
        con.execute("""UPDATE price_summary SET subst_price=(SELECT sb_prc FROM subst_price s WHERE s.stock_code=? ORDER BY trd_dd DESC LIMIT 1),
                       subst_dd=(SELECT trd_dd FROM subst_price s WHERE s.stock_code=? ORDER BY trd_dd DESC LIMIT 1)
                       WHERE stock_code=? AND asof=?""", (stock_code, stock_code, stock_code, asof))
    except Exception:  # noqa
        pass
    con.commit()
    return asof


def backfill(pairs, start="2016-01-01", max_calls=600, markets=None):
    """과거 시세 백필(야간): 최근 날짜부터 거슬러 시장별 max_calls까지. KRX 오류(한도)면 그 시장은 중단하고 다음 시장."""
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS _krx_latest (stock_code TEXT, dt TEXT, shares REAL, mcap REAL, PRIMARY KEY(stock_code, dt))")
    markets = markets or [r[0] for r in con.execute("SELECT DISTINCT market FROM company WHERE market IS NOT NULL")] or ["KOSDAQ"]
    total = 0
    for market in markets:
        try:
            n = fetch_prices_range(con, date.fromisoformat(start), date.today(), market, newest_first=True, max_calls=max_calls)
            total += n
            print(f"[price-backfill] {market} {n} calls", flush=True)
        except Exception as e:  # noqa
            log(con, "-", "price_backfill", "fail", f"{market}: {e}")
            print(f"[price-backfill] {market} 중단: {str(e)[:120]}", flush=True)
    if total:
        for cc, sc in pairs:
            try:
                build_summary(con, sc)
            except Exception as e:  # noqa
                log(con, cc, "price", "fail", e)
    return total


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
