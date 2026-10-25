"""DART 공시원문(document.xml) → 'II. 사업의 내용' 추출 + 감사의견·최대주주·주요공시"""
import html
import io
import re
import zipfile

from common import connect, dart_get, dart_get_binary, log, now, to_num, zpack

WORKERS = 4   # 사업보고서 원문 병렬 다운로드 수

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
    if target is None:                       # 폴백(2026-10-25 진단: '사업의 내용 섹션 미검출' 198건): SECTION 중첩이 다른 문서 — 제목 위치부터 다음 대분류 제목(III. 재무에 관한 사항 등) 전까지
        m = re.search(r"<TITLE[^>]*>\s*(II|Ⅱ|2)?\s*[.．]?\s*사업의\s*내용\s*</TITLE>", xml_text, flags=re.I)
        if not m:
            return None, None
        nxt = re.search(r"<TITLE[^>]*>\s*(III|Ⅲ|3)\s*[.．]\s*[^<]{2,30}</TITLE>", xml_text[m.end():], flags=re.I)
        target = xml_text[m.start(): m.end() + (nxt.start() if nxt else 200000)]
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


_NUM = re.compile(r"^[\d,]+$")
_PCT = re.compile(r"^\d{1,3}(\.\d+)?\s*%?$")


def extract_major_holders(xml_text: str):
    """'VII. 주주에 관한 사항' → '주식의 분포'의 5% 이상 주주 표 → [(주주명, 주식수, 지분율)]"""
    for s1 in re.findall(r"<SECTION-1[^>]*>(.*?)</SECTION-1>", xml_text, flags=re.S | re.I):
        m = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s1, flags=re.S | re.I)
        if not m or "주주에관한사항" not in _strip(m.group(1)).replace(" ", ""):
            continue
        for s2 in re.findall(r"<SECTION-2[^>]*>(.*?)</SECTION-2>", s1, flags=re.S | re.I):
            m2 = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", s2, flags=re.S | re.I)
            if not m2 or "분포" not in _strip(m2.group(1)):
                continue
            out = []
            for tbl in re.findall(r"<TABLE[^>]*>(.*?)</TABLE>", s2, flags=re.S | re.I):
                flat = _strip(tbl)
                if "5%" not in flat.replace(" ", "") and "주요주주" not in flat:
                    continue
                for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", tbl, flags=re.S | re.I):
                    cells = [_strip(c).replace("\n", " ").strip() for c in re.findall(r"<T[DH][^>]*>(.*?)</T[DH]>", tr, flags=re.S | re.I)]
                    cells = [c for c in cells if c and c != "-"]
                    if len(cells) < 3:
                        continue
                    nums = [c for c in cells if _NUM.match(c.replace(" ", ""))]
                    pcts = [c for c in cells if _PCT.match(c.replace(" ", "")) and not _NUM.match(c.replace(" ", ""))]
                    names = [c for c in cells if not _NUM.match(c.replace(" ", "")) and not _PCT.match(c.replace(" ", ""))
                             and c not in ("5%이상주주", "5% 이상 주주", "우리사주조합", "구분", "주주명", "소유주식수", "지분율", "비고")]
                    if not names or not pcts:
                        continue
                    nm = names[-1]
                    if "우리사주" in nm or nm in ("계", "합계"):
                        continue
                    try:
                        rate = float(pcts[-1].replace("%", "").strip())
                        cnt = float(nums[-1].replace(",", "")) if nums else None
                    except ValueError:
                        continue
                    out.append((nm, cnt, rate))
            if out:
                return out
    return []


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


def prefetch_report(corp_code, bsns_year):
    """(병렬용) 사업보고서 접수번호 + 원문 XML 내려받기 — DB 접근 없음"""
    rcept_no = latest_annual_report(corp_code, bsns_year)
    if not rcept_no:
        return rcept_no, None
    return rcept_no, fetch_document_xml(rcept_no)


NOTE_KEYS = ("차입금", "사채", "우발", "약정", "담보", "보증", "특수관계자", "소송", "리스", "금융부채", "매입채무", "파생", "계열회사", "제재")


