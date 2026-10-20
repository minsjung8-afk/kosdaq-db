"""기발행 메자닌(CB·BW·EB·종류주식/RCPS) 수집
1) 발행결정 공시(주요사항보고서 API, 최근 3년): cvbdIsDecsn(전환사채) / bdwtIsDecsn(신주인수권부사채) / exbdIsDecsn(교환사채)
   / piicDecsn(유상증자 중 '기타주식' 발행 = 종류주식·RCPS 후보). 조건·리픽싱 최저가·만기·금리.
2) 미상환 현황: 최신 정기보고서(사업·반기·분기) 본문 'I.회사의 개요 - 자본금 변동사항'의
   '미상환 전환사채 / 신주인수권부사채 / 교환사채 발행현황' 표 파싱. 잔액·전환가·전환가능주식수.
※ DART 정기보고서 주요정보 API에는 CB/BW 항목이 없다(조건부자본증권과 혼동 주의). 그래서 본문 파싱.
호출량: 회사당 발행결정 4회 + 목록 1회 + 원문 1회 = 6회 → 1,800사 기준 약 1.1만 회(일일 한도 분할 실행).
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, dart_get, log, now, to_num  # noqa: E402
from business import _strip, fetch_document_xml  # noqa: E402

DATE_RE = re.compile(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})")
NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")

ISSUE_APIS = (
    ("cvbdIsDecsn", "CB", {"prc": "cv_prc", "rt": "cv_rt", "bgd": "cvrqpd_bgd", "edd": "cvrqpd_edd", "cnt": "cvisstk_cnt", "vs": "cvisstk_tisstk_vs", "knd": "cvisstk_knd"}),
    ("bdwtIsDecsn", "BW", {"prc": "ex_prc", "rt": "ex_rt", "bgd": "expd_bgd", "edd": "expd_edd", "cnt": "nstk_isstk_cnt", "vs": "nstk_isstk_tisstk_vs", "knd": "nstk_isstk_knd"}),
    ("exbdIsDecsn", "EB", {"prc": "ex_prc", "rt": "ex_rt", "bgd": "exrqpd_bgd", "edd": "exrqpd_edd", "cnt": "extg_stkcnt", "vs": "extg_tisstk_vs", "knd": "extg"}),
)


def _d(s):
    """'2025년 03월 15일' / '2025.03.15' / '2025-03-15' → 'YYYY-MM-DD'"""
    m = DATE_RE.search(s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _purpose(r):
    keys = (("fdpp_fclt", "시설"), ("fdpp_op", "운영"), ("fdpp_dtrp", "채무상환"), ("fdpp_ocsa", "타법인취득"), ("fdpp_bsninh", "영업양수"), ("fdpp_etc", "기타"))
    parts = []
    for k, nm in keys:
        v = to_num(r.get(k))
        if v:
            parts.append(f"{nm} {v/1e8:,.0f}억")
    return " · ".join(parts)


# ---------- 1-a) 발행결정 본문에서 풋/콜 조건 ----------
def extract_put_call(xml_text, pymd=None):
    """주요사항보고서 본문의 '조기상환청구권(Put Option)' / '매도청구권(Call Option)' 서술 → (put_dt, put_txt, call_txt)
    put_dt: 납입일 이후 첫 날짜(최초 조기상환청구 가능일). put_txt: '발행일+24개월부터 매 3개월' 식 요약."""
    txt = _strip(xml_text)
    flat = re.sub(r"\s+", " ", txt)

    def section(keys, length=1500):
        for k in keys:
            i = flat.find(k)
            if i >= 0:
                return flat[i:i + length]
        return ""

    put = section(["조기상환청구권", "Put Option", "풋옵션"])
    put_dt = put_txt = None
    if put and "해당사항 없음" not in put[:200] and "해당 사항 없음" not in put[:200]:
        ds = [f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" for m in DATE_RE.finditer(put)]
        ds = [x for x in ds if not pymd or x > pymd]
        put_dt = min(ds) if ds else None
        m1 = re.search(r"발행일로부터\s*(\d+)\s*(개월|년)", put)
        m2 = re.search(r"매\s*(\d+)\s*개월", put)
        parts = []
        if m1:
            parts.append(f"발행일+{m1.group(1)}{m1.group(2)}")
        if m2:
            parts.append(f"이후 매 {m2.group(1)}개월")
        put_txt = " ".join(parts) or put[:120]
    call = section(["매도청구권", "Call Option", "콜옵션"], 600)
    call_txt = None
    if call and "해당사항 없음" not in call[:200] and "해당 사항 없음" not in call[:200]:
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", call)
        mc = re.search(r"발행일로부터\s*(\d+)\s*(개월|년)(?:[^.,]{0,25}?(\d+)\s*(개월|년))?", call)
        rng = (f"발행일+{mc.group(1)}{mc.group(2)}" + (f"~{mc.group(3)}{mc.group(4)}" if mc.group(3) else "")) if mc else ""
        call_txt = " · ".join(x for x in (rng, f"최대 {m.group(1)}%" if m else "") if x) or "있음"
    return put_dt, put_txt, call_txt


# ---------- 1) 발행결정 ----------
def fetch_issue_decisions(con, corp_code, years=3):
    bgn = (date.today() - timedelta(days=365 * years)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    n = 0
    for ep, kind, f in ISSUE_APIS:
        js = dart_get(ep, corp_code=corp_code, bgn_de=bgn, end_de=end)
        for r in js.get("list", []):
            con.execute("""INSERT OR REPLACE INTO mezz_issue (corp_code, rcept_no, kind, tm, bd_knd, face_amt, intr_ex, intr_sf, mtd, method,
                           cv_prc, cv_rt, cv_bgd, cv_edd, lw_prc, stock_cnt, stock_vs, stock_knd, pymd, bddd, purpose, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (corp_code, r["rcept_no"], kind, r.get("bd_tm"), r.get("bd_knd"), to_num(r.get("bd_fta")),
                         to_num(r.get("bd_intr_ex")), to_num(r.get("bd_intr_sf")), _d(r.get("bd_mtd")), r.get("bdis_mthn"),
                         to_num(r.get(f["prc"])), to_num(r.get(f["rt"])), _d(r.get(f["bgd"])), _d(r.get(f["edd"])),
                         to_num(r.get("act_mktprcfl_cvprc_lwtrsprc")), to_num(r.get(f["cnt"])), to_num(r.get(f["vs"])), r.get(f["knd"]),
                         _d(r.get("pymd")), _d(r.get("bddd")), _purpose(r), now()))
            n += 1
            # 풋·콜 조건은 API에 없어 공시 본문에서 추출(건당 1호출, 한 번만)
            if not con.execute("SELECT put_txt FROM mezz_issue WHERE corp_code=? AND rcept_no=? AND kind=? AND put_txt IS NOT NULL", (corp_code, r["rcept_no"], kind)).fetchone():
                try:
                    pd_, pt, ct = extract_put_call(fetch_document_xml(r["rcept_no"]), _d(r.get("pymd")))
                    con.execute("UPDATE mezz_issue SET put_dt=?, put_txt=?, call_txt=? WHERE corp_code=? AND rcept_no=? AND kind=?",
                                (pd_, pt or "-", ct, corp_code, r["rcept_no"], kind))
                except Exception as e:  # noqa
                    log(con, corp_code, "mezz_put", "fail", f"{r['rcept_no']}: {e}")
    # 유상증자 결정 중 기타주식(종류주식) 발행 → RCPS/CPS 후보
    js = dart_get("piicDecsn", corp_code=corp_code, bgn_de=bgn, end_de=end)
    for r in js.get("list", []):
        est = to_num(r.get("nstk_estk_cnt"))
        if not est:
            continue
        con.execute("""INSERT OR REPLACE INTO mezz_issue (corp_code, rcept_no, kind, tm, bd_knd, face_amt, intr_ex, intr_sf, mtd, method,
                       cv_prc, cv_rt, cv_bgd, cv_edd, lw_prc, stock_cnt, stock_vs, stock_knd, pymd, bddd, purpose, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, r["rcept_no"], "종류주식", None, "유상증자(기타주식)", None, None, None, None, r.get("ic_mthn"),
                     None, None, None, None, None, est, None, "기타주식(종류주식·RCPS 등 - 공시 확인)", None, None, _purpose(r), now()))
        n += 1
    con.commit()
    return n


# ---------- 2) 미상환 현황(정기보고서 본문) ----------
def latest_periodic_report(corp_code):
    """최근 15개월 내 정기보고서(사업·반기·분기) 중 최신 1건 → (rcept_no, report_nm)"""
    bgn = (date.today() - timedelta(days=455)).strftime("%Y%m%d")
    js = dart_get("list", corp_code=corp_code, bgn_de=bgn, end_de=date.today().strftime("%Y%m%d"), pblntf_ty="A", page_count=100)
    cands = [r for r in js.get("list", []) if re.search(r"(사업|반기|분기)보고서", r["report_nm"])]
    if not cands:
        return None, None
    cands.sort(key=lambda r: r["rcept_dt"], reverse=True)
    return cands[0]["rcept_no"], cands[0]["report_nm"]


def _classify(header_text):
    t = header_text.replace(" ", "")
    if "조건부" in t:
        return None
    if "교환" in t:
        return "EB"
    if "신주인수권" in t or "행사가" in t:
        return "BW"
    if "전환" in t:
        return "CB"
    return None


UNIT = {"원": 1, "천원": 1e3, "백만원": 1e6, "억원": 1e8}
PARSER_VER = 2       # 파서 변경 시 올리면 mezz_checked가 무효화되어 재파싱


def _unit(text):
    m = re.search(r"단위\s*[:：]\s*([천백억만]*원)", text)
    return UNIT.get(m.group(1), 1) if m else 1


def extract_outstanding(xml_text):
    """정기보고서 'III-7. 증권의 발행을 통한 자금조달에 관한 사항'(구양식: 'I-3. 자본금 변동사항')의
    '미상환 전환사채 / 신주인수권부사채 등 / 교환사채 발행현황' 표 → [dict]. 금액은 표 단위(백만원 등)를 원으로 환산."""
    out = []
    for sec in re.findall(r"<SECTION-2[^>]*>(.*?)</SECTION-2>", xml_text, flags=re.S | re.I):
        m = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", sec, flags=re.S | re.I)
        title = _strip(m.group(1)).replace(" ", "") if m else ""
        if not ("자금조달" in title or "자본금" in title or "증권의발행" in title):
            continue
        for tm in re.finditer(r"<TABLE[^>]*>(.*?)</TABLE>", sec, flags=re.S | re.I):
            lead = _strip(sec[max(0, tm.start() - 2500):tm.start()])[-400:]     # 표 앞 제목·기준일·단위(작은 표 포함)
            tbl = tm.group(1)
            rows = [[_strip(c).replace("\n", " ").strip() for c in re.findall(r"<T[DHE][^>]*>(.*?)</T[DHE]>", tr, flags=re.S | re.I)]
                    for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", tbl, flags=re.S | re.I)]
            if not rows:
                continue
            head = " ".join(" ".join(r) for r in rows[:3])
            if "미상환" not in (lead + head) or not re.search(r"전환|행사|교환", head):
                continue
            kind = _classify(lead[-80:] + " " + head)
            if not kind:
                continue
            unit = _unit(lead)
            for cells in rows:
                cells = [c for c in cells if c]
                if len(cells) < 4:
                    continue
                if any(k in cells[0] for k in ("합계", "합 계", "종류", "구분")):
                    continue
                dates = [c for c in cells if DATE_RE.search(c) and "~" not in c]
                if len(dates) < 1:
                    continue
                period = next((c for c in cells if "~" in c), None)
                nums = [to_num(c.replace(",", "")) for c in cells if NUM_RE.match(c.replace(" ", "").replace(",", "")) and c not in dates]
                nums = [v for v in nums if v is not None]
                if len(nums) < 3:
                    continue
                if len(nums) >= 5:
                    face, rt, prc, rem, stock = nums[0], nums[1], nums[2], nums[-2], nums[-1]
                elif len(nums) == 4:
                    face, rt, prc, rem, stock = nums[0], None, nums[1], nums[2], nums[3]
                else:
                    face, rt, prc, rem, stock = nums[0], None, None, nums[1], nums[2]
                if rt is not None and rt > 1000:          # 비율 자리에 금액이 온 경우(전환비율 열 없음) 보정
                    face, rt, prc, rem, stock = nums[0], None, nums[1], nums[-2], nums[-1]
                name = cells[0]
                tm_cell = next((c for c in cells[1:3] if re.search(r"\d+\s*회", c)), None)   # 회차 열이 따로 있으면 이름 앞에 붙임
                if tm_cell and tm_cell not in name:
                    name = f"{tm_cell} {name}"
                out.append({"kind": kind, "name": name[:80], "isu_dd": _d(dates[0]), "exprd_dd": _d(dates[1]) if len(dates) > 1 else None,
                            "face_amt": face * unit, "cv_rt": rt, "cv_prc": prc, "remndr_amt": rem * unit, "remndr_stock": stock, "period": period})
    return out


def fetch_outstanding(con, corp_code):
    rcept, nm = latest_periodic_report(corp_code)
    if not rcept:
        return 0
    checked = con.execute("SELECT report_nm FROM mezz_checked WHERE corp_code=? AND rcept_no=?", (corp_code, rcept)).fetchone()
    if checked and (checked[0] or "").endswith(f"#v{PARSER_VER}"):
        return -1                                          # 이미 이 보고서를 현재 파서로 처리함
    xml = fetch_document_xml(rcept)
    rows = extract_outstanding(xml)
    con.execute("DELETE FROM mezz_outstanding WHERE corp_code=?", (corp_code,))
    for r in rows:
        con.execute("""INSERT INTO mezz_outstanding (corp_code, rcept_no, report_nm, kind, name, isu_dd, exprd_dd, face_amt, cv_rt, cv_prc,
                       remndr_amt, remndr_stock, period, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (corp_code, rcept, nm, r["kind"], r["name"], r["isu_dd"], r["exprd_dd"], r["face_amt"], r["cv_rt"], r["cv_prc"],
                     r["remndr_amt"], r["remndr_stock"], r["period"], now()))
    con.execute("INSERT OR REPLACE INTO mezz_checked VALUES (?,?,?,?)", (corp_code, rcept, f"{nm}#v{PARSER_VER}", now()))
    con.commit()
    return len(rows)


def run(corp_codes=None, years=3, new_only=False):
    con = connect()
    cs = [r[0] for r in con.execute("SELECT corp_code FROM company ORDER BY corp_name")]
    if corp_codes:
        cs = [c for c in cs if c in corp_codes]
    if new_only:
        done = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM mezz_checked WHERE report_nm LIKE ?", (f"%#v{PARSER_VER}",))}
        cs = [c for c in cs if c not in done]
        print(f"[mezz] --new-only: 미수집 {len(cs)}사", flush=True)
    iss = outs = 0
    recent = {r[0] for r in con.execute("SELECT corp_code FROM fetch_log WHERE job='mezz_issue' AND status='ok' AND at >= date('now','-7 days')")}
    for i, cc in enumerate(cs, 1):
        if cc not in recent:                               # 발행결정 API 4종은 7일 내 조회했으면 생략(한도 절약)
            try:
                iss += fetch_issue_decisions(con, cc, years)
                log(con, cc, "mezz_issue", "ok")
            except Exception as e:  # noqa
                log(con, cc, "mezz_issue", "fail", e)
        try:
            n = fetch_outstanding(con, cc)
            if n > 0:
                outs += 1
            log(con, cc, "mezz", "ok", n)
        except Exception as e:  # noqa
            log(con, cc, "mezz", "fail", e)
        if i % 100 == 0:
            print(f"[mezz] {i}/{len(cs)}, 발행결정 {iss}건, 미상환 보유 {outs}사", flush=True)
    log(con, "-", "mezz", "ok", f"{len(cs)} companies / issues {iss} / outstanding {outs}")
    return con


if __name__ == "__main__":
    print(extract_outstanding(open(sys.argv[1], encoding="utf-8").read()))
