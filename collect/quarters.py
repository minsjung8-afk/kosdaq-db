"""분기 손익 수집(LTM·12분기 추이용): DART 전체 재무제표 API(fnlttSinglAcntAll, 주 재무제표 1콜)로 최근 12분기를 복원하는 데 필요한 보고서를 채운다.
- 보고서 1개 = 1콜(연결·별도 행이 같이 옴). 매출액·영업이익·당기순이익(누적)과 자산·부채·자본만 → fin_kpi에 '빈 열 많은' 행으로 적재.
  이미 fin(전체계정)으로 채워진 (연도,보고서,fs_div) 행은 건드리지 않음.
- 분기별 값은 build_site._quarters가 누적 차감(2Q=반기−1Q, 4Q=연간−3Q)으로 복원하므로 누적금액(thstrm_add_amount)을 저장.
- 우선순위: 신용등급 보유 → 코스피 → 코스닥 → 코넥스. DART 한도 도달 시 중단(다음 catchup에서 이어서).
- quarters_done(corp_code, bsns_year, reprt_code, status) 로 시도 이력 관리(empty 포함) → 매일 같은 빈 보고서를 다시 찌르지 않음.
  python run.py quarters [--limit N]
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import connect, dart_get, log, now, to_num  # noqa: E402

ORDER = ["11013", "11012", "11014", "11011"]      # 1Q, 반기, 3Q, 연간
NM = {"매출액": "revenue", "수익(매출액)": "revenue", "영업수익": "revenue", "영업이익": "op_income", "영업이익(손실)": "op_income",
      "당기순이익": "net_income", "당기순이익(손실)": "net_income", "분기순이익": "net_income", "반기순이익": "net_income",
      "자산총계": "total_assets", "부채총계": "total_liab", "자본총계": "equity"}


def needed_reports(today=None, n_q=12):
    """오늘 기준 제출된 최신 분기부터 거꾸로 n_q+1개 분기(차감 복원에 직전 분기 필요) → [(year, reprt_code)] 최신순"""
    d = today or date.today()
    y = d.year
    if d >= date(y, 11, 15):
        last = (y, 2)          # 3Q (index in ORDER)
    elif d >= date(y, 8, 15):
        last = (y, 1)
    elif d >= date(y, 5, 16):
        last = (y, 0)
    elif d >= date(y, 4, 1):
        last = (y - 1, 3)
    else:
        last = (y - 1, 2)
    out, (yy, qi) = [], last
    for _ in range(n_q + 1):
        out.append((str(yy), ORDER[qi]))
        qi -= 1
        if qi < 0:
            qi, yy = 3, yy - 1
    return out


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS quarters_done (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, status TEXT, fetched_at TEXT,
                   PRIMARY KEY (corp_code, bsns_year, reprt_code))""")
    # v2(2026-10-23): 연결·별도 각각 관리 → 키에 fs 포함. 옛 표(status ok/empty, fs 없음)는 버리고 fin_raw 보유 여부로 다시 판단
    cols = [r[1] for r in con.execute("PRAGMA table_info(quarters_done)")]
    if "fs_div" not in cols:
        con.execute("DROP TABLE quarters_done")
        con.execute("""CREATE TABLE quarters_done (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, status TEXT, fetched_at TEXT,
                       PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))""")
        con.commit()


def _fs_of(con, cc):
    """회사의 주 재무제표 구분(연결 있으면 CFS) — 연간 데이터로 판단, 모르면 둘 다"""
    # 연결 우선, 없으면 별도. 연결 작성을 중단한 회사(두산테스나 2025~ 등)는 최신 연간이 별도뿐이므로 둘 다 시도(빈 응답은 1콜 추가)
    r = con.execute("""SELECT fs_div FROM fin_kpi WHERE corp_code=? AND reprt_code='11011' AND revenue IS NOT NULL
                       ORDER BY bsns_year DESC, fs_div='CFS' DESC LIMIT 1""", (cc,)).fetchone()
    if not r:
        return ("CFS", "OFS")
    return ("CFS", "OFS") if r[0] == "CFS" else ("OFS", "CFS")


def fetch_report(con, cc, year, rc):
    """단건(순차) 호출용 — 주 재무제표 1콜 → fin_raw(손익 누적, 전기 열로 전년 같은 기간 보충) → build_kpi"""
    from financials import _fetch_list, store_raw, build_kpi
    n = 0
    for fs in _fs_of(con, cc):
        lst = _fetch_list(cc, year, rc, fs)
        if lst:
            n, filled = store_raw(con, cc, year, rc, fs, lst, prev=True)
            build_kpi(con, cc, year, rc)
            if filled:
                build_kpi(con, cc, str(int(year) - 1), rc)
            break
    return n


