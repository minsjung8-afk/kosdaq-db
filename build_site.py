"""정적 사이트 빌더: kosdaq.db → site/ (Cloudflare Pages 배포 단위)
  site/index.html            앱(검색·프로필 렌더)
  site/data/companies.json   검색 인덱스(전종목 요약 1행)
  site/data/c/{stock_code}.json  기업별 상세
"""
import json
import re
import shutil
import sys
from pathlib import Path

from common import ROOT, connect

SITE = ROOT / "site"
TEMPLATE = ROOT / "site_template" / "index.html"


def q(con, sql, *a):
    return [dict(r) for r in con.execute(sql, a)]


def _safe_q(con, sql, *a):
    try:
        return q(con, sql, *a)
    except Exception:  # noqa
        return []


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


def _fs_break(con, cc):
    """감사보고서 연도 연쇄 검증 경고(올해 보고서의 전기 ≠ 전년 보고서의 당기, 합병·분할 등 법인 연속성 단절) → {fs: 단절 직전 연도}.
    예: 삼표산업 2023년 (주)삼표와 합병 — 2023년 보고서의 전기(2022)는 (주)삼표 수치, 2022년 보고서는 舊 삼표산업 → {'OFS': '2022'} (2026-10-26)"""
    out = {}
    try:
        for (msg,) in con.execute("SELECT msg FROM fetch_log WHERE corp_code=? AND job='audit_fs_warn' AND msg LIKE '%연도연쇄 불일치%'", (cc,)):
            m = re.match(r"(\d{4}) (CFS|OFS)", msg or "")
            y = re.search(r"연도연쇄 불일치\((\d{4}) 당기", msg or "")
            if m and y:
                out[m.group(2)] = max(out.get(m.group(2), "0000"), y.group(1))
    except Exception:  # noqa
        pass
    return out


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




def _safe(fn):
    try:
        return fn()
    except Exception:
        return []

def _quarters(con, cc):
    """fin_kpi의 연간·분기(누적) 행에서 분기별 손익을 복원 → 최근 최대 12분기 [{label, rev, op, cogs, nd, ltm_rev, ltm_op, ltm_cogs, ltm_ebitda}].
    1Q=1Q누적, 2Q=반기−1Q, 3Q=3Q누적−반기, 4Q=연간−3Q누적. 누락 분기는 None. LTM은 연속 4분기 합(없으면 None)."""
    out = {}
    for fs in ("CFS", "OFS"):
        rows = q(con, "SELECT bsns_year, reprt_code, revenue, op_income, ebitda, net_borrowings, cogs FROM fin_kpi WHERE corp_code=? AND fs_div=? ORDER BY bsns_year, reprt_code", cc, fs)
        if not rows:
            continue
        by = {(r["bsns_year"], r["reprt_code"]): r for r in rows}
        years = sorted({r["bsns_year"] for r in rows})
        qs = []
        for y in years:
            cum = {"1": by.get((y, "11013")), "2": by.get((y, "11012")), "3": by.get((y, "11014")), "4": by.get((y, "11011"))}
            prevc = None
            for qn in ("1", "2", "3", "4"):
                r = cum[qn]
                if r is None:
                    prevc = None if qn != "1" else prevc
                    qs.append({"label": f"{y[2:]}.{qn}Q", "rev": None, "op": None, "ebitda": None, "cogs": None, "nd": None, "_y": y, "_q": qn})
                    continue
                def diff(k):
                    if r[k] is None:
                        return None
                    if qn == "1":
                        return r[k]
                    if prevc is None or prevc[k] is None:
                        return None
                    return r[k] - prevc[k]
                qs.append({"label": f"{y[2:]}.{qn}Q", "rev": diff("revenue"), "op": diff("op_income"), "ebitda": diff("ebitda"), "cogs": diff("cogs"), "nd": r["net_borrowings"], "_y": y, "_q": qn})
                prevc = r
        # 뒤에서부터 값 있는 분기만 남기되 순서 유지, 최근 12개
        while qs and qs[-1]["rev"] is None:
            qs.pop()
        qs = qs[-12:]
        while qs and qs[0]["rev"] is None:
            qs.pop(0)
        for i, qx in enumerate(qs):
            win = qs[i - 3:i + 1] if i >= 3 else []
            if len(win) == 4 and all(w["rev"] is not None and w["op"] is not None for w in win):
                qx["ltm_rev"] = sum(w["rev"] for w in win); qx["ltm_op"] = sum(w["op"] for w in win)
                qx["ltm_ebitda"] = sum(w["ebitda"] for w in win) if all(w["ebitda"] is not None for w in win) else None
                qx["ltm_cogs"] = sum(w["cogs"] for w in win) if all(w["cogs"] is not None for w in win) else None
            else:
                qx["ltm_rev"] = qx["ltm_op"] = qx["ltm_ebitda"] = qx["ltm_cogs"] = None
            qx.pop("_y", None); qx.pop("_q", None)
        if any(qx["rev"] is not None for qx in qs):
            out[fs] = qs
    return out

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



_NAME_IDX = None
def _norm_name(n):
    return re.sub(r"\(주\)|㈜|주식회사|\(유\)|\s+", "", n or "").strip()

