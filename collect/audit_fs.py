"""감사보고서 원문(XML) → 재무상태표·손익계산서·현금흐름표 → fin_raw (비상장사용)

OpenDART 재무제표 API(fnlttSinglAcntAll)는 사업보고서 제출사(XBRL)만 제공한다. 사업보고서 없이 감사보고서만 내는 비상장 등급사는
공시원문(document.xml)의 본문 표를 직접 읽어 같은 fin_raw 형식(계정명·당기·전기)으로 넣고, 기존 build_kpi·차입금 분류를 그대로 쓴다.

표 구조(2026-10-25 호반건설 감사보고서 확인): 제목 '재 무 상 태 표' 다음 (단위 : 원) → 표 머리 [과 목 | 주 석 | 제 30 (당) 기 | 제 29 (전) 기]
행 이름은 'Ⅰ. 유 동 자 산', '1. 현금및현금성자산' 식(번호·띄어쓰기), 음수는 괄호. 주석 열('6,32')은 숫자로 오해하기 쉬우므로
값은 반드시 머리행에서 찾은 당기·전기 열 위치로만 읽는다(colspan 전개 후 해당 구간의 마지막 숫자 셀).
"""
import html
import re

from common import connect, dart_get, log, to_num

from business import fetch_document_xml, _strip
from notes_v2 import TABLE_RE, TR_RE, _unit_of, _num
import financials

_CELL_A_RE = re.compile(r"<T([DHEU])\b([^>]*)>(.*?)</T[DHEU]>", re.S | re.I)
_COLSPAN_RE = re.compile(r"COLSPAN\s*=\s*\"?(\d+)", re.I)

_TITLE_MAP = [   # 정규화(공백 제거) 제목 → sj_div. 긴 이름을 먼저 본다
    (re.compile(r"^(연결)?재무상태표$"), "BS"),
    (re.compile(r"^(연결)?포괄손익계산서$"), "CIS"),
    (re.compile(r"^(연결)?손익계산서$"), "IS"),
    (re.compile(r"^(연결)?현금흐름표$"), "CF"),
    (re.compile(r"^(연결)?자본변동표$"), "SCE"),
    (re.compile(r"^((연결)?재무제표(에대한)?주석|주석)$"), "NOTES"),
]
_CUR_RE = re.compile(r"(당\s*\)?\s*기|당\s*기|당기말|당분기|당반기)")
_PRE_RE = re.compile(r"(전\s*\)?\s*기|전\s*기|전기말|전분기|전반기)")
_PP_RE = re.compile(r"전\s*전")
_NTH_RE = re.compile(r"제\s*(\d+)\s*\(?[당전]?\)?\s*기")
_NUMBER_PFX = re.compile(r"^(\(\d+\)|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+|[IVX]+|\d+|[가-하])\s*[.)]\s*")


def _cells(tr_xml):
    """행 XML → [(text, colspan)]"""
    out = []
    for _tag, attrs, body in _CELL_A_RE.findall(tr_xml):
        m = _COLSPAN_RE.search(attrs or "")
        cs = int(m.group(1)) if m else 1
        txt = html.unescape(re.sub(r"<[^>]+>", " ", body))
        txt = re.sub(r"[\s\xa0]+", " ", txt).strip()
        out.append((txt, max(1, cs)))
    return out


def _expand(cells):
    """colspan 전개 → 물리 열 목록 [(text, is_first_of_span)]"""
    phys = []
    for txt, cs in cells:
        for i in range(cs):
            phys.append((txt, i == 0))
    return phys


def _header_cols(rows):
    """첫 3행에서 당기·전기 열 구간 찾기 → ((cur_start, cur_end), (prev_start, prev_end), header_row_idx) 또는 None"""
    for hi, cells in enumerate(rows[:3]):
        spans = []     # (text, start, end)
        pos = 0
        for txt, cs in cells:
            spans.append((txt, pos, pos + cs))
            pos += cs
        if len(spans) < 2:
            continue
        cur = prev = None
        nth = []
        for txt, s, e in spans:
            t = txt.replace(" ", "")
            if not t or t in ("과목", "주석", "계정과목", "구분"):
                continue
            if _PP_RE.search(t):
                continue
            if _CUR_RE.search(t) and cur is None:
                cur = (s, e)
            elif _PRE_RE.search(t) and prev is None:
                prev = (s, e)
            else:
                m = _NTH_RE.search(t)
                if m:
                    nth.append((int(m.group(1)), s, e))
        if cur is None and len(nth) >= 1:
            nth.sort(reverse=True)
            cur = (nth[0][1], nth[0][2])
            if prev is None and len(nth) >= 2:
                prev = (nth[1][1], nth[1][2])
        if cur is not None:
            return cur, prev, hi
    return None