def extract_notes(xml_text: str, max_chars=400_000) -> str:
    """재무제표 주석 중 심사 관련 절(차입금·사채·우발채무·약정·담보·보증·특수관계자·소송·리스·파생)만 평문으로 — 3-1 차입금·우발채무·계열용.
    원문 zip은 보관하지 않으므로(용량), 여기서 뽑은 텍스트를 business.notes_z에 압축 저장해 재파싱에 쓴다."""
    out = []
    matches = []
    for lv in ("1", "2", "3", "4", "5"):                                        # 계층별로 따로 훑는다(중첩 섹션은 역참조 매칭에 먹히므로)
        matches += [m.group(1) for m in re.finditer(rf"<SECTION-{lv}[^>]*>(.*?)</SECTION-{lv}>", xml_text, flags=re.S | re.I)]
    for sec in matches:
        t = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", sec, flags=re.S | re.I)
        title = _strip(t.group(1)) if t else ""
        if not title or not any(k in title.replace(" ", "") for k in NOTE_KEYS):
            continue
        if re.search(r"<SECTION-\d", sec, flags=re.I):          # 하위 섹션이 있는 상위 절은 건너뜀(하위에서 잡힘)
            continue
        body = _strip(sec)
        if len(body) > 200:
            out.append(f"### {title}\n{body}")
    txt = "\n\n".join(out)
    return txt[:max_chars]


def fetch_notes_latest(con, corp_code):
    """최신 정기보고서(분기·반기·사업 중 가장 최근) 원문에서 주석(차입금·우발채무·특수관계자 등)만 뽑아 보관 — 회사당 2콜(목록 1 + 원문 1).
    3-1 차입금·우발채무·계열 블록의 원천. 반환 (rcept_no, report_nm) 또는 None"""
    from mezz import latest_periodic_report
    con.execute("""CREATE TABLE IF NOT EXISTS notes_latest (corp_code TEXT PRIMARY KEY, rcept_no TEXT, report_nm TEXT,
                   notes_z BLOB, char_len INTEGER, fetched_at TEXT)""")
    rcept, nm = latest_periodic_report(corp_code)
    if not rcept:
        return None
    old = con.execute("SELECT rcept_no FROM notes_latest WHERE corp_code=?", (corp_code,)).fetchone()
    if old and old[0] == rcept:
        return rcept, nm                                   # 이미 같은 보고서 — 호출 1콜로 끝
    notes = extract_notes(fetch_document_xml(rcept))
    con.execute("INSERT OR REPLACE INTO notes_latest VALUES (?,?,?,?,?,?)",
                (corp_code, rcept, nm, zpack(notes) if notes else None, len(notes or ""), now()))
    con.commit()
    return rcept, nm


def _notes_worker(cc, old_rcept):
    """스레드(HTTP만): 최신 정기보고서 확인(1콜) → 새 보고서면 원문(1콜)에서 주석 추출. 반환 (rcept, nm, notes|None, changed)"""
    from mezz import latest_periodic_report
    rcept, nm = latest_periodic_report(cc)
    if not rcept:                    # 2026-10-25: 정기보고서가 없는 비상장사(감사보고서만 제출) → 최근 감사보고서 원문의 주석(연결 우선)
        from datetime import date as _d
        import audit_fs
        for y in (_d.today().year - 1, _d.today().year - 2):
            reps = audit_fs.find_audit_reports(cc, y)
            fs = "CFS" if reps.get("CFS") else "OFS" if reps.get("OFS") else None
            if fs:
                rcept, nm = reps[fs], f"{'연결' if fs == 'CFS' else ''}감사보고서 ({y}.12)"
                break
    if not rcept:
        return None, None, None, False
    if old_rcept == rcept:
        return rcept, nm, None, False
    from notes_v2 import extract_notes_v2, to_text          # 2026-10-24: 표 구조 보존(v2) — notes_z(평문)와 notes_j(JSON) 둘 다 저장
    secs = extract_notes_v2(fetch_document_xml(rcept))
    return rcept, nm, (to_text(secs), secs), True


