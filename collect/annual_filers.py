"""사업보고서 제출 비상장사 전수 명단 — 드라이런(DB 변경 없음) (2026-10-28 사용자 요청)

배경: 당근마켓·무신사·키움에프앤아이처럼 비상장이지만 사업보고서(정기보고서)를 내는 회사는 상장사와 같은 수준의 자료가 나온다.
등급 보유 여부와 무관하게 이런 회사를 명단으로 뽑아 추가 후보로 검토한다.
- DART 공시검색(list) 정기공시 사업보고서(A001), 법인구분 기타(E = 비상장), 최근 22개월(FY2024·FY2025 보고서)
  · 고유번호 없이 검색하면 기간이 3개월로 제한 → 3개월 구간으로 나눠 조회(구간당 100건씩 페이지)
- 이미 DB에 있는 회사 제외 → 개황(company) 1콜로 업종(KSIC)·대표자·설립일 보강
- 분류(unlisted2와 같은 규칙): 금융 / 공공 / SPC·리츠·펀드 / 일반기업(= 추가 후보)
- 일반기업·금융은 최근 사업보고서의 자본총계를 붙인다(2026-10-28 사용자 결정: 자본총계 500억 이상만 추가, 금융 포함)
  · XBRL 재무(fnlttSinglAcnt 1콜, 연결 우선) → 없으면 사업보고서 본문 재무상태표(audit_fs 파서)
출력: 콘솔 요약 + unlisted_candidates.csv(아티팩트) + filer_candidate 표(추가 작업 unlisted_add --type 사업보고서 가 읽음)
  python run.py filer_scan [--limit 개황조회상한]
"""
import csv
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, dart_get  # noqa: E402
from unlisted2 import FIN, PUB, SPC  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "unlisted_candidates.csv"


def windows(months=22, today=None):
    end = today or date.today()
    start = date(end.year, end.month, 1) - timedelta(days=31 * months)
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt_m = cur.month + 3
        nxt = date(cur.year + (nxt_m - 1) // 12, (nxt_m - 1) % 12 + 1, 1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def scan(months=22):
    """→ {corp_code: {name, report_nm, rcept_dt, rcept_no}} 최신 사업보고서 기준"""
    out = {}
    for b, e in windows(months):
        page = 1
        while True:
            js = dart_get("list", bgn_de=b.strftime("%Y%m%d"), end_de=e.strftime("%Y%m%d"), corp_cls="E",
                          pblntf_ty="A", pblntf_detail_ty="A001", last_reprt_at="Y", page_no=page, page_count=100)
            rows = js.get("list", []) or []
            for r in rows:
                if not re.search(r"사업보고서", r.get("report_nm", "")):
                    continue
                cc = r["corp_code"]
                if cc not in out or r["rcept_dt"] > out[cc]["rcept_dt"]:
                    out[cc] = {"name": r["corp_name"], "report_nm": re.sub(r"\s+", " ", r["report_nm"]).strip(),
                               "rcept_dt": r["rcept_dt"], "rcept_no": r["rcept_no"]}
            tp = int(js.get("total_page") or 1)
            print(f"[filer_scan] {b}~{e} {page}/{tp}쪽 · 누적 {len(out)}사", flush=True)
            if page >= tp:
                break
            page += 1
    return out


def classify(name):
    if SPC.search(name):
        return "SPC·리츠·펀드"
    if FIN.search(name):
        return "금융"
    if PUB.search(name):
        return "공공"
    return "일반기업"


def equity_of(cc, rcept_no, report_nm):
    """→ (자본총계 원, 사업연도, 출처) 또는 (None, fy, 사유)"""
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm or "")
    fy = m.group(1) if m else None
    if fy:
        try:
            js = dart_get("fnlttSinglAcnt", corp_code=cc, bsns_year=fy, reprt_code="11011")
            rows = [r for r in js.get("list", []) if r.get("sj_div") == "BS" and re.sub(r"\s", "", r.get("account_nm", "")) in ("자본총계", "자본합계")]
            for fs in ("CFS", "OFS"):
                for r in rows:
                    if r.get("fs_div") == fs and r.get("thstrm_amount") not in (None, "", "-"):
                        return float(str(r["thstrm_amount"]).replace(",", "")), fy, "XBRL " + fs
        except Exception:  # noqa
            pass
    try:                                                  # XBRL 없음 → 사업보고서 본문 재무상태표
        import audit_fs
        from business import fetch_document_xml
        secs = audit_fs.annual_sections(fetch_document_xml(rcept_no))
        for fs in ("CFS", "OFS"):
            if fs in secs:
                c, _p = audit_fs._row(audit_fs.parse_statements(secs[fs]), "BS", "자본총계", "자본합계", "자본총액", "총자본")
                if c is not None:
                    return c, fy, "본문 " + fs
    except Exception as e:  # noqa
        return None, fy, f"본문 실패: {str(e)[:40]}"
    return None, fy, "자본총계 못 찾음"