def _eb_target(con, c, rows):
    """EB(교환사채)는 교환대상 주식 기준으로 내가격/외가격을 봐야 함. 교환대상이 자기주식이면 발행사 주가,
    타사 상장주식이면 그 회사의 최근 종가를 ex_close로 붙인다(비상장·미매칭이면 ex_close 없음 → 화면에서 판단 보류)."""
    global _NAME_IDX
    for r in rows:
        if r.get("kind") != "EB":
            continue
        tgt = r.get("stock_knd") or ""
        r["ex_tgt"] = tgt
        if "자기주식" in tgt or "자사주" in tgt or (_norm_name(c["corp_name"]) and _norm_name(c["corp_name"]) in _norm_name(tgt)):
            r["ex_self"] = 1
            continue
        if _NAME_IDX is None:
            _NAME_IDX = {_norm_name(x["corp_name"]): x["stock_code"] for x in q(con, "SELECT corp_name, stock_code FROM company")}
        key = _norm_name(re.sub(r"보통주|우선주|기명식|주권|\d+주", "", tgt))
        sc2 = _NAME_IDX.get(key) or next((v for k, v in _NAME_IDX.items() if len(k) >= 2 and (k in key or key in k)), None)
        if sc2:
            px = q(con, "SELECT dt, close FROM price_daily WHERE stock_code=? AND close>0 ORDER BY dt DESC LIMIT 1", sc2)
            if px:
                r["ex_code"], r["ex_close"], r["ex_asof"] = sc2, px[0]["close"], px[0]["dt"]
    return rows

def _peers(con, c, ps):
    """같은 시장·KSIC 중분류 내 시총이 가장 가까운 3곳 (최근 연간 KPI 비교)"""
    ind = (c.get("induty_code") or "")[:2]
    if not ind:
        return []
    rows = q(con, """SELECT c.corp_code, c.stock_code, c.corp_name, p.mcap, k.revenue, k.op_income, k.op_margin, k.debt_ratio, k.ebitda, k.net_borrowings, k.bsns_year
                     FROM company c LEFT JOIN price_summary p ON p.stock_code=c.stock_code
                       AND p.asof=(SELECT MAX(asof) FROM price_summary z WHERE z.stock_code=c.stock_code)
                     LEFT JOIN fin_kpi k ON k.corp_code=c.corp_code AND k.reprt_code='11011'
                       AND k.fs_div=(SELECT CASE WHEN EXISTS(SELECT 1 FROM fin_kpi x WHERE x.corp_code=c.corp_code AND x.fs_div='CFS' AND x.reprt_code='11011') THEN 'CFS' ELSE 'OFS' END)
                       AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi y WHERE y.corp_code=c.corp_code AND y.reprt_code='11011')
                     WHERE substr(c.induty_code,1,2)=? AND c.market=? AND c.corp_code!=? AND k.revenue IS NOT NULL""", ind, c.get("market"), c["corp_code"])
    me = (ps or {}).get("mcap")
    if me:
        rows.sort(key=lambda r: abs((r["mcap"] or 0) - me))
    else:
        rows.sort(key=lambda r: -(r["mcap"] or 0))
    return rows[:3]