def run_notes(con, corp_codes, limit=None, workers=4):
    """주석(차입금·우발채무·약정·담보·보증·소송·특수관계자·계열회사)을 최신 정기보고서 기준으로 보관. 회사당 1~2콜, 4건 동시. DART 한도 시 중단."""
    import common
    from concurrent.futures import ThreadPoolExecutor
    con.execute("""CREATE TABLE IF NOT EXISTS notes_latest (corp_code TEXT PRIMARY KEY, rcept_no TEXT, report_nm TEXT,
                   notes_z BLOB, char_len INTEGER, fetched_at TEXT)""")
    for sql in ("ALTER TABLE notes_latest ADD COLUMN notes_j BLOB", "ALTER TABLE notes_latest ADD COLUMN notes_v INTEGER"):
        try:
            con.execute(sql)
        except Exception:  # noqa
            pass
    import json as _json
    from notes_v2 import PARSER_VER
    old = {r[0]: r[1] for r in con.execute("SELECT corp_code, rcept_no FROM notes_latest")}
    cs = list(corp_codes)[:limit] if limit else list(corp_codes)
    print(f"[notes] 주석 원문 대상 {len(cs)}사 · 병렬 {workers}", flush=True)
    n = i = 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(cs), workers * 5):
            batch = cs[b:b + workers * 5]
            futs = [(cc, ex.submit(_notes_worker, cc, old.get(cc))) for cc in batch]
            stop = False
            for cc, f in futs:
                i += 1
                try:
                    rcept, nm, notes, changed = f.result()
                    if changed:
                        txt, secs = notes if notes else ("", [])
                        con.execute("INSERT OR REPLACE INTO notes_latest (corp_code, rcept_no, report_nm, notes_z, char_len, fetched_at, notes_j, notes_v) VALUES (?,?,?,?,?,?,?,?)",
                                    (cc, rcept, nm, zpack(txt) if txt else None, len(txt or ""), now(), zpack(_json.dumps(secs, ensure_ascii=False)) if secs else None, PARSER_VER))
                        n += 1
                except Exception as e:  # noqa
                    log(con, cc, "notes", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e) or "020" in str(e):
                        stop = True
            con.commit()
            if i % 200 < workers * 5:
                print(f"[notes] {i}/{len(cs)} · 보관 {n} · DART {common.CALLS}콜", flush=True)
            if stop:
                print(f"[notes] DART 한도 도달 — {i}/{len(cs)}에서 중단", flush=True); break
    print(f"[notes] 완료 {i}/{len(cs)} · 보관 {n}", flush=True)


def fetch_business(con, corp_code, bsns_year, pre=None):
    rcept_no, xml_text = pre if pre else prefetch_report(corp_code, bsns_year)
    if not rcept_no:
        log(con, corp_code, f"biz_{bsns_year}", "empty", "사업보고서 없음")
        return False
    full, overview = extract_business_section(xml_text)
    rating_text = extract_rating_section(xml_text)
    notes = extract_notes(xml_text)
    try:
        con.execute("ALTER TABLE business ADD COLUMN notes_z BLOB")
    except Exception:  # noqa
        pass
    for nm, cnt, rate in extract_major_holders(xml_text):
        con.execute("INSERT OR REPLACE INTO major_holder VALUES (?,?,?,?,?)", (corp_code, bsns_year, nm, cnt, rate))
    if not full:
        log(con, corp_code, f"biz_{bsns_year}", "fail", "사업의 내용 섹션 미검출")
        return False
    con.execute("""INSERT INTO business (corp_code, bsns_year, rcept_no, reprt_code, overview_text, full_z,
        char_len, fetched_at, rating_text, notes_z) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corp_code, bsns_year) DO UPDATE SET rcept_no=excluded.rcept_no, overview_text=excluded.overview_text,
        full_z=excluded.full_z, full_text=NULL, char_len=excluded.char_len, fetched_at=excluded.fetched_at, rating_text=excluded.rating_text,
        notes_z=excluded.notes_z""",
                (corp_code, bsns_year, rcept_no, "11011", overview, zpack(full), len(full), now(), rating_text, zpack(notes) if notes else None))
    con.commit()
    log(con, corp_code, f"biz_{bsns_year}", "ok", f"{len(full)} chars")
    return True