def _worker(cc, plan, fs):
    """스레드(HTTP만): 한 fs(연결 또는 별도) 기준으로 최신 보고서부터 받되, 받은 보고서의 '전기'로 채워지는 전년 같은 기간은 건너뜀.
    반환 [(year, rc, fs, list)] — list 빈 것은 empty 표시용"""
    from financials import _fetch_list
    out, filled = [], set()
    for y, rc in plan:
        if (y, rc) in filled:
            continue
        lst = _fetch_list(cc, y, rc, fs)
        out.append((y, rc, fs, lst))
        if lst:
            filled.add((str(int(y) - 1), rc))
    return out


def plan_for(con, cc, reports, fs="CFS"):
    """fs 기준으로 아직 받지 않은 (year, rc). 완료 = fin_raw에 그 fs 행이 있음(연간·분기 모두). 'empty'(해당 fs 보고서 없음) 표시는 fs별로 관리"""
    from financials import ensure_cum_table
    ensure_cum_table(con)
    have = {(r[0], r[1]) for r in con.execute("SELECT DISTINCT bsns_year, reprt_code FROM fin_raw WHERE corp_code=? AND fs_div=?", (cc, fs))}
    empty = {(r[0], r[1]) for r in con.execute("SELECT bsns_year, reprt_code FROM quarters_done WHERE corp_code=? AND fs_div=? AND status='empty'", (cc, fs))}
    return [(y, rc) for (y, rc) in reports if (y, rc) not in have and (y, rc) not in empty]


def priority(con, corp_codes):
    rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
    mk = {r[0]: r[1] for r in con.execute("SELECT corp_code, market FROM company")}
    rank = {"KOSPI": 1, "KOSDAQ": 2, "KONEX": 3}
    return sorted(corp_codes, key=lambda c: (0 if c in rated else 1, rank.get(mk.get(c), 9), c))


def run(corp_codes, limit=None, con=None, workers=4):
    """limit: '미수집 회사' 중 앞에서 N사(우선순위 순)"""
    import common
    from concurrent.futures import ThreadPoolExecutor
    from financials import store_raw, build_kpi
    con = con or connect()
    _ensure(con)
    reports = needed_reports()
    cs = priority(con, corp_codes)
    # 연결·별도 모두 수집(전사): 연결 먼저 전부, 그다음 별도(별도는 '연결 없음'인 회사도 포함)
    has_cfs = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fin_raw WHERE fs_div='CFS'")}   # 연결 자체가 없는 회사(연간 수집 때 확인)는 연결 조회 생략
    todo = [(c, "CFS", plan_for(con, c, reports, "CFS")) for c in cs if c in has_cfs] + [(c, "OFS", plan_for(con, c, reports, "OFS")) for c in cs]
    todo = [(c, fs, p) for c, fs, p in todo if p]
    if limit:
        todo = todo[:limit]
    print(f"[quarters] 대상 보고서 {reports[0]}~{reports[-1]} · 미수집 {len(todo)}건(회사×연결/별도) · 병렬 {workers} (전기 열 재사용으로 건당 ~7콜)", flush=True)
    saved, i, c0 = 0, 0, common.CALLS
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(todo), workers * 3):
            batch = todo[b:b + workers * 3]
            futs = [((cc, fs), ex.submit(_worker, cc, plan, fs)) for cc, fs, plan in batch]
            stop = False
            for (cc, fs), f in futs:
                i += 1
                try:
                    for y, rc, fs_, lst in f.result():
                        if lst:
                            n, filled = store_raw(con, cc, y, rc, fs_, lst, prev=True)
                            build_kpi(con, cc, y, rc)
                            if filled:
                                build_kpi(con, cc, str(int(y) - 1), rc)
                            saved += n
                        con.execute("INSERT OR REPLACE INTO quarters_done VALUES (?,?,?,?,?,?)", (cc, y, rc, fs_, "ok" if lst else "empty", now()))
                except Exception as e:  # noqa
                    log(con, cc, "quarters", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 25 < workers * 3:
                print(f"[quarters] {i}/{len(todo)}사 · {common.CALLS - c0}콜 · 저장 {saved}행", flush=True)
            if stop:
                print(f"[quarters] DART 일일 한도 도달 — {i}/{len(todo)}사에서 중단(다음 catchup에서 이어서)", flush=True)
                return common.CALLS - c0
    print(f"[quarters] 완료 {i}/{len(todo)}사 · {common.CALLS - c0}콜 · 저장 {saved}행", flush=True)
    return common.CALLS - c0


if __name__ == "__main__":
    c = connect()
    run([r[0] for r in c.execute("SELECT corp_code FROM company")], limit=int(sys.argv[1]) if len(sys.argv) > 1 else None, con=c)
