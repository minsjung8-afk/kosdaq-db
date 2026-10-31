"""DART 재무: 전체 재무제표(long) 적재 + 심사용 KPI 산출"""
from common import connect, dart_get, log, to_num

REPRT = {"11011": "사업", "11012": "반기", "11013": "1Q", "11014": "3Q"}

# KPI 매핑: (우선 account_id 리스트, 보조 account_nm 키워드)
KPI_MAP = {
    "revenue":      (["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
                     ["매출액", "수익(매출액)", "영업수익", "매출", "수익", "영업수익(매출액)", "매출액(영업수익)", "매출및지분법손익", "순영업수익"]),
    "op_income":    (["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)", "영업손익", "영업손실"]),
    "cogs":         (["ifrs-full_CostOfSales"], ["매출원가", "영업원가"]),
    "net_income":   (["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)", "당기순손익", "당기순손실", "연결당기순이익", "연결당기순이익(손실)",
                                               "연결당기순손익", "당기순이익(순손실)", "당기순손실(이익)", "연결당기순손실"]),   # 감사보고서 표(표준계정코드 없음) 대응
    "total_assets": (["ifrs-full_Assets"], ["자산총계", "자산합계", "자산총액", "총자산"]),
    "total_liab":   (["ifrs-full_Liabilities"], ["부채총계", "부채합계", "부채총액", "총부채"]),
    "equity":       (["ifrs-full_Equity"], ["자본총계", "자본합계", "자본총액", "총자본"]),
    "cash":         (["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "cfo":          (["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름", "영업활동으로 인한 현금흐름"]),
    "cfi":          (["ifrs-full_CashFlowsFromUsedInInvestingActivities"], ["투자활동현금흐름", "투자활동으로 인한 현금흐름", "투자활동으로인한현금흐름"]),
    "cff":          (["ifrs-full_CashFlowsFromUsedInFinancingActivities"], ["재무활동현금흐름", "재무활동으로 인한 현금흐름", "재무활동으로인한현금흐름"]),
    "capex_ppe":    (["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfPropertyPlantAndEquipment"],
                     ["유형자산의 취득", "유형자산의취득", "유형자산 취득", "유형자산취득"]),
    "capex_int":    (["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfIntangibleAssets"],
                     ["무형자산의 취득", "무형자산의취득", "무형자산 취득", "무형자산취득"]),
    "tax_paid":     (["ifrs-full_IncomeTaxesPaidRefundClassifiedAsOperatingActivities", "ifrs-full_IncomeTaxesPaidRefund"],
                     ["법인세의 납부", "법인세납부", "법인세의납부", "법인세 납부", "법인세의 환급(납부)", "법인세환급(납부)", "법인세 납부액", "법인세의 납부(환급)"]),
    "disp_ppe":     (["ifrs-full_ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "ifrs-full_ProceedsFromSalesOfPropertyPlantAndEquipment"],
                     ["유형자산의 처분", "유형자산의처분", "유형자산 처분", "유형자산처분"]),
    "disp_int":     (["ifrs-full_ProceedsFromSalesOfIntangibleAssetsClassifiedAsInvestingActivities", "ifrs-full_ProceedsFromSalesOfIntangibleAssets"],
                     ["무형자산의 처분", "무형자산의처분", "무형자산 처분", "무형자산처분"]),
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


import re as _re
_BOR_NM = _re.compile(r"(차입금|사채|리스부채|유동성장기부채|금융리스|어음|전자단기사채|기업어음)")
_BOR_EX = _re.compile(r"(할인발행차금|전환권조정|교환권조정|신주인수권조정|상환할증금|할증금|이자|미지급|충당|파생|평가|조정|매입채무|미수|대여|보증|선급|이연|자산|수익|비용|손익|지급어음|매출채권)")
_BOR_ID = _re.compile(r"(Borrowing|BondsIssued|Debenture|LeaseLiabilit|ConvertibleBond|ExchangeableBond|BondsWithWarrant|CommercialPaper|ShortTermBond)", _re.I)
_BOR_ID_EX = _re.compile(r"(Interest|Discount|Premium|Adjust|Conversion|Payable|Receivable|Proceeds|Repayment|Asset|Expense|Cost|Gain|Loss)", _re.I)
_BOR_AGG = _re.compile(r"(및|합계|총계)")
# '금융부채'로만 표시하는 회사(LG디스플레이 등: 유동 금융부채·비유동금융부채 = 차입금·사채). '기타금융부채'·파생은 제외
_BOR_FIN = _re.compile(r"^(유동|비유동|단기|장기)?금융부채$")


_BOR_CUR = _re.compile(r"(유동성|단기|^유동|당좌|전자단기|기업어음)")
_BOR_NON = _re.compile(r"(비유동|장기)")


def _bor_kind(nm):
    """차입성 계정 → 종류: 차입금 | 일반사채 | CB | BW | EB | CP | 리스 (사용자 요청 2026-10-24: 사채는 일반/CB/BW/EB로 표기)"""
    if "리스" in nm:
        return "리스"
    if "사채제외" in nm:                       # '비유동금융기관차입금(사채제외)…' (AJ네트웍스 2024 표준계정명) → 차입금
        return "차입금"
    if "차입금" in nm and "사채" in nm:       # '단기차입금및사채'·'유동차입금(사채포함)' 등 합산 계정(대동 등) — 재무상태표로는 분리 불가
        return "차입금·사채"
    if "전환사채" in nm or "상환전환" in nm:
        return "CB"
    if "신주인수권" in nm:
        return "BW"
    if "교환사채" in nm:
        return "EB"
    if "기업어음" in nm or "전자단기사채" in nm or "단기사채" in nm:
        return "CP"
    if "사채" in nm:
        return "일반사채"
    return "차입금"


def borrow_items(rows):
    """재무상태표 차입성 부채 세부 → [{nm, amt, kind, cur}] (cur: '유동'|'비유동'|None).
    총차입금(_borrowings)과 같은 후보·중복 규칙을 쓰므로 합계가 KPI 총차입금과 일치한다.
    유동/비유동: ① 계정명(유동성·단기 / 장기·비유동) ② 없으면 재무상태표의 유동부채·비유동부채 구간 위치(ord)로 판정"""
    cand = []
    bs = [r for r in rows if r.get("sj_div") == "BS"]
    for r in bs:
        if not r.get("thstrm_amount"):
            continue
        nm = _norm_nm(r.get("account_nm")); aid = r.get("account_id") or ""
        by_id = bool(_BOR_ID.search(aid)) and not _BOR_ID_EX.search(aid)
        by_nm = (bool(_BOR_NM.search(nm)) and not _BOR_EX.search(nm)) or bool(_BOR_FIN.match(nm))
        if by_id or by_nm:
            cand.append((nm, r["thstrm_amount"], aid, r.get("ord")))
    if not cand:
        return []
    agg = [c for c in cand if _BOR_AGG.search(c[0])]
    detail = [c for c in cand if not _BOR_AGG.search(c[0])]
    use = detail if (agg and any(_re.search(r"(차입금|사채|유동성장기부채)", c[0]) for c in detail)) else cand
    # 유동/비유동 구간(ord): DART 재무상태표 행은 소계(유동부채·비유동부채)가 먼저 오고 세부가 뒤따름(헤더 선행) — 아니면 반대로 해석
    def _ord(name):
        for r in bs:
            if _norm_nm(r.get("account_nm")) == name and r.get("ord") is not None:
                return r["ord"]
        return None
    o_cl, o_ncl, o_tl = _ord("유동부채"), _ord("비유동부채"), _ord("부채총계")
    if o_tl is not None and o_ncl is not None and o_tl < o_ncl:
        o_tl = None                                    # 2026-10-30 대한항공: XBRL 순번이 유동부채 42 → 부채총계 51 → 비유동부채 52 로 소계가 세부보다 앞에 와 비유동 차입금(53·56)이 '구분불명'이 됨 → 부채총계 상한을 쓰지 않음
    o_ca = _ord("유동자산"); o_cash = next((r["ord"] for r in bs if "현금및현금성자산" in _norm_nm(r.get("account_nm")) and r.get("ord") is not None), None)
    hdr_first = True if (o_ca is None or o_cash is None) else o_ca < o_cash
    def pos_cur(o):
        if o is None or o_cl is None or o_ncl is None:
            return None
        if hdr_first:
            return "유동" if o_cl < o < o_ncl else "비유동" if o > o_ncl and (o_tl is None or o < o_tl) else None
        return "유동" if o < o_cl else "비유동" if o_cl < o < o_ncl else None
    seen, out = set(), []
    for nm, v, aid, o in use:
        if (nm, v) in seen:
            continue
        seen.add((nm, v))
        # 순서: '비유동'(…의비유동성부분 포함) → '유동성·단기·유동'('유동성장기부채'는 유동) → 재무상태표 위치
        cur = "비유동" if "비유동" in nm else "유동" if ("유동성" in nm or _BOR_CUR.search(nm)) else "비유동" if _BOR_NON.search(nm) else pos_cur(o)
        if cur is None and _bor_kind(nm) in ("일반사채", "CB", "BW", "EB"):
            cur = "비유동"                        # 위치 판정 불가(ord 없음)한 '사채'류 — 1년 내 만기분은 '유동성사채'로 따로 공시되므로 본계정은 비유동
        out.append({"nm": nm, "amt": v, "kind": _bor_kind(nm), "cur": cur})
    return out


def _borrowings(rows):
    """총차입금(근사) v2: 재무상태표에서 차입성 부채를 모은다(세부는 borrow_items). 반환 (금액 or None, 근거 문자열)"""
    items = borrow_items(rows)
    if not items:
        return None, ""
    return sum(i["amt"] for i in items), "+".join(sorted({i["nm"] for i in items}))[:300]


def _norm_nm(nm):
    """계정명 정규화: 공백·주석표시 '(주석15)'·앞 번호 'Ⅰ.','1.','(1)' 제거 — 정확 일치 매칭 누락 방지"""
    import re
    s = (nm or "").replace(" ", "").replace("\u3000", "")
    s = re.sub(r"\((주석|주|Note|\*)[^)]*\)|<[^>]*>|\[[^\]]*\]", "", s)
    s = re.sub(r"^(\(\d+\)|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|[IVXㅣl]+|\d+|[가-하])[.)])", "", s)   # 'ㅣ.영업활동…'(자모 ㅣ를 로마자 Ⅰ로 쓴 표, 에스피엘)
    return s


_REV_PARTS = ("이자수익", "수수료수익", "리스수익", "할부금융수익", "할부수익", "렌탈수익", "임대료수익", "기타영업수익", "보험수익", "신용카드수익", "카드수익",
              "수수료매출", "기타매출", "상품매출", "제품매출", "용역매출", "임대수익", "금융상품관련순손익", "유가증권관련수익", "배당수익", "배당금수익")


def _rev_fallback(rows, op):
    """매출(영업수익) 합계 줄이 없을 때(2026-10-28 삼성카드·신한카드·빗썸 반기):
    ① 손익계산서에 '영업비용' 합계 줄이 있으면 영업이익 + 영업비용 (항등식)
    ② 아니면 '영업이익·영업비용' 줄보다 위(영업 구간)에 있는 수익 구성 줄의 합 — 영업외 구간의 이자·배당수익은 제외
    → (값, 출처) 또는 (None, None)"""
    isr = sorted([r for r in rows if r["sj_div"] in ("IS", "CIS")], key=lambda r: (r["sj_div"] != "IS", r.get("ord") if r.get("ord") is not None else 10**6))
    if not isr:
        return None, None
    for sj in ("IS", "CIS"):
        part = [r for r in isr if r["sj_div"] == sj]
        exp = [r for r in part if _norm_nm(r["account_nm"]) in ("영업비용", "영업비용합계", "영업비용계", "영업비용총계")]
        if exp and op is not None and exp[0].get("thstrm_amount") is not None:
            return op + abs(exp[0]["thstrm_amount"]), "영업이익+영업비용"
    for sj in ("IS", "CIS"):
        part = [r for r in isr if r["sj_div"] == sj]
        if not part or any(r.get("ord") is None for r in part):
            continue
        head = []
        for r in part:
            if _norm_nm(r["account_nm"]) in ("영업비용", "영업이익", "영업이익(손실)", "영업손익", "영업손실", "영업비용합계"):
                break
            head.append(r)
        vals = [r["thstrm_amount"] for r in head if _norm_nm(r["account_nm"]) in _REV_PARTS and r.get("thstrm_amount")]
        if vals and len(head) < len(part):
            return sum(vals), "영업수익 구성 합"
    return None, None


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
    pat = _FALLBACK_RE.get(tuple(ids))
    if pat:                                   # 감사보고서 표의 변형 계정명(2026-10-25: '영업활동으로인한순현금흐름' 등으로 영업CF 누락 6사)
        for r in rows:
            if (sj is None or r["sj_div"] in sj) and r.get("thstrm_amount") is not None and pat.fullmatch(_norm_nm(r["account_nm"])):
                return r["thstrm_amount"]
    return None


import re as _re
_FALLBACK_RE = {
    ("ifrs-full_CashFlowsFromUsedInOperatingActivities",): _re.compile(r"영업활동(으로|에서|으로부터|으로부터의)?(인한|의|발생한|창출된|조달된)?(순)?현금(흐름|유입|유출|유입\(유출\)|흐름합계)"),
    ("ifrs-full_CashFlowsFromUsedInInvestingActivities",): _re.compile(r"투자활동(으로|에서|으로부터)?(인한|의|발생한)?(순)?현금(흐름|유입|유출|유입\(유출\)|흐름합계)"),
    ("ifrs-full_CashFlowsFromUsedInFinancingActivities",): _re.compile(r"재무활동(으로|에서|으로부터)?(인한|의|발생한)?(순)?현금(흐름|유입|유출|유입\(유출\)|흐름합계)"),
    ("ifrs-full_Assets",): _re.compile(r"(자산|총자산)(총계|합계|총액|계)?(\(.*\))?"),
    ("ifrs-full_ProfitLoss",): _re.compile(r"(연결)?(당기|당해연도|당기의)?순(이익|손실|손익)(\(.*\))?"),
}


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
                 ("CF",) if key in ("cfo", "dep", "amort", "rou_dep", "cfi", "cff", "capex_ppe", "capex_int", "disp_ppe", "disp_int", "tax_paid") else ("IS", "CIS")
            k[key] = _pick(rows, ids, names, sj)
        if k.get("revenue") is None:              # 2026-10-28: 금융업(카드·캐피탈)·일부 비상장은 매출 합계 줄이 없음 → 영업이익+영업비용 또는 영업수익 구성 합
            k["revenue"], k["_rev_src"] = _rev_fallback(rows, k.get("op_income"))
        # 상각비: 계정명 패턴으로 재탐색 (합산 계정 '감가상각비와무형자산상각비' 우선, 없으면 감가+무형+사용권 합)
        _da_from_rows(rows, k)
        # 차입금 근사: BS 계정명 키워드 합(중복 방지: 정확 일치만)
        k["borrowings"], k["_bor_src"] = _borrowings(rows)
        if k["borrowings"] is None and k.get("total_liab") is not None:
            # 2026-10-25 확인(고영·PN풍년·YW·KNN 등 사업보고서 원문 대조): 재무상태표에 차입성 계정이 아예 없거나 금액이 0이면 '차입금 없음' = 0.
            # 차입성으로 보이는 계정에 금액이 있는데 못 잡은 경우는 비워 둔다(진단 대상)
            hit = [r for r in rows if r["sj_div"] == "BS" and (r.get("thstrm_amount") or 0) != 0
                   and _re.search(r"차입|사채|리스부채|어음|유동화", _norm_nm(r["account_nm"])) and not _re.search(r"매입채무|미지급|선수|충당|이연|채권", _norm_nm(r["account_nm"]))]
            if not hit:
                k["borrowings"], k["_bor_src"] = 0, "차입성 계정 없음"
        eq, rev = k.get("equity"), k.get("revenue")
        k["debt_ratio"] = round(k["total_liab"] / eq * 100, 1) if k.get("total_liab") and eq else None
        k["op_margin"] = round(k["op_income"] / rev * 100, 1) if k.get("op_income") is not None and rev else None
        k["cogs_ratio"] = round(k["cogs"] / rev * 100, 1) if k.get("cogs") is not None and rev else None
        k["net_borrow_ratio"] = round((k["borrowings"] - (k.get("cash") or 0)) / eq * 100, 1) \
            if k.get("borrowings") is not None and eq else None
        k["net_borrowings"] = (k["borrowings"] - (k.get("cash") or 0)) if k.get("borrowings") is not None else None
        da = [abs(x) for x in (k.get("dep"), k.get("amort"), k.get("rou_dep")) if x is not None]
        cands = [("본문", sum(da) if da else None)]
        for tbl in ("da_xbrl", "da_notes"):            # XBRL 주석 → 주석 변동표(2026-10-24) 순 폴백
            try:
                x = con.execute(f"SELECT da FROM {tbl} WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? AND da IS NOT NULL",
                                (corp_code, bsns_year, reprt_code, fs_div)).fetchone()
                cands.append((tbl, abs(x[0]) if x else None))
            except Exception:  # noqa
                cands.append((tbl, None))
        # 상식 점검(2026-10-26 HL D&I 2026 반기: 주석 변동표 오독으로 상각비 3,195억(실제 약 90억) → EBITDA 왜곡):
        # 직전 연간 상각비를 기간 비율로 환산한 값의 1/4~4배 밖이거나 총자산의 25% 초과면 버리고 다음 후보
        ref = pa = None
        try:
            # 2026-10-30 소노인터내셔널: 직전 연간이 비어 2021(232억)을 기준으로 삼아 2023·2024(1,029억·1,094억, 실제 증가)를 버림 → 바로 전 해만 기준으로
            pr = con.execute("""SELECT da FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' AND bsns_year=? AND da IS NOT NULL""",
                             (corp_code, fs_div, str(int(bsns_year) - 1))).fetchone()
            if pr and pr[0]:
                pa = abs(pr[0])
                ref = abs(pr[0]) * {"11013": 0.25, "11012": 0.5, "11014": 0.75}.get(reprt_code, 1.0)
        except Exception:  # noqa
            pass
        cap = None                                     # 같은 사업연도 연간 상각비가 있으면 분기·반기 누적은 그 비율 이내(2026-10-29 동부건설 2025 반기 156억 > 연간 152억 오독)
        if reprt_code != "11011":
            try:
                sa = con.execute("SELECT da FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' AND bsns_year=? AND da IS NOT NULL",
                                 (corp_code, fs_div, bsns_year)).fetchone()
                if sa and sa[0]:
                    cap = abs(sa[0]) * 1.05                 # 2026-10-31: 0.5/0.8 비율은 상반기 비중이 큰 회사·연간값이 일부 항목만 잡힌 경우(미래에셋증권·현대해상 반기 ≒ 연간 81%)를 잘못 버림(375행) → 누적 분기·반기는 연간을 넘을 수 없다는 상한만
            except Exception:  # noqa
                pass
        ta = k.get("total_assets")
        k["da"] = None
        rejects = []                                   # (src, v, 사유) — 왜 비었는지 추적용(2026-10-30: AI 보완 1,188건이 사이트에 안 보임)
        for src, v in cands:
            if v is None:
                continue
            if ta and v > ta * 0.25:
                rejects.append((src, v, "총자산25%초과")); continue
            if ta and src != "본문" and v < ta * 0.000001:
                rejects.append((src, v, "총자산0.0001%미만")); continue   # 2026-10-30 18:40: 0.02% 기준은 지주·증권·보험·건설(자산 대비 상각비 극소)과 1분기 값 754건을 잘못 버림 → 사실상 0인 값만 거르고, 단위 누락은 파서(patch219)와 직전 연간 대조로 막는다
            if ref and ta and pa < ta * 0.0002:
                ref = None                             # 2026-10-31: 기준이 되는 직전 연간값 자체가 총자산 대비 0.02% 미만이면(HD현대 2023 연결 12억 — 오독) 기준으로 못 씀 → 점검 생략
            if ref and src != "본문" and not (ref / 8 <= v <= ref * 8):
                rejects.append((src, v, f"직전연간 1/8~8배 밖(ref {ref:.0f})")); continue   # 본문(현금흐름표) 값은 기준 점검 없이 신뢰 — 점검은 주석 오독 방지용(2026-10-26 HL D&I). 1/4~4배는 연결범위 변동·리스 도입·기준값이 일부 항목뿐인 경우 806행을 잘못 버림(2026-10-31) → 1/8~8배(단위 오류 1,000배는 여전히 걸러짐)
            if cap and v > cap:
                rejects.append((src, v, f"같은해 연간 비율 초과(cap {cap:.0f})")); continue
            if reprt_code != "11011" and pa and abs(v - pa) <= pa * 0.003:
                rejects.append((src, v, "직전연간과 동일(전기열)")); continue   # 분기·반기 값이 직전 연간과 똑같으면 전기(연간) 열을 읽은 것(2026-10-29 CJ대한통운 별도 2026 반기 1,996억 = 2025 연간)
            k["da"] = v
            break
        try:
            con.execute("CREATE TABLE IF NOT EXISTS da_reject (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, src TEXT, v REAL, reason TEXT, at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))")
            con.execute("DELETE FROM da_reject WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?", (corp_code, bsns_year, reprt_code, fs_div))
            if k["da"] is None and rejects:
                con.execute("INSERT INTO da_reject VALUES (?,?,?,?,?,?,?,datetime('now'))", (corp_code, bsns_year, reprt_code, fs_div, rejects[0][0], rejects[0][1], " / ".join(f"{s}:{r}" for s, _, r in rejects)))
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
        # 현금흐름: CAPEX(순액) = 유·무형자산 취득 − 유·무형자산 처분(절대값 기준), FCF = 영업활동CF − CAPEX(순액)
        # 2026-10-24: 계정명 변형·자산별 세분 표시(토지의 취득, 기계장치의 취득 …) 폴백 — dq 'Capex 없음' 180사 대응
        if k.get("capex_ppe") is None or k.get("capex_int") is None or k.get("disp_ppe") is None:
            cf = [(_norm_nm(r["account_nm"]), r["thstrm_amount"]) for r in rows if r["sj_div"] == "CF" and r["account_nm"] and r["thstrm_amount"] is not None]
            def _sum(pat, excl=r"(처분|매각|감소|회수|보증금|대여|상환|배당|이자|차입|예금|주식|채권|출자|지분|파생|리스부채)"):
                vals = [v for nm, v in cf if _re.search(pat, nm) and not _re.search(excl, nm)]
                return sum(abs(v) for v in vals) if vals else None
            if k.get("capex_ppe") is None:
                k["capex_ppe"] = _sum(r"^(유형자산|토지|건물|구축물|기계장치|차량운반구|비품|공구와기구|공구|시설장치|건설중인자산|임차시설|투자부동산|사용권자산)(의|에대한)?(취득|증가|매입|구입)")
            if k.get("capex_int") is None:
                k["capex_int"] = _sum(r"^(무형자산|산업재산권|소프트웨어|개발비|회원권|영업권|기타의?무형자산|특허권|상표권|라이선스|라이센스)(의|에대한)?(취득|증가|매입|구입)")
            if k.get("disp_ppe") is None:
                k["disp_ppe"] = _sum(r"^(유형자산|토지|건물|구축물|기계장치|차량운반구|비품|공구와기구|공구|시설장치|투자부동산)(의|에대한)?(처분|매각|감소)", excl=r"(손실|이익|취득|증가|보증금)")
        cp = [abs(k[x]) for x in ("capex_ppe", "capex_int") if k.get(x) is not None]
        dp = [abs(k[x]) for x in ("disp_ppe", "disp_int") if k.get(x) is not None]
        k["capex_disp"] = sum(dp) if dp else None
        k["capex"] = (sum(cp) - sum(dp)) if cp else None
        k["fcf"] = (k["cfo"] - k["capex"]) if k.get("cfo") is not None and k["capex"] is not None else None
        # 2026-10-31 보고서 자체의 단위 오류(이큐셀 2025 반기 총자산 1,486,253,622억 = 100만 배, 디와이에이·CMG제약·이트론·에프알텍·진코스텍·다원넥스뷰·대산FB·신영스팩11호 등 26행):
        # DART가 제출된 XBRL 값을 그대로 주므로 우리 쪽 파싱 문제가 아님. 앞뒤 기간 총자산과 50배 이상 차이(또는 0)면 그 기간 KPI 숫자를 전부 비우고 kpi_flag에 기록 — 틀린 숫자보다 빈칸
        bad = _ta_outlier(con, corp_code, fs_div, bsns_year, reprt_code, k.get("total_assets"))
        if bad:
            con.execute("CREATE TABLE IF NOT EXISTS kpi_flag (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, reason TEXT, at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))")
            con.execute("INSERT OR REPLACE INTO kpi_flag VALUES (?,?,?,?,?,datetime('now'))", (corp_code, bsns_year, reprt_code, fs_div, bad))
            k = {}
            ie = None
        else:
            try:
                con.execute("DELETE FROM kpi_flag WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?", (corp_code, bsns_year, reprt_code, fs_div))
            except Exception:  # noqa
                pass
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
            cfi, cff, capex, capex_ppe, capex_int, fcf, capex_disp, tax_paid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, bsns_year, reprt_code, fs_div, k.get("revenue"), k.get("op_income"),
                     k.get("net_income"), k.get("total_assets"), k.get("total_liab"), k.get("equity"),
                     k.get("cash"), k.get("borrowings"), k.get("cfo"), k.get("debt_ratio"),
                     k.get("op_margin"), k.get("net_borrow_ratio"), k.get("net_borrowings"), k.get("da"),
                     k.get("ebitda"), ie, k.get("interest_cover"), k.get("borrow_dep"), rcept,
                     k.get("cur_assets"), k.get("cur_liab"), k.get("receivables"), k.get("inventory"), k.get("capital"),
                     k.get("cur_ratio"), k.get("recv_turn"), k.get("inv_turn"), k.get("nd_ebitda"),
                     k.get("payables"), k.get("nwc"), k.get("net_margin"), k.get("borrow_op"),
                     k.get("contract_assets"), k.get("contract_liab"), k.get("advances"), k.get("nwc_proj"), k.get("cogs"), k.get("cogs_ratio"),
                     k.get("cfi"), k.get("cff"), k.get("capex"), k.get("capex_ppe"), k.get("capex_int"), k.get("fcf"), k.get("capex_disp"), (abs(k["tax_paid"]) if k.get("tax_paid") is not None else None)))
    con.commit()


_PORD = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def _ta_outlier(con, corp_code, fs_div, bsns_year, reprt_code, ta):
    """총자산이 바로 앞·뒤 기간(둘 다 있으면 둘 다, 하나뿐이면 그 하나)과 50배 이상 차이거나 0이면 사유 문자열, 아니면 None.
    스팩 합병처럼 진짜로 급변하면 다음 기간이 쌓이는 순간 한쪽 이웃과 비슷해져 자동 해제"""
    try:
        rows = con.execute("SELECT bsns_year, reprt_code, total_assets FROM fin_kpi WHERE corp_code=? AND fs_div=? AND total_assets IS NOT NULL AND total_assets > 0 AND NOT (bsns_year=? AND reprt_code=?)",
                           (corp_code, fs_div, bsns_year, reprt_code)).fetchall()
    except Exception:  # noqa
        return None
    if not rows:
        return None
    try:   # 이미 단위 오류로 표시된 기간은 이웃으로 쓰지 않음
        flagged = {(r[0], r[1]) for r in con.execute("SELECT bsns_year, reprt_code FROM kpi_flag WHERE corp_code=? AND fs_div=?", (corp_code, fs_div))}
        rows = [r for r in rows if (r[0], r[1]) not in flagged]
    except Exception:  # noqa
        pass
    if not rows:
        return None
    me = (int(bsns_year), _PORD.get(reprt_code, 4))
    pts = sorted(((int(r[0]), _PORD.get(r[1], 4)), r[2]) for r in rows)
    prev = max((p for p in pts if p[0] < me), default=None)
    nxt = min((p for p in pts if p[0] > me), default=None)
    nb = [p[1] for p in (prev, nxt) if p]
    if not nb:
        return None
    if ta is None:
        return None                                    # 재무상태표가 아직 안 받아진 행(총자산 없음)은 오류가 아님 — 손익 등 다른 값은 그대로 둔다(2026-10-31 다원넥스뷰 2025 1Q가 통째로 지워진 뒤 수정)
    if ta == 0:
        return f"총자산 0(이웃 {nb[0]:.0f})"
    if all(ta >= v * 50 or ta * 50 <= v for v in nb):
        return f"총자산 이웃 기간 대비 50배 이상 차이({ta:.0f} vs {','.join(f'{v:.0f}' for v in nb)})"
    return None


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


def _ofs_worker(cc, year, interim_rc):
    """스레드(HTTP만): 별도(OFS) 연간 최신 1건 + 최신 분기 1건(각 보고서의 전기 열로 전년도·전년동기 보충)"""
    out = []
    for y in (year, year - 1):
        lst = _fetch_list(cc, y, "11011", "OFS")
        if lst:
            out.append((y, "11011", lst)); break
    if interim_rc:
        y, rc = interim_rc
        lst = _fetch_list(cc, y, rc, "OFS")
        if lst:
            out.append((y, rc, lst))
    return out


def run_ofs(corp_codes, year, con=None, workers=4):
    """연결만 있고 별도가 없는 회사의 별도 재무 보강(심사는 연결·별도 병기). 회사당 2콜(연간+최신분기, 전기 열 재사용)."""
    import common
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    ensure_cum_table(con)
    have_ofs = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fin_raw WHERE fs_div='OFS' AND reprt_code='11011'")}
    has_cfs = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fin_raw WHERE fs_div='CFS' AND reprt_code='11011'")}
    tried = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fetch_log WHERE job='fin_ofs' AND status IN ('ok','empty')")}
    cs = [c for c in corp_codes if c in has_cfs and c not in have_ofs and c not in tried]
    # 최신 분기: 연결 기준으로 가장 최근에 받은 분기보고서와 같은 보고서
    def latest_q(cc):
        r = con.execute("""SELECT bsns_year, reprt_code FROM fin_raw WHERE corp_code=? AND fs_div='CFS' AND reprt_code<>'11011'
                           AND (rcept_no IS NULL OR rcept_no NOT LIKE '%:전기') GROUP BY 1,2
                           ORDER BY bsns_year DESC, CASE reprt_code WHEN '11014' THEN 3 WHEN '11012' THEN 2 ELSE 1 END DESC LIMIT 1""", (cc,)).fetchone()
        return (r[0], r[1]) if r else None
    print(f"[fin-ofs] 별도 재무 보강 대상 {len(cs)}사 · 회사당 2콜 · 병렬 {workers}", flush=True)
    n = i = 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(cs), workers * 5):
            batch = cs[b:b + workers * 5]
            futs = [(cc, ex.submit(_ofs_worker, cc, year, latest_q(cc))) for cc in batch]
            stop = False
            for cc, f in futs:
                i += 1
                try:
                    got = f.result()
                    for y, rc, lst in got:
                        _, filled = store_raw(con, cc, y, rc, "OFS", lst, prev=True)
                        build_kpi(con, cc, str(y), rc)
                        if filled:
                            build_kpi(con, cc, str(int(y) - 1), rc)
                    log(con, cc, "fin_ofs", "ok" if got else "empty")
                    n += bool(got)
                except Exception as e:  # noqa
                    log(con, cc, "fin_ofs", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 100 < workers * 5:
                print(f"[fin-ofs] {i}/{len(cs)} · 적재 {n} · DART {common.CALLS}콜", flush=True)
            if stop:
                print(f"[fin-ofs] DART 한도 도달 — {i}/{len(cs)}에서 중단", flush=True); break
    print(f"[fin-ofs] 완료 {i}/{len(cs)} · 적재 {n}", flush=True)
    return n


def run_yoy(corp_codes, con=None, workers=4):
    """전년동기 열 재무상태표 보강: 최신 분기보고서의 '전년 같은 분기'가 전기 열 재사용(손익만)이라 BS가 빈 경우,
    그 분기보고서를 직접 받아 BS 포함 전체로 교체. 연결·별도 각각 1콜."""
    import common
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    ensure_cum_table(con)
    cs = set(corp_codes)
    todo = []
    latest = {}
    for r in con.execute("""SELECT corp_code, fs_div, bsns_year, reprt_code FROM fin_raw
                            WHERE reprt_code<>'11011' AND (rcept_no IS NULL OR rcept_no NOT LIKE '%:전기')
                            GROUP BY corp_code, fs_div, bsns_year, reprt_code"""):
        k = (r[0], r[1]); o = {"11013": 1, "11012": 2, "11014": 3}[r[3]]
        if k not in latest or (r[2], o) > (latest[k][0], latest[k][2]):
            latest[k] = (r[2], r[3], o)
    for (cc, fs), (y, rc, _) in latest.items():
        if cc not in cs:
            continue
        py = str(int(y) - 1)
        has_bs = con.execute("SELECT 1 FROM fin_raw WHERE corp_code=? AND fs_div=? AND bsns_year=? AND reprt_code=? AND sj_div='BS' LIMIT 1",
                             (cc, fs, py, rc)).fetchone()
        if not has_bs:
            todo.append((cc, fs, py, rc))
    tried = {(r[0], r[1]) for r in con.execute("SELECT corp_code, msg FROM fetch_log WHERE job='fin_yoy' AND status IN ('ok','empty')")}
    todo = [t for t in todo if (t[0], f"{t[1]} {t[2]} {t[3]}") not in tried]
    print(f"[fin-yoy] 전년동기 재무상태표 보강 대상 {len(todo)}건(연결·별도) · 병렬 {workers}", flush=True)
    n = i = 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(todo), workers * 5):
            batch = todo[b:b + workers * 5]
            futs = [(t, ex.submit(_fetch_list, t[0], t[2], t[3], t[1])) for t in batch]
            stop = False
            for (cc, fs, py, rc), f in futs:
                i += 1
                try:
                    lst = f.result()
                    if lst:
                        store_raw(con, cc, py, rc, fs, lst, prev=False)
                        build_kpi(con, cc, py, rc)
                        n += 1
                    log(con, cc, "fin_yoy", "ok" if lst else "empty", f"{fs} {py} {rc}")
                except Exception as e:  # noqa
                    log(con, cc, "fin_yoy", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 200 < workers * 5:
                print(f"[fin-yoy] {i}/{len(todo)} · 보강 {n} · DART {common.CALLS}콜", flush=True)
            if stop:
                print(f"[fin-yoy] DART 한도 도달 — {i}/{len(todo)}에서 중단", flush=True); break
    print(f"[fin-yoy] 완료 {i}/{len(todo)} · 보강 {n}", flush=True)
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