# 2026-10-28 사용자 결정: 금융 중 여전사(캐피탈·카드·리스·할부)만 추가 — 은행·보험·증권·자산운용·금융지주 제외, 애매한 곳은 명단만 보여 판단
YEO = re.compile(r"캐피탈|카드|리스|할부|커머셜|파이낸셜|파이낸스|Capital|Card", re.I)
YEO_EX = re.compile(r"은행|뱅크|생명|화재|손해|보험|증권|자산운용|운용|금융지주|지주|저축|신탁|종금|종합금융|대부|라이프|투자증권", re.I)


def yeo_class(name):
    """금융 후보 → '여전사' | '제외' | '애매'"""
    nm = name or ""
    if YEO_EX.search(nm):
        return "제외"
    if YEO.search(nm):
        return "여전사"
    return "애매"


def eligible(con):
    """추가 대상(자본총계 500억↑ · DB 미등록): 일반기업 전부 + 금융 중 여전사. → [(corp_code, name, grp, equity, eq_src)] 큰 순"""
    rows = con.execute("""SELECT corp_code, name, grp, equity, eq_src FROM filer_candidate WHERE grp IN ('일반기업','금융') AND equity >= 5e10
                          AND corp_code NOT IN (SELECT corp_code FROM company) ORDER BY equity DESC""").fetchall()
    return [r for r in rows if r[2] == "일반기업" or yeo_class(r[1]) == "여전사"]


def pilot(con):
    """시범 10곳: XBRL 일반기업 5 · 본문 파싱 일반기업 3 · 여전사 2 (각 자본총계 큰 순)"""
    el = eligible(con)
    x = [r for r in el if r[2] == "일반기업" and (r[4] or "").startswith("XBRL")][:5]
    d = [r for r in el if r[2] == "일반기업" and (r[4] or "").startswith("본문")][:3]
    y = [r for r in el if r[2] == "금융"][:2]
    return x + d + y


def preview(con):
    """DB 변경 없음: 금융 분류(포함·제외·애매) + 전체 대상 수 + 시범 10곳"""
    fin = con.execute("""SELECT corp_code, name, equity FROM filer_candidate WHERE grp='금융' AND equity >= 5e10
                         AND corp_code NOT IN (SELECT corp_code FROM company) ORDER BY equity DESC""").fetchall()
    for k in ("여전사", "애매", "제외"):
        g = [r for r in fin if yeo_class(r[1]) == k]
        print(f"[filer] 금융 {k} {len(g)}사: " + ", ".join(f"{r[1]}({r[2] / 1e8:,.0f})" for r in g), flush=True)
    el = eligible(con)
    src = Counter((r[4] or "?").split()[0] for r in el if r[2] == "일반기업")
    print(f"[filer] 추가 대상 {len(el)}사 = 일반기업 {sum(1 for r in el if r[2] == '일반기업')}(재무 출처 " + " · ".join(f"{k} {n}" for k, n in src.items())
          + f") + 여전사 {sum(1 for r in el if r[2] == '금융')}", flush=True)
    for r in pilot(con):
        print(f"  [시범] {r[0]}\t{r[1]}\t{r[2]}\t{r[3] / 1e8:,.0f}억\t{r[4]}", flush=True)


