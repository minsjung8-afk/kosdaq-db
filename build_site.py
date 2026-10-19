"""정적 사이트 빌더: kosdaq.db → site/ (Cloudflare Pages 배포 단위)
  site/index.html            앱(검색·프로필 렌더)
  site/data/companies.json   검색 인덱스(전종목 요약 1행)
  site/data/c/{stock_code}.json  기업별 상세
"""
import json
import re
import shutil
from pathlib import Path

from common import ROOT, connect

SITE = ROOT / "site"
TEMPLATE = ROOT / "site_template" / "index.html"


def q(con, sql, *a):
    return [dict(r) for r in con.execute(sql, a)]


def _fva(con, sc):
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from analyze_report import forecast_vs_actual
        return forecast_vs_actual(con, sc)
    except Exception:  # noqa
        return []


def _shareholders(con, cc):
    """최근 사업보고서 기준 최대주주·특수관계인: 상위 5명 + 합계('계' 행이 있으면 그 값, 없으면 합산)"""
    rows = q(con, """SELECT nm, relate, stock_knd, trmend_posesn_stock_co, trmend_posesn_stock_qota_rt, bsns_year FROM shareholder
                     WHERE corp_code=? AND bsns_year=(SELECT MAX(bsns_year) FROM shareholder WHERE corp_code=?)""", cc, cc)
    if not rows:
        return {"year": None, "top": [], "total": None}
    year = rows[0]["bsns_year"]
    total_rows = [r for r in rows if (r["nm"] or "").strip() in ("계", "합계", "총계")]
    body = [r for r in rows if r not in total_rows and (r["stock_knd"] or "보통주").startswith("보통")]
    body.sort(key=lambda r: r["trmend_posesn_stock_qota_rt"] or 0, reverse=True)
    if total_rows:
        t = next((r for r in total_rows if (r["stock_knd"] or "보통주").startswith("보통")), total_rows[0])
        total = {"co": t["trmend_posesn_stock_co"], "rt": t["trmend_posesn_stock_qota_rt"]}
    else:
        total = {"co": sum(r["trmend_posesn_stock_co"] or 0 for r in body), "rt": round(sum(r["trmend_posesn_stock_qota_rt"] or 0 for r in body), 2)}
    # 외부 5% 이상 주주 합류 (이름 정규화로 중복 제거), 지분율 순 상위 5
    norm = lambda x: re.sub(r"\s|\(주\)|주식회사|㈜", "", x or "")
    merged = [{"nm": r["nm"], "relate": r["relate"] or "특수관계인", "co": r["trmend_posesn_stock_co"], "rt": r["trmend_posesn_stock_qota_rt"], "ins": True} for r in body]
    seen = {norm(r["nm"]) for r in body}
    for r in q(con, "SELECT nm, stock_co, rate FROM major_holder WHERE corp_code=? AND bsns_year=?", cc, year):
        if norm(r["nm"]) not in seen:
            merged.append({"nm": r["nm"], "relate": "외부(5%↑)", "co": r["stock_co"], "rt": r["rate"], "ins": False})
    merged.sort(key=lambda r: r["rt"] or 0, reverse=True)
    return {"year": year, "top": merged[:5], "n": len(body), "total": total}


def _ltm(rows):
    """연간 2개년 + 분기(전년동기, 최근) 행에서 LTM(최근 4분기 합산) 손익 근사.
    최근 분기 누적이 있으면: 직전 연간 + 최근 누적 - 전년 동기 누적. 없으면 최근 연간."""
    ann = [r for r in rows if r.get("kind") == "annual"]
    cur = next((r for r in rows if r.get("kind") == "interim"), None)
    prev = next((r for r in rows if r.get("kind") == "prev_interim"), None)
    if not ann:
        return None, None
    base = ann[-1]
    out = {}
    for k in ("revenue", "op_income", "net_income", "ebitda"):
        if cur and prev and cur.get(k) is not None and prev.get(k) is not None and base.get(k) is not None:
            out[k] = base[k] + cur[k] - prev[k]
        else:
            out[k] = base.get(k)
    label = f"LTM {cur['label']}" if cur and prev else f"FY{base['bsns_year']}"
    return out, label