def _compact_prices(rows, cut):
    """최근 3년은 일봉 그대로, 그 이전은 주봉(월요일 기준)으로 축약 — 전체기간 차트용 용량 절감"""
    import datetime as _dt
    old, new = [r for r in rows if r["dt"] < cut], [r for r in rows if r["dt"] >= cut]
    out, cur, key = [], None, None
    for r in old:
        d = _dt.date.fromisoformat(r["dt"]); k = (d - _dt.timedelta(days=d.weekday())).isoformat()
        if k != key:
            if cur: out.append(cur)
            cur = dict(r); key = k
        else:
            cur["high"] = max(cur["high"] or 0, r["high"] or 0); cur["low"] = min(cur["low"] or 1e18, r["low"] or 1e18)
            cur["close"] = r["close"]; cur["volume"] = (cur["volume"] or 0) + (r["volume"] or 0)
    if cur: out.append(cur)
    return out + new

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
        latest = q(con, "SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code!='11011' ORDER BY bsns_year DESC, CASE reprt_code WHEN '11014' THEN 3 WHEN '11012' THEN 2 ELSE 1 END DESC LIMIT 1", cc, fs)   # 3Q>반기>1Q (코드 순서와 다름)
        # 분기·반기 재무가 없는 회사(감사보고서만 있는 비상장사 등)는 최근 감사보고서 연도 포함 3개년(2026-10-26), 그 외 연간 2개년 + 최근 분기
        ann = q(con, "SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' ORDER BY bsns_year DESC LIMIT ?", cc, fs, 2 if latest else 3)
        ann = sorted(ann, key=lambda r: r["bsns_year"])
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
    ann5 = {}                                    # 분기 재무가 없는 회사의 연간 손익 추이 그래프용 최근 5개년(2026-10-26, 가벼운 필드만)
    for fs in ("CFS", "OFS"):
        a5 = q(con, "SELECT bsns_year, revenue, op_income, net_borrowings, ebitda FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' AND revenue IS NOT NULL ORDER BY bsns_year DESC LIMIT 5", cc, fs)
        if len(a5) >= 2:
            ann5[fs] = sorted(a5, key=lambda r: r["bsns_year"])
    from prices import adjusted_rows  # noqa  수정주가(분할·병합 반영)
    prices = _compact_prices(adjusted_rows(con, sc), con.execute("SELECT date('now','-3 years')").fetchone()[0])   # 최근 3년 일봉 + 그 이전 주봉
    ps = q(con, "SELECT * FROM price_summary WHERE stock_code=? ORDER BY asof DESC LIMIT 1", sc)
    # 대용가는 subst_price(적용일별 이력)에서 최신값 — price_summary는 시세 수집 때 새 asof 행이 생기며 대용가가 비는 문제(2026-10-22) 방지
    try:
        sp = con.execute("SELECT sb_prc, trd_dd FROM subst_price WHERE stock_code=? ORDER BY trd_dd DESC LIMIT 1", (sc,)).fetchone()
        if ps and sp:
            ps[0] = dict(ps[0]); ps[0]["subst_price"], ps[0]["subst_dd"] = sp[0], sp[1]
    except Exception:  # noqa
        pass
    d = {
        "company": {**c, "adres": re.sub(r"&nbsp;?|\s+", " ", c.get("adres") or "").strip()},   # DART 주소의 &nbsp 제거
        "audit": q(con, "SELECT bsns_year, adtor, adt_opinion, emphs_matter, core_adt_matter FROM audit_opinion WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 3", cc),
        "review": _review(con, cc),
        "shareholders": _shareholders(con, cc),
        "minor": (q(con, "SELECT * FROM minor_shareholder WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 1", cc) or [None])[0],
        "business": {"year": b.get("bsns_year"), "rcept_no": b.get("rcept_no"),
                     "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={b['rcept_no']}" if b.get("rcept_no") else None,
                     "overview": (b.get("overview_text") or "")[:12000], "summary": json.loads(b["summary_json"]) if b.get("summary_json") else None},
        "kpi": kpi,
        "annual5": ann5,
        "fs_break": _fs_break(con, cc),
        "quarters": _quarters(con, cc),
        "major_reports": q(con, "SELECT rcept_no, rcept_dt, repror, report_resn, stkrt, stkrt_irds, ctr_stkqy, ctr_stkrt FROM major_report WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 12", cc),
        "price_summary": ps[0] if ps else None,
        "notes_raw": _notes_raw(con, cc),
        "borrow_bd": _safe(lambda: _borrow_bd(con, cc)),
        "notes_struct": (lambda r: {"rcept_no": r[0]["rcept_no"], "report_nm": r[0]["report_nm"], **json.loads(r[0]["json"])} if r else None)(
            _safe_q(con, "SELECT rcept_no, report_nm, json FROM notes_struct WHERE corp_code=?", cc)),
        "valuation": _valuation(kpi, ps[0] if ps else None),
        "flags": None,   # 아래에서 채움
        "industry_pos": None,
        "prices": [[p["dt"], p["open"], p["high"], p["low"], p["close"], p["volume"]] for p in prices],
        "rating_text": b.get("rating_text"),
        "rating_summary": (lambda rs: {**rs[0], "summary": json.loads(rs[0]["summary_json"])} if rs else None)(
            q(con, "SELECT agency, disc_dt, grade, file_url, summary_json, summary_md FROM rating_summary WHERE corp_code=? AND COALESCE(grade,'') NOT IN ('취소','철회','WD','NR','-')", cc)),   # 등급취소 요약은 사용 안 함(사업보고서 요약/원문으로 대체)
        "ratings": _safe_q(con, """SELECT r.agency, r.target_type, r.eval_kind, r.disc_dt, r.grade, r.watch, r.outlook, r.file_url, v.valid_to
                                   FROM credit_rating r LEFT JOIN rating_valid v ON v.file_url = r.file_url WHERE r.corp_code=? ORDER BY r.disc_dt DESC""", cc)
                   or q(con, "SELECT agency, target_type, eval_kind, disc_dt, grade, watch, outlook, file_url FROM credit_rating WHERE corp_code=? ORDER BY disc_dt DESC", cc),   # valid_to: 평가서 기재 유효기간(2026-10-26)
        "events": q(con, "SELECT rcept_dt, flag, report_nm, rcept_no FROM disclosure_event WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 20", cc),
        "reports": q(con, """SELECT m.pub_dt, m.source, m.publisher, m.analyst, m.title, m.url, m.opinion, m.target_price, m.old_target_price,
                             a.forecast_json, a.valuation_json, a.points_json, a.risks_json, a.memo_md
                             FROM report_meta m LEFT JOIN report_analysis a ON a.report_id=m.id
                             WHERE m.stock_code=? ORDER BY m.pub_dt DESC LIMIT 12""", sc),
        "forecast_vs_actual": _fva(con, sc),
        "conv_bonds": [],
        "mezz_out": q(con, "SELECT * FROM mezz_outstanding WHERE corp_code=? ORDER BY kind, exprd_dd", cc),
        "mezz_events": _safe(lambda: q(con, "SELECT rcept_no, rcept_dt, kind, round, amount, shares, price, report_nm FROM mezz_event WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 30", cc)),
        "mezz_issue": _eb_target(con, c, q(con, "SELECT * FROM mezz_issue WHERE corp_code=? ORDER BY COALESCE(bddd, pymd, rcept_no) DESC", cc)),
        "news": q(con, "SELECT dt, title, source, url FROM news WHERE corp_code=? ORDER BY dt DESC LIMIT 60", cc),
        "peers": _peers(con, c, ps[0] if ps else None),
        "other_invest": q(con, """SELECT * FROM other_invest WHERE corp_code=? AND COALESCE(rcept_no,'')||bsns_year||COALESCE(reprt_code,'11011') =
                               (SELECT COALESCE(rcept_no,'')||bsns_year||COALESCE(reprt_code,'11011') FROM other_invest WHERE corp_code=?
                                ORDER BY COALESCE(rcept_no,'') DESC, bsns_year DESC, CASE COALESCE(reprt_code,'11011') WHEN '11014' THEN 3 WHEN '11012' THEN 2 WHEN '11013' THEN 1 ELSE 0 END DESC LIMIT 1)
                               ORDER BY trmend_book_amt DESC LIMIT 8""", cc, cc),   # 최신 보고서(접수번호 최대) 세트
    }
    d["flags"] = _flags(c, kpi, d["price_summary"], d["audit"], d["events"], d["ratings"])
    rv = d.get("review")
    if rv and rv.get("opinion") and not re.match(r"^\s*적정", rv["opinion"]):     # 반기·분기 검토의견 비적정 — 다음 연말 감사의견의 선행 신호
        d["flags"].insert(0, (f"{rv['period']} 검토의견 비적정", "red", f"{rv['bsns_year']} {rv['period']} {rv['opinion']} ({rv.get('adtor') or ''})"))
    halt = [r for r in prices[-60:] if r["close"] and not r["volume"] and r["open"] == r["high"] == r["low"] == r["close"]]   # 거래정지일(시·고·저 평탄화된 행)
    if prices and prices[-1] in halt:
        n = 0
        for r in reversed(prices):
            if r in halt: n += 1
            else: break
        d["flags"].insert(0, ("거래정지", "red", f"{prices[-n]['dt']}부터 {n}거래일 연속 거래 없음(종가 {prices[-1]['close']:,.0f}원 고정)"))
    cb_tot = sum(r["remndr_amt"] or 0 for r in d["mezz_out"])
    ps0 = d["price_summary"] or {}
    if cb_tot and ps0.get("mcap") and cb_tot > ps0["mcap"] * 0.2:
        d["flags"].append(("CB·BW 잔액 > 시총 20%", "amber", f"잔액 {cb_tot/1e8:,.0f}억 / 시총 {ps0['mcap']/1e8:,.0f}억"))
    if ps0.get("close") and any((r["cv_prc"] or 0) > ps0["close"] * 1.5 and (r["remndr_amt"] or 0) > 0 for r in d["mezz_out"]):
        d["flags"].append(("전환가 > 주가×1.5 메자닌 보유", "amber", "전환 유인 낮음 → 조기상환(풋)·리픽싱 부담"))
    if ps0.get("close") and any((r["lw_prc"] or 0) and r["lw_prc"] < ps0["close"] * 0.8 and r["kind"] in ("CB", "BW") for r in d["mezz_issue"]):
        d["flags"].append(("리픽싱 최저가 < 주가×0.8", "amber", "하향 리픽싱 여지 → 희석 가능"))
    ind = (c.get("induty_code") or "")[:2]
    st = IND_STATS.get((c.get("market"), ind))
    rows = kpi.get("CFS") or kpi.get("OFS") or []
    ann = [r for r in rows if r.get("kind") == "annual"]
    if st and ann:
        last = ann[-1]
        d["industry_pos"] = {"ind": ind, "name": c.get("sector_krx"), "n": len(st["debt_ratio"]), "market": c.get("market"),
                             "debt_ratio": _rank(st["debt_ratio"], last.get("debt_ratio"), lower_better=True),
                             "op_margin": _rank(st["op_margin"], last.get("op_margin")),
                             "cur_ratio": _rank(st["cur_ratio"], last.get("cur_ratio")),
                             "borrow_dep": _rank(st["borrow_dep"], last.get("borrow_dep"), lower_better=True),
                             "net_margin": _rank(st.get("net_margin", []), last.get("net_margin")),
                             "borrow_op": _rank(st.get("borrow_op", []), last.get("borrow_op"), lower_better=True),
                             "mcap": _rank(st["mcap"], (d["price_summary"] or {}).get("mcap"))}
    return d


