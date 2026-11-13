"""새로 공시된 정기보고서(분기·반기·사업) 즉시 반영 — 2026-11-13 사용자 "3Q 분기보고서가 공시되고 있다 업데이트 해줄거지?"
문제: 분기 수집(quarters·latest_interim_plan)은 제출 마감일(3Q 11/15, 반기 8/15, 1Q 5/16)이 지나야 새 분기를 찾기 시작하고,
      그때 아직 안 낸 회사는 quarters_done 'empty'로 영구 표시돼 마감 직후(공휴일 이월 등)에 낸 회사는 다시 받지 않았다.
해결: DART 공시목록(정기공시 pblntf_ty=A)에서 대상회사의 새 정기보고서를 잡아 그 보고서를 바로 받는다(마감일과 무관).
  1) list(pblntf_ty=A) — 마지막 조회일 하루 전부터(첫 실행은 14일 전부터), 100건/콜
  2) 보고서명 '(YYYY.MM)' + 결산월 → (사업연도, 보고서코드). 그 보고서의 'empty' 표시를 지우고 전체 재무제표(연결·별도) 적재 + KPI 재계산
  3) 받은 회사는 주석 원문(차입금·우발채무 등)을 새 보고서 기준으로 갱신, XBRL 상각비 계산
  4) 재무 API에 아직 안 올라온 보고서(공시 직후 시차)는 대기로 두고 다음 실행에서 재시도, 7일 지나면 포기
  python run.py periodic_new
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import connect, dart_get, log, now  # noqa: E402


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS periodic_new (corp_code TEXT, rcept_no TEXT PRIMARY KEY, rcept_dt TEXT, report_nm TEXT,
                   bsns_year TEXT, reprt_code TEXT, status TEXT, tries INTEGER DEFAULT 0, done_at TEXT)""")


def map_report(report_nm, acc_mt):
    """'분기보고서 (2026.09)' + 결산월 12 → ('2026', '11014'). 해당 없으면 None (financials.backfill_rcept와 같은 규칙)"""
    from financials import _RN
    m = _RN.search(report_nm or "")
    if not m:
        return None
    acc = int(acc_mt) if str(acc_mt or "").strip().isdigit() else 12
    kind, yy, mm = m.group(1), int(m.group(2)), int(m.group(3))
    if kind == "사업보고서":
        return (str(yy if acc == 12 else yy - 1), "11011")
    by = yy if (acc == 12 or mm > acc) else yy - 1
    qn = ((mm - acc - 1) % 12) // 3 + 1
    rc = {1: "11013", 2: "11012", 3: "11014"}.get(qn)
    if not rc or (kind == "반기보고서") != (rc == "11012"):
        return None
    return (str(by), rc)


def scan(con, days=None):
    """정기공시 목록 → periodic_new(대기). 반환 새로 잡은 건수"""
    _ensure(con)
    comp = {r[0]: r[1] for r in con.execute("SELECT corp_code, acc_mt FROM company")}
    last = con.execute("SELECT MAX(msg) FROM fetch_log WHERE job='periodic_new' AND status='ok'").fetchone()[0]
    if days:
        b = date.today() - timedelta(days=days)
    elif last:
        b = date.fromisoformat(last) - timedelta(days=1)
    else:
        b = date.today() - timedelta(days=14)
    bgn, end = b.strftime("%Y%m%d"), date.today().strftime("%Y%m%d")
    page, new = 1, 0
    while True:
        js = dart_get("list", bgn_de=bgn, end_de=end, pblntf_ty="A", page_no=page, page_count=100)
        rows = js.get("list", []) or []
        for r in rows:
            cc = r.get("corp_code")
            if cc not in comp:
                continue
            mp = map_report(r.get("report_nm"), comp[cc])
            if not mp:
                continue
            cur = con.execute("INSERT OR IGNORE INTO periodic_new (corp_code, rcept_no, rcept_dt, report_nm, bsns_year, reprt_code, status) VALUES (?,?,?,?,?,?,'대기')",
                              (cc, r["rcept_no"], r.get("rcept_dt"), r.get("report_nm"), mp[0], mp[1]))
            new += cur.rowcount
        con.commit()
        if page >= int(js.get("total_page") or 1) or not rows:
            break
        page += 1
    log(con, "-", "periodic_new", "ok", date.today().isoformat())
    print(f"[periodic] 정기공시 {bgn}~{end} 목록 {page}p · 대상회사 새 보고서 {new}건", flush=True)
    return new