def _valuation(kpi, ps):
    rows = kpi.get("CFS") or kpi.get("OFS") or []
    if not rows or not ps or not ps.get("mcap"):
        return None
    ltm, label = _ltm(rows)
    if not ltm:
        return None
    latest = rows[-1]                         # 최근 분기말(없으면 연간) BS
    mcap = ps["mcap"]
    nb = latest.get("net_borrowings")
    ev = mcap + (nb or 0)
    v = {"basis": label, "fs": "연결" if kpi.get("CFS") else "별도", "mcap": mcap, "ev": ev, "net_borrowings": nb,
         "ltm_net_income": ltm.get("net_income"), "ltm_op_income": ltm.get("op_income"), "ltm_ebitda": ltm.get("ebitda"),
         "per": None, "pbr": None, "ev_ebitda": None, "ev_basis": None, "psr": None}
    ni, op, eb, rev, eq = ltm.get("net_income"), ltm.get("op_income"), ltm.get("ebitda"), ltm.get("revenue"), latest.get("equity")
    if ni and ni > 0:
        v["per"] = round(mcap / ni, 1)
    if eq and eq > 0:
        v["pbr"] = round(mcap / eq, 2)
    if rev and rev > 0:
        v["psr"] = round(mcap / rev, 2)
    if eb and eb > 0:
        v["ev_ebitda"], v["ev_basis"] = round(ev / eb, 1), "EBITDA"
    elif op and op > 0:
        v["ev_ebitda"], v["ev_basis"] = round(ev / op, 1), "영업이익"
    return v


IND_STATS = {}


def _peers(con, c, ps):
    """같은 KSIC 중분류 코스닥사 중 시총이 가장 가까운 3곳 (최근 연간 KPI 비교)"""
    ind = (c.get("induty_code") or "")[:2]
    if not ind:
        return []
    rows = q(con, """SELECT c.corp_code, c.stock_code, c.corp_name, p.mcap, k.revenue, k.op_income, k.op_margin, k.debt_ratio, k.ebitda, k.net_borrowings, k.bsns_year
                     FROM company c LEFT JOIN price_summary p ON p.stock_code=c.stock_code
                     LEFT JOIN fin_kpi k ON k.corp_code=c.corp_code AND k.reprt_code='11011'
                       AND k.fs_div=(SELECT CASE WHEN EXISTS(SELECT 1 FROM fin_kpi x WHERE x.corp_code=c.corp_code AND x.fs_div='CFS' AND x.reprt_code='11011') THEN 'CFS' ELSE 'OFS' END)
                       AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi y WHERE y.corp_code=c.corp_code AND y.reprt_code='11011')
                     WHERE substr(c.induty_code,1,2)=? AND c.corp_code!=? AND k.revenue IS NOT NULL""", ind, c["corp_code"])
    me = (ps or {}).get("mcap")
    if me:
        rows.sort(key=lambda r: abs((r["mcap"] or 0) - me))
    else:
        rows.sort(key=lambda r: -(r["mcap"] or 0))
    return rows[:3]