def _industry_stats(con):
    """(시장, KSIC 중분류)별 최근 연간 지표 분포 → 백분위 계산용 정렬 리스트"""
    rows = q(con, """SELECT c.corp_code, c.market, substr(c.induty_code,1,2) AS ind, k.debt_ratio, k.op_margin, k.cur_ratio, k.borrow_dep, k.net_margin, k.borrow_op
                     FROM company c JOIN fin_kpi k ON k.corp_code=c.corp_code AND k.reprt_code='11011'
                     AND k.fs_div=(SELECT CASE WHEN EXISTS(SELECT 1 FROM fin_kpi x WHERE x.corp_code=c.corp_code AND x.fs_div='CFS' AND x.reprt_code='11011') THEN 'CFS' ELSE 'OFS' END)
                     AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi y WHERE y.corp_code=c.corp_code AND y.reprt_code='11011')""")
    mc = {r["stock_code"]: r["mcap"] for r in q(con, "SELECT stock_code, mcap FROM price_summary ORDER BY asof")}   # 뒤(최신 asof)가 덮어씀
    code2stock = {r["corp_code"]: r["stock_code"] for r in q(con, "SELECT corp_code, stock_code FROM company")}
    stats = {}
    for r in rows:
        d = stats.setdefault((r["market"], r["ind"]), {"debt_ratio": [], "op_margin": [], "cur_ratio": [], "borrow_dep": [], "net_margin": [], "borrow_op": [], "mcap": []})
        for k in ("debt_ratio", "op_margin", "cur_ratio", "borrow_dep", "net_margin", "borrow_op"):
            if r[k] is not None:
                d[k].append(r[k])
        m = mc.get(code2stock.get(r["corp_code"]))
        if m:
            d["mcap"].append(m)
    for d in stats.values():
        for k in d:
            d[k].sort()
    return stats


def _rank(sorted_vals, v, lower_better=False):
    """같은 시장·업종 내 순위. 1위 = 가장 양호. 반환 {"rank", "n"} (모수 5 미만이면 None)"""
    if v is None or not sorted_vals or len(sorted_vals) < 5:
        return None
    import bisect
    n = len(sorted_vals)
    if lower_better:
        rank = bisect.bisect_left(sorted_vals, v) + 1          # 자기보다 작은 값의 수 + 1
    else:
        rank = (n - bisect.bisect_right(sorted_vals, v)) + 1   # 자기보다 큰 값의 수 + 1
    return {"rank": min(rank, n), "n": n, "v": v}


_RC_NM = {"11012": "반기", "11013": "1분기", "11014": "3분기"}


