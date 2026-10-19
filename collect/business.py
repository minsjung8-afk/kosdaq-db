"""DART 공시원문(document.xml) → 'II. 사업의 내용' 추출 + 감사의견·최대주주·주요공시"""
import html
import io
import re
import zipfile

from common import connect, dart_get, dart_get_binary, log, now, to_num

EVENT_FLAGS = {
    "유상증자": "유상증자", "전환사채": "CB", "신주인수권부사채": "BW", "교환사채": "EB",
    "감사보고서": "감사의견", "관리종목": "관리종목", "최대주주변경": "최대주주변경", "소송": "소송",
    "횡령": "횡령배임", "배임": "횡령배임", "거래정지": "거래정지", "상장폐지": "상장폐지",
    "회생": "회생", "채무": "채무", "담보제공": "담보제공", "타법인주식": "타법인출자", "자기주식": "자기주식",
}


# ---------- 사업보고서 찾기 ----------
def latest_annual_report(corp_code, bsns_year):
    """해당 사업연도 사업보고서 접수번호(정정본 포함 최신)"""
    bgn = f"{int(bsns_year) + 1}0101"
    end = f"{int(bsns_year) + 1}1231"
    js = dart_get("list", corp_code=corp_code, bgn_de=bgn, end_de=end, pblntf_ty="A", page_count=100)
    pat = re.compile(rf"사업보고서.*\({bsns_year}\.\d{{2}}\)")   # 결산월 무관
    cands = [r for r in js.get("list", []) if pat.search(r["report_nm"])]
    if not cands:
        return None
    cands.sort(key=lambda r: r["rcept_dt"], reverse=True)
    return cands[0]["rcept_no"]


# ---------- 원문 파싱 ----------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\xa0]+")
NL_RE = re.compile(r"\n{3,}")


def _strip(xml_fragment: str) -> str:
    t = re.sub(r"</T[DH]>", " | ", xml_fragment, flags=re.I)
    t = re.sub(r"</(P|TR|TITLE|TABLE|DIV|BR)[^>]*>", "\n", t, flags=re.I)
    t = TAG_RE.sub("", t)
    t = html.unescape(t)
    t = WS_RE.sub(" ", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    return NL_RE.sub("\n\n", t).strip()


def extract_business_section(xml_text: str):
    """document.xml 본문에서 'II. 사업의 내용' SECTION-1 블록과 '1. 사업의 개요' 하위 블록을 추출.
    반환 (full_text, overview_text)"""
    sections = re.findall(r"<SECTION-1[^>]*>(.*?)</SECTION-1>", xml_text, flags=re.S | re.I)
    target = None
    for s in sections:
        m = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s, flags=re.S | re.I)
        title = _strip(m.group(1)) if m else ""
        if "사업의 내용" in title.replace(" ", "") or "사업의내용" in title.replace(" ", ""):
            target = s
            break
    if target is None:
        return None, None
    full = _strip(target)
    overview = None
    subs = re.findall(r"<SECTION-2[^>]*>(.*?)</SECTION-2>", target, flags=re.S | re.I)
    for s in subs:
        m = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s, flags=re.S | re.I)
        title = _strip(m.group(1)) if m else ""
        if "사업의 개요" in title or "사업의개요" in title.replace(" ", ""):
            overview = _strip(s)
            break
    return full, overview


def extract_rating_section(xml_text: str):
    """'회사의 개요' SECTION-1 안의 '신용평가에 관한 사항' SECTION-2 텍스트(표는 ' | ' 구분)"""
    for s1 in re.findall(r"<SECTION-1[^>]*>(.*?)</SECTION-1>", xml_text, flags=re.S | re.I):
        m = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s1, flags=re.S | re.I)
        if not m or "회사의개요" not in _strip(m.group(1)).replace(" ", ""):
            continue
        for s2 in re.findall(r"<SECTION-2[^>]*>(.*?)</SECTION-2>", s1, flags=re.S | re.I):
            m2 = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s2, flags=re.S | re.I)
            if m2 and "신용평가" in _strip(m2.group(1)):
                return _strip(s2)[:6000]
    return None


def fetch_document_xml(rcept_no: str) -> str:
    raw = dart_get_binary("document", rcept_no=rcept_no)
    z = zipfile.ZipFile(io.BytesIO(raw))
    # 본문 파일: 접수번호.xml (첨부는 _숫자 접미)
    names = sorted(z.namelist(), key=lambda n: (("_" in n.split(".")[0]), n))
    data = z.read(names[0])
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("euc-kr", errors="ignore")