def company_detail(con, c):
    cc, sc = c["corp_code"], c["stock_code"]
    biz = q(con, "SELECT bsns_year, rcept_no, overview_text, summary_json, summary_md, rating_text FROM business WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 1", cc)
    b = biz[0] if biz else {}
    kpi = {}
    Q = {"11013": 1, "11012": 2, "11014": 3}
    acc = int(c.get("acc_mt") or 12) if str(c.get("acc_mt") or "").strip().isdigit() else 12

    def plabel(year, rc):                       # 분기말 월 기준 라벨: 2025.06 (결산월 반영)
        mm = ((acc + 3 * Q.get(rc, 0) - 1) % 12) + 1
        return f"{year}.{mm:02d}"
    for fs in ("CFS", "OFS"):
        ann = q(con, "SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' ORDER BY bsns_year DESC LIMIT 2", cc, fs)
        ann = sorted(ann, key=lambda r: r["bsns_year"])
        latest = q(con, "SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code!='11011' ORDER BY bsns_year DESC, reprt_code DESC LIMIT 1", cc, fs)
        rows = []
        for r in ann:
            rows.append({**r, "label": r["bsns_year"], "kind": "annual"})
        if latest:
            L = latest[0]
            prev = q(con, "SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code=? AND bsns_year=?", cc, fs, L["reprt_code"], str(int(L["bsns_year"]) - 1))
            if prev:
                rows.append({**prev[0], "label": plabel(prev[0]["bsns_year"], L["reprt_code"]), "kind": "prev_interim"})
            rows.append({**L, "label": plabel(L["bsns_year"], L["reprt_code"]), "kind": "interim"})
        if rows:
            kpi[fs] = [{k: v for k, v in r.items() if k not in ("corp_code", "fs_div")} for r in rows]
    prices = q(con, "SELECT dt, open, high, low, close, volume FROM price_daily WHERE stock_code=? AND dt >= date('now','-3 years') ORDER BY dt", sc)
    ps = q(con, "SELECT * FROM price_summary WHERE stock_code=? ORDER BY asof DESC LIMIT 1", sc)
    d = {
        "company": c,
        "audit": q(con, "SELECT bsns_year, adtor, adt_opinion, emphs_matter, core_adt_matter FROM audit_opinion WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 3", cc),
        "shareholders": _shareholders(con, cc),
        "minor": (q(con, "SELECT * FROM minor_shareholder WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 1", cc) or [None])[0],
        "business": {"year": b.get("bsns_year"), "rcept_no": b.get("rcept_no"),
                     "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={b['rcept_no']}" if b.get("rcept_no") else None,
                     "overview": (b.get("overview_text") or "")[:4000], "summary": json.loads(b["summary_json"]) if b.get("summary_json") else None},
        "kpi": kpi,
        "price_summary": ps[0] if ps else None,
        "valuation": _valuation(kpi, ps[0] if ps else None),
        "flags": None,   # 아래에서 채움
        "industry_pos": None,
        "prices": [[p["dt"], p["open"], p["high"], p["low"], p["close"], p["volume"]] for p in prices],
        "rating_text": b.get("rating_text"),
        "rating_summary": (lambda rs: {**rs[0], "summary": json.loads(rs[0]["summary_json"])} if rs else None)(
            q(con, "SELECT agency, disc_dt, grade, file_url, summary_json, summary_md FROM rating_summary WHERE corp_code=?", cc)),
        "ratings": q(con, "SELECT agency, target_type, eval_kind, disc_dt, grade, watch, outlook FROM credit_rating WHERE corp_code=? ORDER BY disc_dt DESC", cc),
        "events": q(con, "SELECT rcept_dt, flag, report_nm, rcept_no FROM disclosure_event WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 20", cc),
        "reports": q(con, """SELECT m.pub_dt, m.source, m.publisher, m.analyst, m.title, m.url, m.opinion, m.target_price, m.old_target_price,
                             a.forecast_json, a.valuation_json, a.points_json, a.risks_json, a.memo_md
                             FROM report_meta m LEFT JOIN report_analysis a ON a.report_id=m.id
                             WHERE m.stock_code=? ORDER BY m.pub_dt DESC LIMIT 12""", sc),
        "forecast_vs_actual": _fva(con, sc),
        "conv_bonds": q(con, "SELECT * FROM conv_bond WHERE corp_code=? AND bsns_year=(SELECT MAX(bsns_year) FROM conv_bond WHERE corp_code=?) ORDER BY exprd_dd", cc, cc),
        "news": q(con, "SELECT dt, title, source, url FROM news WHERE corp_code=? ORDER BY dt DESC LIMIT 60", cc),
        "peers": _peers(con, c, ps[0] if ps else None),
        "other_invest": q(con, "SELECT * FROM other_invest WHERE corp_code=? AND bsns_year=(SELECT MAX(bsns_year) FROM other_invest WHERE corp_code=?) ORDER BY trmend_book_amt DESC LIMIT 8", cc, cc),
    }
    d["flags"] = _flags(c, kpi, d["price_summary"], d["audit"], d["events"], d["ratings"])
    cb_tot = sum(r["remndr_amt"] or 0 for r in d["conv_bonds"])
    ps0 = d["price_summary"] or {}
    if cb_tot and ps0.get("mcap") and cb_tot > ps0["mcap"] * 0.2:
        d["flags"].append(("CB·BW 잔액 > 시총 20%", "amber", f"잔액 {cb_tot/1e8:,.0f}억 / 시총 {ps0['mcap']/1e8:,.0f}억"))
    if ps0.get("close") and any((r["cnvrs_prc"] or 0) > ps0["close"] * 1.5 and (r["remndr_amt"] or 0) > 0 for r in d["conv_bonds"]):
        d["flags"].append(("전환가 > 주가×1.5 CB 보유", "amber", "전환 유인 낮음 → 상환·리픽싱 부담"))
    ind = (c.get("induty_code") or "")[:2]
    st = IND_STATS.get(ind)
    rows = kpi.get("CFS") or kpi.get("OFS") or []
    ann = [r for r in rows if r.get("kind") == "annual"]
    if st and ann:
        last = ann[-1]
        d["industry_pos"] = {"ind": ind, "name": c.get("sector_krx"), "n": len(st["debt_ratio"]),
                             "debt_ratio": _pctile(st["debt_ratio"], last.get("debt_ratio")),
                             "op_margin": _pctile(st["op_margin"], last.get("op_margin")),
                             "cur_ratio": _pctile(st["cur_ratio"], last.get("cur_ratio")),
                             "borrow_dep": _pctile(st["borrow_dep"], last.get("borrow_dep")),
                             "mcap": _pctile(st["mcap"], (d["price_summary"] or {}).get("mcap"))}
    return d


