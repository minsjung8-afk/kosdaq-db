"""일일 표본 검증(2026-11-20 사용자 결정): 하루 20사의 감사보고서·사업보고서 원문 표 ↔ DB(fin_raw) 핵심 계정을 대조하고
  ① 정정공시 미반영·단위 오류만 자동 보완(제3 근거가 일치할 때)   ② 나머지는 승인 큐(dq 페이지)에 올려 사람이 판단
  python run.py verify [--limit 20] [--codes ...] [--dry-run]

왜 '원문 vs DB'가 상장사에선 거의 같은 출처인가: DB 재무는 DART XBRL 정형 API, 감사보고서도 DART 문서 → 원시 계정은 어긋날 일이 적고
실제 오류는 정정공시 미반영·비상장 감사보고서 파싱(단위·표 인식)·가공값에서 난다. 그래서 표본은 정정공시 접수사·비상장·경고 있는 회사를 우선한다.

판정(항목마다)
  일치        |DB-원문| ≤ max(0.5%, 1천원)
  단위 불일치  원문×1,000 또는 ÷1,000이 DB와 0.5% 안 → 비상장(감사보고서 파싱분)이고 모든 항목이 같은 배수면 자동 환산(fin_raw 전체 행 × 배수 → KPI 재계산)
  정정 미반영  값이 다르고 DB 접수번호 ≠ 원문(최신) 접수번호 → 자동 재수집(상장: XBRL API 재호출 / 비상장: 감사보고서 재파싱)
  값 불일치    같은 접수번호인데 값이 다름(계정 매핑·파싱 차이) → 승인 큐
  항목 없음    한쪽에만 있음 → 승인 큐(정보)
  파싱 실패    원문 표를 못 읽음 → 승인 큐(정보)
기록: verify_log(일시·회사·연도·기준·항목·DB값·원문값·접수번호·판정·조치) — 사이트 회사 화면 '표본 검증' 줄과 dq 페이지 큐에 쓰인다.
"""
import random
import re
from datetime import date, timedelta

from common import connect, dart_get, log, now

import audit_fs
import financials
from business import fetch_document_xml