def _review(con, cc):
    """최근 분기·반기 검토의견(2026-10-26) — audit_review 없으면 None"""
    try:
        r = con.execute("""SELECT bsns_year, reprt_code, adtor, opinion FROM audit_review WHERE corp_code=? AND opinion IS NOT NULL
                           ORDER BY bsns_year DESC, CASE reprt_code WHEN '11014' THEN 3 WHEN '11012' THEN 2 ELSE 1 END DESC LIMIT 1""", (cc,)).fetchone()
    except Exception:  # noqa
        return None
    if not r:
        return None
    return {"bsns_year": r[0], "period": _RC_NM.get(r[1], r[1]), "adtor": r[2], "opinion": r[3]}


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
        cr = current_rating(ratings)
        g = (cr.get("grade") or "").strip() if cr else ""
        if g in ("D", "SD", "RD"):          # 부도 등급(중앙일보 2026-06 최종부도 사례)
            out.insert(0, ("신용등급 D (부도)", "red", f"{cr['agency']} {cr['disc_dt']} {cr.get('target_type') or ''}"))
        elif re.match(r"^C{1,3}([+-]|$)", g):
            out.insert(0, (f"신용등급 {g} (CCC 이하)", "red", f"{cr['agency']} {cr['disc_dt']}"))
        r0 = ratings[0]
        if r0.get("watch") or (r0.get("outlook") and "부정" in r0["outlook"]):
            out.append(("등급전망 부정적/감시", "amber", f"{r0['agency']} {r0['grade']} {r0.get('outlook') or ''} {r0.get('watch') or ''}"))
    return out


_NOTES_SKIP = ("특수관계자", "계열회사", "매입채무", "제재", "위험관리")   # 위험관리 및 파생거래 절 제외(사용자 결정 2026-10-25)      # 3-1 원문 발췌에서 제외(사용자 결정 2026-10-23: 특수관계자·계열 불필요)
_NOTES_ORDER = ("차입금", "사채", "우발", "담보", "보증", "약정", "소송", "리스", "파생", "금융부채")


def _borrow_bd(con, cc):
    """3-1 차입금 현황(사용자 요청 2026-10-24): 재무상태표 계정으로 차입금·사채·리스 × 유동/비유동을 연간 3개년 + 최근 분기로.
    DART 호출 0 — fin_raw(BS) 재분류. 합계는 KPI 총차입금과 같은 규칙(financials.borrow_items)."""
    from financials import borrow_items
    out = {}
    acc = 12
    try:
        a = con.execute("SELECT acc_mt FROM company WHERE corp_code=?", (cc,)).fetchone()
        acc = int(a[0]) if a and a[0] else 12
    except Exception:  # noqa
        pass
    Q = {"11013": 1, "11012": 2, "11014": 3}
    def plabel(year, rc):
        if rc == "11011":
            return year
        mm = ((acc + 3 * Q.get(rc, 0) - 1) % 12) + 1
        return f"{year}.{mm:02d}"
    for fs in ("CFS", "OFS"):
        per = con.execute("""SELECT bsns_year, reprt_code, MAX(rcept_no) rc FROM fin_raw WHERE corp_code=? AND fs_div=? AND sj_div='BS'
                             GROUP BY bsns_year, reprt_code""", (cc, fs)).fetchall()
        if not per:
            continue
        ann = sorted([p for p in per if p[1] == "11011"], key=lambda p: p[0])[-3:]
        inter = sorted([p for p in per if p[1] != "11011"], key=lambda p: (p[0], Q.get(p[1], 0)))
        cols = ann + ([inter[-1]] if inter and (not ann or (inter[-1][0], 1) >= (ann[-1][0], 1)) else [])
        res = []
        for y, rc, rcept in cols:
            rows = [dict(r) for r in con.execute("SELECT sj_div, account_id, account_nm, thstrm_amount, ord FROM fin_raw WHERE corp_code=? AND fs_div=? AND bsns_year=? AND reprt_code=?", (cc, fs, y, rc))]
            items = borrow_items(rows)
            if not items:
                continue
            agg = {}
            for it in items:
                k = "사채" if it["kind"] in ("일반사채", "CB", "BW", "EB", "CP") else it["kind"]
                c = it["cur"] or "미구분"
                agg.setdefault(k, {}); agg[k][c] = agg[k].get(c, 0) + it["amt"]
            cash = con.execute("SELECT cash, ebitda FROM fin_kpi WHERE corp_code=? AND fs_div=? AND bsns_year=? AND reprt_code=?", (cc, fs, y, rc)).fetchone()
            res.append({"label": plabel(y, rc), "year": y, "rc": rc, "rcept_no": rcept, "interim": rc != "11011",
                        "k": agg, "total": sum(i["amt"] for i in items), "cash": cash[0] if cash else None, "ebitda": cash[1] if cash else None,
                        "detail": [[i["nm"], i["kind"], i["cur"], i["amt"]] for i in items]})
        if res:
            out[fs] = res
    return out or None