def _industry_stats(con):
    """KSIC 중분류(induty_code 앞 2자리)별 최근 연간 지표 분포 → 백분위 계산용 정렬 리스트"""
    rows = q(con, """SELECT c.corp_code, substr(c.induty_code,1,2) AS ind, k.debt_ratio, k.op_margin, k.cur_ratio, k.borrow_dep
                     FROM company c JOIN fin_kpi k ON k.corp_code=c.corp_code AND k.reprt_code='11011'
                     AND k.fs_div=(SELECT CASE WHEN EXISTS(SELECT 1 FROM fin_kpi x WHERE x.corp_code=c.corp_code AND x.fs_div='CFS' AND x.reprt_code='11011') THEN 'CFS' ELSE 'OFS' END)
                     AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi y WHERE y.corp_code=c.corp_code AND y.reprt_code='11011')""")
    mc = {r["stock_code"]: r["mcap"] for r in q(con, "SELECT stock_code, mcap FROM price_summary")}
    code2stock = {r["corp_code"]: r["stock_code"] for r in q(con, "SELECT corp_code, stock_code FROM company")}
    stats = {}
    for r in rows:
        d = stats.setdefault(r["ind"], {"debt_ratio": [], "op_margin": [], "cur_ratio": [], "borrow_dep": [], "mcap": []})
        for k in ("debt_ratio", "op_margin", "cur_ratio", "borrow_dep"):
            if r[k] is not None:
                d[k].append(r[k])
        m = mc.get(code2stock.get(r["corp_code"]))
        if m:
            d["mcap"].append(m)
    for d in stats.values():
        for k in d:
            d[k].sort()
    return stats


def _pctile(sorted_vals, v):
    if v is None or not sorted_vals or len(sorted_vals) < 5:
        return None
    import bisect
    return round(bisect.bisect_left(sorted_vals, v) / len(sorted_vals) * 100)


def _flags(c, kpi, ps, audit, events, ratings):
    """심사 1차 스크리닝 경고 플래그. (코드, 수준 red/amber, 설명)"""
    rows = kpi.get("CFS") or kpi.get("OFS") or []
    ann = [r for r in rows if r.get("kind") == "annual"]
    last = ann[-1] if ann else None
    latest = rows[-1] if rows else None
    out = []
    if last and last.get("capital") and last.get("equity") is not None:
        if last["equity"] < 0:
            out.append(("완전자본잠식", "red", f"자본총계 {last['equity']/1e8:,.0f}억"))
        elif last["equity"] < last["capital"]:
            out.append(("부분자본잠식", "red", f"자본총계 {last['equity']/1e8:,.0f}억 < 자본금 {last['capital']/1e8:,.0f}억"))
    if len(ann) >= 2 and all((r.get("op_income") or 0) < 0 for r in ann[-2:]):
        out.append(("연속 영업적자", "red" if (latest and (latest.get("op_income") or 0) < 0) else "amber", f"{ann[-2]['bsns_year']}~{ann[-1]['bsns_year']} 연간 영업적자"))
    elif last and (last.get("op_income") or 0) < 0:
        out.append(("최근 연간 영업적자", "amber", f"{last['bsns_year']} 영업이익 {last['op_income']/1e8:,.0f}억"))
    if last and last.get("interest_cover") is not None and last["interest_cover"] < 1:
        out.append(("EBITDA/이자비용 < 1", "red", f"{last['interest_cover']}x"))
    if last and last.get("debt_ratio") is not None and last["debt_ratio"] > 300:
        out.append(("부채비율 300% 초과", "amber", f"{last['debt_ratio']}%"))
    if last and last.get("cur_ratio") is not None and last["cur_ratio"] < 80:
        out.append(("유동비율 80% 미만", "amber", f"{last['cur_ratio']}%"))
    if last and last.get("cfo") is not None and last["cfo"] < 0 and (last.get("op_income") or 0) < 0:
        out.append(("영업CF·영업이익 동반 음수", "amber", f"영업CF {last['cfo']/1e8:,.0f}억"))
    if audit:
        a = audit[0]
        op = a.get("adt_opinion") or ""
        if op and "적정" not in op or op.startswith("한정") or "부적정" in op or "거절" in op:
            out.append(("감사의견 비적정", "red", f"{a['bsns_year']} {op}"))
        if "계속기업" in (a.get("emphs_matter") or ""):
            out.append(("계속기업 불확실성 강조", "red", f"{a['bsns_year']} 감사보고서 강조사항"))
    st = c.get("sect_tp") or ""
    if "관리" in st or "환기" in st:
        out.append((st, "red", "KRX 소속부"))
    if ps:
        if ps.get("chg_1y") is not None and ps["chg_1y"] <= -50:
            out.append(("주가 1년 −50% 이하", "amber", f"{ps['chg_1y']}%"))
        if ps.get("mcap") and last and last.get("equity") and ps["mcap"] < last["equity"] * 0.5:
            out.append(("PBR 0.5배 미만", "amber", "시총 < 자본총계×0.5"))
    from datetime import date, timedelta
    cut = (date.today() - timedelta(days=365)).strftime("%Y%m%d")
    recent = [e for e in events if (e.get("rcept_dt") or "") >= cut]
    fund = [e for e in recent if e.get("flag") in ("유상증자", "CB", "BW", "EB")]
    if len(fund) >= 2:
        out.append(("1년 내 자금조달 공시 2건+", "amber", ", ".join(sorted({e['flag'] for e in fund}))))
    elif fund:
        out.append(("1년 내 자금조달 공시", "amber", fund[0]["flag"]))
    for f, lab in (("최대주주변경", "최대주주 변경"), ("횡령배임", "횡령·배임"), ("거래정지", "거래정지"), ("회생", "회생절차"), ("소송", "소송")):
        if any(e.get("flag") == f for e in recent):
            out.append((lab + " 공시(1년)", "red" if f != "소송" else "amber", ""))
    if ratings:
        r0 = ratings[0]
        if r0.get("watch") or (r0.get("outlook") and "부정" in r0["outlook"]):
            out.append(("등급전망 부정적/감시", "amber", f"{r0['agency']} {r0['grade']} {r0.get('outlook') or ''} {r0.get('watch') or ''}"))
    return out


