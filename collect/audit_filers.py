"""감사보고서만 내는 비상장사(외감법인) 중 자본총계 500억 이상 — 명단 → 자본총계 선별 → DB 추가 (2026-10-29 사용자 요청)

배경: 사업보고서 제출 비상장사(500억↑, 일반기업+여전사)는 2026-10-28 추가 완료. 다음 단계로 사업보고서는 없고 감사보고서만 내는 회사도
같은 기준(자본총계 500억↑, 일반기업 + 금융 중 여전사, SPC·공공 제외)으로 넣는다. 감사보고서 제출사는 연 4만여 곳이라 하루 DART 한도로 다 못 보므로
매일 남는 한도로 이어서 선별한다(catchup 뒤 단계).

절차
  ① scan: DART 공시검색 '외부감사관련'(F) 감사보고서·연결감사보고서, 법인구분 기타(E), 최근 14개월 → 회사별 최신 보고서
     (DB에 이미 있는 회사, 사업보고서 제출사 명단(filer_candidate)은 제외). 명단은 audit_candidate 표에 쌓고 20일 안에 다시 훑지 않음
  ② screen: 자본총계 미확인 회사의 감사보고서 본문(1콜) → 재무상태표 자본총계. 연결감사보고서가 있으면 연결, 없으면 별도
     순서: 연결감사보고서 제출사 먼저(대체로 규모가 큼) → 나머지. DART 한도 도달 시 중단 → 다음 실행에서 이어감
  ③ eligible: 자본총계 500억↑ · 일반기업 또는 여전사 · DB 미등록 → unlisted_add --type 감사보고서 가 큰 순으로 읽음
  python run.py audit_scan [--limit 선별 상한]
"""
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import connect, dart_get  # noqa: E402
from annual_filers import classify, yeo_class  # noqa: E402