def _notes_raw(con, cc, per_sec=7000, total=30000):
    """notes_latest → 절별 발췌 — 비용 0.
    v2(notes_j, 표 구조 보존)가 있으면 [{t, g, fs, blocks:[{p}|{tbl:{unit,rows}}]}], 없으면 평문 [{t, g, b}].
    g: 'debt'(3-1) | 'mezz'(3-2) | 'asset'(표시 안 함, 상각비 추출용). 특수관계자·계열·매입채무·제재 절은 제외(사용자 결정 2026-10-23)."""
    try:
        r = con.execute("SELECT rcept_no, report_nm, notes_z, notes_j FROM notes_latest WHERE corp_code=?", (cc,)).fetchone()
    except Exception:  # noqa
        try:
            r = con.execute("SELECT rcept_no, report_nm, notes_z, NULL AS notes_j FROM notes_latest WHERE corp_code=?", (cc,)).fetchone()
        except Exception:  # noqa
            return None
    if not r:
        return None
    from common import ztext
    def grp(t):
        tt = t.replace(" ", "")
        if any(k in tt for k in _NOTES_SKIP):
            return None
        if "파생" in tt and not re.search(r"전환사채|신주인수권|교환사채|전환우선주|상환전환|복합금융", tt):
            return None                      # 파생금융상품 절은 표시 안 함(사용자 결정 2026-10-26: 메자닌 칸만 남김)
        if re.search(r"전환사채|신주인수권|교환사채|전환우선주|상환전환|복합금융", tt):
            return "mezz"
        if re.search(r"유형자산|무형자산|사용권자산|투자부동산|현금흐름", tt):
            return "asset"
        return "debt"
    def pri(t):
        tt = t.replace(" ", "")
        return next((i for i, k in enumerate(_NOTES_ORDER) if k in tt), 99)
    if r["notes_j"]:
        try:
            secs = json.loads(ztext(r["notes_j"]) or "[]")
        except Exception:  # noqa
            secs = []
        out, used, trunc = [], 0, False
        # 연결 주석 우선, 같은 제목의 별도 절은 뒤에(둘 다 표시 — 제목에 [연결]/[별도] 표기)
        items = []
        for sc in secs:
            g = grp(sc.get("t", ""))
            if not g or g == "asset":
                continue
            items.append((0 if sc.get("fs") in (None, "CFS") else 1, pri(sc["t"]), sc, g))
        items.sort(key=lambda x: (x[0], x[1]))
        for _, _, sc, g in items:
            if used >= total:
                trunc = True; break
            blocks, size = [], 0
            for b in sc.get("blocks", []):
                if "p" in b:
                    txt = b["p"][:2500]; blocks.append({"p": txt}); size += len(txt)
                else:
                    rows = b["tbl"]["rows"][:40]
                    if len(b["tbl"]["rows"]) > 40:
                        trunc = True
                    blocks.append({"tbl": {"unit": b["tbl"].get("unit", 1), "rows": rows}}); size += sum(len(" ".join(x)) for x in rows)
                if size > per_sec:
                    trunc = True; break
            t = sc["t"] + ("" if re.search(r"\((연결|별도)\)", sc["t"]) else " [별도]" if sc.get("fs") == "OFS" else " [연결]" if sc.get("fs") == "CFS" else "")   # 제목에 (연결)/(별도)가 있으면 꼬리표 생략
            out.append({"t": t, "g": g, "fs": sc.get("fs"), "blocks": blocks}); used += size
        return {"rcept_no": r["rcept_no"], "report_nm": r["report_nm"], "sections": out, "truncated": trunc, "v": 2} if out else None
    if not r["notes_z"]:
        return None
    txt = ztext(r["notes_z"]) or ""
    secs = []
    for part in txt.split("\n### ")[0:]:
        part = part[4:] if part.startswith("### ") else part
        t, _, b = part.partition("\n")
        t = t.strip(); b = b.strip()
        g = grp(t) if t else None
        if not t or not b or not g or g == "asset":
            continue
        secs.append((pri(t), t, b, g))
    secs.sort(key=lambda x: x[0])
    out, used, trunc = [], 0, False
    for _, t, b, g in secs:
        if used >= total:
            trunc = True; break
        if len(b) > per_sec:
            b = b[:per_sec] + " …(이하 생략)"; trunc = True
        out.append({"t": t, "b": b, "g": g}); used += len(b)
    return {"rcept_no": r["rcept_no"], "report_nm": r["report_nm"], "sections": out, "truncated": trunc} if out else None


def _site_meta(con, index):
    """상단 기준·출처 띠용 집계(실패해도 빌드는 계속)"""
    m = {}
    def one(sql, *a):
        try:
            r = con.execute(sql, a).fetchone(); return r[0] if r else None
        except Exception:  # noqa
            return None
    try:
        m["by_market"] = {}
        for it in index:
            m["by_market"][it.get("market") or "-"] = m["by_market"].get(it.get("market") or "-", 0) + 1
        m["delisted"] = sum(1 for it in index if it.get("delisted"))
    except Exception:  # noqa
        pass
    m["fin_annual"] = one("SELECT COUNT(DISTINCT corp_code) FROM fin_kpi WHERE reprt_code='11011' AND (revenue IS NOT NULL OR total_assets IS NOT NULL)")
    m["fin_year"] = one("SELECT MAX(bsns_year) FROM fin_kpi WHERE reprt_code='11011'")
    m["fin_q"] = one("SELECT COUNT(*) FROM (SELECT corp_code FROM fin_cum_ok GROUP BY corp_code HAVING COUNT(*)>=10)")
    try:
        lq = con.execute("""SELECT bsns_year, reprt_code, COUNT(DISTINCT corp_code) n FROM fin_kpi WHERE reprt_code<>'11011'
                            GROUP BY 1,2 HAVING n>=100 ORDER BY 1 DESC,
                            CASE reprt_code WHEN '11014' THEN 3 WHEN '11012' THEN 2 ELSE 1 END DESC LIMIT 1""").fetchone()
        if lq:
            m["latest_q"] = lq[0] + "." + {"11013": "03", "11012": "06", "11014": "09"}.get(lq[1], "")
    except Exception:  # noqa
        pass
    m["rated"] = one("SELECT COUNT(DISTINCT corp_code) FROM credit_rating")
    m["rating_asof"] = one("SELECT MAX(disc_dt) FROM credit_rating")
    m["subst_dd"] = one("SELECT MAX(trd_dd) FROM subst_price") or one("SELECT MAX(subst_dd) FROM price_summary")
    m["mezz"] = one("SELECT COUNT(DISTINCT corp_code) FROM mezz_outstanding") or one("SELECT COUNT(DISTINCT corp_code) FROM mezzanine")
    m["disc_asof"] = one("SELECT MAX(rcept_dt) FROM disclosure_event")
    return {k: v for k, v in m.items() if v is not None}


