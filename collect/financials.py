"""DART 재무: 전체 재무제표(long) 적재 + 심사용 KPI 산출"""
from common import connect, dart_get, log, to_num

REPRT = {"11011": "사업", "11012": "반기", "11013": "1Q", "11014": "3Q"}

# KPI 매핑: (우선 account_id 리스트, 보조 account_nm 키워드)
KPI_MAP = {
    "revenue":      (["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익"]),
    "op_income":    (["dart_OperatingIncomeLoss"], ["영업이익"]),
    "net_income":   (["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "total_assets": (["ifrs-full_Assets"], ["자산총계"]),
    "total_liab":   (["ifrs-full_Liabilities"], ["부채총계"]),
    "equity":       (["ifrs-full_Equity"], ["자본총계"]),
    "cash":         (["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "cfo":          (["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름", "영업활동으로 인한 현금흐름"]),
    "dep":          (["ifrs-full_DepreciationExpense", "ifrs-full_AdjustmentsForDepreciationExpense"], ["감가상각비", "유형자산감가상각비", "감가상각"]),
    "amort":        (["ifrs-full_AmortisationExpense", "ifrs-full_AdjustmentsForAmortisationExpense"], ["무형자산상각비", "무형자산상각"]),
    "rou_dep":      ([], ["사용권자산상각비", "사용권자산감가상각비"]),
    "int_exp":      (["ifrs-full_InterestExpense", "ifrs-full_FinanceCosts"], ["이자비용", "금융비용", "금융원가"]),
    "cur_assets":   (["ifrs-full_CurrentAssets"], ["유동자산"]),
    "cur_liab":     (["ifrs-full_CurrentLiabilities"], ["유동부채"]),
    "receivables":  (["ifrs-full_TradeAndOtherCurrentReceivables", "ifrs-full_CurrentTradeReceivables"], ["매출채권", "매출채권및기타채권", "매출채권 및 기타채권", "매출채권및기타유동채권"]),
    "inventory":    (["ifrs-full_Inventories"], ["재고자산"]),
    "capital":      (["ifrs-full_IssuedCapital"], ["자본금"]),
}
BORROW_KEYS = ["단기차입금", "장기차입금", "유동성장기부채", "유동성장기차입금", "사채", "전환사채", "신주인수권부사채",
               "리스부채", "유동리스부채", "비유동리스부채", "장기사채"]


def fetch_fin_raw(con, corp_code, bsns_year, reprt_code="11011"):
    n = 0
    for fs_div in ("CFS", "OFS"):
        js = dart_get("fnlttSinglAcntAll", corp_code=corp_code, bsns_year=bsns_year,
                      reprt_code=reprt_code, fs_div=fs_div)
        for r in js.get("list", []):
            con.execute("""INSERT OR REPLACE INTO fin_raw (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_nm,
                thstrm_amount, frmtrm_amount, bfefrmtrm_amount, ord, rcept_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (corp_code, bsns_year, reprt_code, fs_div, r.get("sj_div"),
                         r.get("account_id"), r.get("account_nm"),
                         to_num(r.get("thstrm_amount")), to_num(r.get("frmtrm_amount")),
                         to_num(r.get("bfefrmtrm_amount")), int(r.get("ord") or 0), r.get("rcept_no")))
            n += 1
    con.commit()
    return n


_DA_EXCL = ("채권", "할인", "현재가치", "충당", "환입", "손상", "처분", "누계")


def _da_from_rows(rows, k):
    """CF(간접법 조정) → 없으면 IS에서 상각비 탐색. 결과를 k['dep'], k['amort'], k['rou_dep']에 반영"""
    for sj in (("CF",), ("IS", "CIS")):
        cand = [(r["account_nm"].replace(" ", ""), r["thstrm_amount"], r["account_id"] or "")
                for r in rows if r["sj_div"] in sj and r["account_nm"] and "상각" in r["account_nm"] and r["thstrm_amount"]]
        cand = [c for c in cand if not any(x in c[0] for x in _DA_EXCL)]
        if not cand:
            continue
        comb = [c for c in cand if ("감가" in c[0] and "무형" in c[0]) or "DepreciationAndAmortisation" in c[2]
                or c[0] in ("상각비", "감가상각비등", "유무형자산상각비", "유·무형자산상각비")]
        if comb:
            k["dep"], k["amort"], k["rou_dep"] = abs(comb[0][1]), None, None
            return
        dep = [c for c in cand if "감가" in c[0] and "사용권" not in c[0] and "투자부동산" not in c[0]]
        amo = [c for c in cand if "무형" in c[0]]
        rou = [c for c in cand if "사용권" in c[0]]
        inv = [c for c in cand if "투자부동산" in c[0]]
        if dep or amo or rou:
            k["dep"] = (abs(dep[0][1]) if dep else 0) + (abs(inv[0][1]) if inv else 0) or None
            k["amort"] = abs(amo[0][1]) if amo else None
            k["rou_dep"] = abs(rou[0][1]) if rou else None
            return


def _pick(rows, ids, names, sj=None):
    """rows: fin_raw dict 목록. account_id 우선, 없으면 계정명 정확/포함 매칭."""
    for r in rows:
        if r["account_id"] in ids and (sj is None or r["sj_div"] in sj):
            return r["thstrm_amount"]
    for nm in names:
        for r in rows:
            an = (r["account_nm"] or "").replace(" ", "")
            if an == nm.replace(" ", "") and (sj is None or r["sj_div"] in sj):
                return r["thstrm_amount"]
    return None


def build_kpi(con, corp_code, bsns_year, reprt_code="11011"):
    for fs_div in ("CFS", "OFS"):
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?",
            (corp_code, bsns_year, reprt_code, fs_div))]
        if not rows:
            continue
        k = {}
        for key, (ids, names) in KPI_MAP.items():
            sj = ("BS",) if key in ("total_assets", "total_liab", "equity", "cash", "cur_assets", "cur_liab", "receivables", "inventory", "capital") else \
                 ("CF",) if key in ("cfo", "dep", "amort", "rou_dep") else ("IS", "CIS")
            k[key] = _pick(rows, ids, names, sj)
        # 상각비: 계정명 패턴으로 재탐색 (합산 계정 '감가상각비와무형자산상각비' 우선, 없으면 감가+무형+사용권 합)
        _da_from_rows(rows, k)
        # 차입금 근사: BS 계정명 키워드 합(중복 방지: 정확 일치만)
        bor = 0.0
        hit = False
        for r in rows:
            an = (r["account_nm"] or "").replace(" ", "")
            if r["sj_div"] == "BS" and an in [x.replace(" ", "") for x in BORROW_KEYS] and r["thstrm_amount"]:
                bor += r["thstrm_amount"]
                hit = True
        k["borrowings"] = bor if hit else None
        eq, rev = k.get("equity"), k.get("revenue")
        k["debt_ratio"] = round(k["total_liab"] / eq * 100, 1) if k.get("total_liab") and eq else None
        k["op_margin"] = round(k["op_income"] / rev * 100, 1) if k.get("op_income") is not None and rev else None
        k["net_borrow_ratio"] = round((k["borrowings"] - (k.get("cash") or 0)) / eq * 100, 1) \
            if k.get("borrowings") is not None and eq else None
        k["net_borrowings"] = (k["borrowings"] - (k.get("cash") or 0)) if k.get("borrowings") is not None else None
        da = [abs(x) for x in (k.get("dep"), k.get("amort"), k.get("rou_dep")) if x is not None]
        k["da"] = sum(da) if da else None
        k["ebitda"] = (k["op_income"] + k["da"]) if k.get("op_income") is not None and k["da"] is not None else None
        ie = abs(k["int_exp"]) if k.get("int_exp") else None
        k["interest_cover"] = round(k["ebitda"] / ie, 1) if k.get("ebitda") is not None and ie else None   # EBITDA/이자비용
        k["borrow_dep"] = round(k["borrowings"] / k["total_assets"] * 100, 1) \
            if k.get("borrowings") is not None and k.get("total_assets") else None
        k["cur_ratio"] = round(k["cur_assets"] / k["cur_liab"] * 100, 1) if k.get("cur_assets") and k.get("cur_liab") else None
        k["recv_turn"] = round(rev / k["receivables"], 1) if rev and k.get("receivables") else None       # 연환산은 사이트에서 분기 보정
        k["inv_turn"] = round(rev / k["inventory"], 1) if rev and k.get("inventory") else None
        k["nd_ebitda"] = round(k["net_borrowings"] / k["ebitda"], 1) if k.get("net_borrowings") is not None and k.get("ebitda") and k["ebitda"] > 0 else None
        rcept = next((r["rcept_no"] for r in rows if r.get("rcept_no")), None)
        if rcept is None:   # 기존 행에 저장된 접수번호 유지
            old = con.execute("SELECT rcept_no FROM fin_kpi WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?",
                              (corp_code, bsns_year, reprt_code, fs_div)).fetchone()
            rcept = old["rcept_no"] if old else None
        con.execute("""INSERT OR REPLACE INTO fin_kpi (corp_code, bsns_year, reprt_code, fs_div, revenue, op_income, net_income,
            total_assets, total_liab, equity, cash, borrowings, cfo, debt_ratio, op_margin, net_borrow_ratio,
            net_borrowings, da, ebitda, int_exp, interest_cover, borrow_dep, rcept_no,
            cur_assets, cur_liab, receivables, inventory, capital, cur_ratio, recv_turn, inv_turn, nd_ebitda)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, bsns_year, reprt_code, fs_div, k.get("revenue"), k.get("op_income"),
                     k.get("net_income"), k.get("total_assets"), k.get("total_liab"), k.get("equity"),
                     k.get("cash"), k.get("borrowings"), k.get("cfo"), k.get("debt_ratio"),
                     k.get("op_margin"), k.get("net_borrow_ratio"), k.get("net_borrowings"), k.get("da"),
                     k.get("ebitda"), ie, k.get("interest_cover"), k.get("borrow_dep"), rcept,
                     k.get("cur_assets"), k.get("cur_liab"), k.get("receivables"), k.get("inventory"), k.get("capital"),
                     k.get("cur_ratio"), k.get("recv_turn"), k.get("inv_turn"), k.get("nd_ebitda")))
    con.commit()


from datetime import date


def latest_interim_plan(today: date | None = None):
    """오늘 기준으로 '최근 분기' 후보 (bsns_year, reprt_code) 를 최신순으로. 제출마감 기준:
    1Q 5/15, 반기 8/14, 3Q 11/14, 사업보고서 3/31(전년도)."""
    d = today or date.today()
    y = d.year
    cands = []
    if d >= date(y, 11, 15):
        cands += [(y, "11014"), (y, "11012"), (y, "11013")]
    elif d >= date(y, 8, 15):
        cands += [(y, "11012"), (y, "11013")]
    elif d >= date(y, 5, 16):
        cands += [(y, "11013")]
    cands += [(y - 1, "11014"), (y - 1, "11012"), (y - 1, "11013")]   # 전년도 분기(연도 초반 대비)
    return cands


def fetch_latest_interim(con, cc, plan):
    """plan 순서로 시도해 처음 데이터가 있는 분기보고서 + 전년 동기 적재. 반환 (year, reprt_code) or None"""
    for y, rc in plan:
        n = fetch_fin_raw(con, cc, str(y), rc)
        if n:
            build_kpi(con, cc, str(y), rc)
            try:
                n2 = fetch_fin_raw(con, cc, str(y - 1), rc)   # 전년 동기
                if n2:
                    build_kpi(con, cc, str(y - 1), rc)
            except Exception as e:  # noqa
                log(con, cc, f"fin_{y-1}_{rc}", "fail", e)
            return y, rc
    return None


import re as _re

_RN = _re.compile(r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")


def backfill_rcept(con, corp_codes=None):
    """fin_kpi.rcept_no가 비어 있는 회사에 대해 DART 공시목록(정기보고서) 1회 호출로 접수번호 채움.
    보고서명 '(YYYY.MM)'과 결산월로 (bsns_year, reprt_code)를 역산."""
    from common import dart_get, log
    q = """SELECT DISTINCT k.corp_code, c.acc_mt FROM fin_kpi k JOIN company c USING(corp_code) WHERE k.rcept_no IS NULL"""
    targets = [(r["corp_code"], r["acc_mt"]) for r in con.execute(q)]
    if corp_codes:
        targets = [t for t in targets if t[0] in corp_codes]
    n = 0
    for cc, acc_mt in targets:
        acc = int(acc_mt) if str(acc_mt or "").strip().isdigit() else 12
        try:
            js = dart_get("list", corp_code=cc, bgn_de="20220101", end_de="20991231", pblntf_ty="A", page_count=100)
        except Exception as e:  # noqa
            log(con, cc, "rcept", "fail", e)
            continue
        found = {}
        for r in sorted(js.get("list", []), key=lambda r: r["rcept_dt"]):   # 정정본이 뒤에 오면 최신으로 덮음
            m = _RN.search(r["report_nm"])
            if not m:
                continue
            kind, yy, mm = m.group(1), int(m.group(2)), int(m.group(3))
            if kind == "사업보고서":
                by, rc = (yy if acc == 12 else yy - 1), "11011"
            else:
                by = yy if (acc == 12 or mm > acc) else yy - 1
                qn = ((mm - acc - 1) % 12) // 3 + 1          # 결산월 기준 몇 번째 분기말인지
                rc = {1: "11013", 2: "11012", 3: "11014"}.get(qn)
                if not rc or (kind == "반기보고서") != (rc == "11012"):
                    continue
            found[(str(by), rc)] = r["rcept_no"]
        for (by, rc), rn in found.items():
            cur = con.execute("UPDATE fin_kpi SET rcept_no=? WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND rcept_no IS NULL", (rn, cc, by, rc))
            n += cur.rowcount
        con.commit()
    return n


def rebuild_kpi_all(con):
    """fin_raw에서 fin_kpi 전부 재계산 (DART 호출 없음). KPI 정의 변경 시 사용"""
    keys = con.execute("SELECT DISTINCT corp_code, bsns_year, reprt_code FROM fin_raw").fetchall()
    for r in keys:
        build_kpi(con, r["corp_code"], r["bsns_year"], r["reprt_code"])
    return len(keys)


def run(corp_codes, years, reprt_code="11011", interim=True, annual_years=None):
    """years: 연간(사업보고서) 적재 연도. interim=True면 최근 분기 + 전년 동기 추가.
    오늘 기준 기본: 직전 2개년 연간 + 최근분기·전년동기 = 회사당 (2+2)×2(연결/별도) = 8호출"""
    con = connect()
    if annual_years is None:
        annual_years = years
    plan = latest_interim_plan()
    for cc in corp_codes:
        for y in annual_years:
            try:
                n = fetch_fin_raw(con, cc, str(y), "11011")
                if n:
                    build_kpi(con, cc, str(y), "11011")
                log(con, cc, f"fin_{y}", "ok" if n else "empty", f"{n} rows")
            except Exception as e:  # noqa
                log(con, cc, f"fin_{y}", "fail", e)
        if interim:
            try:
                r = fetch_latest_interim(con, cc, plan)
                log(con, cc, "fin_interim", "ok" if r else "empty", str(r))
            except Exception as e:  # noqa
                log(con, cc, "fin_interim", "fail", e)
    try:
        backfill_rcept(con, set(corp_codes))
    except Exception as e:  # noqa
        log(con, "-", "rcept", "fail", e)
    return con
