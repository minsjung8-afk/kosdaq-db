"""비상장 등급사 드라이런: 금감원 신용평가서 공시 전체 목록(최근 N년) → 회사별 집계 → 상장사 제외 → SPC 필터 → 후보 명단(xlsx)
  python run.py unlisted --years 3            # 드라이런(명단만, DB 변경 없음) → unlisted_candidates.xlsx + 콘솔 요약 (기본: 평가대상종류=기업신용평가)
  python run.py unlisted --type ""           # 전수(28만건·78시간 — 실행 금지 수준) / --type 회사채 등 다른 종류
판정 규칙:
  ① 평가대상 종류: 기업신용평가(ICR) 보유 = 1군, 회사채·사모사채·기업어음만 = 2군, 유동화(ABS·ABSTB·ABCP 등)만 = SPC 추정
  ② 회사명 패턴: 제N차/제N호/유동화/에스피씨/SPC/PFV/피에프브이/자산관리/신탁/투자목적/제일차…  → SPC 추정
  ③ DART 고유번호 목록에 회사명이 있으면 'DART 확인', 없으면 보류
"""
import io
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import UA, api_key, connect  # noqa: E402
from fss_rating import parse, norm  # noqa: E402

URL = "https://www.fss.or.kr/fss/job/creditRatingReport/list.do"
SPC_NAME = re.compile(r"제\s*[일이삼사오육칠팔구십백\d]+\s*차|제\s*\d+\s*호|유동화|에스피씨|SPC|PFV|피에프브이|자산관리|부동산신탁|투자목적|캐피탈제|제[일이삼사오육칠팔구십]+유|"
                      r"ABS|ABCP|프로젝트금융|개발사업|지역주택조합|도시개발|리츠|REIT", re.I)
ABS_TYPE = re.compile(r"유동화|ABS|ABSTB|ABCP|자산담보|STB|전자단기사채\(유동화\)", re.I)
ICR_TYPE = re.compile(r"기업신용평가|ICR|기업신용등급", re.I)
BOND_TYPE = re.compile(r"사채|기업어음|CP|채권", re.I)


def fetch_page(years, page, ttype=""):
    today = date.today()
    params = {"menuNo": "200411", "pageIndex": page, "searchCompany": "", "searchAssGbn": "", "searchTargetCoNm": "",
              "searchTargetTypeKor": ttype, "searchCnd": "1", "sdate": (today - timedelta(days=365 * years)).isoformat(), "edate": today.isoformat()}
    r = requests.get(URL + "?" + urlencode(params), headers=UA, timeout=(15, 60))
    r.raise_for_status()
    return r.text


def dart_names():
    """DART corpCode.xml → {정규화명: (corp_code, stock_code)} (1콜, zip)"""
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": api_key()}, headers=UA, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml = z.read(z.namelist()[0]).decode("utf-8", errors="ignore")
    out = {}
    for m in re.finditer(r"<corp_code>(\d+)</corp_code>\s*<corp_name>(.*?)</corp_name>\s*<corp_eng_name>.*?</corp_eng_name>\s*<stock_code>\s*(\S*?)\s*</stock_code>", xml, flags=re.S):
        out.setdefault(norm(m.group(2)), (m.group(1), m.group(3).strip() or None))
    return out


