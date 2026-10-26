"""분기·반기보고서 원문(XML) 재무제표 → fin_raw (XBRL 재무가 없는 사업보고서 제출 비상장사용, 2026-10-26)

배경: 당근마켓처럼 사업보고서·분기·반기보고서를 내지만 OpenDART 재무 API(fnlttSinglAcntAll)에 숫자가 없는 회사는
연간은 audit_fs가 사업보고서 본문(III. 재무에 관한 사항)을 읽어 채우지만, 분기·반기는 비어 상장사와 같은 '최근 분기 + 전년 동기' 비교가 안 된다.
같은 방식으로 분기·반기보고서 본문의 '연결재무제표'·'재무제표' 절을 읽어 API와 같은 fin_raw 형식으로 넣는다(손익·현금흐름은 누적).

손익계산서 열: 분기·반기 손익표는 [과목|주석|당기 3개월|당기 누적|전기 3개월|전기 누적] 식이고 머리행이 두 줄(3개월·누적)이다.
'누적' 열을 머리행에서 직접 찾아 당기·전기 누적만 읽는다(1분기는 3개월=누적이라 열이 하나씩). 못 찾으면 audit_fs 규칙(구간의 마지막 숫자)을 쓴다.
검증: ① 자산=부채+자본(audit_fs.validate) ② 누적 매출이 같은 해 앞 분기 누적보다 작으면 3개월치를 읽은 것 → 적재하지 않음.
  python run.py interim_doc --codes U01547845 [--dry-run]
"""
import re

from common import connect, dart_get, log, now
from business import fetch_document_xml
import audit_fs
import financials
import quarters

RC_MONTH = {"11013": "03", "11012": "06", "11014": "09"}
RC_NAME = {"11013": "분기보고서", "11012": "반기보고서", "11014": "분기보고서"}
RC_ORDER = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def find_periodic(cc, y, rc):
    """그 해 해당 분기·반기보고서 접수번호(정정 포함 최신) 또는 None. 12월 결산 기준(03·06·09월)."""
    m = RC_MONTH[rc]
    js = dart_get("list", corp_code=cc, bgn_de=f"{y}{m}01", end_de=f"{int(y) + (1 if m == '09' else 0)}{'03' if m == '09' else '12'}31",
                  pblntf_ty="A", page_count=100)
    pat = re.compile(rf"{RC_NAME[rc]}\s*\({y}\.{m}\)")
    out = None
    for r in sorted(js.get("list", []), key=lambda r: r["rcept_dt"]):
        if pat.search(r["report_nm"]):
            out = r["rcept_no"]
    return out


def _cum_spans(rows):
    """머리 2~3행에 '누적'이 있으면 (당기누적 열, 전기누적 열, 머리 마지막 행 index). rowspan 때문에 아래 머리행은 오른쪽에 붙는다고 보고
    본문 열 수 - 머리행 열 수 만큼 밀어서 위치를 맞춘다."""
    for hi, cells in enumerate(rows[:3]):
        txts = [t.replace(" ", "") for t, _ in cells]
        if sum(1 for t in txts if "누적" in t) >= 1 and any("3개월" in t or "3개월" in t for t in txts):
            body = [len(audit_fs._expand(r)) for r in rows[hi + 1:hi + 12]]
            if not body:
                return None
            dn = max(body)
            phys = audit_fs._expand(cells)
            off = dn - len(phys)
            pos = [i + off for i, (t, first) in enumerate(phys) if first and "누적" in t.replace(" ", "")]
            if not pos or off < 0:
                return None
            cur = (pos[0], pos[0] + 1)
            prev = (pos[1], pos[1] + 1) if len(pos) > 1 else None
            return cur, prev, hi
    return None


_ORIG_PARSE = audit_fs.parse_table


def _parse_table_interim(tbl_xml, unit, cont=None):
    rows = [audit_fs._cells(tr) for tr in audit_fs.TR_RE.findall(tbl_xml)]
    rows = [r for r in rows if any(t for t, _ in r)]
    cs = _cum_spans(rows) if len(rows) >= 3 else None
    if not cs:
        return _ORIG_PARSE(tbl_xml, unit, cont)
    cur, prev, hi = cs
    audit_fs._LAST_HC["v"] = {"cur": cur, "prev": prev, "ncol": max(len(audit_fs._expand(r)) for r in rows[hi + 1:hi + 6])}
    out = []
    for cells in rows[hi + 1:]:
        phys = audit_fs._expand(cells)
        nm = audit_fs._clean_nm(cells[0][0]) if cells else ""
        if not nm or len(nm) > 60:
            continue
        c, p = audit_fs._val(phys, cur), audit_fs._val(phys, prev)
        if c is None and p is None:
            continue
        out.append((nm, None if c is None else c * unit, None if p is None else p * unit))
    return out if len(out) >= 5 else None


