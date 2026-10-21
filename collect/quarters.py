"""분기 손익 수집(LTM·8분기 추이용): DART 주요계정 API(fnlttSinglAcnt)로 최근 8분기를 복원하는 데 필요한 보고서를 채운다.
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


def needed_reports(today=None, n_q=8):
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


def fetch_report(con, cc, year, rc):
    """1콜 → fin_kpi 행(CFS/OFS) 삽입(기존 행 보존). 반환 저장 fs_div 수"""
    js = dart_get("fnlttSinglAcnt", corp_code=cc, bsns_year=year, reprt_code=rc)
    acc = {}
    for r in js.get("list", []):
        fs = r.get("fs_div")
        key = NM.get((r.get("account_nm") or "").replace(" ", ""))
        if not fs or not key:
            continue
        v = to_num(r.get("thstrm_add_amount")) if r.get("sj_div") == "IS" and r.get("thstrm_add_amount") not in (None, "", "-") else to_num(r.get("thstrm_amount"))
        acc.setdefault(fs, {}).setdefault(key, v)        # 같은 계정 복수행이면 첫 행
    n = 0
    for fs, k in acc.items():
        if k.get("revenue") is None and k.get("op_income") is None:
            continue
        con.execute("""INSERT OR IGNORE INTO fin_kpi (corp_code, bsns_year, reprt_code, fs_div, revenue, op_income, net_income, total_assets, total_liab, equity,
                       debt_ratio, op_margin, rcept_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cc, year, rc, fs, k.get("revenue"), k.get("op_income"), k.get("net_income"), k.get("total_assets"), k.get("total_liab"), k.get("equity"),
                     (k["total_liab"] / k["equity"] * 100) if k.get("total_liab") is not None and k.get("equity") else None,
                     (k["op_income"] / k["revenue"] * 100) if k.get("op_income") is not None and k.get("revenue") else None,
                     next((r.get("rcept_no") for r in js.get("list", []) if r.get("rcept_no")), None)))
        n += 1
    return n


def plan_for(con, cc, reports):
    """이 회사에 아직 없는 (year, rc) 목록 — fin_kpi에 매출 있는 행이 있거나 quarters_done에 기록되면 제외"""
    have = {(r[0], r[1]) for r in con.execute("SELECT bsns_year, reprt_code FROM fin_kpi WHERE corp_code=? AND revenue IS NOT NULL", (cc,))}
    done = {(r[0], r[1]) for r in con.execute("SELECT bsns_year, reprt_code FROM quarters_done WHERE corp_code=?", (cc,))}
    return [(y, rc) for (y, rc) in reports if (y, rc) not in have and (y, rc) not in done]


def priority(con, corp_codes):
    rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
    mk = {r[0]: r[1] for r in con.execute("SELECT corp_code, market FROM company")}
    rank = {"KOSPI": 1, "KOSDAQ": 2, "KONEX": 3}
    return sorted(corp_codes, key=lambda c: (0 if c in rated else 1, rank.get(mk.get(c), 9), c))


def run(corp_codes, limit=None, con=None):
    con = con or connect()
    _ensure(con)
    reports = needed_reports()
    cs = priority(con, corp_codes)
    todo = [(c, plan_for(con, c, reports)) for c in cs]
    todo = [(c, p) for c, p in todo if p]
    if limit:
        todo = todo[:limit]
    tot = sum(len(p) for _, p in todo)
    print(f"[quarters] 대상 보고서 {reports[0]}~{reports[-1]} · 미수집 회사 {len(todo)} · 콜 예정 {tot}", flush=True)
    calls, saved, i = 0, 0, 0
    for i, (cc, plan) in enumerate(todo, 1):
        for y, rc in plan:
            try:
                n = fetch_report(con, cc, y, rc)
                calls += 1; saved += n
                con.execute("INSERT OR REPLACE INTO quarters_done VALUES (?,?,?,?,?)", (cc, y, rc, "ok" if n else "empty", now()))
            except Exception as e:  # noqa
                log(con, cc, f"quarters_{y}_{rc}", "fail", e)
                if common.QUOTA_HIT or "한도" in str(e):
                    con.commit()
                    print(f"[quarters] DART 일일 한도 도달 — {i}/{len(todo)}사, {calls}콜에서 중단(다음 catchup에서 이어서)", flush=True)
                    return calls
        con.commit()
        if i % 100 == 0:
            print(f"[quarters] {i}/{len(todo)}사 · {calls}콜 · 저장 {saved}행", flush=True)
    con.commit()
    print(f"[quarters] 완료 {i}/{len(todo)}사 · {calls}콜 · 저장 {saved}행", flush=True)
    return calls


if __name__ == "__main__":
    c = connect()
    run([r[0] for r in c.execute("SELECT corp_code FROM company")], limit=int(sys.argv[1]) if len(sys.argv) > 1 else None, con=c)
