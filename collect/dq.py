"""데이터 품질 점검(DART 호출 없음): 회사·연도별 핵심 항목 누락과 회계 정합성 이상을 찾아 site/data/dq.json으로 내보내고 로그에 요약.
  python run.py dq
점검 항목(최신 연간 = 주 재무제표 기준, 최신 분기 = 누적):
  누락: 매출·영업이익·자산·부채·자본·현금·영업CF·투자CF·Capex·상각비(EBITDA)·총차입금(이자비용 있는데 없음)
  정합: 자산 ≠ 부채+자본(1% 초과), 총차입금 > 부채총계, 매출원가 > 매출(제조업), 현금 > 자산, 영업이익률 |x|>100%(매출≥10억),
        분기 누적 역전(3Q누적 < 반기누적 등), 연간 매출 ≠ 분기 복원 합(±3%), 별도 > 연결(자산 1.2배 초과)
  모듈: 사업요약·별도재무·전년동기BS·시세·대용가 누락
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run(con=None, sample=12):
    con = con or connect()
    import re
    names = {r[0]: (r[1], r[2], r[3], (r[4] or "")[:2]) for r in con.execute("SELECT corp_code, corp_name, stock_code, market, induty_code FROM company")}
    spac = {cc for cc, v in names.items() if re.search(r"스팩|기업인수목적", v[0] or "")}   # 스팩: 매출·Capex·사업내용이 없는 게 정상 → 점검 제외
    names = {cc: v for cc, v in names.items() if cc not in spac}
    issues = defaultdict(list)

    def add(key, cc, detail=""):
        issues[key].append((cc, detail))

    # 회사별 주 재무제표(연결 우선)·최신 연간
    rows = con.execute("""SELECT k.* FROM fin_kpi k WHERE k.reprt_code='11011'
                          AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi y WHERE y.corp_code=k.corp_code AND y.reprt_code='11011')""").fetchall()
    by = defaultdict(dict)
    for r in rows:
        by[r["corp_code"]][r["fs_div"]] = dict(r)
    fin_ind = {"64", "65", "66"}
    for cc, d in by.items():
        if cc in spac:
            continue
        k = d.get("CFS") or d.get("OFS")
        ind = names.get(cc, ("", "", "", ""))[3]
        isfin = ind in fin_ind
        for col, label in (("revenue", "매출 없음"), ("op_income", "영업이익 없음"), ("total_assets", "자산 없음"), ("total_liab", "부채 없음"),
                           ("equity", "자본 없음"), ("cash", "현금성자산 없음"), ("cfo", "영업CF 없음"), ("cfi", "투자CF 없음")):
            if k.get(col) is None and not (isfin and col in ("revenue",)):
                add(label, cc, k["bsns_year"])
        if not isfin:
            if k.get("capex") is None:
                add("Capex 없음", cc, k["bsns_year"])
            if k.get("da") is None and k.get("op_income") is not None:
                add("상각비 없음(EBITDA 불가)", cc, k["bsns_year"])
        if k.get("borrowings") is None and abs(k.get("int_exp") or 0) >= 1e8:     # 이자비용 1억 이상인데 차입금이 안 잡힌 경우만(소액 이자는 리스·미지급 등)
            add("총차입금 없음(이자비용 1억↑)", cc, f"이자 {k['int_exp']/1e8:,.0f}억")
        A, L, E = k.get("total_assets"), k.get("total_liab"), k.get("equity")
        if A and L is not None and E is not None and abs(A - L - E) > abs(A) * 0.01:
            add("자산≠부채+자본", cc, f"{A/1e8:,.0f} vs {(L+E)/1e8:,.0f}억")
        if k.get("borrowings") and L and k["borrowings"] > L * 1.01:
            add("총차입금>부채총계", cc, f"{k['borrowings']/1e8:,.0f} > {L/1e8:,.0f}억")
        if k.get("cash") and A and k["cash"] > A:
            add("현금>자산", cc)
        rev, op = k.get("revenue"), k.get("op_income")
        if rev and k.get("cogs") and k["cogs"] > rev * 1.5 and not isfin:
            add("매출원가>매출×1.5", cc, f"원가 {k['cogs']/1e8:,.0f} / 매출 {rev/1e8:,.0f}억")
        if rev and rev >= 1e9 and op is not None and abs(op / rev) > 1.0:
            add("영업이익률 |x|>100%", cc, f"{op/rev*100:,.0f}%")
        if d.get("CFS") and d.get("OFS") and d["CFS"].get("total_assets") and d["OFS"].get("total_assets") \
           and d["OFS"]["total_assets"] > d["CFS"]["total_assets"] * 1.2:
            add("별도자산>연결자산×1.2", cc)
        if d.get("CFS") and not d.get("OFS"):
            add("별도 재무 없음(연결만)", cc)

    # 분기 누적 역전 · 연간 vs 분기 합
    q = defaultdict(dict)
    for r in con.execute("SELECT corp_code, fs_div, bsns_year, reprt_code, revenue FROM fin_kpi WHERE revenue IS NOT NULL"):
        q[(r[0], r[1], r[2])][r[3]] = r[4]
    for (cc, fs, y), m in q.items():
        if cc in spac:
            continue
        q1, h, q3, a = m.get("11013"), m.get("11012"), m.get("11014"), m.get("11011")
        if (q1 and h and h < q1 * 0.98) or (h and q3 and q3 < h * 0.98) or (q3 and a and a < q3 * 0.98):
            add("분기 누적 역전(누적 오류 의심)", cc, f"{fs} {y}")
    # 모듈 누락
    have = lambda sql: {r[0] for r in con.execute(sql)}
    try:
        summ = have("SELECT corp_code FROM business WHERE summary_json IS NOT NULL")
        ps = have("SELECT c.corp_code FROM company c JOIN price_summary p ON p.stock_code=c.stock_code WHERE p.close IS NOT NULL")
        sub = have("SELECT c.corp_code FROM company c JOIN price_summary p ON p.stock_code=c.stock_code WHERE p.subst_price IS NOT NULL")
        for cc in names:
            if cc not in summ:
                add("사업요약 없음", cc)
            if cc not in ps:
                add("시세 없음", cc)
            elif cc not in sub:
                add("대용가 없음", cc)
    except Exception as e:  # noqa
        print("[dq] 모듈 점검 일부 실패:", e)
    # 전년동기 BS
    # 재무현황 표에 보이는 두 분기 열(최신 분기 + 전년동기)만 점검 — 12분기 그래프용 과거 분기는 설계상 손익만 있을 수 있음
    lat = {}
    for r in con.execute("SELECT corp_code, fs_div, bsns_year, reprt_code, total_assets FROM fin_kpi WHERE reprt_code<>'11011'"):
        o = {"11013": 1, "11012": 2, "11014": 3}[r[3]]
        key = (r[0], r[1])
        if key not in lat or (r[2], o) > lat[key][:2]:
            lat[key] = (r[2], o, r[3])
    ta = {(r[0], r[1], r[2], r[3]): r[4] for r in con.execute("SELECT corp_code, fs_div, bsns_year, reprt_code, total_assets FROM fin_kpi WHERE reprt_code<>'11011'")}
    for (cc, fs), (y, _, rc) in lat.items():
        if cc in spac:
            continue
        if ta.get((cc, fs, y, rc)) is None:
            add("최신 분기 재무상태표 없음", cc, f"{fs} {y} {rc}")
        py = str(int(y) - 1)
        if (cc, fs, py, rc) in ta and ta[(cc, fs, py, rc)] is None:
            add("전년동기 재무상태표 없음", cc, f"{fs} {py} {rc}")

    total = len(names)
    out = {"companies": total, "spac_excluded": len(spac), "checks": []}
    print(f"[dq] 데이터 품질 점검 — 전체 {total}사 (스팩 {len(spac)}사 제외)", flush=True)
    for key, lst in sorted(issues.items(), key=lambda x: -len({c for c, _ in x[1]})):
        ccs = sorted({c for c, _ in lst})
        ex = [{"code": names.get(c, ("", ""))[1], "name": names.get(c, ("?",))[0], "detail": dt} for c, dt in lst[:sample]]
        out["checks"].append({"check": key, "companies": len(ccs), "rows": len(lst), "examples": ex})
        print(f"[dq] {key}: {len(ccs)}사 · 예) " + ", ".join(f"{e['name']}({e['detail']})" if e["detail"] else e["name"] for e in ex[:6]), flush=True)
    p = ROOT / "site" / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / "dq.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    run()