def parse(xml):
    """분기·반기보고서 XML → {'CFS': stmts, 'OFS': stmts} (audit_fs.parse_statements 형식)"""
    secs = audit_fs.annual_sections(xml)
    out = {}
    audit_fs.parse_table = _parse_table_interim
    try:
        for fs, sx in secs.items():
            st = audit_fs.parse_statements(sx)
            for sj in ("IS", "CIS", "CF"):           # '반기순이익'·'분기순이익' → '당기순이익'(KPI 계정 대응)
                if st.get(sj):
                    st[sj]["rows"] = [(re.sub(r"^(연결)?(반기|\d?분기)순", r"\1당기순", nm), c, p) for nm, c, p in st[sj]["rows"]]
            out[fs] = st
    finally:
        audit_fs.parse_table = _ORIG_PARSE
    return out


def _rev(st):
    for sj in ("IS", "CIS"):
        for nm, c, _p in (st.get(sj) or {}).get("rows", []):
            if re.search(r"^(매출액?|영업수익|수익\(매출액\))$", nm):
                return c
    return None


def targets(con, corp_codes=None):
    """사업보고서 제출(연간 fin_raw가 사업보고서 본문 파싱분) 비상장사 중 분기·반기 재무가 비어 있는 회사"""
    q = """SELECT DISTINCT c.corp_code FROM company c JOIN fin_raw r ON r.corp_code=c.corp_code
           WHERE c.market='UNLISTED' AND r.reprt_code='11011' AND r.account_id='-표준계정코드 미사용-'"""
    ccs = [r[0] for r in con.execute(q)]
    if corp_codes:
        ccs = [c for c in ccs if c in set(corp_codes)] + [c for c in corp_codes if c not in ccs]
    return ccs


def run(corp_codes=None, con=None, dry=False):
    con = con or connect()
    quarters._ensure(con)
    reports = quarters.needed_reports()                      # 최신 분기부터 13개(연간 포함)
    interim = sorted([(y, rc) for y, rc in reports if rc != "11011"], key=lambda x: (x[0], RC_ORDER[x[1]]))   # 오래된 것부터(앞 분기 누적과 비교 검증)
    ccs = targets(con, corp_codes)
    names = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    print(f"[interim_doc] 대상 {len(ccs)}사 · 보고서 {interim[0]}~{interim[-1]}", flush=True)
    for cc in ccs:
        got = []
        for y, rc in interim:
            have = con.execute("SELECT 1 FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? LIMIT 1", (cc, y, rc)).fetchone()
            if have and not dry:
                continue
            try:
                rcept = find_periodic(cc, y, rc)
            except Exception as e:  # noqa
                log(con, cc, "interim_doc", "fail", f"{y} {rc} 목록: {e}"); continue
            if not rcept:
                continue
            try:
                stm = parse(fetch_document_xml(rcept))
            except Exception as e:  # noqa
                log(con, cc, "interim_doc", "fail", f"{y} {rc} {rcept}: {e}"); continue
            for fs, st in stm.items():
                st.pop("_diag", None)
                warn = audit_fs.validate(st) if st else ["재무제표 표 없음"]
                rv = _rev(st)
                # 같은 해 앞 분기 누적 매출보다 작으면 3개월치를 읽은 것(누적 열 오인)
                prior = [k for k, o in RC_ORDER.items() if o < RC_ORDER[rc] and k != "11011"]
                if rv is not None and prior:
                    pr = con.execute(f"""SELECT MAX(revenue) FROM fin_kpi WHERE corp_code=? AND bsns_year=? AND fs_div=? AND reprt_code IN ({','.join('?' * len(prior))})""",
                                     (cc, y, fs, *prior)).fetchone()
                    if pr and pr[0] and rv < pr[0] * 0.98:
                        warn.append(f"누적 매출 {rv:,.0f} < 앞 분기 누적 {pr[0]:,.0f} — 3개월치 오인 의심")
                tag = f"  [{names.get(cc, cc)} {fs}] {y} {rc} {rcept} 매출 {rv:,.0f}" if rv is not None else f"  [{names.get(cc, cc)} {fs}] {y} {rc} {rcept} 매출 없음"
                print(tag + (f" · 경고: {'; '.join(warn)}" if warn else ""), flush=True)
                if dry:
                    for sj in ("BS", "IS", "CIS", "CF"):
                        for nm, c, p in (st.get(sj) or {}).get("rows", [])[:3]:
                            print(f"     {sj} {nm} | {c} | {p}", flush=True)
                    continue
                if "BS" not in st or any("오인" in w or "자산" in w for w in warn):
                    log(con, cc, "interim_doc_warn", "warn", f"{y} {rc} {fs} {rcept}: " + "; ".join(warn)); continue
                n, f = financials.store_raw(con, cc, y, rc, fs, audit_fs.to_api_rows(st, rcept), prev=True)
                financials.build_kpi(con, cc, y, rc)
                if f:
                    financials.build_kpi(con, cc, str(int(y) - 1), rc)
                con.execute("INSERT OR REPLACE INTO quarters_done VALUES (?,?,?,?,?,?)", (cc, y, rc, fs, "ok", now()))
                got.append(f"{y}.{RC_MONTH[rc]}{fs[0]}")
            con.commit()
        print(f"[interim_doc] {names.get(cc, cc)}: 적재 {', '.join(got) or '없음'}", flush=True)
