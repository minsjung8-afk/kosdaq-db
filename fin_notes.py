"""재무 특이사항(규칙 기반) — 사이트 '3. 재무현황' 오른쪽 패널 (2026-10-28 사용자 요청, 대한항공 시안)

원칙
  ① 숫자는 전부 DB(fin_kpi·borrow_bd·audit_opinion) 값으로 계산 — 원인은 단정하지 않음(원문 확인 지점만 표시)
  ② 기준: 연결(CFS)이 있으면 연결, 없으면 별도. 연결·별도 비교 규칙은 둘 다 있을 때만
  ③ 비교: 손익·현금흐름 = 같은 기간끼리(최근 분기 누적 ↔ 전년 동기, 연간 ↔ 전년), 잔액(부채비율·차입금) = 최근 ↔ 직전 연말
  ④ 등급 bad(위험) > warn(주의) > info(참고), 위험부터 최대 10개
  ⑤ 금융업(KSIC 64~66)은 부채비율·유동비율·순차입금 규칙 제외(업종 특성상 의미 없음)
반기·분기 '순차입금/EBITDA'는 최근 4분기 합산(LTM) EBITDA로 다시 계산(fix_ltm) — 반기 누적 EBITDA로 나눠 2배로 보이던 오류 수정
"""


def _e(v):
    return "-" if v is None else f"{v / 1e8:,.0f}억"


def _p(v):
    return "-" if v is None else f"{v:.1f}%"


def _chg(a, b):
    """증감률(%) — b 기준, b<=0이면 None"""
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1) * 100


def _rows(kpi, fs):
    rows = kpi.get(fs) or []
    ann = [r for r in rows if r.get("kind") == "annual"]
    cur = next((r for r in rows if r.get("kind") == "interim"), None)
    prev = next((r for r in rows if r.get("kind") == "prev_interim"), None)
    return ann, cur, prev


def fix_ltm(kpi):
    """반기·분기 열의 순차입금/EBITDA·EBITDA/이자비용을 LTM 기준으로(전년 연간 − 전년 동기 누적 + 당기 누적). 못 구하면 None."""
    for fs in ("CFS", "OFS"):
        ann, cur, prev = _rows(kpi, fs)
        for r in (cur, prev):
            if not r:
                continue
            y = str(int(r["bsns_year"]) - 1)
            a = next((x for x in ann if x["bsns_year"] == y), None)
            p = prev if r is cur else None
            ltm_e = ltm_i = None
            if a and p and a.get("ebitda") is not None and p.get("ebitda") is not None and r.get("ebitda") is not None:
                ltm_e = a["ebitda"] - p["ebitda"] + r["ebitda"]
                if a.get("int_exp") is not None and p.get("int_exp") is not None and r.get("int_exp") is not None:
                    ltm_i = a["int_exp"] - p["int_exp"] + r["int_exp"]
            r["ltm_ebitda"] = ltm_e
            nb = r.get("net_borrowings")
            r["nd_ebitda"] = round(nb / ltm_e, 1) if nb is not None and ltm_e and ltm_e > 0 else None
            if r is prev:      # 전년 동기 열은 그 전년 연간이 없어 LTM 불가 → 비움
                r["nd_ebitda"] = None
    return kpi


def _lease_share(bb_fs):
    if not bb_fs:
        return None
    col = bb_fs[-1]
    tot = col.get("total") or 0
    lease = sum((v or 0) for v in (col.get("k", {}).get("리스") or {}).values())
    cur = sum((col.get("k", {}).get(k) or {}).get("유동") or 0 for k in col.get("k", {}) if k != "리스")
    return (lease / tot * 100 if tot else None), lease, cur, col.get("cash"), (col.get("k", {}).get("리스") or {}).get("유동") or 0