def build():
    con = connect()
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data" / "c").mkdir(parents=True)
    shutil.copy(TEMPLATE, SITE / "index.html")
    (SITE / "_headers").write_text("/*\n  Cache-Control: no-cache\n", encoding="utf-8")   # 매 요청 ETag 재검증 → 배포 즉시 반영

    global IND_STATS
    IND_STATS = _industry_stats(con)
    index = []
    for c in q(con, "SELECT * FROM company ORDER BY corp_name"):
        d = company_detail(con, c)
        (SITE / "data" / "c" / f"{c['stock_code']}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        _rows = d["kpi"].get("CFS") or d["kpi"].get("OFS") or [{}]
        k = next((r for r in reversed(_rows) if r.get("kind") == "annual"), _rows[-1])   # 검색 인덱스는 최근 연간 기준
        ps = d["price_summary"] or {}
        ao = d["audit"][0] if d["audit"] else {}
        LONG = ("기업신용평가", "회사채", "사채")
        cur_rt = next((r for r in d["ratings"] if any(k in (r["target_type"] or "") for k in LONG)), None)
        index.append({"code": c["stock_code"], "name": c["corp_name"], "sector": c["sector_krx"], "sect": c.get("sect_tp"), "market": c["market"],
                      "rev": k.get("revenue"), "op": k.get("op_income"), "debt": k.get("debt_ratio"),
                      "mcap": ps.get("mcap"), "opinion": ao.get("adt_opinion"), "year": k.get("bsns_year"),
                      "industry": (d["business"]["summary"] or {}).get("industry") if d["business"]["summary"] else None,
                      "rating": cur_rt["grade"] if cur_rt else None,
                      "per": (d["valuation"] or {}).get("per"), "ev_ebitda": (d["valuation"] or {}).get("ev_ebitda"),
                      "red": sum(1 for f in d["flags"] if f[1] == "red"), "amber": sum(1 for f in d["flags"] if f[1] == "amber")})
    meta = {"built_at": con.execute("SELECT datetime('now','localtime')").fetchone()[0], "count": len(index),
            "price_asof": max((r["asof"] for r in q(con, "SELECT asof FROM price_summary")), default=None)}
    (SITE / "data" / "companies.json").write_text(json.dumps({"meta": meta, "items": index}, ensure_ascii=False), encoding="utf-8")
    print(f"site/ 생성: {len(index)}개사")


if __name__ == "__main__":
    build()