# (항목, DB sj_div 후보, 계정명 후보 — financials._norm_nm 정규화 후 비교)
KEYS = [
    ("자산총계", ("BS",), ("자산총계", "자산합계", "자산총액", "총자산")),
    ("부채총계", ("BS",), ("부채총계", "부채합계", "부채총액", "총부채")),
    ("자본총계", ("BS",), ("자본총계", "자본합계", "자본총액", "총자본")),
    ("현금및현금성자산", ("BS",), ("현금및현금성자산",)),
    ("매출액", ("IS", "CIS"), ("매출액", "매출", "수익(매출액)", "영업수익", "매출액및지분법손익", "순영업수익")),
    ("영업이익", ("IS", "CIS"), ("영업이익", "영업이익(손실)", "영업손실", "영업손익")),
    ("당기순이익", ("IS", "CIS"), ("당기순이익", "당기순이익(손실)", "당기순손실", "당기순손익", "연결당기순이익", "당기순이익(손실)")),
]
TOL = 0.005
AUDIT_ID = "-표준계정코드 미사용-"     # audit_fs(감사보고서 파싱)로 넣은 행의 account_id


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS verify_log (at TEXT, corp_code TEXT, bsns_year TEXT, fs_div TEXT, item TEXT,
                   db_val REAL, src_val REAL, rcept_db TEXT, rcept_src TEXT, verdict TEXT, action TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_verify_cc ON verify_log(corp_code, at)")


def _norm(s):
    return financials._norm_nm(s)


def _db_rows(con, cc, y, fs):
    rows = con.execute("SELECT sj_div, account_id, account_nm, thstrm_amount, rcept_no FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code='11011' AND fs_div=?",
                       (cc, y, fs)).fetchall()
    return [dict(r) for r in rows]


def _pick_val(rows, sjs, names, key_nm="account_nm", key_v="thstrm_amount"):
    want = {_norm(n) for n in names}
    for sj in sjs:
        for r in rows:
            if r["sj_div"] == sj and _norm(r[key_nm]) in want and r[key_v] is not None:
                return r[key_v]
    return None


def _src_rows(st):
    out = []
    for sj in ("BS", "IS", "CIS", "CF"):
        for nm, c, p in (st.get(sj) or {}).get("rows", []):
            out.append({"sj_div": sj, "account_nm": nm, "thstrm_amount": c})
    return out


def _judge(v, s, rc_db, rc_src):
    if v is None and s is None:
        return None
    if v is None or s is None:
        return "항목 없음"
    tol = max(abs(s) * TOL, 1000)
    if abs(v - s) <= tol:
        return "일치"
    if abs(v - s * 1000) <= max(abs(s * 1000) * TOL, 1000) or abs(v - s / 1000) <= max(abs(s / 1000) * TOL, 1000):
        return "단위 불일치"
    if rc_db and rc_src and rc_db != rc_src:
        return "정정 미반영"
    return "값 불일치"


def pick(con, n=20):
    """표본: ① 최근 3일 재무 정정공시(사업·분기·반기·감사보고서 [기재정정]) 전부(최대 10) ② 비상장(감사보고서 파싱분) 5 ③ 경고(⚑) 있는 상장사 3 ④ 나머지 무작위 상장사 — 모두 '가장 오래전에 검증된 회사' 우선"""
    _ensure(con)
    last = {r[0]: r[1] for r in con.execute("SELECT corp_code, MAX(at) FROM verify_log GROUP BY corp_code")}
    has_fin = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fin_raw WHERE reprt_code='11011'")}
    mk = {r[0]: r[1] for r in con.execute("SELECT corp_code, market FROM company")}
    since = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
    out, why = [], {}
    for r in con.execute("SELECT DISTINCT corp_code FROM disclosure_event WHERE flag='재무정정' AND rcept_dt>=? ORDER BY rcept_dt DESC", (since,)):
        if r[0] in has_fin and len([c for c in out if why[c] == "정정공시"]) < 10:
            out.append(r[0]); why[r[0]] = "정정공시"
    def order(ccs):
        ccs = list(ccs); random.shuffle(ccs)
        return sorted(ccs, key=lambda c: last.get(c) or "")
    unl = [c for c in order(has_fin) if mk.get(c) == "UNLISTED" and c not in out][:5]
    for c in unl:
        out.append(c); why[c] = "비상장 파싱분"
    try:
        flagged = [r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fetch_log WHERE job='audit_fs_warn' AND at>=date('now','-400 day')")]
    except Exception:  # noqa
        flagged = []
    for c in order([c for c in flagged if c in has_fin and c not in out])[:3]:
        out.append(c); why[c] = "파싱 경고 이력"
    for c in order([c for c in has_fin if mk.get(c) in ("KOSPI", "KOSDAQ") and c not in out]):
        if len(out) >= n:
            break
        out.append(c); why[c] = "무작위"
    return out[:n], why


def verify_one(con, cc, dry=False):
    """회사 1곳: 최신 연간(fin_raw 기준) 연도의 연결·별도를 원문과 대조 → [(fs, item, db, src, rc_db, rc_src, verdict)], 조치 문자열"""
    y = con.execute("SELECT MAX(bsns_year) FROM fin_raw WHERE corp_code=? AND reprt_code='11011'", (cc,)).fetchone()[0]
    if not y:
        return y, [], "연간 재무 없음"
    api_cc = cc[1:] if cc.startswith("U") else cc
    reps = audit_fs.find_audit_reports(api_cc, y)
    ann = {}
    if reps.get("ANNUAL") and not reps.get("OFS") and not reps.get("CFS"):
        try:
            ann = audit_fs.annual_sections(fetch_document_xml(reps["ANNUAL"]))
        except Exception as e:  # noqa
            return y, [("-", "원문", None, None, None, reps["ANNUAL"], f"파싱 실패({str(e)[:40]})")], ""
        for fs in ann:
            reps[fs] = reps["ANNUAL"]
    res, actions = [], []
    for fs in ("CFS", "OFS"):
        db = _db_rows(con, cc, y, fs)
        rc = reps.get(fs)
        if not db and not rc:
            continue
        if not rc:
            res.append((fs, "원문", None, None, (db[0]["rcept_no"] if db else None), None, "원문 없음"))
            continue
        if not db:
            # 자동 보완 ③(2026-11-23): 원문(감사보고서)은 있는데 DB에 없음 — 상장사가 XBRL API에 연결을 안 올린 경우(한네트·세니젠 등 매일 2~4사 반복) → 원문 파싱으로 적재(별도 XBRL은 보존)
            if dry:
                res.append((fs, "DB", None, None, None, rc, "DB 없음")); actions.append(f"{fs} 원문 파싱 적재 예정({rc})")
            else:
                try:
                    got = audit_fs.fetch_year(con, cc, int(y), only=(fs,))
                    if got.get(fs):
                        actions.append(f"{fs} 원문 파싱 적재({got[fs]}행, {rc})"); res.append((fs, "DB", None, None, None, rc, "DB 없음(원문 적재)"))
                    else:
                        res.append((fs, "DB", None, None, None, rc, "DB 없음")); actions.append(f"{fs} 원문 파싱 실패(표 없음)")
                except Exception as e:  # noqa
                    res.append((fs, "DB", None, None, None, rc, "DB 없음")); actions.append(f"{fs} 원문 파싱 실패 {str(e)[:40]}")
            continue
        try:
            xml = ann.get(fs) or fetch_document_xml(rc)
            st = audit_fs.parse_statements(xml); st.pop("_diag", None)
        except Exception as e:  # noqa
            res.append((fs, "원문", None, None, db[0]["rcept_no"], rc, f"파싱 실패({str(e)[:40]})")); continue
        if "BS" not in st:
            res.append((fs, "원문", None, None, db[0]["rcept_no"], rc, "파싱 실패(재무상태표 없음)")); continue
        src = _src_rows(st)
        rc_db = max((r["rcept_no"] or "") for r in db) or None
        items = []
        for item, sjs, names in KEYS:
            v, s = _pick_val(db, sjs, names), _pick_val(src, sjs, names)
            jd = _judge(v, s, rc_db, rc)
            if jd:
                items.append((fs, item, v, s, rc_db, rc, jd))
        res.extend(items)
        verdicts = {i[6] for i in items}
        is_audit_parsed = all(r["account_id"] == AUDIT_ID for r in db)
        # 자동 보완 ①: 정정 미반영 → 재수집(상장: XBRL API / 비상장: 감사보고서 재파싱)
        if "정정 미반영" in verdicts:
            if dry:
                actions.append(f"{fs} 재수집 예정(정정 {rc_db}→{rc})")
            else:
                try:
                    if is_audit_parsed:
                        audit_fs.fetch_year(con, cc, int(y))
                    else:
                        financials.fetch_fin_raw(con, cc, y, "11011", (fs,), prev=True)
                        financials.build_kpi(con, cc, y, "11011")
                    actions.append(f"{fs} 재수집(정정 {rc_db}→{rc})")
                except Exception as e:  # noqa
                    actions.append(f"{fs} 재수집 실패 {str(e)[:40]}")
        # 자동 보완 ②: 단위 불일치 — 비상장 파싱분이고 비교된 모든 항목이 같은 배수일 때만
        elif verdicts and verdicts <= {"단위 불일치", "항목 없음"} and "단위 불일치" in verdicts and is_audit_parsed:
            ratios = {round(i[2] / i[3]) for i in items if i[6] == "단위 불일치" and i[3]}
            if len(ratios) == 1:
                k = ratios.pop()   # DB가 원문의 k배 → DB를 1/k로
                if dry:
                    actions.append(f"{fs} 단위 환산 예정(DB ÷{k:,})")
                else:
                    con.execute("UPDATE fin_raw SET thstrm_amount=thstrm_amount/?, frmtrm_amount=frmtrm_amount/?, bfefrmtrm_amount=bfefrmtrm_amount/? WHERE corp_code=? AND bsns_year=? AND reprt_code='11011' AND fs_div=?",
                                (k, k, k, cc, y, fs))
                    financials.build_kpi(con, cc, y, "11011")
                    actions.append(f"{fs} 단위 환산(DB ÷{k:,})")
    return y, res, " · ".join(actions)


def run(con=None, n=20, codes=None, dry=False):
    con = con or connect()
    _ensure(con)
    nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    if codes:
        ccs = [r[0] for r in con.execute("SELECT corp_code FROM company WHERE corp_code IN (%s) OR stock_code IN (%s)" % (",".join("?" * len(codes)), ",".join("?" * len(codes))), list(codes) + list(codes))]
        why = {c: "지정" for c in ccs}
    else:
        ccs, why = pick(con, n)
    print(f"[verify] 표본 {len(ccs)}사{' (드라이런: DB 변경 없음)' if dry else ''}: " + ", ".join(f"{nm.get(c, c)}({why[c]})" for c in ccs), flush=True)
    stat = {}
    for cc in ccs:
        try:
            y, res, act = verify_one(con, cc, dry=dry)
        except Exception as e:  # noqa
            print(f"[verify]   {nm.get(cc, cc)}: 실패 {str(e)[:80]}", flush=True)
            res, y, act = [("-", "-", None, None, None, None, f"실패({str(e)[:40]})")], None, ""
        worst = _worst([r[6] for r in res])
        stat[worst] = stat.get(worst, 0) + 1
        f = lambda v: "-" if v is None else f"{v / 1e8:,.1f}억"
        print(f"[verify]   {nm.get(cc, cc)} {y or ''}: {worst}" + (f" → {act}" if act else "") + " · " +
              " / ".join(f"{r[0]} {r[1]} {r[6]}" + (f"({f(r[2])} vs {f(r[3])})" if r[6] not in ('일치',) and (r[2] is not None or r[3] is not None) else "") for r in res if r[6] != "일치")[:400], flush=True)
        if not dry:
            at = now()
            for r in res:
                con.execute("INSERT INTO verify_log VALUES (?,?,?,?,?,?,?,?,?,?,?)", (at, cc, y, r[0], r[1], r[2], r[3], r[4], r[5], r[6], act))
            con.commit()
    print(f"[verify] 결과: " + ", ".join(f"{k} {v}사" for k, v in sorted(stat.items(), key=lambda x: -x[1])), flush=True)
    return stat


_ORDER = ["정정 미반영", "단위 불일치", "값 불일치", "파싱 실패", "DB 없음", "원문 없음", "실패", "항목 없음", "DB 없음(원문 적재)", "일치"]


def _worst(vs):
    vs = [v if not v.startswith("파싱 실패") and not v.startswith("실패") else v.split("(")[0] for v in vs] or ["일치"]
    for o in _ORDER:
        if o in vs:
            return o
    return vs[0]


def summary(con, cc):
    """사이트용: 가장 최근 검증 1회분 {at, verdict, items:[...], action} 또는 None"""
    try:
        at = con.execute("SELECT MAX(at) FROM verify_log WHERE corp_code=?", (cc,)).fetchone()[0]
    except Exception:  # noqa
        return None
    if not at:
        return None
    rows = con.execute("SELECT fs_div, item, db_val, src_val, rcept_db, rcept_src, verdict, action, bsns_year FROM verify_log WHERE corp_code=? AND at=?", (cc, at)).fetchall()
    return {"at": at[:10], "year": rows[0][8] if rows else None, "verdict": _worst([r[6] for r in rows]), "action": rows[0][7] if rows else "",
            "items": [{"fs": r[0], "item": r[1], "db": r[2], "src": r[3], "verdict": r[6]} for r in rows if r[6] != "일치"]}


def queue(con, days=30):
    """dq 페이지용 승인 큐: 최근 days일 중 자동 보완되지 않은 불일치(값 불일치·항목 없음·파싱 실패·DB/원문 없음)"""
    try:
        rows = con.execute("""SELECT v.at, v.corp_code, c.corp_name, c.stock_code, v.bsns_year, v.fs_div, v.item, v.db_val, v.src_val, v.verdict, v.action
                              FROM verify_log v JOIN company c ON c.corp_code=v.corp_code
                              WHERE v.at>=datetime('now','localtime',?) AND v.verdict NOT IN ('일치') ORDER BY v.at DESC""", (f"-{days} day",)).fetchall()
    except Exception:  # noqa
        return {"queue": [], "stats": {}}
    q = [{"at": r[0][:10], "code": r[3], "name": r[2], "year": r[4], "fs": r[5], "item": r[6], "db": r[7], "src": r[8], "verdict": r[9], "action": r[10] or ""} for r in rows]
    st = {}
    try:
        for r in con.execute("SELECT verdict, COUNT(DISTINCT corp_code||at) FROM verify_log WHERE at>=datetime('now','localtime',?) GROUP BY verdict", (f"-{days} day",)):
            st[r[0]] = r[1]
        st["검증 회사"] = con.execute("SELECT COUNT(DISTINCT corp_code) FROM verify_log WHERE at>=datetime('now','localtime',?)", (f"-{days} day",)).fetchone()[0]
    except Exception:  # noqa
        pass
    return {"queue": q[:300], "stats": st}