def build(d, company):
    kpi = d.get("kpi") or {}
    fs = "CFS" if kpi.get("CFS") else ("OFS" if kpi.get("OFS") else None)
    if not fs:
        return []
    ks2 = (company.get("induty_code") or "")[:2]
    is_fin = ks2 in ("64", "65", "66")
    ann, cur, prev = _rows(kpi, fs)
    a1 = ann[-1] if ann else None
    a0 = ann[-2] if len(ann) >= 2 else None
    now, base = (cur, prev) if cur else (a1, a0)          # 손익 비교쌍
    ref = a1 if cur else a0                               # 잔액 비교 기준(직전 연말)
    pl = (now or {}).get("label") or (now or {}).get("bsns_year") or ""
    bl = (base or {}).get("label") or (base or {}).get("bsns_year") or ""
    rl = (ref or {}).get("label") or (ref or {}).get("bsns_year") or ""
    N = []

    def add(sev, title, text, rule):
        N.append({"sev": sev, "title": title, "text": text, "rule": rule})

    g = lambda r, k: (r or {}).get(k)
    if now:
        ni, ni0, op, op0 = g(now, "net_income"), g(base, "net_income"), g(now, "op_income"), g(base, "op_income")
        # 손익
        if op is not None and op < 0:
            if op0 is not None and op0 >= 0:
                add("bad", "영업적자 전환", f"{pl} 영업이익 {_e(op)} (전년 동기 {_e(op0)})", "영업이익 흑자→적자")
            elif op0 is not None:
                add("bad", "영업적자 지속", f"{pl} 영업이익 {_e(op)} · {bl} {_e(op0)}", "영업이익 2기 연속 적자")
        if ni is not None and ni < 0 and ni0 is not None and ni0 > 0:
            gap = f" 영업이익 {_e(op)}에서 영업외·법인세로 {_e(op - ni)} 빠짐 — 원인(외화환산·손상 등)은 주석 원문 확인 필요" if op and op > 0 else ""
            add("bad", "순손실 전환", f"{pl} 당기순이익 {_e(ni)} (전년 동기 {_e(ni0)}).{gap}", "당기순이익 흑자→적자")
        rv, rv0 = g(now, "revenue"), g(base, "revenue")
        if rv and rv0 and op is not None and op0 is not None:
            m, m0 = op / rv * 100, op0 / rv0 * 100
            if m0 - m >= 3 and not (op < 0 and op0 >= 0):
                add("warn", "수익성 하락", f"영업이익률 {_p(m0)}({bl}) → {_p(m)}({pl}), {m0 - m:.1f}%p 하락", "영업이익률 3%p 이상 하락")
        c = _chg(rv, rv0)
        if c is not None and c <= -20:
            add("warn", "매출 급감", f"매출 {_e(rv0)} → {_e(rv)} ({c:+.1f}%)", "매출 −20% 이하")
        # 연결·별도 비교
        if fs == "CFS" and kpi.get("OFS"):
            oann, ocur, oprev = _rows(kpi, "OFS")
            onow, obase = (ocur, oprev) if cur else ((oann[-1] if oann else None), (oann[-2] if len(oann) >= 2 else None))
            if onow and (onow.get("label") or onow.get("bsns_year")) == (now.get("label") or now.get("bsns_year")):
                oop = onow.get("op_income")
                if oop and oop > 0 and op is not None and op < oop * 0.9:
                    t = f"별도 영업이익 {_e(oop)} > 연결 {_e(op)} → 종속회사·연결조정 {_e(op - oop)}"
                    odr, dr = onow.get("debt_ratio"), now.get("debt_ratio")
                    if not is_fin and odr is not None and dr is not None and dr - odr >= 50:
                        t += f". 연결 부채비율 {_p(dr)} vs 별도 {_p(odr)}"
                    add("warn", "종속회사가 이익을 깎음", t, "별도 영업이익 > 연결 영업이익(10% 이상 차이)")
                c1, c2 = _chg(rv, rv0), _chg(onow.get("revenue"), (obase or {}).get("revenue"))
                if c1 is not None and c2 is not None and abs(c1 - c2) >= 20:
                    add("info", "연결범위 변동 가능성", f"매출 증가율 연결 {c1:+.1f}% vs 별도 {c2:+.1f}% — 종속회사 편입·제외 여부를 사업보고서에서 확인, 전년 비교 시 유의", "연결·별도 매출 증가율 차 20%p 이상")
        # 현금흐름(연간 기준)
        if a1:
            cfo, fcf, capex = g(a1, "cfo"), g(a1, "fcf"), g(a1, "capex")
            if cfo is not None and cfo < 0:
                add("bad" if (a0 and (a0.get("cfo") or 0) < 0) else "warn", "영업활동 현금 유출", f"{a1['bsns_year']} 영업활동CF {_e(cfo)}" + (f" · {a0['bsns_year']} {_e(a0.get('cfo'))}" if a0 else ""), "영업활동CF 적자(2년 연속이면 위험)")
            elif a1.get("net_income") and a1["net_income"] > 0 and cfo is not None and cfo < a1["net_income"] * 0.3:
                add("warn", "이익의 현금 뒷받침 부족", f"{a1['bsns_year']} 순이익 {_e(a1['net_income'])} vs 영업활동CF {_e(cfo)}", "영업활동CF < 순이익의 30%")
            if fcf is not None and fcf < 0 and not is_fin:
                cc = _chg(capex, (a0 or {}).get("capex"))
                add("info", "FCF 적자", f"{a1['bsns_year']} FCF {_e(fcf)} (영업활동CF {_e(cfo)} − Capex {_e(capex)}{f', Capex {cc:+.0f}%' if cc is not None else ''})"
                    + (f". {pl} 누적 FCF {_e(cur.get('fcf'))}" if cur and cur.get("fcf") is not None else ""), "연간 FCF 적자")
            if a0 and not is_fin:
                c_r, c_ar = _chg(a1.get("revenue"), a0.get("revenue")), _chg(a1.get("receivables"), a0.get("receivables"))
                if c_r is not None and c_ar is not None and c_ar - c_r >= 20 and (a1.get("receivables") or 0) > (a1.get("revenue") or 0) * 0.05:
                    add("warn", "매출채권이 매출보다 빨리 늘어남", f"{a1['bsns_year']} 매출 {c_r:+.1f}% vs 매출채권 {c_ar:+.1f}% ({_e(a1.get('receivables'))})", "매출채권 증가율 − 매출 증가율 20%p 이상")
                c_inv = _chg(a1.get("inventory"), a0.get("inventory"))
                if c_r is not None and c_inv is not None and c_inv - c_r >= 20 and (a1.get("inventory") or 0) > (a1.get("revenue") or 0) * 0.05:
                    add("warn", "재고가 매출보다 빨리 늘어남", f"{a1['bsns_year']} 매출 {c_r:+.1f}% vs 재고자산 {c_inv:+.1f}% ({_e(a1.get('inventory'))})", "재고 증가율 − 매출 증가율 20%p 이상")
                ie, ie0 = a1.get("int_exp"), a0.get("int_exp")
                ci = _chg(ie, ie0)
                if ci is not None and ci >= 30 and ie and ie > 1e9:
                    add("warn", "이자비용 급증", f"{a1['bsns_year']} 이자비용 {_e(ie)} ({ci:+.0f}%)", "이자비용 +30% 이상")
    # 잔액·안정성
    if now and not is_fin:
        dr, dr0 = g(now, "debt_ratio"), g(ref, "debt_ratio")
        eq = g(now, "equity")
        if eq is not None and eq < 0:
            add("bad", "완전자본잠식", f"{pl} 자본총계 {_e(eq)}", "자본총계 < 0")
        elif dr is not None and dr0 is not None and dr - dr0 >= 30:
            add("bad" if dr >= 200 else "warn", "부채비율 급등", f"부채비율 {_p(dr0)}({rl}) → {_p(dr)}({pl}), {dr - dr0:+.1f}%p. 자본총계 {_e(g(ref, 'equity'))} → {_e(eq)}", "부채비율 직전 연말 대비 +30%p 이상")
        elif dr is not None and dr >= 400:
            add("bad", "고부채", f"{pl} 부채비율 {_p(dr)}", "부채비율 400% 이상")
        crt = g(now, "cur_ratio")
        ls = _lease_share((d.get("borrow_bd") or {}).get(fs))
        if crt is not None and crt < 100:
            t = f"{pl} 유동비율 {_p(crt)}"
            ca, cl = g(now, "cur_assets"), g(now, "cur_liab")
            if ca is not None and cl is not None:
                t += f". 유동부채가 유동자산보다 {_e(cl - ca)} 많음"
            if ls and ls[2] is not None and ls[3] is not None:
                st = ls[2] + ls[4]
                if st > ls[3]:
                    t += f". 1년 내 상환 차입(리스 포함) {_e(st)} vs 현금 {_e(ls[3])}"
            add("bad" if crt < 70 else "warn", "단기 유동성 부족", t, "유동비율 100% 미만(70% 미만이면 위험)")
        b, b0 = g(now, "borrowings"), g(ref, "borrowings")
        cb = _chg(b, b0)
        if cb is not None and cb >= (10 if cur else 20) and b and b > 1e10:
            add("warn", "차입 증가", f"총차입금 {_e(b0)}({rl}) → {_e(b)}({pl}), {cb:+.1f}%", f"총차입금 {'직전 연말 대비 +10%' if cur else '전년 대비 +20%'} 이상")
        nde = g(now, "nd_ebitda")
        e_ltm = g(now, "ltm_ebitda") if cur else g(now, "ebitda")
        nb = g(now, "net_borrowings")
        if nb is not None and nb > 0 and e_ltm is not None and e_ltm <= 0:
            add("bad", "EBITDA 적자 속 순차입", f"{pl} 순차입금 {_e(nb)} · {'LTM ' if cur else ''}EBITDA {_e(e_ltm)}", "EBITDA ≤ 0 이면서 순차입금 > 0")
        elif nde is not None and nde >= 5:
            hist = " → ".join(f"{_x(r.get('nd_ebitda'))}({r['bsns_year']})" for r in ann if r.get("nd_ebitda") is not None)
            add("bad" if nde >= 8 else "warn", "상환능력 약화", f"순차입금/{'LTM ' if cur else ''}EBITDA {nde:.1f}x" + (f" · 연간 {hist}" if hist else ""), "순차입금/EBITDA 5배 이상(8배 이상이면 위험)")
        ic = g(a1, "interest_cover")
        if ic is not None and ic < 3:
            add("bad" if ic < 1.5 else "warn", "이자 부담", f"{a1['bsns_year']} EBITDA/이자비용 {ic:.1f}x", "EBITDA/이자비용 3배 미만(1.5배 미만이면 위험)")
        bd = g(now, "borrow_dep")
        if bd is not None and bd >= 50:
            add("warn", "차입금의존도 높음", f"{pl} 차입금의존도 {_p(bd)}", "차입금의존도 50% 이상")
        if ls and ls[0] is not None and ls[0] >= 40 and ls[1] > 1e10:
            add("info", "리스부채 비중 큼", f"리스부채 {_e(ls[1])} = 총차입금의 {ls[0]:.1f}%" + (f". 리스 제외 순차입금 {_e(nb - ls[1])}" if nb is not None else ""), "리스부채 비중 40% 이상")
    # 감사의견
    au = (d.get("audit") or [None])[0]
    if au:
        op_ = au.get("adt_opinion") or ""
        if op_ and "적정" not in op_.replace("부적정", "") or "부적정" in op_ or "한정" in op_ or "거절" in op_:
            add("bad", "감사의견 비적정", f"{au.get('bsns_year')} {op_} ({au.get('adtor') or '-'})", "감사의견 적정 아님")
        if "계속기업" in (au.get("emphs_matter") or ""):
            add("bad", "계속기업 불확실성", f"{au.get('bsns_year')} 강조사항에 계속기업 관련 기재", "감사보고서 강조사항 '계속기업'")
    order = {"bad": 0, "warn": 1, "info": 2}
    seen, out = set(), []
    for n in sorted(N, key=lambda n: order[n["sev"]]):
        if n["title"] in seen:
            continue
        seen.add(n["title"])
        out.append(n)
    return out[:10]


def _x(v):
    return "-" if v is None else f"{v:.1f}x"
