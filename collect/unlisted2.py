"""등급 보유 비상장사 전수 명단(ICR + 일반사채 + 기업어음 + 전자단기사채) — 드라이런, DB 변경 없음.

2026-10-25 사용자 요청: ICR만 보면 롯데건설·SK에코플랜트·포스코이앤씨처럼 회사채·CP 등급만 있는 대형 비상장사가 빠진다 → 사채·어음 등급 포함.
- 금감원 신용평가서 목록은 pageUnit=1000이 먹는다(1쪽 ≈2초) → 15개월치 ≈1.5만건도 1~2분.
- 유동화(ABS·ABCP·ABSTB) 행은 제외. 회사별로 ICR/사채/CP 최신 등급을 모은다.
- DART corpCode.xml(1콜)로 고유번호·종목코드 매칭. 금감원은 '에스케이에너지(주)', DART는 'SK에너지'처럼 표기가 달라
  한글 음차(에스케이→SK 등)를 양쪽에 적용해 맞춘다.
- 분류: 이미 수록 / 상장사(DB 상장·종목코드 있음) / 금융 / 공공 / SPC / 외은지점 / 일반기업(= 추가 후보)
출력: 콘솔 TSV(로그에서 바로 읽기) + unlisted_candidates.xlsx(아티팩트)
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
from fss_rating import parse  # noqa: E402

URL = "https://www.fss.or.kr/fss/job/creditRatingReport/list.do"
TYPES = ("기업신용평가", "일반사채", "기업어음", "전자단기사채")
ABS = re.compile(r"유동화|ABS|ABCP|ABSTB|자산담보", re.I)
TR = [("케이티앤지", "KT&G"), ("디에이치", "DH"), ("에스에프에이", "SFA"), ("에스티엑스", "STX"), ("에이치엘비", "HLB"), ("엔에스", "NS"), ("엘아이지", "LIG"), ("케이지", "KG"), ("에이치비", "HB"), ("에스디비", "SDB"), ("와이지", "YG"), ("제이와이피", "JYP"), ("디와이", "DY"), ("케이지씨", "KGC"), ("에이치디씨", "HDC"), ("에이치엘디앤아이", "HL디앤아이"), ("에이치엠엠", "HMM"), ("에스케이", "SK"), ("지에스", "GS"),
      ("엘지", "LG"), ("씨제이", "CJ"), ("케이티", "KT"), ("에이치디", "HD"), ("디비", "DB"), ("엘에스", "LS"), ("케이비", "KB"), ("비엔케이", "BNK"),
      ("디지비", "DGB"), ("에이치엘", "HL"), ("엘엑스", "LX"), ("케이씨씨", "KCC"), ("에이제이", "AJ"), ("오씨아이", "OCI"), ("티와이", "TY"),
      ("에이케이", "AK"), ("디엘", "DL"), ("엔에이치", "NH"), ("아이비케이", "IBK"), ("제이비", "JB"), ("에스비에스", "SBS"), ("에스지씨", "SGC"),
      ("에쓰오일", "S-OIL"), ("티씨씨", "TCC"), ("에스알", "SR"), ("케이이씨", "KEC"), ("피케이씨", "PKC"), ("비에스", "BS"), ("에스엠", "SM"),
      ("에이치에스", "HS"), ("디엔", "DN"), ("에스케이", "SK"), ("에프앤비", "F&B"), ("이앤씨", "E&C"), ("이엔에스", "E&S"), ("앤", "&")]
FIN = re.compile(r"은행|저축|카드|증권|캐피탈|신탁|보험|생명|화재|손해|금융|자산운용|대부|리스|종금|종합금융|파이낸셜|파이낸스|여신|크레디|펀드|인베스트|벤처투자|에셋|머니|뱅크|에프앤아이|F&I|투자증권|넥스트$", re.I)
PUB = re.compile(r"공사$|공단$|공사\(|공제|중앙회|협동조합|조합$|진흥|재단|학교|대학|철도공|항만공|기금|정부|상공회의소|협회|^국립|^한국전력|^한국\w*발전$|^한국\w*공사|^한국수력|^한국가스공사|^한국지역난방|^한국도로|^한국토지|^한국석유공사|^한국수자원|^국민|^농협|^수협|^한전")
SPC = re.compile(r"투융자회사|인프라펀드|제\s*[일이삼사오육칠팔구십백\d]+\s*차|제\s*\d+\s*호|유동화|에스피씨|SPC|PFV|피에프브이|프로젝트금융|개발사업|리츠|REIT|위탁관리|부동산투자회사|도시고속화도로|민자|터널|고속도로\(주\)", re.I)


def _base(s):
    return re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(사\)|\(학\)|농업회사법인|[\s.·,]", "", s or "").upper()


def keys(s):
    b = _base(s)
    t = b
    for k, v in TR:
        t = t.replace(k.upper(), v)
    return {b, t, t.replace("&", "앤"), b.replace("&", "앤")}


def fetch(ttype, sdate, edate, page):
    params = {"menuNo": "200411", "pageIndex": page, "pageUnit": 1000, "searchCompany": "", "searchAssGbn": "", "searchTargetCoNm": "",
              "searchTargetTypeKor": ttype, "searchCnd": "1", "sdate": sdate, "edate": edate}
    for i in range(4):
        try:
            r = requests.get(URL + "?" + urlencode(params), headers=UA, timeout=(15, 120))
            r.raise_for_status()
            return parse(r.text)
        except Exception as e:  # noqa
            if i == 3:
                raise
            print(f"[unlisted2] {ttype} p{page} 재시도: {e}", flush=True)
            time.sleep(10 * (i + 1))


def dart_corps():
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": api_key()}, headers=UA, timeout=180)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml = z.read(z.namelist()[0]).decode("utf-8", errors="ignore")
    idx = {}
    import html as _h
    for m in re.finditer(r"<corp_code>(\d+)</corp_code>\s*<corp_name>(.*?)</corp_name>.*?<stock_code>\s*(\S*?)\s*</stock_code>\s*<modify_date>(\d*)</modify_date>", xml, flags=re.S):
        nm = _h.unescape(m.group(2))                       # XML의 &amp; → & (대륜E&S·KT&G 매칭 누락 원인)
        for k in keys(nm):
            idx.setdefault(k, []).append((m.group(1), nm, m.group(3).strip() or None, m.group(4)))
    return idx


def pick(hits, db):
    """동명 여러 사: ① DB에 있는 코드 ② 종목코드 있는 코드(상장·상장폐지) 1개 ③ DART 최근 갱신(modify_date) 코드"""
    if len(hits) <= 1:
        return hits[0] if hits else None
    in_db = [h for h in hits if h[0] in db]
    if len(in_db) == 1:
        return in_db[0]
    st = [h for h in hits if h[2]]
    if len(st) == 1:
        return st[0]
    # ④ 최근 15개월 공시가 있는 코드가 하나뿐이면 그것(DART list 1콜/후보, 최대 6) — 휴면 동명 법인 배제
    try:
        from common import dart_get
        bgn = (date.today() - timedelta(days=456)).strftime("%Y%m%d")
        live = [h for h in hits[:6] if dart_get("list", corp_code=h[0], bgn_de=bgn, page_count=1).get("list")]
        if len(live) == 1:
            return live[0]
    except Exception as e:  # noqa
        print(f"[unlisted2] 동명 확인 실패 {hits[0][1]}: {e}", flush=True)
    return None                    # 판별 불가 → 'DART 동명 확인필요'로 남기고 자동 추가하지 않음


def run(days=456, out_path="unlisted_candidates.xlsx"):
    con = connect()
    today = date.today()
    sdate, edate = (today - timedelta(days=days)).isoformat(), today.isoformat()
    comp = {}
    t0 = time.time()
    for tt in TYPES:
        page, n = 1, 0
        while True:
            rows, total = fetch(tt, sdate, edate, page)
            n += len(rows)
            for r in rows:
                if ABS.search(r["ttype"]):
                    continue
                cls = "ICR" if "기업신용평가" in r["ttype"] else "BOND" if "일반사채" in r["ttype"] else "CP"
                d = comp.setdefault(r["target"], {"name": r["target"], "ag": set(), "ICR": None, "BOND": None, "CP": None})
                d["ag"].add(r["agency"].replace("신용평가", "").replace("한국기업평가", "한기평"))
                if not d[cls] or r["disc_dt"] > d[cls][0]:
                    d[cls] = (r["disc_dt"], r["grade"])
            if not rows or n >= total:
                break
            page += 1
            time.sleep(1)
        print(f"[unlisted2] {tt}: {n:,}건 (누적 회사 {len(comp)})", flush=True)
    idx = dart_corps()
    db = {r[0]: (r[1], r[2]) for r in con.execute("SELECT corp_code, market, delisted_dt FROM company")}
    out = []
    for d in comp.values():
        hits = {}
        for k in keys(d["name"]):
            for h in idx.get(k, []):
                hits[h[0]] = h
        hits = list(hits.values())
        nm0 = _base(d["name"])
        web_listed = False
        if not hits and not (nm0.endswith("지점") or SPC.search(d["name"]) or FIN.search(nm0) or PUB.search(nm0)):
            try:                                # corpCode.xml에 없는 이름 = 대개 사명 변경(신평 공시는 옛 사명) → DART 웹 검색은 옛 사명도 찾는다
                import dart_web
                w = dart_web.corp_lookup(re.sub(r"\(주\)|주식회사", "", d["name"]).strip())
                time.sleep(0.5)
                if len(w) == 1:
                    hits = [(w[0][0], w[0][2], None if w[0][1] in ("기", "") else "Y", "")]
                    web_listed = w[0][1] in ("유", "코", "넥")
            except Exception as e:  # noqa
                print(f"[unlisted2] 웹 사명 조회 실패 {d['name']}: {e}", flush=True)
        need = not (nm0.endswith("지점") or SPC.search(d["name"]) or FIN.search(nm0) or PUB.search(nm0))
        h = pick(hits, db) if (need or len(hits) <= 1) else (next((x for x in hits if x[0] in db), None))
        cc = h[0] if h else ""
        stock = h[2] if h else None
        nm = _base(d["name"])
        mk = db.get(cc, (None, None))
        if cc and mk[0] == "UNLISTED":
            g = "이미 수록"
        elif cc and mk[0] in ("KOSPI", "KOSDAQ", "KONEX") and not mk[1]:
            g = "상장사"
        elif nm.endswith("지점"):
            g = "외은지점"
        elif SPC.search(d["name"]):
            g = "SPC"
        elif FIN.search(nm):
            g = "금융"
        elif PUB.search(nm):
            g = "공공"
        elif (stock and cc not in db) or web_listed:
            g = "상장사(DB 미수록)"
        else:
            g = "일반기업"
        sub = "ICR보유" if d["ICR"] else "사채·CP만"
        note = ("DART 미확인" if not hits else f"DART 동명 {len(hits)}사 확인필요: " + ",".join(x[0] for x in hits[:4])) if not cc else \
               (f"동명 {len(hits)}사 중 선택" if len(hits) > 1 else "")
        grade = (d["ICR"] or d["BOND"] or d["CP"])[1]
        out.append({"구분": g, "세부": sub, "회사명": d["name"], "대표등급": grade, "ICR": d["ICR"][1] if d["ICR"] else "",
                    "회사채": d["BOND"][1] if d["BOND"] else "", "CP": d["CP"][1] if d["CP"] else "",
                    "평가사": "·".join(sorted(d["ag"])), "DART고유번호": cc, "비고": note})
    order = {"일반기업": 0, "상장사(DB 미수록)": 1, "이미 수록": 2, "금융": 3, "공공": 4, "SPC": 5, "외은지점": 6, "상장사": 7}
    out.sort(key=lambda r: (order.get(r["구분"], 9), r["세부"], r["회사명"]))
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "등급 비상장사"
        cols = list(out[0].keys())
        ws.append(cols)
        for r in out:
            ws.append([r[c] for c in cols])
        wb.save(out_path)
    except Exception as e:  # noqa
        print("[unlisted2] xlsx 저장 실패:", e)
    from collections import Counter
    cnt = Counter((r["구분"], r["세부"]) for r in out)
    print(f"[unlisted2] 회사 {len(out)} · 소요 {(time.time() - t0) / 60:.1f}분")
    for (g, s), n in sorted(cnt.items(), key=lambda x: (order.get(x[0][0], 9), x[0][1])):
        print(f"  {g} / {s}: {n}")
    print("[unlisted2] ===== TSV 시작 =====")
    for r in out:
        if r["구분"] in ("일반기업", "상장사(DB 미수록)", "이미 수록"):
            print("TSV\t" + "\t".join(str(r[c]) for c in ("구분", "세부", "회사명", "대표등급", "ICR", "회사채", "CP", "평가사", "DART고유번호", "비고")))
    print("[unlisted2] ===== TSV 끝 =====", flush=True)
    return out
