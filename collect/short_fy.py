"""3·6개월 결산 회사(리츠)의 기간별 재무 — 2026-11-27 사용자 지적(대신밸류리츠 "연도 보고서만 있음").

문제: 대신밸류리츠·SK리츠·삼성FN리츠·코람코더원리츠는 결산기가 3개월(사업보고서를 해마다 4번), 대부분의 상장 리츠는 6개월(사업보고서 2번 + 분기보고서 2번).
      OpenDART 재무 API는 (사업연도, 보고서종류)로만 받으므로 한 해의 사업보고서 4개 중 1개만 잡혀 '연간' 열에 3개월(또는 6개월)치가 들어갔다.
해법: 정기보고서 목록에서 결산주기를 판정(12개월 내 사업보고서 4건=3개월, 2건=6개월)하고, 보고서마다 원문(III. 재무에 관한 사항)을 접수번호 단위로 파싱해
      결산월(acc_mt)을 끝으로 하는 '가상 12개월 사업연도'의 1Q·반기·3Q·연간 칸(11013·11012·11014·11011)에 누적값으로 적재한다.
      - 손익·현금흐름: 기간 조각을 더한 누적(1Q=첫 3개월, 반기=6개월, 3Q=9개월, 연간=12개월) → 12월 결산사와 같은 틀로 비교 가능
      - 재무상태표: 그 기간 말 시점 값
      사업연도 표기는 DART 관례(사업연도 시작 연도)를 따른다: 8월 결산 → FY2025 = 2025.09~2026.08, 1Q=2025.11, 반기=2026.02, 3Q=2026.05, 연간=2026.08.
적재 뒤 fin_kpi를 다시 만들며, 일반 fin·quarters 단계가 이 회사들을 덮어쓰지 않도록 short_fy 표에 등록한다(run.py에서 제외).
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, dart_get, log, now  # noqa: E402
import financials  # noqa: E402
import audit_fs  # noqa: E402

CODE = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
PERIOD_RE = re.compile(r"\((\d{4})\.(\d{2})\)")
FLOW = ("IS", "CIS", "CF")


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS short_fy (corp_code TEXT PRIMARY KEY, fy_months INTEGER, acc_mt INTEGER, n_reports INTEGER, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS short_fy_piece (corp_code TEXT, rcept_no TEXT, period_end TEXT, kind TEXT, months INTEGER,
                   fs_div TEXT, sj_div TEXT, account_nm TEXT, amount REAL, ord INTEGER, PRIMARY KEY (corp_code, rcept_no, fs_div, sj_div, account_nm))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_sfp ON short_fy_piece (corp_code, fs_div, period_end)")


def periodic_reports(cc, days=900):
    """정기보고서(사업·반기·분기) 목록 → [(period_end 'YYYY.MM', kind, rcept_no, rcept_dt)] 기간말 오름차순. 같은 기간은 최신 접수(정정본)만."""
    bgn = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    out = {}
    for page in (1, 2):
        js = dart_get("list", corp_code=cc, bgn_de=bgn, end_de=date.today().strftime("%Y%m%d"), pblntf_ty="A", page_no=page, page_count=100)
        for r in js.get("list", []):
            nm = r.get("report_nm") or ""
            m = PERIOD_RE.search(nm)
            if not m:
                continue
            kind = "A" if "사업보고서" in nm else "Q" if ("분기보고서" in nm or "반기보고서" in nm) else None
            if not kind:
                continue
            pe = f"{m.group(1)}.{m.group(2)}"
            if pe not in out or r["rcept_no"] > out[pe][2]:
                out[pe] = (pe, kind, r["rcept_no"], r.get("rcept_dt"))
        if len(js.get("list", [])) < 100:
            break
    return sorted(out.values())


def fy_months(reps):
    """최근 12개월(가장 최근 사업보고서 기간말 기준) 사업보고서 건수로 결산주기 판정: 4건→3, 2건→6, 1건→12"""
    ann = [r for r in reps if r[1] == "A"]
    if not ann:
        return 12
    last = ann[-1][0]
    ly, lm = int(last[:4]), int(last[5:])
    def months_between(pe):
        return (ly - int(pe[:4])) * 12 + (lm - int(pe[5:]))
    n = sum(1 for r in ann if 0 <= months_between(r[0]) < 12)
    return 3 if n >= 4 else 6 if n >= 2 else 12


def _parse(rc):
    """접수번호 1건 → {fs: {sj: {rows:[(nm, cur, prev)], unit}}} (연결·별도 절 분리). 재무제표 절이 없으면 {}"""
    xml = audit_fs._src_doc(rc)
    secs = audit_fs.annual_sections(xml)
    out = {}
    for fs, sx in secs.items():
        st = audit_fs.parse_statements(sx)
        st.pop("_diag", None)
        if st and "BS" in st:
            out[fs] = st
    return out


def store_pieces(con, cc, rep, st, months):
    pe, kind, rc, _ = rep
    con.execute("DELETE FROM short_fy_piece WHERE corp_code=? AND rcept_no=?", (cc, rc))
    n = 0
    for fs, sts in st.items():
        for sj, d in sts.items():
            for i, (nm, cur, prev) in enumerate(d["rows"], 1):
                if cur is None:
                    continue
                con.execute("INSERT OR REPLACE INTO short_fy_piece VALUES (?,?,?,?,?,?,?,?,?,?)", (cc, rc, pe, kind, months, fs, sj, nm, cur, i))
                n += 1
    return n


def _pe_add(pe, k):
    y, m = int(pe[:4]), int(pe[5:])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y}.{m:02d}"


def rebuild(con, cc, acc, fym):
    """조각 → 가상 12개월 사업연도의 누적 칸 적재 + KPI. 반환 적재한 (bsns_year, reprt_code) 목록"""
    pieces = {}
    for r in con.execute("SELECT rcept_no, period_end, kind, months, fs_div, sj_div, account_nm, amount, ord FROM short_fy_piece WHERE corp_code=?", (cc,)):
        pieces.setdefault((r[4], r[1]), {"rc": r[0], "kind": r[2], "months": r[3], "rows": {}})["rows"][(r[5], r[6])] = (r[7], r[8])
    done, cells = [], []
    for fs in ("CFS", "OFS"):
        pes = sorted(pe for (f, pe) in pieces if f == fs)
        if not pes:
            continue
        # 가상 사업연도 끝(결산월 acc)마다 1Q~연간 칸을 만든다
        ends = sorted({pe for pe in pes if int(pe[5:]) == acc} | {_pe_add(pe, (acc - int(pe[5:])) % 12) for pe in pes})
        for fe in ends:                                   # fe = 가상 연간 기간말 'YYYY.MM'
            fy = int(fe[:4]) - (1 if acc != 12 else 0)   # 사업연도 표기 = 시작 연도(12월 결산은 당해)
            for k in (1, 2, 3, 4):
                qe = _pe_add(fe, -(12 - 3 * k))           # k번째 칸의 기간말
                if qe not in pes:
                    continue
                # 누적 = 기간말 qe까지, 가상 사업연도 시작(fe-12개월) 이후의 조각을 겹치지 않게 합산
                start = _pe_add(fe, -12)
                cum = {}
                bs = pieces[(fs, qe)]
                segs = _segments(pieces, fs, start, qe)
                if segs is None:
                    continue
                for pe_, sign in segs:
                    for (sj, nm), (v, o) in pieces[(fs, pe_)]["rows"].items():
                        if sj in FLOW:
                            cum[(sj, nm)] = (cum.get((sj, nm), (0, o))[0] + sign * v, o)
                lst = []
                for (sj, nm), (v, o) in bs["rows"].items():
                    if sj in FLOW:
                        continue
                    lst.append({"sj_div": sj, "account_id": "-표준계정코드 미사용-", "account_nm": nm, "thstrm_amount": v, "frmtrm_amount": None, "bfefrmtrm_amount": None, "ord": o, "rcept_no": bs["rc"]})
                for (sj, nm), (v, o) in cum.items():
                    lst.append({"sj_div": sj, "account_id": "-표준계정코드 미사용-", "account_nm": nm, "thstrm_amount": v, "frmtrm_amount": None, "bfefrmtrm_amount": None, "ord": o, "rcept_no": bs["rc"]})
                if not lst:
                    continue
                cells.append((str(fy), CODE[k], fs, lst))
    if not cells:
        return []
    # 기존 API·감사보고서 경로가 넣어 둔 행(3·6개월치가 '연간'으로 들어간 것)은 전부 걷어내고 누적 칸으로 교체
    financials.ensure_cum_table(con)
    con.execute("DELETE FROM fin_raw WHERE corp_code=?", (cc,))
    con.execute("DELETE FROM fin_kpi WHERE corp_code=?", (cc,))
    con.execute("DELETE FROM fin_cum_ok WHERE corp_code=?", (cc,))
    financials.SHORT_FY_WRITER = True
    try:
        for fy, rc, fs, lst in cells:
            financials.store_raw(con, cc, fy, rc, fs, lst, prev=False)
            if _has_qd(con):
                con.execute("INSERT OR REPLACE INTO quarters_done VALUES (?,?,?,?,?,?)", (cc, fy, rc, fs, "ok", now()))
            done.append((fy, rc, fs))
    finally:
        financials.SHORT_FY_WRITER = False
    for y, rc, _ in sorted(set(done)):
        financials.build_kpi(con, cc, y, rc)
    con.commit()
    return sorted(set(done))


def _has_qd(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='quarters_done'").fetchone() is not None


def _segments(pieces, fs, start, qe):
    """start(제외)~qe(포함)를 보고서 조각으로 겹침 없이 덮는 [(period_end, 부호)]. 사업보고서(months)와 분기보고서(3개월)를 섞어 쓴다.
    6개월 결산: 3Q 누적 = 사업보고서(6개월) + 분기보고서(3개월). 덮을 수 없으면 None(그 칸은 만들지 않음)."""
    cover = []
    cur = qe
    guard = 0
    while cur > start and guard < 8:
        guard += 1
        p = pieces.get((fs, cur))
        if not p:
            return None
        m = p["months"]
        cover.append((cur, 1))
        cur = _pe_add(cur, -m)
    return cover if cur == start else None


def run(corp_codes=None, con=None, force=False, dry=False):
    """대상: corp_codes(없으면 short_fy 등록사 + 리츠 이름 회사). 새 정기보고서가 있을 때만 다시 파싱."""
    con = con or connect()
    _ensure(con)
    if corp_codes is None:
        corp_codes = [r[0] for r in con.execute("SELECT corp_code FROM short_fy")]
        corp_codes += [r[0] for r in con.execute("SELECT corp_code FROM company WHERE corp_name LIKE '%리츠%' AND corp_code NOT IN (SELECT corp_code FROM short_fy)")]
    n_ok = 0
    for cc in corp_codes:
        try:
            reps = periodic_reports(cc)
            fym = fy_months(reps)
            row = con.execute("SELECT acc_mt, corp_name FROM company WHERE corp_code=?", (cc,)).fetchone()
            acc = int(row[0]) if row and str(row[0] or "").strip().isdigit() else 12
            if fym == 12:
                if con.execute("SELECT 1 FROM short_fy WHERE corp_code=?", (cc,)).fetchone():
                    print(f"[short_fy] {row[1] if row else cc}: 12개월 결산으로 재판정 → 등록 해제", flush=True)
                    con.execute("DELETE FROM short_fy WHERE corp_code=?", (cc,))
                continue
            have = {r[0] for r in con.execute("SELECT DISTINCT rcept_no FROM short_fy_piece WHERE corp_code=?", (cc,))}
            todo = [r for r in reps if force or r[2] not in have]
            if dry:
                print(f"[short_fy] {row[1] if row else cc}: 결산 {fym}개월(결산월 {acc}) · 보고서 {len(reps)}건 · 신규 {len(todo)}건 {[r[0] + r[1] for r in todo]}", flush=True)
                continue
            parsed = 0
            for rep in todo:
                months = fym if rep[1] == "A" else 3
                try:
                    st = _parse(rep[2])
                except Exception as e:  # noqa
                    print(f"[short_fy] {cc} {rep[0]} {rep[2]} 파싱 실패: {e}", flush=True)
                    continue
                if not st:
                    print(f"[short_fy] {cc} {rep[0]} {rep[2]} 재무제표 절 없음", flush=True)
                    continue
                store_pieces(con, cc, rep, st, months)
                parsed += 1
            con.execute("INSERT OR REPLACE INTO short_fy VALUES (?,?,?,?,?)", (cc, fym, acc, len(reps), now()))
            if parsed or force:
                done = rebuild(con, cc, acc, fym)
                print(f"[short_fy] {row[1] if row else cc}: 결산 {fym}개월 · 보고서 {len(reps)}건(신규 파싱 {parsed}) → 칸 {len(done)}개 적재", flush=True)
                log(con, cc, "short_fy", "ok", f"{fym}개월 결산 · {parsed}건 파싱 · {len(done)}칸")
            n_ok += 1
            con.commit()
        except Exception as e:  # noqa
            print(f"[short_fy] {cc} 실패: {e}", flush=True)
    print(f"[short_fy] 완료 {n_ok}/{len(corp_codes)}사", flush=True)


def short_codes(con):
    """run.py에서 fin·quarters 등 XBRL 단계 제외용"""
    try:
        return {r[0] for r in con.execute("SELECT corp_code FROM short_fy")}
    except Exception:  # noqa
        return set()


if __name__ == "__main__":
    run(sys.argv[1:] or None, dry="--dry" in sys.argv)
