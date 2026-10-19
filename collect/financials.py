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
}
BORROW_KEYS = ["단기차입금", "장기차입금", "유동성장기부채", "유동성장기차입금", "사채", "전환사채", "신주인수권부사채",
               "리스부채", "유동리스부채", "비유동리스부채", "장기사채"]


def fetch_fin_raw(con, corp_code, bsns_year, reprt_code="11011"):
    n = 0
    for fs_div in ("CFS", "OFS"):
        js = dart_get("fnlttSinglAcntAll", corp_code=corp_code, bsns_year=bsns_year,
                      reprt_code=reprt_code, fs_div=fs_div)
        for r in js.get("list", []):
            con.execute("""INSERT OR REPLACE INTO fin_raw VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (corp_code, bsns_year, reprt_code, fs_div, r.get("sj_div"),
                         r.get("account_id"), r.get("account_nm"),
                         to_num(r.get("thstrm_amount")), to_num(r.get("frmtrm_amount")),
                         to_num(r.get("bfefrmtrm_amount")), int(r.get("ord") or 0)))
            n += 1
    con.commit()
    return n


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
            sj = ("BS",) if key in ("total_assets", "total_liab", "equity", "cash") else \
                 ("CF",) if key == "cfo" else ("IS", "CIS")
            k[key] = _pick(rows, ids, names, sj)
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
        con.execute("""INSERT OR REPLACE INTO fin_kpi VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, bsns_year, reprt_code, fs_div, k.get("revenue"), k.get("op_income"),
                     k.get("net_income"), k.get("total_assets"), k.get("total_liab"), k.get("equity"),
                     k.get("cash"), k.get("borrowings"), k.get("cfo"), k.get("debt_ratio"),
                     k.get("op_margin"), k.get("net_borrow_ratio")))
    con.commit()


def run(corp_codes, years, reprt_code="11011"):
    con = connect()
    for cc in corp_codes:
        for y in years:
            try:
                n = fetch_fin_raw(con, cc, str(y), reprt_code)
                build_kpi(con, cc, str(y), reprt_code)
                log(con, cc, f"fin_{y}", "ok" if n else "empty", f"{n} rows")
            except Exception as e:  # noqa
                log(con, cc, f"fin_{y}", "fail", e)
    return con
