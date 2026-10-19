"""정적 사이트 빌더: kosdaq.db → site/ (Cloudflare Pages 배포 단위)
  site/index.html            앱(검색·프로필 렌더)
  site/data/companies.json   검색 인덱스(전종목 요약 1행)
  site/data/c/{stock_code}.json  기업별 상세
"""
import json
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


def company_detail(con, c):
    cc, sc = c["corp_code"], c["stock_code"]
    biz = q(con, "SELECT bsns_year, rcept_no, overview_text, summary_json, summary_md FROM business WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 1", cc)
    b = biz[0] if biz else {}
    kpi = {}
    RC = {"11013": "1Q", "11012": "반기", "11014": "3Q"}
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
                rows.append({**prev[0], "label": f"{prev[0]['bsns_year']}.{RC.get(L['reprt_code'], L['reprt_code'])}", "kind": "prev_interim"})
            rows.append({**L, "label": f"{L['bsns_year']}.{RC.get(L['reprt_code'], L['reprt_code'])}", "kind": "interim"})
        if rows:
            kpi[fs] = [{k: v for k, v in r.items() if k not in ("corp_code", "fs_div")} for r in rows]
    prices = q(con, "SELECT dt, close, volume FROM price_daily WHERE stock_code=? AND dt >= date('now','-1 year') ORDER BY dt", sc)
    ps = q(con, "SELECT * FROM price_summary WHERE stock_code=? ORDER BY asof DESC LIMIT 1", sc)
    return {
        "company": c,
        "audit": q(con, "SELECT bsns_year, adtor, adt_opinion, emphs_matter, core_adt_matter FROM audit_opinion WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 3", cc),
        "shareholders": q(con, """SELECT nm, relate, trmend_posesn_stock_co, trmend_posesn_stock_qota_rt FROM shareholder WHERE corp_code=?
                                  AND bsns_year=(SELECT MAX(bsns_year) FROM shareholder WHERE corp_code=?) ORDER BY trmend_posesn_stock_qota_rt DESC LIMIT 8""", cc, cc),
        "business": {"year": b.get("bsns_year"), "rcept_no": b.get("rcept_no"),
                     "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={b['rcept_no']}" if b.get("rcept_no") else None,
                     "overview": (b.get("overview_text") or "")[:4000], "summary": json.loads(b["summary_json"]) if b.get("summary_json") else None},
        "kpi": kpi,
        "price_summary": ps[0] if ps else None,
        "prices": [[p["dt"], p["close"], p["volume"]] for p in prices],
        "events": q(con, "SELECT rcept_dt, flag, report_nm, rcept_no FROM disclosure_event WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 20", cc),
        "reports": q(con, """SELECT m.pub_dt, m.source, m.publisher, m.analyst, m.title, m.url, m.opinion, m.target_price, m.old_target_price,
                             a.forecast_json, a.valuation_json, a.points_json, a.risks_json, a.memo_md
                             FROM report_meta m LEFT JOIN report_analysis a ON a.report_id=m.id
                             WHERE m.stock_code=? ORDER BY m.pub_dt DESC LIMIT 12""", sc),
        "forecast_vs_actual": _fva(con, sc),
    }


def build():
    con = connect()
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data" / "c").mkdir(parents=True)
    shutil.copy(TEMPLATE, SITE / "index.html")
    (SITE / "_headers").write_text("/*\n  Cache-Control: no-cache\n", encoding="utf-8")   # 매 요청 ETag 재검증 → 배포 즉시 반영

    index = []
    for c in q(con, "SELECT * FROM company ORDER BY corp_name"):
        d = company_detail(con, c)
        (SITE / "data" / "c" / f"{c['stock_code']}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        _rows = d["kpi"].get("CFS") or d["kpi"].get("OFS") or [{}]
        k = next((r for r in reversed(_rows) if r.get("kind") == "annual"), _rows[-1])   # 검색 인덱스는 최근 연간 기준
        ps = d["price_summary"] or {}
        ao = d["audit"][0] if d["audit"] else {}
        index.append({"code": c["stock_code"], "name": c["corp_name"], "sector": c["sector_krx"], "sect": c.get("sect_tp"), "market": c["market"],
                      "rev": k.get("revenue"), "op": k.get("op_income"), "debt": k.get("debt_ratio"),
                      "mcap": ps.get("mcap"), "opinion": ao.get("adt_opinion"), "year": k.get("bsns_year"),
                      "industry": (d["business"]["summary"] or {}).get("industry") if d["business"]["summary"] else None})
    meta = {"built_at": con.execute("SELECT datetime('now','localtime')").fetchone()[0], "count": len(index),
            "price_asof": max((r["asof"] for r in q(con, "SELECT asof FROM price_summary")), default=None)}
    (SITE / "data" / "companies.json").write_text(json.dumps({"meta": meta, "items": index}, ensure_ascii=False), encoding="utf-8")
    print(f"site/ 생성: {len(index)}개사")


if __name__ == "__main__":
    build()