def _val(phys, span):
    """물리 열 구간에서 마지막 숫자 셀(세부·합계 2열이면 합계 쪽)"""
    if span is None:
        return None
    s, e = span
    v = None
    for txt, first in phys[s:e]:
        if not first and v is not None:
            continue
        n = _num(txt)
        if n is not None:
            v = n
    return v


def _clean_nm(nm):
    s = _NUMBER_PFX.sub("", nm.strip())
    s = re.sub(r"\((주석|주)\s*[\d,\s]*\)", "", s)
    return re.sub(r"[\s\xa0　]+", "", s)


def parse_table(tbl_xml, unit):
    """표 XML → [(계정명, 당기, 전기)] 또는 None(재무제표 표가 아님)"""
    rows = [_cells(tr) for tr in TR_RE.findall(tbl_xml)]
    rows = [r for r in rows if any(t for t, _ in r)]
    if len(rows) < 6:
        return None
    hc = _header_cols(rows)
    if not hc:
        return None
    cur, prev, hi = hc
    out = []
    for cells in rows[hi + 1:]:
        phys = _expand(cells)
        nm = _clean_nm(cells[0][0]) if cells else ""
        if not nm or len(nm) > 60:
            continue
        c, p = _val(phys, cur), _val(phys, prev)
        if c is None and p is None:
            continue
        out.append((nm, None if c is None else c * unit, None if p is None else p * unit))
    return out if len(out) >= 5 else None


def parse_statements(xml_text):
    """감사보고서 XML → {sj_div: {'unit':..., 'rows': [(nm, cur, prev)]}} (BS/IS/CIS/CF). 주석 제목이 나오면 중단."""
    found = {}
    cur_sj = None
    pos = 0
    diag = []

    def scan(text):
        nonlocal cur_sj
        for line in _strip(text).splitlines():
            t = re.sub(r"[\s|\u3000]+", "", line)          # 표 셀 구분자(|)·공백 제거
            if not t or len(t) > 30:
                continue
            for rx, sj in _TITLE_MAP:
                if rx.search(t):
                    cur_sj = sj
                    diag.append(t)
                    break
    for tm in TABLE_RE.finditer(xml_text):
        scan(xml_text[pos:tm.start()])
        if cur_sj == "NOTES" and found:
            break
        pos = tm.end()
        unit = _unit_of(_strip(xml_text[max(0, tm.start() - 1500):tm.start()]))
        rows = parse_table(tm.group(0), unit) if cur_sj not in (None, "SCE", "NOTES") else None
        if not rows:
            scan(tm.group(0))        # 제목('재 무 상 태 표'·'제 N 기 … 현재'·단위)이 작은 표 안에 있는 양식
            continue
        found.setdefault(cur_sj, {"unit": unit, "rows": []})["rows"].extend(rows)   # 쪽 나뉜 표는 이어붙임
    found["_diag"] = diag
    return found


def to_api_rows(stmts, rcept_no):
    """parse_statements 결과 → fnlttSinglAcntAll 형식 목록(store_raw 입력)"""
    out = []
    for sj in ("BS", "IS", "CIS", "CF"):
        d = stmts.get(sj)
        if not d:
            continue
        for i, (nm, c, p) in enumerate(d["rows"], 1):
            out.append({"sj_div": sj, "account_id": "-표준계정코드 미사용-", "account_nm": nm,
                        "thstrm_amount": c, "frmtrm_amount": p, "bfefrmtrm_amount": None, "ord": i, "rcept_no": rcept_no})
    return out


def find_audit_reports(corp_code, bsns_year):
    """해당 사업연도 감사보고서 접수번호 {'OFS': rcept|None, 'CFS': rcept|None} (정정본 포함 최신)"""
    y = int(bsns_year)
    js = dart_get("list", corp_code=corp_code, bgn_de=f"{y + 1}0101", end_de=f"{y + 1}1231", pblntf_ty="F", page_count=100)
    pat = re.compile(rf"^(\[[^\]]*\]\s*)?(연결)?감사보고서\s*\({y}\.\d{{2}}\)")
    out = {"OFS": None, "CFS": None}
    for r in sorted(js.get("list", []), key=lambda r: r["rcept_dt"]):
        m = pat.search(r["report_nm"].strip())
        if m:
            out["CFS" if m.group(2) else "OFS"] = r["rcept_no"]
    return out