def _is_cancel(r):
    return bool(re.search(r"취소|철회|WD|^NR$", (r.get("grade") or "") + " " + (r.get("eval_kind") or "")))


_SUB_RE = re.compile(r"신종자본|후순위|조건부자본|영구채|하이브리드")
_SHORT_RE = re.compile(r"기업어음|단기사채|CP|전단채")


def _cls(r):
    """평가대상 분류(2026-10-28 한화투자증권: 신종자본증권 A가 대표, 만기 ELB '취소'가 장기 AA- 취소로 표시되던 문제)
    ① 기업신용평가 → issuer ② 신종자본·후순위·조건부자본 → sub(선순위보다 낮게 매겨지는 노칭 등급) ③ CP·전단채 → short
    ④ 그 밖의 사채(회사채·금융채·파생결합사채) → senior ⑤ 나머지 → other"""
    t = r.get("target_type") or ""
    if "기업신용평가" in t:
        return "issuer"
    if _SHORT_RE.search(t):
        return "short"
    if _SUB_RE.search(t):
        return "sub"
    if re.search(r"사채|금융채|채권", t):
        return "senior"
    return "other"


def _is_long(r):
    return _cls(r) in ("issuer", "senior", "sub")


def _tkey(r):
    return (r.get("agency") or "", re.sub(r"\s+", "", r.get("target_type") or ""))


def _issue_cancelled(r, cancels):
    """같은 평가사·같은 평가대상(같은 회차)에 그 뒤 취소 공시가 있으면 True — 만기상환된 개별 사채(ELB·DLB 등)의 등급 취소"""
    k = _tkey(r)
    return any(_tkey(c) == k and (c.get("disc_dt") or "") >= (r.get("disc_dt") or "") for c in cancels)


def rating_status(ratings, today=None):
    """대표 등급의 유효 상태(2026-10-25 사용자 요청: 유효 등급 없는 회사는 회사명 옆 등급 오른쪽에 '취소'·'유효기간 만료' 표시).
    ① 대표 등급과 같은 종류(장기/단기)에서 그 뒤 '취소·철회' 공시가 있으면 → 등급 취소
    ② 대표 등급 공시일로부터 1년 넘게 새 공시가 없으면 → 유효기간 만료(추정 — 신용평가 유효기간은 통상 1년, 정확한 기한은 평가서 기재)"""
    from datetime import date, datetime
    cur = current_rating(ratings)
    today = today or date.today()
    cancels = [r for r in ratings if _is_cancel(r)]
    if cur:
        # 회사 단위 취소로 보는 경우: 대표 등급과 같은 평가대상(같은 회차)이 취소됐거나, 기업신용평가가 취소됐을 때만 — 다른 회차 사채의 만기 취소는 무시(2026-10-28)
        same = [r for r in cancels if (_tkey(r) == _tkey(cur) or (_cls(r) == "issuer" and _cls(cur) == "issuer")) and (r.get("disc_dt") or "") >= (cur.get("disc_dt") or "")]
        if same:
            return {"kind": "cancel", "label": "등급 취소", "date": same[0]["disc_dt"], "tip": f"{same[0]['agency']} {same[0]['disc_dt']} {same[0].get('target_type') or ''} 등급 취소 공시"}
        if cur.get("valid_to"):                     # 평가서에 기재된 유효기간이 있으면 그 날짜로 판단(2026-10-26 씨제이포디플렉스 CP 6개월)
            try:
                vt = datetime.strptime(cur["valid_to"][:10], "%Y-%m-%d").date()
                if today > vt:
                    return {"kind": "expired", "label": "유효기간 만료", "date": cur["valid_to"], "tip": f"평가서 기재 유효기간 {cur['valid_to']} 경과 · 새 평가 공시 없음"}
                return None
            except Exception:  # noqa
                pass
        try:
            dd = datetime.strptime(cur["disc_dt"][:10], "%Y-%m-%d").date()
            if (today - dd).days > 365:
                return {"kind": "expired", "label": "유효기간 만료", "date": cur["disc_dt"], "tip": f"최근 공시 {cur['disc_dt']} 후 1년 경과 · 새 평가 공시 없음(유효기간 통상 1년, 정확한 기한은 평가서 확인)"}
        except Exception:  # noqa
            pass
    elif cancels:
        return {"kind": "cancel", "label": "등급 취소", "date": cancels[0]["disc_dt"], "tip": f"{cancels[0]['agency']} {cancels[0]['disc_dt']} 등급 취소 공시"}
    return None


def rating_view(ratings, today=None):
    """화면용 대표 등급(2026-10-26 효성화학: 장기 BBB는 만료인데 단기 A3가 유효 → 'BBB 만료'만 보여 착오).
    장기 등급이 만료·취소이고 유효한 단기 등급(CP·전단채)이 있으면 단기 등급을 대표로, 만료된 장기 등급은 보조 표시(aux)로."""
    cur = current_rating(ratings)
    st = rating_status(ratings, today)
    if st and cur and _is_long(cur):
        shorts = [r for r in ratings if _cls(r) == "short"]
        s0 = current_rating(shorts)
        aux = {"grade": cur.get("grade"), "label": st["label"], "date": st.get("date"), "kind": st["kind"], "tip": st.get("tip")}
        if s0 and not _is_cancel(s0):
            s_st = rating_status(shorts, today)
            if not s_st:
                return s0, None, aux
            if (s0.get("disc_dt") or "") > (cur.get("disc_dt") or ""):   # 둘 다 만료면 더 최근 평가(단기)를 대표로 — 씨제이포디플렉스 A3-(~2026.06.30) 만료가 보이도록(2026-10-26)
                return s0, s_st, aux
    return cur, st, None