RPT = re.compile(r"^(\[[^\]]*\]\s*)?(연결)?감사보고서\s*\((\d{4})\.(\d{2})\)")


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS audit_candidate (corp_code TEXT PRIMARY KEY, name TEXT, grp TEXT, fy TEXT,
                   rcept_ofs TEXT, rcept_cfs TEXT, equity REAL, eq_src TEXT, scanned_at TEXT, screened_at TEXT)""")


def _windows(months):
    end = date.today()
    cur = date(end.year, end.month, 1) - timedelta(days=31 * months)
    cur = date(cur.year, cur.month, 1)
    while cur <= end:
        m = cur.month + 3
        nxt = date(cur.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def scan(con, months=14):
    _ensure(con)
    last = con.execute("SELECT MAX(scanned_at) FROM audit_candidate").fetchone()[0]
    if last and last >= (date.today() - timedelta(days=20)).isoformat():
        print(f"[audit_scan] 명단은 {last} 수집분 사용(20일 내)", flush=True)
        return
    have = {r[0] for r in con.execute("SELECT corp_code FROM company")}
    try:
        have |= {r[0] for r in con.execute("SELECT corp_code FROM filer_candidate")}
    except Exception:  # noqa
        pass
    found = {}
    for b, e in _windows(months):
        page = 1
        while True:
            js = dart_get("list", bgn_de=b.strftime("%Y%m%d"), end_de=e.strftime("%Y%m%d"), corp_cls="E", pblntf_ty="F",
                          last_reprt_at="Y", page_no=page, page_count=100)
            for r in js.get("list", []) or []:
                m = RPT.search((r.get("report_nm") or "").strip())
                cc = r.get("corp_code")
                if not m or not cc or cc in have:
                    continue
                fy = m.group(3)
                x = found.setdefault(cc, {"name": r["corp_name"], "fy": fy, "OFS": None, "CFS": None})
                if fy > x["fy"]:                       # 더 최근 사업연도가 나오면 갈아탐
                    x.update(fy=fy, OFS=None, CFS=None)
                if fy == x["fy"]:
                    x["CFS" if m.group(2) else "OFS"] = r["rcept_no"]
            tp = int(js.get("total_page") or 1)
            if page % 20 == 0 or page >= tp:
                print(f"[audit_scan] {b}~{e} {page}/{tp}쪽 · 누적 {len(found)}사", flush=True)
            if page >= tp:
                break
            page += 1
    rows = [(cc, v["name"], classify(v["name"]), v["fy"], v["OFS"], v["CFS"]) for cc, v in found.items()]
    con.executemany("""INSERT INTO audit_candidate (corp_code, name, grp, fy, rcept_ofs, rcept_cfs, scanned_at) VALUES (?,?,?,?,?,?,date('now'))
                       ON CONFLICT(corp_code) DO UPDATE SET name=excluded.name, grp=excluded.grp, scanned_at=excluded.scanned_at,
                       equity=CASE WHEN audit_candidate.fy=excluded.fy THEN audit_candidate.equity END,
                       eq_src=CASE WHEN audit_candidate.fy=excluded.fy THEN audit_candidate.eq_src END,
                       screened_at=CASE WHEN audit_candidate.fy=excluded.fy THEN audit_candidate.screened_at END,
                       fy=excluded.fy, rcept_ofs=excluded.rcept_ofs, rcept_cfs=excluded.rcept_cfs""", rows)
    con.commit()
    from collections import Counter
    print(f"[audit_scan] 감사보고서 제출 비상장 {len(rows)}사(DB·사업보고서 제출사 제외) · " + " · ".join(f"{k} {n}" for k, n in Counter(r[2] for r in rows).most_common())
          + f" · 연결감사보고서 {sum(1 for r in rows if r[5])}사", flush=True)


def _equity(rc, fs):
    import audit_fs
    from business import fetch_document_xml
    st = audit_fs.parse_statements(fetch_document_xml(rc))
    c, _p = audit_fs._row(st, "BS", "자본총계", "자본합계", "자본총액", "총자본")
    return c


def screen(con, limit=None, workers=4):
    _ensure(con)
    q = """SELECT corp_code, name, grp, rcept_ofs, rcept_cfs FROM audit_candidate WHERE screened_at IS NULL
           AND (grp='일반기업' OR grp='금융') ORDER BY (rcept_cfs IS NULL), name"""
    todo = [r for r in con.execute(q).fetchall() if r[2] == "일반기업" or yeo_class(r[1]) == "여전사"]
    if limit:
        todo = todo[:limit]
    print(f"[audit_scan] 자본총계 선별 대상 {len(todo)}사(연결감사보고서 제출사 먼저)", flush=True)
    lock, res, st = threading.Lock(), [], {"ok": 0, "big": 0, "fail": 0}

    def work(r):
        if common.QUOTA_HIT:
            return
        cc, nm, grp, ro, rcf = r
        fs, rc = ("CFS", rcf) if rcf else ("OFS", ro)
        try:
            eq = _equity(rc, fs) if rc else None
            src = f"감사보고서 {fs}" if eq is not None else "자본총계 못 찾음"
        except Exception as e:  # noqa
            if common.QUOTA_HIT or "한도" in str(e):
                return
            eq, src = None, f"실패: {str(e)[:40]}"
        with lock:
            res.append((eq, src, cc))
            st["ok"] += 1
            st["big"] += 1 if eq and eq >= 5e10 else 0
            st["fail"] += 1 if eq is None else 0

    with ThreadPoolExecutor(workers) as ex:
        for i in range(0, len(todo), 200):
            list(ex.map(work, todo[i:i + 200]))
            with lock:
                batch, res[:] = list(res), []
            con.executemany("UPDATE audit_candidate SET equity=?, eq_src=?, screened_at=datetime('now','localtime') WHERE corp_code=?", batch)
            con.commit()
            print(f"[audit_scan] 선별 {st['ok']}/{len(todo)} · 500억↑ {st['big']} · 확인 불가 {st['fail']}", flush=True)
            if common.QUOTA_HIT:
                print("[audit_scan] DART 한도 도달 — 다음 실행에서 이어서", flush=True)
                break
    left = con.execute("SELECT COUNT(*) FROM audit_candidate WHERE screened_at IS NULL AND grp IN ('일반기업','금융')").fetchone()[0]
    print(f"[audit_scan] 이번 선별 {st['ok']}사 · 남은 미선별 {left}사 · 추가 대상 누적 {len(eligible(con))}사", flush=True)


def eligible(con):
    """→ [(corp_code, name, grp, equity, eq_src)] 자본총계 큰 순 (500억↑ · 일반기업 또는 여전사 · DB 미등록)"""
    _ensure(con)
    rows = con.execute("""SELECT corp_code, name, grp, equity, eq_src FROM audit_candidate WHERE equity >= 5e10 AND grp IN ('일반기업','금융')
                          AND corp_code NOT IN (SELECT corp_code FROM company) ORDER BY equity DESC""").fetchall()
    return [r for r in rows if r[2] == "일반기업" or yeo_class(r[1]) == "여전사"]


def run(limit=None):
    con = connect()
    scan(con)
    if not common.QUOTA_HIT:
        screen(con, limit=limit)
    el = eligible(con)
    print(f"[audit_scan] 추가 대상(500억↑) {len(el)}사 · 상위: " + ", ".join(f"{r[1]}({r[3] / 1e8:,.0f})" for r in el[:30]), flush=True)
