"""사업보고서 제출 비상장사 전수 명단 — 드라이런(DB 변경 없음) (2026-10-28 사용자 요청)

배경: 당근마켓·무신사·키움에프앤아이처럼 비상장이지만 사업보고서(정기보고서)를 내는 회사는 상장사와 같은 수준의 자료가 나온다.
등급 보유 여부와 무관하게 이런 회사를 명단으로 뽑아 추가 후보로 검토한다.
- DART 공시검색(list) 정기공시 사업보고서(A001), 법인구분 기타(E = 비상장), 최근 22개월(FY2024·FY2025 보고서)
  · 고유번호 없이 검색하면 기간이 3개월로 제한 → 3개월 구간으로 나눠 조회(구간당 100건씩 페이지)
- 이미 DB에 있는 회사 제외 → 개황(company) 1콜로 업종(KSIC)·대표자·설립일 보강
- 분류(unlisted2와 같은 규칙): 금융 / 공공 / SPC·리츠·펀드 / 일반기업(= 추가 후보)
출력: 콘솔 요약 + unlisted_candidates.csv(아티팩트, 워크플로가 자동 보관)
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
        if g == "일반기업" and (not limit or i <= limit):
            try:
                info = dart_get("company", corp_code=cc)
            except Exception as e:  # noqa
                info = {"_err": str(e)[:60]}
            nm_full = info.get("corp_name") or ""
            if nm_full and classify(nm_full) != "일반기업":
                g = classify(nm_full)
        rows.append({"corp_code": cc, "name": v["name"], "group": g, "induty_code": info.get("induty_code", ""),
                     "ceo": info.get("ceo_nm", ""), "est_dt": info.get("est_dt", ""), "adres": (info.get("adres") or "")[:40],
                     "report": v["report_nm"], "rcept_dt": v["rcept_dt"], "rcept_no": v["rcept_no"]})
        if i % 100 == 0:
            print(f"[filer_scan] 개황 {i}/{len(new)}", flush=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["corp_code"])
        w.writeheader()
        w.writerows(rows)
    by = Counter(r["group"] for r in rows)
    print("[filer_scan] 분류: " + " · ".join(f"{k} {n}" for k, n in by.most_common()), flush=True)
    ks = Counter((r["induty_code"] or "")[:2] for r in rows if r["group"] == "일반기업")
    print("[filer_scan] 일반기업 업종(KSIC 앞 2자리) 상위: " + " · ".join(f"{k or '?'} {n}" for k, n in ks.most_common(15)), flush=True)
    print("[filer_scan] 일반기업 명단(TSV): corp_code\tname\tKSIC\t최근 보고서", flush=True)
    for r in rows:
        if r["group"] == "일반기업":
            print(f"  {r['corp_code']}\t{r['name']}\t{r['induty_code']}\t{r['report']} ({r['rcept_dt']})", flush=True)
    print(f"[filer_scan] 저장: {OUT.name} ({len(rows)}행) — DB 변경 없음", flush=True)
    return rows
