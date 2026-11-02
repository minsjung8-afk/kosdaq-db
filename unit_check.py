"""AI 요약 금액 단위 검증(2026-10-29 사용자 요청) — 사업요약·신용평가서 요약·재무 AI 의견에 적힌 'N조원 / N억원'을 회사 재무 수치와 대조

발견 사례: 대한항공 사업요약 '단기성차입금 72.4조원' — 실제 72,356억(7.2조). 억→조 환산에서 자릿수 하나 밀림.

판단 절차(금액 하나마다)
  ① 금액을 억원으로 환산(조 = 1만 억). 'USD·달러·엔·위안'이 붙은 금액은 원화가 아니므로 제외
  ② 앞 14자 안에서 금액에 가장 가까운 계정 키워드 하나(차입·매출·영업이익·현금·자산·부채·자본·EBITDA·순이익)로 대조할 재무 수치 후보를 고름
     후보 = 해당 계정의 연결·별도 전 기간 값 + (차입 키워드면) 재무 특이사항 문구에 계산돼 있는 억원 금액
  ③ 금액이 회사 규모 상한(자산·부채·매출 최댓값의 1.5배)을 넘거나, 후보와 25% 안으로 맞는 값이 없을 때만 의심
  ④ 의심 금액(후보 최댓값의 2배 이상)을 10·100·1000배로 나눈 값이 후보와 3% 안에서 맞거나, 상한을 넘으면 → '⚠ 단위 확인 필요' 표시
  ⑤ (2026-11-02 사용자 지시로 변경) 의심 금액은 '경고만 붙여 그대로 노출'하지 않는다 — 266건 전수 검토 결과 모두 원문 금액이 그 계정의 DB 최대값의 2배 이상이거나
     회사 규모 상한을 넘는 값이었음(E8 '매출 1,655억'↔DB 17억, SV인베스트먼트 '창업투자자산 71,590억'↔자산총계 956억, 경동나비엔 '차입금 342.4조' 등 = 백만원·천원을 억으로 읽은 AI 오류)
     ⑤-1 ④의 배수 일치(÷10·100·1000이 같은 계정 DB 값과 3% 이내)가 있으면 → 그 값으로 바꾸고 '(원문 X 정정)' 표시
     ⑤-2 일치 없이 회사 규모 상한을 넘으면 → 숫자를 지우고 '[금액 삭제: 회사 규모 초과]' 표시(틀린 숫자보다 빈칸)
"""
import re

# 2026-11-02: '1,300.3억'(쉼표+소수)·'1, 731.2억'(AI가 쉼표 뒤 띄어씀)이 '300.3억'·'731.2억'으로 잘려 읽히던 문제 → 숫자 중간에서 시작 금지, 쉼표 뒤 공백·소수 허용
#             '1조 8,000억원'은 한 금액(1.8조)으로 읽음
AMT = re.compile(r"(?<![\d.,])(?:(\d+)\s*조\s+(?=\d{1,3}(?:,\d{3})*\s*억))?(\d{1,3}(?:,\s?\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(조|억)(\s*원)?(?!\s*(?:USD|달러|불|엔|위안|유로|\$))")
KW = [("현금성자산", ("cash",)), ("현금흐름", ("cfo", "fcf")), ("매출총이익", ()), ("차입", ("borrowings", "net_borrowings")), ("매출", ("revenue",)), ("영업이익", ("op_income",)), ("EBITDA", ("ebitda",)),
      ("순이익", ("net_income",)), ("현금", ("cash",)), ("자산", ("total_assets",)), ("부채", ("total_liab",)), ("자본", ("equity",))]
_OTHER = re.compile(r"운용자산|AUM|수탁|일임|시장|산업|업계|그룹|재계|공정(?:거래|위)|계열|자회사|종속|관계사|지분법|시가총액|자산규모|하이닉스|모회사|모기업|지주사|최대주주|대주주|"
                    r"법인|Bank|Securities|은행|보험|발주처|매출처|고객사|거래처|누적|업체|경쟁사|정부|국내\s*총|전체\s*시장")
