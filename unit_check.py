"""AI 요약 금액 단위 검증(2026-10-29 사용자 요청) — 사업요약·신용평가서 요약·재무 AI 의견에 적힌 'N조원 / N억원'을 회사 재무 수치와 대조

발견 사례: 대한항공 사업요약 '단기성차입금 72.4조원' — 실제 72,356억(7.2조). 억→조 환산에서 자릿수 하나 밀림.

판단 절차(금액 하나마다)
  ① 금액을 억원으로 환산(조 = 1만 억). 'USD·달러·엔·위안'이 붙은 금액은 원화가 아니므로 제외
  ② 앞 14자 안의 계정 키워드(차입·매출·영업이익·현금·자산·부채·자본·EBITDA·순이익)로 대조할 재무 수치 후보를 고름
     후보 = 해당 계정의 연결·별도 전 기간 값 + (차입 키워드면) 재무 특이사항 문구에 계산돼 있는 억원 금액
  ③ 금액이 회사 규모 상한(자산·부채·매출 최댓값의 1.5배)을 넘거나, 후보와 25% 안으로 맞는 값이 없을 때만 의심
  ④ 의심 금액을 10·100·1000배로 나누거나 10배로 곱한 값이 후보와 3% 안에서 맞으면 → 그 값으로 고치고 원문을 괄호로 남김
  ⑤ 계정 키워드가 있고 상한을 넘는데 맞는 후보가 없으면 → 숫자는 그대로 두고 '⚠ 단위 확인 필요' 표시(추정값으로 바꾸지 않음)
"""
import re

AMT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(조|억)(\s*원)?(?!\s*(?:USD|달러|불|엔|위안|유로|\$))")
KW = [("차입", ("borrowings", "net_borrowings")), ("매출", ("revenue",)), ("영업이익", ("op_income",)), ("EBITDA", ("ebitda",)),
      ("순이익", ("net_income",)), ("현금", ("cash",)), ("자산", ("total_assets",)), ("부채", ("total_liab",)), ("자본", ("equity",))]
MONEY = ("revenue", "op_income", "net_income", "ebitda", "total_assets", "total_liab", "equity", "cash", "borrowings", "net_borrowings", "cfo", "fcf")


def _refs(kpi, notes):
    rows = (kpi or {}).get("CFS", []) + (kpi or {}).get("OFS", [])
    by = {k: [abs(r[k]) / 1e8 for r in rows if r.get(k)] for k in MONEY}
    cap = max([v for k in ("total_assets", "total_liab", "revenue") for v in by[k]] or [0]) * 1.5
    nn = []
    for n in notes or []:
        for t in (n.get("text"), n.get("text_ofs")):
            for m in re.finditer(r"(\d{1,3}(?:,\d{3})+|\d+)\s*억", t or ""):
                nn.append(float(m.group(1).replace(",", "")))
    return by, cap, nn


def _fmt(v_eok, unit, src):
    if unit == "조":
        dec = len(src.split(".")[1]) if "." in src else 1
        return f"{v_eok / 1e4:,.{max(1, dec)}f}"
    return f"{round(v_eok):,}"


def _fix_text(s, by, cap, nn, log):
    def rep(m):
        raw, unit, won = m.group(1), m.group(2), m.group(3) or ""
        v = float(raw.replace(",", "")) * (1e4 if unit == "조" else 1)
        if v <= 0:
            return m.group(0)
        ctx = s[max(0, m.start() - 14):m.start()]
        cands = []
        for kw, fields in KW:
            if kw in ctx:
                for f in fields:
                    cands += by.get(f, [])
                if kw == "차입":
                    cands += nn
        near = lambda x, tol: any(abs(x - c) <= tol * c for c in cands if c)
        over = bool(cap and v > cap and cands)   # 계정 키워드가 없는 금액(시장규모·그룹 전체 등)은 상한을 넘어도 건드리지 않음
        if not cands or (not over and near(v, 0.25)):
            return m.group(0)
        for f in (10, 100, 1000, 0.1):
            x = v / f
            if cands and near(x, 0.03) and (not cap or x <= cap):
                new = f"{_fmt(x, unit, raw)}{unit}{won}"
                log.append({"from": m.group(0).strip(), "to": new.strip(), "ctx": ctx.strip()})
                return f"{new} (AI 원문 {m.group(0).strip()} → 단위 보정)"
        if over:
            log.append({"from": m.group(0).strip(), "to": None, "ctx": ctx.strip()})
            return f"{m.group(0)} ⚠ 단위 확인 필요"
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
    by, cap, nn = _refs(d.get("kpi"), d.get("fin_notes"))
    log = []
    fn = lambda s: _fix_text(s, by, cap, nn, log)
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
                                  "key_financials_view": "2025년 매출 25.2조원, 총차입금 24.6조원, 엔진정비공장 5,780억원 투자"}}}
    print(apply(d))
    print(d["business"]["summary"])