# ---------- 감사의견 / 최대주주 ----------
def fetch_audit_opinion(con, corp_code, bsns_year):
    js = dart_get("accnutAdtorNmNdAdtOpinion", corp_code=corp_code, bsns_year=bsns_year, reprt_code="11011")
    for r in js.get("list", []):
        by = (r.get("bsns_year") or "").replace(" ", "")
        if by and by != bsns_year and "당기" not in by:   # API의 bsns_year는 '제56기(당기)'·'제55기(전기)' 형식 — 연도와 비교해 전부 버려져 감사의견 0건이던 문제(2026-10-25)
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


def fetch_conv_bonds(con, corp_code, bsns_year):
    """미상환 전환사채(CB)·신주인수권부사채(BW) 잔액·전환가 (사업보고서 기준)"""
    for ep, kind in (("cndlCaplScritsNrdmpBlce", "CB"), ("wrtcaplScritsNrdmpBlce", "BW")):
        js = dart_get(ep, corp_code=corp_code, bsns_year=bsns_year, reprt_code="11011")
        for r in js.get("list", []):
            con.execute("""INSERT OR REPLACE INTO conv_bond (corp_code, bsns_year, kind, name, isu_dd, exprd_dd, remndr_amt, cnvrs_prc, cnvrs_pd, remndr_stock)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (corp_code, bsns_year, kind, r.get("bd_nm") or r.get("isu_dcrs_de") or kind, r.get("isu_dd"), r.get("exprd_dd"),
                         to_num(r.get("remndr_amt") or r.get("remndr")), to_num(r.get("cnvrs_prc") or r.get("ex_prc") or r.get("act_pric")),
                         (r.get("cnvrs_pd_bgd") or r.get("ex_pd_bgd") or "") + "~" + (r.get("cnvrs_pd_edd") or r.get("ex_pd_edd") or ""),
                         to_num(r.get("cnvrs_psbl_stock_co") or r.get("ex_psbl_stock_co"))))
    con.commit()


def fetch_other_investments(con, corp_code, bsns_year, latest=True):
    """타법인 출자현황 — latest=True면 최신 분기·반기보고서부터 시도(있으면 그것을 기준), 없으면 사업보고서(bsns_year).
    저장 키는 (연도, 보고서코드)별로 두고 사이트는 접수번호가 가장 큰(최신) 보고서 세트를 보여준다."""
    try:
        con.execute("ALTER TABLE other_invest ADD COLUMN reprt_code TEXT")
        con.execute("ALTER TABLE other_invest ADD COLUMN rcept_no TEXT")
    except Exception:  # noqa
        pass
    plans = []
    if latest:
        from financials import latest_interim_plan
        plans += [(str(y), rc) for y, rc in latest_interim_plan()[:2]]       # 최신 분기 후보 2개(예: 2026 반기, 2026 1Q)
    plans.append((str(bsns_year), "11011"))
    for y, rc in plans:
        js = dart_get("otrCprInvstmntSttus", corp_code=corp_code, bsns_year=y, reprt_code=rc)
        rows = [r for r in js.get("list", []) if r.get("inv_prm") and r.get("inv_prm") not in ("합계", "합 계")]
        if not rows:
            continue
        con.execute("DELETE FROM other_invest WHERE corp_code=? AND bsns_year=? AND COALESCE(reprt_code,'11011')=?", (corp_code, y, rc))
        for r in rows:
            con.execute("""INSERT OR REPLACE INTO other_invest (corp_code, bsns_year, name, purpose, acqs_de, trmend_qota_rt, trmend_book_amt, recent_rev, recent_ni, reprt_code, rcept_no)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (corp_code, y, r.get("inv_prm"), r.get("invstmnt_purps"), r.get("frst_acqs_de"), to_num(r.get("trmend_blce_qota_rt")),
                         to_num(r.get("trmend_blce_acntbk_amount")), to_num(r.get("recent_bsns_year_fnnr_sttus_tot_assets")), to_num(r.get("recent_bsns_year_fnnr_sttus_thstrm_ntpf")),
                         rc, r.get("rcept_no")))
        con.commit()
        return (y, rc)
    con.commit()
    return None