# 2026-11-02 전수 검토(268건): 다른 주체의 금액(리츠 시장 총자산·재계 순위 자산·그룹형지 차입금·발주처 매출·북미법인 총자산)을 이 회사 값으로 정정·삭제한 오류 → 앞 60자 안에 위 단어가 있으면 대조 안 함
# 부분 금액(부문·제품·내수 매출, 매출채권, 건설중인자산, 리스부채 등)은 전체 계정과 비교할 수 없음 → 정정 안 함(회사 규모 상한 초과일 때만 삭제)
_PART = re.compile(r"매출채권|매출원가|중단영업|외화|리스|건설중인|렌탈|부문|사업부|제품\s*매출|상품\s*매출|서비스\s*매출|용역\s*매출|내수|수출|프로젝트|신규\s*매출|해외|"
                   r"분기\s*매출|\d분기|상반기|하반기")
_YEAR = re.compile(r"(?<!\d)(20\d{2}|\d{2})\s*년(?!\s*(?:간|대|이상|이내|내|만기|후))")
MONEY = ("revenue", "op_income", "net_income", "ebitda", "total_assets", "total_liab", "equity", "cash", "borrowings", "net_borrowings", "cfo", "fcf")


def _refs(kpi, notes):
    rows = (kpi or {}).get("CFS", []) + (kpi or {}).get("OFS", [])
    by = {k: [abs(r[k]) / 1e8 for r in rows if r.get(k)] for k in MONEY}
    byy = {k: {} for k in MONEY}
    for r in rows:
        for k in MONEY:
            if r.get(k):
                byy[k].setdefault(str(r.get("bsns_year") or ""), []).append(abs(r[k]) / 1e8)
    cap = max([v for k in ("total_assets", "total_liab", "revenue") for v in by[k]] or [0]) * 1.5
    nn = []
    for n in notes or []:
        for t in (n.get("text"), n.get("text_ofs")):
            for m in re.finditer(r"차입[^\d]{0,15}?(\d{1,3}(?:,\d{3})+|\d+)\s*억", t or ""):   # 2026-11-02: '차입' 바로 뒤 금액만(주석 속 아무 금액과 우연히 맞는 것 방지)
                nn.append(float(m.group(1).replace(",", "")))
    return by, cap, nn, byy


def _fmt(v_eok, unit, src):
    if unit == "조" and v_eok >= 1e4:
        dec = len(src.split(".")[1]) if "." in src else 1
        return f"{v_eok / 1e4:,.{max(1, dec)}f}조"
    if v_eok < 10:
        return f"{v_eok:,.1f}억"            # 2026-11-02: '0억'(노브메타파마 0.35억) 방지
    return f"{round(v_eok):,}억"