def fetch_year(con, cc, year, dry=False):
    """한 사업연도: 연결·별도 감사보고서 → fin_raw(당기 + 전기 보충) → KPI. 반환 {fs: 행수}"""
    reps = find_audit_reports(cc, year)
    got = {}
    if dry and not any(reps.values()):
        js = dart_get("list", corp_code=cc, bgn_de=f"{year + 1}0101", end_de=f"{year + 1}1231", page_count=100)
        print(f"  {year} 감사보고서 없음 · 그 해 공시: {[r['report_nm'][:20] for r in js.get('list', [])][:12]}", flush=True)
    for fs in ("CFS", "OFS"):
        rc = reps.get(fs)
        if not rc:
            continue
        xml = fetch_document_xml(rc)
        st = parse_statements(xml)
        diag = st.pop("_diag", [])
        lst = to_api_rows(st, rc)
        if dry:
            print(f"  [{fs}] {rc} " + " ".join(f"{k}:{len(v['rows'])}행(단위{v['unit']})" for k, v in st.items()) +
                  f" · 제목감지 {diag[:8]} · 표 {len(TABLE_RE.findall(xml))}개 · {len(xml)//1000}KB", flush=True)
            if not st:
                for i, tm in enumerate(TABLE_RE.finditer(xml)):
                    if i >= 12:
                        break
                    rws = [[t for t, _ in _cells(tr)] for tr in TR_RE.findall(tm.group(0))][:2]
                    print(f"     표{i} {rws}"[:200], flush=True)
            for sj in ("BS", "IS", "CIS", "CF"):
                for nm, c, p in (st.get(sj) or {}).get("rows", [])[:4]:
                    print(f"     {sj} {nm} | {c} | {p}", flush=True)
        if not lst or "BS" not in st:
            continue
        if not dry:
            financials.store_raw(con, cc, year, "11011", fs, lst, prev=True)
        got[fs] = len(lst)
    if got and not dry:
        financials.build_kpi(con, cc, str(year), "11011")
        financials.build_kpi(con, cc, str(year - 1), "11011")
        con.commit()
    return got


def run(corp_codes=None, year=None, con=None, dry=False, new_only=True, years_back=2):
    """대상: 비상장(market=UNLISTED) 중 연간 KPI가 없는 회사(new_only). year(기본 전년도)와 그 전 해의 감사보고서로 3개년 적재."""
    import common
    from datetime import date
    con = con or connect()
    year = year or date.today().year - 1
    if corp_codes:
        cs = list(corp_codes)
    else:
        q = "SELECT corp_code FROM company WHERE market='UNLISTED'"
        if new_only:
            q += " AND corp_code NOT IN (SELECT corp_code FROM fin_kpi WHERE reprt_code='11011')"
        cs = [r[0] for r in con.execute(q)]
    names = dict(con.execute("SELECT corp_code, corp_name FROM company"))
    print(f"[audit_fs] 대상 {len(cs)}사 · 기준연도 {year} · {'점검만' if dry else '적재'}", flush=True)
    ok = 0
    for i, cc in enumerate(cs, 1):
        try:
            got = {}
            for y in range(year, year - years_back, -1):
                g = fetch_year(con, cc, y, dry=dry)
                got.update({f"{y}:{k}": v for k, v in g.items()})
            if got:
                ok += 1
            print(f"[audit_fs] {i}/{len(cs)} {names.get(cc, cc)} {got or '감사보고서 재무제표 없음'}", flush=True)
            if not dry:
                log(con, cc, "audit_fs", "ok" if got else "empty", str(got))
        except Exception as e:  # noqa
            print(f"[audit_fs] {names.get(cc, cc)} 실패: {e}", flush=True)
            if not dry:
                log(con, cc, "audit_fs", "fail", e)
            if common.QUOTA_HIT:
                break
    print(f"[audit_fs] 완료 {ok}/{len(cs)} · DART {common.CALLS}콜", flush=True)
    if not dry:
        try:
            kp = con.execute("SELECT COUNT(*) FROM fin_kpi WHERE reprt_code='11011' AND corp_code IN (SELECT corp_code FROM company WHERE market='UNLISTED')").fetchone()[0]
            print(f"[audit_fs] 비상장 연간 KPI 행 {kp}", flush=True)
        except Exception:  # noqa
            pass
    return ok