def process(con=None, limit=None, days=None):
    """대기 중인 새 정기보고서 반영. 반환 반영한 회사 수"""
    from financials import fetch_fin_raw, build_kpi
    con = con or connect()
    _ensure(con)
    try:
        scan(con, days=days)
    except Exception as e:  # noqa
        print(f"[periodic] 목록 조회 실패: {e}", flush=True)
    con.execute("UPDATE periodic_new SET status='포기' WHERE status='대기' AND rcept_dt < ?", ((date.today() - timedelta(days=7)).strftime("%Y%m%d"),))
    pend = con.execute("""SELECT corp_code, bsns_year, reprt_code, GROUP_CONCAT(rcept_no), MAX(report_nm) FROM periodic_new WHERE status='대기'
                          GROUP BY corp_code, bsns_year, reprt_code ORDER BY MIN(rcept_dt)""").fetchall()
    if limit:
        pend = pend[:limit]
    from collections import Counter
    kinds = Counter(f"{r[1]} {r[2]}" for r in pend)
    print(f"[periodic] 반영 대기 {len(pend)}건 {dict(kinds)}", flush=True)
    ok_cc, n_ok, n_wait, c0 = [], 0, 0, common.CALLS
    for cc, y, rc, rns, nm in pend:
        if common.QUOTA_HIT:
            print("[periodic] DART 한도 도달 — 나머지는 다음 실행에서", flush=True)
            break
        try:
            con.execute("DELETE FROM quarters_done WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND status='empty'", (cc, y, rc))
        except Exception:  # noqa
            pass
        try:
            n = fetch_fin_raw(con, cc, y, rc, prev=True)
        except Exception as e:  # noqa
            log(con, cc, "periodic_new", "fail", f"{y} {rc} {e}")
            n = 0
        ids = rns.split(",")
        q = ",".join("?" * len(ids))
        if n:
            build_kpi(con, cc, y, rc)
            build_kpi(con, cc, str(int(y) - 1), rc)
            con.execute(f"UPDATE periodic_new SET status='반영', done_at=?, tries=tries+1 WHERE rcept_no IN ({q})", (now(), *ids))
            ok_cc.append(cc); n_ok += 1
        else:
            con.execute(f"UPDATE periodic_new SET tries=tries+1 WHERE rcept_no IN ({q})", ids)
            n_wait += 1
        con.commit()
    print(f"[periodic] 재무 반영 {n_ok}건 · 재무 API 미게시(다음 실행 재시도) {n_wait}건 · DART {common.CALLS - c0}콜", flush=True)
    if ok_cc and not common.QUOTA_HIT:
        try:
            import business
            business.run_notes(con, ok_cc)                 # 주석 원문을 새 보고서 기준으로(새 보고서일 때만 원문 1콜 추가)
        except Exception as e:  # noqa
            print(f"[periodic] 주석 갱신 실패: {e}", flush=True)
    if ok_cc and not common.QUOTA_HIT:
        try:
            import da_xbrl
            da_xbrl.run(corp_codes=ok_cc, con=con)         # 새 분기 상각비(XBRL)
        except Exception as e:  # noqa
            print(f"[periodic] 상각비 계산 실패: {e}", flush=True)
    con.commit()
    return n_ok


if __name__ == "__main__":
    process()