def _fix_text(s, by, cap, nn, log, byy=None):
    byy = byy or {}

    def rep(m):
        jo, raw, unit, won = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        v = float(raw.replace(",", "").replace(" ", "")) * (1e4 if unit == "조" else 1) + (float(jo) * 1e4 if jo else 0)
        if v <= 0:
            return m.group(0)
        ctx = s[max(0, m.start() - 14):m.start()]
        if _OTHER.search(s[max(0, m.start() - 60):m.start()]):
            return m.group(0)                    # 다른 주체(시장·그룹·발주처·자회사 등)의 금액 — 이 회사 재무와 대조할 대상이 아님
        # 금액 바로 앞에 가장 가까이 나온 계정 키워드 하나만 씀('현금성자산'은 현금 — '자산'으로 읽으면 자산총계와 맞춰져 오보정, 2026-10-29 대한항공 5.4조→54조 사례)
        hits = [(ctx.rfind(k) + len(k), len(k), k, fields) for k, fields in KW if k in ctx]
        if not hits:
            return m.group(0)
        end, _, kw, fields = max(hits)
        gap = ctx[end:]
        # 2026-11-02: 키워드와 금액 사이가 멀거나 다른 금액이 끼면(메리츠 '당기순이익 기준. 장기보험 7조', 삼립 '순차입금도 5,165억원→ 3,192억원') 그 키워드의 금액이 아님
        if len(gap) > 8 or re.search(r"\d\s*[조억]", gap):
            return m.group(0)
        part = bool(_PART.search(s[max(0, m.start() - 16):m.start()]))
        cands = [c for f in fields for c in by.get(f, [])] + (nn if kw == "차입" else [])
        over = bool(cap and v > cap and cands)
        # 연도: 금액 앞 25자 안의 'YYYY년'·'YY년' — 그 연도 값이 DB에 있으면 그 연도 값하고만 대조, DB에 없는 연도면 확인할 근거가 없으므로 손대지 않음(에코프로비엠 '2023년 매출 6.9조'·롯데지주 '2020년 말 순차입금 19조')
        ys = _YEAR.findall(s[max(0, m.start() - 25):m.start()])
        yr = None
        if ys:
            yr = ys[-1] if len(ys[-1]) == 4 else "20" + ys[-1]
            yc = [c for f in fields for c in byy.get(f, {}).get(yr, [])]
            if not yc:
                return m.group(0)
            cands_fix = yc
        else:
            kv = [c for f in fields for c in by.get(f, [])]
            # 연도 없이 맞춘 값이 그 계정 규모의 절반도 안 되면 우연 일치(한온시스템 '차입금 4.6조'→4,600억) — 정정 안 함. 재무 특이사항에 '차입 … N억'으로 계산돼 있는 금액은 직접 근거로 인정(대한항공 1년 내 상환 차입 72,356억)
            cands_fix = [c for c in kv if c and c >= 0.5 * max(kv)] + (nn if kw == "차입" else [])
        near = lambda x, cs, tol: any(abs(x - c) <= tol * c for c in cs if c)
        if not cands or (not over and near(v, cands, 0.25)):
            return m.group(0)
        if v < 2 * max(cands) and not over:      # 아래로만 보정(AI 오류는 자릿수를 키우는 쪽) — 후보보다 2배 이상 클 때만
            return m.group(0)
        info = {"from": m.group(0).strip(), "ctx": ctx.strip(), "kw": kw, "yr": yr, "part": part, "ref": sorted({round(c) for c in cands if c})[-4:], "cap": round(cap)}
        if not part:
            for f in (10, 100, 1000):
                x = v / f
                if near(x, cands_fix, 0.03) and (not cap or x <= cap):
                    # 원문 금액이 같은 계정 DB 최대값의 2배 이상이라 원문은 틀린 값이 확실 — 배수로 나눈 값이 (같은 연도의) DB 값과 3% 이내면 그 값으로 정정(⑤-1)
                    g = _fmt(x, unit, raw)
                    log.append({**info, "to": g, "guess": g})
                    return f"{g}원(원문 {m.group(0).strip()} 정정)"
        if over:
            log.append({**info, "to": "삭제"})
            return "[금액 삭제: 회사 규모 초과]"
        return m.group(0)
    return AMT.sub(rep, s)


def _walk(o, fn):
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, list):
        return [_walk(x, fn) for x in o]
    if isinstance(o, dict):
        return {k: (v if str(k).startswith("_") else _walk(v, fn)) for k, v in o.items()}
    return o


def apply(d):
    """d(회사 상세)의 AI 텍스트를 제자리에서 보정. 반환: 보정·표시 건수 목록"""
    by, cap, nn, byy = _refs(d.get("kpi"), d.get("fin_notes"))
    log = []
    fn = lambda s: _fix_text(s, by, cap, nn, log, byy)
    b = d.get("business") or {}
    if isinstance(b.get("summary"), dict):
        b["summary"] = _walk(b["summary"], fn)
        if log:
            b["summary"]["_unit_fix"] = list(log)
    rs = d.get("rating_summary")
    if rs and isinstance(rs.get("summary"), dict):
        n0 = len(log)
        rs["summary"] = _walk(rs["summary"], fn)
        if len(log) > n0:
            rs["summary"]["_unit_fix"] = log[n0:]
    fa = d.get("fin_ai")
    if fa:
        fa["summary"] = _walk(fa.get("summary"), fn)
        fa["causes"] = _walk(fa.get("causes"), fn)
    return log


if __name__ == "__main__":   # 대한항공 사례 자가 점검
    d = {"kpi": {"CFS": [{"total_assets": 52824400000000, "total_liab": 42059100000000, "revenue": 25225500000000, "borrowings": 24646600000000, "cash": 2601200000000}]},
         "fin_notes": [{"text": "유동비율 58.7%. 1년 내 상환 차입(리스 포함) 72,356억 vs 현금 26,012억"}],
         "business": {"summary": {"key_risks": ["단기성차입금 72.4조원(2026.6월)으로 단기 상환 부담", "항공유 매입액 292억 USD"],
                                  "key_financials_view": "2025년 매출 25.2조원, 총차입금 24.6조원, 현금성자산 5.4조원, 엔진정비공장 5,780억원 투자"}}}
    print(apply(d))
    print(d["business"]["summary"])
