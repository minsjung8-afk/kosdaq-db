"""DART 재무: 전체 재무제표(long) 적재 + 심사용 KPI 산출"""
from common import connect, dart_get, log, to_num

REPRT = {"11011": "사업", "11012": "반기", "11013": "1Q", "11014": "3Q"}

# KPI 매핑: (우선 account_id 리스트, 보조 account_nm 키워드)
KPI_MAP = {
    "revenue":      (["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
                     ["매출액", "수익(매출액)", "영업수익", "매출", "수익", "영업수익(매출액)", "매출액(영업수익)", "매출및지분법손익", "순영업수익"]),
    "op_income":    (["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)", "영업손익", "영업손실"]),
    "cogs":         (["ifrs-full_CostOfSales"], ["매출원가", "영업원가"]),
    "net_income":   (["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
    "total_assets": (["ifrs-full_Assets"], ["자산총계"]),
    "total_liab":   (["ifrs-full_Liabilities"], ["부채총계"]),
    "equity":       (["ifrs-full_Equity"], ["자본총계"]),
    "cash":         (["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "cfo":          (["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름", "영업활동으로 인한 현금흐름"]),
    "cfi":          (["ifrs-full_CashFlowsFromUsedInInvestingActivities"], ["투자활동현금흐름", "투자활동으로 인한 현금흐름", "투자활동으로인한현금흐름"]),
    "cff":          (["ifrs-full_CashFlowsFromUsedInFinancingActivities"], ["재무활동현금흐름", "재무활동으로 인한 현금흐름", "재무활동으로인한현금흐름"]),
    "capex_ppe":    (["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfPropertyPlantAndEquipment"],
                     ["유형자산의 취득", "유형자산의취득", "유형자산 취득", "유형자산취득"]),
    "capex_int":    (["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfIntangibleAssets"],
                     ["무형자산의 취득", "무형자산의취득", "무형자산 취득", "무형자산취득"]),
    "dep":          (["ifrs-full_DepreciationExpense", "ifrs-full_AdjustmentsForDepreciationExpense"], ["감가상각비", "유형자산감가상각비", "감가상각"]),
    "amort":        (["ifrs-full_AmortisationExpense", "ifrs-full_AdjustmentsForAmortisationExpense"], ["무형자산상각비", "무형자산상각"]),
    "rou_dep":      ([], ["사용권자산상각비", "사용권자산감가상각비"]),
    "int_exp":      (["ifrs-full_InterestExpense", "ifrs-full_FinanceCosts"], ["이자비용", "금융비용", "금융원가"]),
    "cur_assets":   (["ifrs-full_CurrentAssets"], ["유동자산"]),
    "cur_liab":     (["ifrs-full_CurrentLiabilities"], ["유동부채"]),
    "receivables":  (["ifrs-full_TradeAndOtherCurrentReceivables", "ifrs-full_CurrentTradeReceivables"], ["매출채권", "매출채권및기타채권", "매출채권 및 기타채권", "매출채권및기타유동채권"]),
    "inventory":    (["ifrs-full_Inventories"], ["재고자산"]),
    "capital":      (["ifrs-full_IssuedCapital"], ["자본금"]),
    "payables":     (["ifrs-full_TradeAndOtherCurrentPayables", "ifrs-full_TradeAndOtherPayables", "ifrs-full_TradeAndOtherCurrentPayablesToTradeSuppliers"],
                     ["매입채무", "매입채무및기타채무", "매입채무 및 기타채무", "매입채무및기타유동채무", "매입채무및기타유동부채"]),
    # 건설·조선 등 수주산업: IFRS15 계약자산/계약부채(= 미청구공사/초과청구공사) + 선수금
    "contract_assets": (["ifrs-full_CurrentContractAssets", "ifrs-full_ContractAssets"], ["미청구공사", "계약자산", "유동계약자산", "미청구공사(계약자산)"]),
    "contract_liab":   (["ifrs-full_CurrentContractLiabilities", "ifrs-full_ContractLiabilities"], ["초과청구공사", "계약부채", "유동계약부채", "초과청구공사(계약부채)"]),
    "advances":        ([], ["선수금", "유동선수금", "분양선수금", "선수금및선수수익"]),
}
BORROW_KEYS = ["단기차입금", "장기차입금", "유동성장기부채", "유동성장기차입금", "사채", "전환사채", "신주인수권부사채",
               "유동성사채", "유동성전환사채", "유동성신주인수권부사채", "교환사채", "유동성교환사채", "단기사채", "유동차입금", "비유동차입금",
               "유동성리스부채", "단기차입금및유동성장기부채", "장기차입금및사채",
               "리스부채", "유동리스부채", "비유동리스부채", "장기사채"]


def _cum(r, key):
    """분기·반기보고서 손익계산서는 thstrm_amount=3개월, thstrm_add_amount=누적 → 누적을 쓴다(사업보고서·BS·CF는 그대로)"""
    add = r.get(key.replace("_amount", "_add_amount"))
    if r.get("sj_div") in ("IS", "CIS") and add not in (None, "", "-"):
        return to_num(add)
    return to_num(r.get(key))


def ensure_cum_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS fin_cum_ok (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code))")


def _fetch_list(corp_code, bsns_year, reprt_code, fs_div):
    """HTTP만(스레드 안전) — DB 접근 없음"""
    return dart_get("fnlttSinglAcntAll", corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=fs_div).get("list", [])


def store_raw(con, corp_code, bsns_year, reprt_code, fs_div, lst, prev=True):
    """받은 목록 저장(손익은 누적). prev=True면 같은 보고서의 '전기' 열로 전년 같은 기간도 채운다(이미 그 보고서를 받았으면 건드리지 않음).
    - 연간: 전기 = 전년 사업보고서와 같은 기준(BS 포함) → 전 계정 저장
    - 분기·반기: 전기 손익·현금흐름 = 전년 같은 기간 누적, BS 전기 = 전년 말(분기말 아님) → BS는 저장하지 않음(분기말 순차입금은 해당 보고서에서만)
    반환: (당기 저장 행 수, 전기 보충 여부)"""
    ensure_cum_table(con)
    y = str(bsns_year)
    con.execute("DELETE FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?", (corp_code, y, reprt_code, fs_div))
    ins = """INSERT OR REPLACE INTO fin_raw (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_nm,
             thstrm_amount, frmtrm_amount, bfefrmtrm_amount, ord, rcept_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""
    for r in lst:
        con.execute(ins, (corp_code, y, reprt_code, fs_div, r.get("sj_div"), r.get("account_id"), r.get("account_nm"),
                          _cum(r, "thstrm_amount"), _cum(r, "frmtrm_amount"), to_num(r.get("bfefrmtrm_amount")),
                          int(r.get("ord") or 0), r.get("rcept_no")))
    con.execute("INSERT OR IGNORE INTO fin_cum_ok VALUES (?,?,?)", (corp_code, y, reprt_code))
    filled = False
    if prev and lst:
        py = str(int(y) - 1)
        have = (reprt_code == "11011" or con.execute("SELECT 1 FROM fin_cum_ok WHERE corp_code=? AND bsns_year=? AND reprt_code=?", (corp_code, py, reprt_code)).fetchone()) and \
               con.execute("SELECT 1 FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? LIMIT 1",
                           (corp_code, py, reprt_code, fs_div)).fetchone()
        if not have:     # 없거나 누적 오류(패치68 이전 3개월치) 행이면 손익·현금흐름만 교체(분기말 BS는 보존)
            if reprt_code == "11011":
                con.execute("DELETE FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?", (corp_code, py, reprt_code, fs_div))
            else:
                con.execute("DELETE FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? AND sj_div IN ('IS','CIS','CF')", (corp_code, py, reprt_code, fs_div))
            for r in lst:
                if reprt_code != "11011" and r.get("sj_div") in ("BS", "SCE"):
                    continue
                con.execute(ins, (corp_code, py, reprt_code, fs_div, r.get("sj_div"), r.get("account_id"), r.get("account_nm"),
                                  _cum(r, "frmtrm_amount"), to_num(r.get("bfefrmtrm_amount")), None, int(r.get("ord") or 0),
                                  (r.get("rcept_no") or "") + ":전기"))
            con.execute("INSERT OR IGNORE INTO fin_cum_ok VALUES (?,?,?)", (corp_code, py, reprt_code))
            filled = True
    return len(lst), filled


def fetch_fin_raw(con, corp_code, bsns_year, reprt_code="11011", fs_divs=("CFS", "OFS"), prev=False):
    n = 0
    for fs_div in fs_divs:
        lst = _fetch_list(corp_code, bsns_year, reprt_code, fs_div)
        if lst:
            n += store_raw(con, corp_code, bsns_year, reprt_code, fs_div, lst, prev=prev)[0]
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


def _norm_nm(nm):
    """계정명 정규화: 공백·주석표시 '(주석15)'·앞 번호 'Ⅰ.','1.','(1)' 제거 — 정확 일치 매칭 누락 방지"""
    import re
    s = (nm or "").replace(" ", "").replace("\u3000", "")
    s = re.sub(r"\((주석|주)[^)]*\)", "", s)
    s = re.sub(r"^(\(\d+\)|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|\d+|[가-하])[.)])", "", s)
    return s


def _pick(rows, ids, names, sj=None):
    """rows: fin_raw dict 목록. account_id 우선, 없으면 계정명 정확/포함 매칭."""
    for r in rows:
        if r["account_id"] in ids and (sj is None or r["sj_div"] in sj):
            return r["thstrm_amount"]
    for nm in names:
        for r in rows:
            an = _norm_nm(r["account_nm"])
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
            sj = ("BS",) if key in ("total_assets", "total_liab", "equity", "cash", "cur_assets", "cur_liab", "receivables", "inventory", "capital", "payables", "contract_assets", "contract_liab", "advances") else \
                 ("CF",) if key in ("cfo", "dep", "amort", "rou_dep", "cfi", "cff", "capex_ppe", "capex_int") else ("IS", "CIS")
            k[key] = _pick(rows, ids, names, sj)
        # 상각비: 계정명 패턴으로 재탐색 (합산 계정 '감가상각비와무형자산상각비' 우선, 없으면 감가+무형+사용권 합)
        _da_from_rows(rows, k)
        # 차입금 근사: BS 계정명 키워드 합(중복 방지: 정확 일치만)
        bor = 0.0
        hit = False
        bk = {x.replace(" ", "") for x in BORROW_KEYS}
        for r in rows:
            an = _norm_nm(r["account_nm"])
            if r["sj_div"] == "BS" and an in bk and r["thstrm_amount"]:
                bor += r["thstrm_amount"]
                hit = True
        k["borrowings"] = bor if hit else None
        eq, rev = k.get("equity"), k.get("revenue")
        k["debt_ratio"] = round(k["total_liab"] / eq * 100, 1) if k.get("total_liab") and eq else None
        k["op_margin"] = round(k["op_income"] / rev * 100, 1) if k.get("op_income") is not None and rev else None
        k["cogs_ratio"] = round(k["cogs"] / rev * 100, 1) if k.get("cogs") is not None and rev else None
        k["net_borrow_ratio"] = round((k["borrowings"] - (k.get("cash") or 0)) / eq * 100, 1) \
            if k.get("borrowings") is not None and eq else None
        k["net_borrowings"] = (k["borrowings"] - (k.get("cash") or 0)) if k.get("borrowings") is not None else None
        da = [abs(x) for x in (k.get("dep"), k.get("amort"), k.get("rou_dep")) if x is not None]
        k["da"] = sum(da) if da else None
        if k["da"] is None:          # 본문에 상각비가 없으면 XBRL 주석에서 뽑은 값(da_xbrl) 사용
            try:
                x = con.execute("SELECT da FROM da_xbrl WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? AND da IS NOT NULL",
                                (corp_code, bsns_year, reprt_code, fs_div)).fetchone()
                if x:
                    k["da"] = x[0]
            except Exception:  # noqa
                pass
        k["ebitda"] = (k["op_income"] + k["da"]) if k.get("op_income") is not None and k["da"] is not None else None
        ie = abs(k["int_exp"]) if k.get("int_exp") else None
        k["interest_cover"] = round(k["ebitda"] / ie, 1) if k.get("ebitda") is not None and ie else None   # EBITDA/이자비용
        k["borrow_dep"] = round(k["borrowings"] / k["total_assets"] * 100, 1) \
            if k.get("borrowings") is not None and k.get("total_assets") else None
        k["cur_ratio"] = round(k["cur_assets"] / k["cur_liab"] * 100, 1) if k.get("cur_assets") and k.get("cur_liab") else None
        k["recv_turn"] = round(rev / k["receivables"], 1) if rev and k.get("receivables") else None       # 연환산은 사이트에서 분기 보정
        k["inv_turn"] = round(rev / k["inventory"], 1) if rev and k.get("inventory") else None
        k["nd_ebitda"] = round(k["net_borrowings"] / k["ebitda"], 1) if k.get("net_borrowings") is not None and k.get("ebitda") and k["ebitda"] > 0 else None
        k["net_margin"] = round(k["net_income"] / rev * 100, 1) if k.get("net_income") is not None and rev else None
        k["borrow_op"] = round(k["borrowings"] / k["op_income"], 1) if k.get("borrowings") is not None and k.get("op_income") and k["op_income"] > 0 else None   # 총차입금/영업이익(배), 적자면 N/A
        k["nwc"] = ((k.get("receivables") or 0) + (k.get("inventory") or 0) - (k.get("payables") or 0)) \
            if any(k.get(x) is not None for x in ("receivables", "inventory", "payables")) else None      # 순운전자본 근사(매출채권+재고−매입채무)
        k["nwc_proj"] = (k["nwc"] + (k.get("contract_assets") or 0) - (k.get("contract_liab") or 0) - (k.get("advances") or 0)) \
            if k.get("nwc") is not None else None        # 수주산업형: +미청구공사 −초과청구공사 −선수금
        # 현금흐름: CAPEX = 유형+무형자산 취득(절대값), FCF = 영업활동CF − CAPEX
        cp = [abs(k[x]) for x in ("capex_ppe", "capex_int") if k.get(x) is not None]
        k["capex"] = sum(cp) if cp else None
        k["fcf"] = (k["cfo"] - k["capex"]) if k.get("cfo") is not None and k["capex"] is not None else None
        rcept = next((r["rcept_no"] for r in rows if r.get("rcept_no")), None)
        if rcept is None:   # 기존 행에 저장된 접수번호 유지
            old = con.execute("SELECT rcept_no FROM fin_kpi WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?",
                              (corp_code, bsns_year, reprt_code, fs_div)).fetchone()
            rcept = old["rcept_no"] if old else None
        con.execute("""INSERT OR REPLACE INTO fin_kpi (corp_code, bsns_year, reprt_code, fs_div, revenue, op_income, net_income,
            total_assets, total_liab, equity, cash, borrowings, cfo, debt_ratio, op_margin, net_borrow_ratio,
            net_borrowings, da, ebitda, int_exp, interest_cover, borrow_dep, rcept_no,
            cur_assets, cur_liab, receivables, inventory, capital, cur_ratio, recv_turn, inv_turn, nd_ebitda,
            payables, nwc, net_margin, borrow_op, contract_assets, contract_liab, advances, nwc_proj, cogs, cogs_ratio,
            cfi, cff, capex, capex_ppe, capex_int, fcf)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, bsns_year, reprt_code, fs_div, k.get("revenue"), k.get("op_income"),
                     k.get("net_income"), k.get("total_assets"), k.get("total_liab"), k.get("equity"),
                     k.get("cash"), k.get("borrowings"), k.get("cfo"), k.get("debt_ratio"),
                     k.get("op_margin"), k.get("net_borrow_ratio"), k.get("net_borrowings"), k.get("da"),
                     k.get("ebitda"), ie, k.get("interest_cover"), k.get("borrow_dep"), rcept,
                     k.get("cur_assets"), k.get("cur_liab"), k.get("receivables"), k.get("inventory"), k.get("capital"),
                     k.get("cur_ratio"), k.get("recv_turn"), k.get("inv_turn"), k.get("nd_ebitda"),
                     k.get("payables"), k.get("nwc"), k.get("net_margin"), k.get("borrow_op"),
                     k.get("contract_assets"), k.get("contract_liab"), k.get("advances"), k.get("nwc_proj"), k.get("cogs"), k.get("cogs_ratio"),
                     k.get("cfi"), k.get("cff"), k.get("capex"), k.get("capex_ppe"), k.get("capex_int"), k.get("fcf")))
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


def _annual_worker(cc, year):
    """스레드: 최근 사업보고서(연결→별도), 없으면 전년도 — HTTP만. 반환 [(year, fs, list)] 또는 예외"""
    for y in (year, year - 1):
        for fs in ("CFS", "OFS"):
            lst = _fetch_list(cc, y, "11011", fs)
            if lst:
                return [(y, fs, lst)]
    return []


def run_annual_fast(corp_codes, year, con=None, workers=4):
    """연간 재무 빠른 수집: 최근 사업보고서 1건(전체 재무제표, 연결 우선→없으면 별도)으로 당기·전기 2개년을 함께 적재.
    회사당 1~4콜, HTTP는 workers개 병렬(DB 쓰기는 메인 스레드). 시도 결과는 fetch_log(fin_fast)에 남겨 재시도하지 않음. DART 한도 도달 시 중단."""
    import common
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    ensure_cum_table(con)
    cs = list(corp_codes)
    print(f"[fin] 연간 재무 대상 {len(cs)}사 · 병렬 {workers}", flush=True)
    n = i = 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(cs), workers * 5):
            batch = cs[b:b + workers * 5]
            futs = [(cc, ex.submit(_annual_worker, cc, year)) for cc in batch]
            stop = False
            for cc, f in futs:
                i += 1
                try:
                    got = f.result()
                    for y, fs, lst in got:
                        store_raw(con, cc, y, "11011", fs, lst, prev=True)
                        build_kpi(con, cc, str(y), "11011")
                        build_kpi(con, cc, str(y - 1), "11011")
                    log(con, cc, "fin_fast", "ok" if got else "empty")
                    n += bool(got)
                except Exception as e:  # noqa
                    log(con, cc, "fin_fast", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 50 < workers * 5:
                print(f"[fin] {i}/{len(cs)} · 적재 {n} · DART {common.CALLS}콜", flush=True)
            if stop:
                print(f"[fin] DART 한도 도달 — {i}/{len(cs)}에서 중단", flush=True); break
    print(f"[fin] 완료 {i}/{len(cs)} · 적재 {n} · DART {common.CALLS}콜", flush=True)
    return n


def run(corp_codes, years, reprt_code="11011", interim=True, annual_years=None):
    """years: 연간(사업보고서) 적재 연도. interim=True면 최근 분기 + 전년 동기 추가.
    오늘 기준 기본: 직전 2개년 연간 + 최근분기·전년동기 = 회사당 (2+2)×2(연결/별도) = 8호출"""
    con = connect()
    if annual_years is None:
        annual_years = years
    plan = latest_interim_plan()
    cs = list(corp_codes)
    quota = False
    for i, cc in enumerate(cs, 1):
        for y in annual_years:
            try:
                n = fetch_fin_raw(con, cc, str(y), "11011")
                if n:
                    build_kpi(con, cc, str(y), "11011")
                log(con, cc, f"fin_{y}", "ok" if n else "empty", f"{n} rows")
            except Exception as e:  # noqa
                log(con, cc, f"fin_{y}", "fail", e)
                if "한도" in str(e):
                    quota = True
                    break
        if not quota and interim:
            try:
                r = fetch_latest_interim(con, cc, plan)
                log(con, cc, "fin_interim", "ok" if r else "empty", str(r))
            except Exception as e:  # noqa
                log(con, cc, "fin_interim", "fail", e)
                if "한도" in str(e):
                    quota = True
        if quota:
            print(f"[fin] DART 일일 한도 도달 — {i}/{len(cs)}에서 중단(내일 --new-only로 이어서)", flush=True)
            break
        if i % 100 == 0:
            print(f"[fin] {i}/{len(cs)}", flush=True)
    try:
        backfill_rcept(con, set(corp_codes))
    except Exception as e:  # noqa
        log(con, "-", "rcept", "fail", e)
    return con