def run(years=3, max_pages=None, out_path="unlisted_candidates.xlsx", ttype="기업신용평가"):
    """ttype: 금감원 평가대상종류 필터. 기본 '기업신용평가'(3년 ≈1.7천건·174p·30분). 빈 문자열이면 전수(3년 ≈28만건 — 비현실적)"""
    con = connect()
    listed = {norm(r[0]) for r in con.execute("SELECT corp_name FROM company")} | {norm(r[0]) for r in con.execute("SELECT corp_name_full FROM company WHERE corp_name_full IS NOT NULL")}
    comp = {}
    page, total_pages, fails = 1, None, 0
    t0 = time.time()
    while True:
        try:
            html = fetch_page(years, page, ttype)
            rows, total = parse(html)
            fails = 0
        except Exception as e:  # noqa
            fails += 1
            print(f"[unlisted] p{page} 실패 {e}", flush=True)
            if fails >= 5:
                print("[unlisted] 금감원 연속 실패 — 중단(지금까지 수집분으로 집계)", flush=True); break
            time.sleep(20); continue
        if total_pages is None:
            total_pages = max(1, (total + 9) // 10)
            print(f"[unlisted] 전체 {total:,}건 · {total_pages}페이지 · {years}년 · 종류='{ttype or '전체'}'", flush=True)
        for r in rows:
            d = comp.setdefault(norm(r["target"]), {"name": r["target"], "n": 0, "icr": 0, "bond": 0, "abs": 0, "agencies": set(), "last": "", "grade": "", "types": set()})
            d["n"] += 1; d["agencies"].add(r["agency"]); d["types"].add(r["ttype"])
            if ICR_TYPE.search(r["ttype"]): d["icr"] += 1
            elif ABS_TYPE.search(r["ttype"]): d["abs"] += 1
            elif BOND_TYPE.search(r["ttype"]): d["bond"] += 1
            if r["disc_dt"] > d["last"]:
                d["last"], d["grade"] = r["disc_dt"], r["grade"]
        if page % 50 == 0:
            print(f"[unlisted] {page}/{total_pages}p · 회사 {len(comp)} · {(time.time()-t0)/page:.1f}s/p", flush=True)
        if page >= total_pages or (max_pages and page >= max_pages) or not rows:
            break
        page += 1
        time.sleep(0.7)
    try:
        dn = dart_names()
    except Exception as e:  # noqa
        print("[unlisted] DART 고유번호 목록 실패:", e); dn = {}
    out = []
    for k, d in comp.items():
        if k in listed:
            continue
        spc_name = bool(SPC_NAME.search(d["name"]))
        in_dart = k in dn
        if d["icr"] and not spc_name:
            grp = "1군 채택후보(ICR)"
        elif d["abs"] and not d["icr"] and not d["bond"]:
            grp = "SPC 추정(유동화만)"
        elif spc_name:
            grp = "SPC 추정(명칭)"
        elif d["bond"]:
            grp = "2군 후보(사채·CP만)"
        else:
            grp = "보류"
        if grp.startswith(("1군", "2군")) and not in_dart:
            grp += " · DART 미확인"
        out.append({"구분": grp, "회사명": d["name"], "최근공시": d["last"], "최근등급": d["grade"], "건수": d["n"], "ICR": d["icr"], "사채CP": d["bond"],
                    "유동화": d["abs"], "평가사": "·".join(sorted(d["agencies"])), "평가대상종류": "·".join(sorted(d["types"]))[:120],
                    "DART고유번호": dn.get(k, ("", ""))[0]})
    out.sort(key=lambda r: (r["구분"], r["회사명"]))
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "후보"
        cols = list(out[0].keys()) if out else ["구분"]
        ws.append(cols)
        for r in out:
            ws.append([r[c] for c in cols])
        wb.save(out_path)
    except Exception as e:  # noqa
        import csv
        with open(out_path.replace(".xlsx", ".csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["구분"]); w.writeheader(); w.writerows(out)
        print("[unlisted] openpyxl 없음 → csv 저장:", e)
    from collections import Counter
    cnt = Counter(r["구분"] for r in out)
    print(f"[unlisted] 비상장 회사 {len(out)} (상장 제외) · 소요 {(time.time()-t0)/60:.1f}분")
    for g, n in sorted(cnt.items()):
        print(f"  {g}: {n}")
    return out


if __name__ == "__main__":
    run(years=int(sys.argv[1]) if len(sys.argv) > 1 else 3, max_pages=int(sys.argv[2]) if len(sys.argv) > 2 else None)
