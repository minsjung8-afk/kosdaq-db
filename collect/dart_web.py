"""DART 웹사이트(dart.fss.or.kr) 폴백 — OpenDART API가 끊겼을 때 인증키 없이 공시 검색·원문(뷰어 HTML)을 받는다.

2026-10-25 확인(브라우저): 공시검색 POST /dsab007/detailSearch.ax(textCrpCik=고유번호, publicType=F001 감사보고서) → 접수번호,
뷰어 /dsaf001/main.do?rcpNo= 의 목차(node['text'|'dcmNo'|'eleId'|'offset'|'length'|'dtd']) → /report/viewer.do?… 절 HTML.
표 구조(TABLE/TR/TD)는 원문 XML과 같아 audit_fs.parse_statements를 그대로 쓴다. 공식 API가 아니므로 폴백 전용(일 한도·장애와 무관).
"""
import html as _html
import re
import time

import requests

BASE = "https://dart.fss.or.kr"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
CALLS = 0
_sess = requests.Session()
_sess.headers.update(UA)


def _get(url, **kw):
    global CALLS
    CALLS += 1
    for i in range(3):
        try:
            r = _sess.get(url, timeout=40, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == 2:
                raise RuntimeError(f"DART웹 연결 실패: {type(e).__name__}") from e
            time.sleep(3 * (i + 1))


def _post(url, data):
    global CALLS
    CALLS += 1
    for i in range(3):
        try:
            r = _sess.post(url, data=data, timeout=40, headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/dsab007/main.do"})
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == 2:
                raise RuntimeError(f"DART웹 연결 실패: {type(e).__name__}") from e
            time.sleep(3 * (i + 1))


def _text(r):
    r.encoding = r.encoding or "utf-8"
    t = r.text
    if "�" in t[:20000] or re.search(r"[㄰-㆏]{3}", t[:5000]):   # 깨진 한글이면 재해석
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return r.content.decode(enc)
            except UnicodeDecodeError:
                continue
    return t


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_RCP_RE = re.compile(r"rcpNo=(\d{14})|openReportViewer\('(\d{14})'|'(\d{14})'")


def search_reports(corp_code, bgn_de, end_de, public_type="F001", max_results=100):
    """공시검색 → [{'rcept_no','report_nm','rcept_dt'}] (최신순). public_type: F001 감사보고서, A001 사업보고서, ''=전체"""
    data = {"currentPage": "1", "maxResults": str(max_results), "maxLinks": "10", "sort": "date", "series": "desc",
            "textCrpCik": corp_code, "startDate": bgn_de, "endDate": end_de}
    if public_type:
        data["publicType"] = public_type
    h = _text(_post(BASE + "/dsab007/detailSearch.ax", data))
    out = []
    for m in _ROW_RE.finditer(h):
        row = m.group(1)
        rc = _RCP_RE.search(row)
        if not rc:
            continue
        rcept = next(g for g in rc.groups() if g)
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        name = next((c for c in cells if re.search(r"보고서|공시|신고", c)), cells[2] if len(cells) > 2 else "")
        dt = next((c for c in cells if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", c)), "")
        out.append({"rcept_no": rcept, "report_nm": name, "rcept_dt": dt.replace(".", "")})
    return out


_NODE_RE = re.compile(r"node(\d+)\['text'\]\s*=\s*\"([^\"]*)\"(.*?)(?=node\d+\['text'\]|</script>)", re.S)


def doc_tree(rcept_no):
    """뷰어 목차 → [{'text','dcmNo','eleId','offset','length','dtd'}]"""
    h = _text(_get(BASE + "/dsaf001/main.do", params={"rcpNo": rcept_no}))
    nodes = []
    for m in _NODE_RE.finditer(h):
        body = m.group(3)
        d = {"text": _html.unescape(m.group(2)).strip()}
        for k in ("dcmNo", "eleId", "offset", "length", "dtd"):
            mm = re.search(rf"\['{k}'\]\s*=\s*\"([^\"]*)\"", body)
            d[k] = mm.group(1) if mm else ""
        if d["dcmNo"] and d["eleId"] != "":
            nodes.append(d)
    return nodes


def fetch_section(rcept_no, node):
    r = _get(BASE + "/report/viewer.do", params={"rcpNo": rcept_no, "dcmNo": node["dcmNo"], "eleId": node["eleId"],
                                                  "offset": node["offset"], "length": node["length"], "dtd": node["dtd"] or "dart4.xsd"})
    return _text(r)


_FS_RE = re.compile(r"(재무제표|재무상태표|포괄손익계산서|손익계산서|현금흐름표|대차대조표)")
_NOTES_RE = re.compile(r"주석")


def fetch_fs_html(rcept_no):
    """감사보고서 접수번호 → 재무제표 절(들) HTML 이어붙임. 상위 '재무제표' 절이 있으면 그것만(하위 절 중복 방지)."""
    nodes = doc_tree(rcept_no)
    if not nodes:
        raise RuntimeError("DART웹 목차 없음")
    cand = [n for n in nodes if _FS_RE.search(n["text"].replace(" ", "")) and not _NOTES_RE.search(n["text"])]
    if not cand:
        cand = nodes

    def contains(a, b):   # a가 b를 포함(같은 문서, 구간 포함)
        try:
            ao, al, bo, bl = int(a["offset"]), int(a["length"]), int(b["offset"]), int(b["length"])
        except ValueError:
            return False
        return a is not b and a["dcmNo"] == b["dcmNo"] and ao <= bo and bo + bl <= ao + al
    top = [n for n in cand if not any(contains(o, n) for o in cand)]
    parts = []
    for n in top:
        parts.append(fetch_section(rcept_no, n))
        time.sleep(0.3)
    return "\n".join(parts)


def find_audit_reports(corp_code, bsns_year):
    """감사보고서 접수번호 {'OFS': rcept|None, 'CFS': rcept|None} — audit_fs.find_audit_reports와 같은 규격"""
    y = int(bsns_year)
    rows = search_reports(corp_code, f"{y}0101", f"{y + 1}1231", public_type="F001")   # 비12월 결산 대응
    pat = re.compile(rf"(\[[^\]]*\]\s*)?(연결)?감사보고서\s*\({y}\.\d{{2}}\)")
    out = {"OFS": None, "CFS": None}
    for r in sorted(rows, key=lambda r: r["rcept_dt"]):
        m = pat.search(r["report_nm"])
        if m:
            out["CFS" if m.group(2) else "OFS"] = r["rcept_no"]
    return out