def refresh_other_investments(con, corp_codes, bsns_year):
    """야간: 타법인 출자현황을 최신 분기보고서 기준으로 재수집(회사당 1~3콜). 한도 도달 시 중단."""
    import common
    n = 0
    for i, cc in enumerate(corp_codes, 1):
        try:
            if fetch_other_investments(con, cc, bsns_year, latest=True):
                n += 1
        except Exception as e:  # noqa
            log(con, cc, "invest_latest", "fail", e)
            if common.QUOTA_HIT or "한도" in str(e):
                print(f"[invest] DART 한도 도달 — {i}/{len(corp_codes)}에서 중단", flush=True); break
        if i % 200 == 0:
            print(f"[invest] {i}/{len(corp_codes)} · 갱신 {n}", flush=True)
    print(f"[invest] 완료 {i}/{len(corp_codes)} · 갱신 {n}", flush=True)



def fetch_major_reports(con, corp_code, bsns_year=None):
    """DART 대량보유상황보고(majorstock): 5% 이상 보고자의 보유·증감·주요체결약정(담보제공 등) 비율. 1콜."""
    js = dart_get("majorstock", corp_code=corp_code)
    for r in js.get("list", []):
        dt = r.get("rcept_dt") or ""
        con.execute("INSERT OR REPLACE INTO major_report VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (corp_code, r.get("rcept_no"), f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt, r.get("repror"), r.get("report_tp"),
                     r.get("report_resn"), to_num(r.get("stkqy")), to_num(r.get("stkqy_irds")), to_num(r.get("stkrt")),
                     to_num(r.get("stkrt_irds")), to_num(r.get("ctr_stkqy")), to_num(r.get("ctr_stkrt")), now()))
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
    cs = list(corp_codes)
    from concurrent.futures import ThreadPoolExecutor
    import time as _t
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futs = {}
    def ensure(idx):   # idx 이후 WORKERS*2개를 미리 내려받기(원문 zip이 병목)
        for k in range(idx, min(idx + WORKERS * 2, len(cs))):
            if cs[k] not in futs:
                futs[cs[k]] = pool.submit(prefetch_report, cs[k], str(bsns_year))
    t0 = _t.time()
    for i, cc in enumerate(cs, 1):
        ensure(i - 1)
        quota = False
        try:
            pre = futs.pop(cc).result()
        except Exception as e:  # noqa
            log(con, cc, "biz", "fail", e)
            pre = (None, None)
            if "한도" in str(e):
                quota = True
        for fn, name in ((fetch_business, "biz"), (fetch_audit_opinion, "audit"), (fetch_shareholders, "shr"), (fetch_minor_shareholders, "minor"),
                         (fetch_other_investments, "invest"), (fetch_major_reports, "major")):          # CB·BW는 mezz.py (DART 정기보고서 API에 CB/BW 없음 — cndlCapl*은 조건부자본증권)
            if quota:
                break
            try:
                if name == "biz":
                    if pre[0] is None and pre[1] is None:
                        log(con, cc, f"biz_{bsns_year}", "empty", "사업보고서 없음/다운로드 실패")
                    else:
                        fn(con, cc, str(bsns_year), pre)
                else:
                    fn(con, cc, str(bsns_year))
            except Exception as e:  # noqa
                log(con, cc, name, "fail", e)
                if "한도" in str(e):
                    quota = True
                    break
        if quota:
            print(f"[biz] DART 일일 한도 도달 — {i}/{len(cs)}에서 중단(내일 --new-only로 이어서)", flush=True)
            break
        try:
            fetch_events(con, cc, event_from, event_to)
        except Exception as e:  # noqa
            log(con, cc, "events", "fail", e)
        if i % 50 == 0:
            print(f"[biz] {i}/{len(cs)} · {(_t.time()-t0)/i:.1f}s/사", flush=True)
    pool.shutdown(wait=False, cancel_futures=True)
    return con