def fetch_business(con, corp_code, bsns_year):
    rcept_no = latest_annual_report(corp_code, bsns_year)
    if not rcept_no:
        log(con, corp_code, f"biz_{bsns_year}", "empty", "사업보고서 없음")
        return False
    xml_text = fetch_document_xml(rcept_no)
    full, overview = extract_business_section(xml_text)
    rating_text = extract_rating_section(xml_text)
    if not full:
        log(con, corp_code, f"biz_{bsns_year}", "fail", "사업의 내용 섹션 미검출")
        return False
    con.execute("""INSERT INTO business (corp_code, bsns_year, rcept_no, reprt_code, overview_text, full_text,
        char_len, fetched_at, rating_text) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corp_code, bsns_year) DO UPDATE SET rcept_no=excluded.rcept_no, overview_text=excluded.overview_text,
        full_text=excluded.full_text, char_len=excluded.char_len, fetched_at=excluded.fetched_at, rating_text=excluded.rating_text""",
                (corp_code, bsns_year, rcept_no, "11011", overview, full, len(full), now(), rating_text))
    con.commit()
    log(con, corp_code, f"biz_{bsns_year}", "ok", f"{len(full)} chars")
    return True


# ---------- 감사의견 / 최대주주 ----------
def fetch_audit_opinion(con, corp_code, bsns_year):
    js = dart_get("accnutAdtorNmNdAdtOpinion", corp_code=corp_code, bsns_year=bsns_year, reprt_code="11011")
    for r in js.get("list", []):
        if r.get("bsns_year") != bsns_year and r.get("bsns_year"):
            continue
        con.execute("INSERT OR REPLACE INTO audit_opinion VALUES (?,?,?,?,?,?,?)",
                    (corp_code, bsns_year, r.get("adtor"), r.get("adt_opinion"),
                     r.get("adt_reprt_spcmnt_matter"), r.get("emphs_matter"), r.get("core_adt_matter")))
    con.commit()


def fetch_shareholders(con, corp_code, bsns_year):
    js = dart_get("hyslrSttus", corp_code=corp_code, bsns_year=bsns_year, reprt_code="11011")
    for r in js.get("list", []):
        con.execute("INSERT OR REPLACE INTO shareholder VALUES (?,?,?,?,?,?,?,?)",
                    (corp_code, bsns_year, r.get("nm"), r.get("relate"), r.get("stock_knd"),
                     to_num(r.get("bsis_posesn_stock_co")), to_num(r.get("trmend_posesn_stock_co")),
                     to_num(r.get("trmend_posesn_stock_qota_rt"))))
    con.commit()


def fetch_minor_shareholders(con, corp_code, bsns_year):
    """소액주주 현황: 주주수·주식수·지분율"""
    js = dart_get("mrhlSttus", corp_code=corp_code, bsns_year=bsns_year, reprt_code="11011")
    for r in js.get("list", []):
        con.execute("INSERT OR REPLACE INTO minor_shareholder VALUES (?,?,?,?,?,?)",
                    (corp_code, bsns_year, to_num(r.get("shrholdr_co")), to_num(r.get("hold_stock_co")),
                     to_num(r.get("hold_stock_rate")), to_num(r.get("shrholdr_rate"))))
    con.commit()


def fetch_events(con, corp_code, bgn_de, end_de):
    """공시목록에서 심사 관점 키워드 공시만 적재"""
    page = 1
    while True:
        js = dart_get("list", corp_code=corp_code, bgn_de=bgn_de, end_de=end_de, page_no=page, page_count=100)
        for r in js.get("list", []):
            nm = r["report_nm"]
            flag = next((v for k, v in EVENT_FLAGS.items() if k in nm), None)
            if flag:
                con.execute("INSERT OR REPLACE INTO disclosure_event VALUES (?,?,?,?,?)",
                            (corp_code, r["rcept_no"], r["rcept_dt"], nm, flag))
        if page >= int(js.get("total_page") or 1):
            break
        page += 1
    con.commit()


def run(corp_codes, bsns_year, event_from="20240101", event_to="20991231"):
    con = connect()
    for cc in corp_codes:
        for fn, name in ((fetch_business, "biz"), (fetch_audit_opinion, "audit"), (fetch_shareholders, "shr"), (fetch_minor_shareholders, "minor")):
            try:
                fn(con, cc, str(bsns_year))
            except Exception as e:  # noqa
                log(con, cc, name, "fail", e)
        try:
            fetch_events(con, cc, event_from, event_to)
        except Exception as e:  # noqa
            log(con, cc, "events", "fail", e)
    return con