def run(limit=None, months=22):
    con = connect()
    have = {r[0] for r in con.execute("SELECT corp_code FROM company")}
    found = scan(months)
    new = {cc: v for cc, v in found.items() if cc not in have}
    print(f"[filer_scan] 사업보고서 제출 비상장(기타법인) {len(found)}사 · DB에 이미 있음 {len(found) - len(new)} · 신규 후보 {len(new)}", flush=True)
    rows = []
    for i, (cc, v) in enumerate(sorted(new.items(), key=lambda x: x[1]["name"]), 1):
        g = classify(v["name"])
        info = {}
        if g in ("일반기업", "금융") and (not limit or i <= limit):
            try:
                info = dart_get("company", corp_code=cc)
            except Exception as e:  # noqa
                info = {"_err": str(e)[:60]}
            nm_full = info.get("corp_name") or ""
            if nm_full and g == "일반기업" and classify(nm_full) != "일반기업":
                g = classify(nm_full)
        eq, fy, src = equity_of(cc, v["rcept_no"], v["report_nm"]) if g in ("일반기업", "금융") else (None, None, "")
        rows.append({"corp_code": cc, "name": v["name"], "group": g, "equity_eok": (round(eq / 1e8) if eq is not None else ""), "fy": fy or "", "eq_src": src,
                     "induty_code": info.get("induty_code", ""),
                     "ceo": info.get("ceo_nm", ""), "est_dt": info.get("est_dt", ""), "adres": (info.get("adres") or "")[:40],
                     "report": v["report_nm"], "rcept_dt": v["rcept_dt"], "rcept_no": v["rcept_no"]})
        if i % 100 == 0:
            print(f"[filer_scan] 개황 {i}/{len(new)}", flush=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["corp_code"])
        w.writeheader()
        w.writerows(rows)
    con.execute("""CREATE TABLE IF NOT EXISTS filer_candidate (corp_code TEXT PRIMARY KEY, name TEXT, grp TEXT, equity REAL, fy TEXT, eq_src TEXT,
                   ksic TEXT, report TEXT, rcept_no TEXT, scanned_at TEXT)""")
    con.executemany("INSERT OR REPLACE INTO filer_candidate VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                    [(r["corp_code"], r["name"], r["group"], (r["equity_eok"] * 1e8 if r["equity_eok"] != "" else None), r["fy"], r["eq_src"],
                      r["induty_code"], r["report"], r["rcept_no"]) for r in rows])
    con.commit()
    gen = [r for r in rows if r["group"] in ("일반기업", "금융")]
    big = [r for r in gen if r["equity_eok"] != "" and r["equity_eok"] >= 500]
    unk = [r for r in gen if r["equity_eok"] == ""]
    print(f"[filer_scan] 일반기업·금융 {len(gen)}사 중 자본총계 500억 이상 {len(big)}사(금융 {sum(1 for r in big if r['group'] == '금융')}) · 미만 {len(gen) - len(big) - len(unk)}사 · 확인 불가 {len(unk)}사", flush=True)
    by = Counter(r["group"] for r in rows)
    print("[filer_scan] 분류: " + " · ".join(f"{k} {n}" for k, n in by.most_common()), flush=True)
    ks = Counter((r["induty_code"] or "")[:2] for r in rows if r["group"] == "일반기업")
    print("[filer_scan] 일반기업 업종(KSIC 앞 2자리) 상위: " + " · ".join(f"{k or '?'} {n}" for k, n in ks.most_common(15)), flush=True)
    print("[filer_scan] 자본총계 500억 이상(TSV, 큰 순): corp_code\tname\t구분\t자본총계(억)\tKSIC\t출처", flush=True)
    for r in sorted(big, key=lambda r: -r["equity_eok"]):
        print(f"  {r['corp_code']}\t{r['name']}\t{r['group']}\t{r['equity_eok']:,}\t{r['induty_code']}\t{r['eq_src']}", flush=True)
    for r in unk[:30]:
        print(f"  [확인 불가] {r['corp_code']}\t{r['name']}\t{r['eq_src']}", flush=True)
    print(f"[filer_scan] 저장: {OUT.name} ({len(rows)}행) — DB 변경 없음", flush=True)
    return rows