def current_rating(ratings):
    """대표 등급(공시일 내림차순 목록에서): ① 장기(기업신용평가·회사채, 단기사채 제외) 중 '정정' 아닌 최신 ② 단기(CP·전단채) 중 정정 아닌 최신 ③ 정정 포함.
    2026-10-25: '전자단기사채'가 '사채'에 걸려 장기로 잡히고, 옛 평가의 '본평가(정정)' 재공시(공시일만 최신)가 대표가 되어 부도(D)난 중앙일보가 A3로 표시되던 문제 수정"""
    # 2026-10-28: 순서 ① 기업신용평가·선순위 사채 ② 단기(CP·전단채) ③ 신종자본·후순위(노칭) ④ 그 밖 — 각 단계에서 정정 아닌 최신, 이미 취소된 회차는 건너뜀
    cancels = [r for r in ratings if _is_cancel(r)]
    alive = [r for r in ratings if not _is_cancel(r)]
    live = [r for r in alive if not _issue_cancelled(r, cancels)] or alive
    plain = [r for r in live if "정정" not in (r.get("eval_kind") or "")]
    main = lambda r: _cls(r) in ("issuer", "senior")
    for pool in ([r for r in plain if main(r)], [r for r in plain if _cls(r) == "short"], [r for r in plain if _cls(r) == "sub"], plain,
                 [r for r in live if main(r)], live):
        if pool:
            return pool[0]
    return None


def build():
    con = connect()
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data" / "c").mkdir(parents=True)
    shutil.copy(TEMPLATE, SITE / "index.html")
    shutil.copytree(TEMPLATE.parent / "lib", SITE / "lib")          # 차트 라이브러리(Lightweight Charts, Apache-2.0)
    fdir = ROOT / "functions" / "api"                                   # Pages Functions(회사 추가: /api/corp 검색, /api/add 등록) — pages deploy가 저장소 루트 functions/를 함께 올림(2026-10-26)
    fdir.mkdir(parents=True, exist_ok=True)
    for src in TEMPLATE.parent.glob("fn_api_*.js"):
        shutil.copy(src, fdir / src.name.replace("fn_api_", ""))
    (SITE / "_headers").write_text("/*\n  Cache-Control: no-cache\n", encoding="utf-8")   # 매 요청 ETag 재검증 → 배포 즉시 반영

    global IND_STATS
    IND_STATS = _industry_stats(con)
    index = []
    try:
        MANUAL = {r[0] for r in con.execute("SELECT corp_code FROM manual_company")}   # 사이트 '회사 추가'로 넣은 회사(2026-10-26)
    except Exception:  # noqa
        MANUAL = set()
    for c in q(con, "SELECT * FROM company ORDER BY corp_name"):
        d = company_detail(con, c)
        _rows = d["kpi"].get("CFS") or d["kpi"].get("OFS") or [{}]
        k = next((r for r in reversed(_rows) if r.get("kind") == "annual"), _rows[-1])   # 검색 인덱스는 최근 연간 기준
        ps = d["price_summary"] or {}
        ao = d["audit"][0] if d["audit"] else {}
        cur_rt, d["rating_status"], d["rating_aux"] = rating_view(d["ratings"])
        d["rating_cur"] = cur_rt
        (SITE / "data" / "c" / f"{c['stock_code']}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        index.append({"code": c["stock_code"], "name": c["corp_name"], "sector": c["sector_krx"], "sect": c.get("sect_tp"), "market": c["market"], "delisted": c.get("delisted_dt"),
                      "rev": k.get("revenue"), "op": k.get("op_income"), "debt": k.get("debt_ratio"),
                      "mcap": ps.get("mcap"), "opinion": ao.get("adt_opinion"), "year": k.get("bsns_year"),
                      "industry": (d["business"]["summary"] or {}).get("industry") if d["business"]["summary"] else None,
                      "rating": cur_rt["grade"] if cur_rt else None, "rt_st": (d.get("rating_status") or {}).get("label"), "rt_aux": (f"{d['rating_aux']['grade']} {d['rating_aux']['label'].replace('유효기간 ', '').replace('등급 ', '')}" if d.get("rating_aux") else None), "manual": 1 if c["corp_code"] in MANUAL else None,
                      "per": (d["valuation"] or {}).get("per"), "ev_ebitda": (d["valuation"] or {}).get("ev_ebitda"),
                      "red": sum(1 for f in d["flags"] if f[1] == "red"), "amber": sum(1 for f in d["flags"] if f[1] == "amber")})
    meta = {"built_at": con.execute("SELECT datetime('now','localtime')").fetchone()[0], "count": len(index),
            "price_asof": max((r["asof"] for r in q(con, "SELECT asof FROM price_summary")), default=None)}
    meta.update(_site_meta(con, index))
    (SITE / "data" / "companies.json").write_text(json.dumps({"meta": meta, "items": index}, ensure_ascii=False), encoding="utf-8")
    try:   # 데이터 품질 점검 결과(site/data/dq.json) + 점검 페이지(dq.html)
        sys.path.insert(0, str(Path(__file__).resolve().parent / "collect"))
        import dq
        dq.run(con)
        if (TEMPLATE.parent / "dq.html").exists():
            shutil.copy(TEMPLATE.parent / "dq.html", SITE / "dq.html")
    except Exception as e:  # noqa
        print("[site] 품질 점검 실패:", e)
    print(f"site/ 생성: {len(index)}개사")


if __name__ == "__main__":
    build()
