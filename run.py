"""실행 진입점.
  python run.py init --limit 5            # 상장사 목록·개황 (샘플 5개, --market KOSPI 등으로 시장 지정)
  python run.py init --market KOSPI KONEX --new-only   # 기존 종목 제외하고 추가
  python run.py fin --new-only            # 재무 미수집 회사만
  python run.py mezz --new-only           # 기발행 메자닌(CB·BW·EB·종류주식) 발행결정 + 미상환 현황
  python run.py fin --years 2023 2024 2025
  python run.py biz --year 2025
  python run.py price --year 2025
  python run.py summarize                 # ANTHROPIC_API_KEY 필요
  python run.py rating                    # 금감원 신용평가서 → 신용등급 이력 (3년)
  python run.py kind                      # KIND IR자료실 링크(선택)
  python run.py hk --period y1            # 한경컨센서스 기업리포트 메타+PDF (DB 내 종목만)
  python run.py analyze --limit 20        # 받은 PDF → 정형 분석 (ANTHROPIC_API_KEY)
  python run.py profile                   # profiles/*.md 출력
  python run.py site                      # site/ 정적 사이트 빌드 → deploy.bat
  python run.py all --limit 5 --year 2025
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "collect"))

from common import connect  # noqa


def corp_pairs(con, limit=None):
    q = "SELECT corp_code, stock_code FROM company ORDER BY corp_name"
    rows = [(r["corp_code"], r["stock_code"]) for r in con.execute(q)]
    return rows[:limit] if limit else rows



def publish(tag):
    import subprocess, time, os
    """사이트 중간 반영: 빌드 → Cloudflare Pages 배포(토큰 없으면 건너뜀). 실패해도 수집은 계속"""
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        return
    try:   # 안전장치: 회사 수 1,000 미만(빈·손상 DB)이면 중간 배포하지 않음
        import sqlite3
        n = sqlite3.connect(str(Path(__file__).resolve().with_name("kosdaq.db"))).execute("SELECT COUNT(*) FROM company").fetchone()[0]
        if n < 1000:
            print(f"[chain] 사이트 반영 생략({tag}): 회사 수 {n} — 비정상 DB", flush=True); return
    except Exception as e:  # noqa
        print(f"[chain] 사이트 반영 생략({tag}): DB 확인 실패 {e}", flush=True); return
    t0 = time.time()
    try:
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "site"], check=True, stdout=subprocess.DEVNULL, timeout=900)
        subprocess.run(["npx", "--yes", "wrangler@3", "pages", "deploy", "site", "--project-name", "kosdaq-db", "--branch", "main",
                        "--commit-dirty=true"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        print(f"[chain] 사이트 반영 완료({tag}, {int(time.time() - t0)}초)", flush=True)
    except Exception as e:  # noqa
        print(f"[chain] 사이트 반영 실패({tag}): {str(e)[:120]}", flush=True)


def annual_done(con):
    """연간 재무를 이미 받았거나(fin_raw 연간) 빠른 수집을 시도한(ok/empty) 회사 — 금융사처럼 '매출액'이 없는 회사를 매번 다시 받지 않도록"""
    s = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fin_raw WHERE reprt_code='11011'")}
    s |= {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM fetch_log WHERE job='fin_fast' AND status IN ('ok','empty')")}
    return s


def record_usage(con, job):
    import common, datetime
    try:
        con.execute("CREATE TABLE IF NOT EXISTS dart_usage (dt TEXT, job TEXT, calls INTEGER, finished_at TEXT)")
        if common.CALLS:
            con.execute("INSERT INTO dart_usage VALUES (?,?,?,?)", (datetime.date.today().isoformat(), job, common.CALLS, datetime.datetime.now().isoformat(timespec="seconds")))
            con.commit()
        tot = con.execute("SELECT COALESCE(SUM(calls),0) FROM dart_usage WHERE dt=?", (datetime.date.today().isoformat(),)).fetchone()[0]
        print(f"[run] DART 호출 이번 {common.CALLS:,}콜 · 오늘 누적 {tot:,}콜 / 10,000", flush=True)
    except Exception as e:  # noqa
        print("[run] 사용량 기록 실패:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["init", "fin", "kpi", "biz", "mezz", "price", "rating", "news", "kind", "hk", "analyze", "summarize", "profile", "site", "all", "delta", "daily", "catchup", "unlisted", "quarters", "subst", "fin_fast", "rating_fix", "da", "chain", "fin_ofs", "fin_yoy", "notes_struct", "newlist", "notes", "invest", "da_notes", "rawdump", "unlisted_add", "audit_fs", "coverage", "review", "rating_valid", "interim_doc", "filer_scan"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--year", type=int, default=__import__("datetime").date.today().year)
    ap.add_argument("--years", type=int, nargs="*")
    ap.add_argument("--period", default="m1", help="hk: d7/m1/m6/y1")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-interim", action="store_true", help="fin: 분기보고서 생략")
    ap.add_argument("--market", nargs="*", default=["KOSDAQ", "KOSPI", "KONEX"], help="init/price 대상 시장")
    ap.add_argument("--debug", nargs="*", help="mezz: 종목코드 지정 진단·강제 재추출 (예: --debug 178320)")
    ap.add_argument("--start", help="price: 시세 수집 시작일 YYYY-MM-DD (기본 3년 전). 과거 백필 시 예: --start 2016-01-01")
    ap.add_argument("--type", default="기업신용평가", help="unlisted: 금감원 평가대상종류 필터(기본 기업신용평가, \"\"=전수)")
    ap.add_argument("--model", default="sonnet", help="summarize: sonnet(기본)/haiku/opus 또는 모델 ID")
    ap.add_argument("--force", action="store_true", help="summarize: 이미 요약된 회사도 다시 생성")
    ap.add_argument("--codes", nargs="*", help="대상 종목코드 지정 (예: --codes 060310 006840) — 모든 작업 공통")
    ap.add_argument("--hop", type=int, default=0, help="chain: 자동 이어하기 횟수(무한 반복 방지, 8회까지)")
    ap.add_argument("--steps", nargs="*", help="chain: 순서대로 실행할 작업 (예: --steps da quarters \"price:--start 2016-01-01\")")
    ap.add_argument("--dry-run", action="store_true", help="audit_fs: 적재 없이 파싱 결과(표별 행수·앞 4행)만 출력")
    ap.add_argument("--years-back", type=int, default=None, help="audit_fs: 감사보고서를 몇 개 연도 거슬러 받을지(보고서 1개 = 당기+전기 2개년, 기본 2 → 3개년 / 4 → 5개년)")
    ap.add_argument("--web", action="store_true", help="audit_fs: OpenDART 대신 dart.fss.or.kr 웹에서 바로 수집(API 장애 시)")
    ap.add_argument("--new-only", action="store_true", help="init: 기존 종목 생략 / fin·biz·rating·news: 해당 데이터가 없는 회사만")
    a = ap.parse_args()

    if a.job in ("init", "all"):
        import listing
        for m in a.market:
            listing.run(limit=a.limit, market=m, skip_existing=a.new_only)
    con = connect()
    pairs = corp_pairs(con, a.limit)
    if a.codes:
        want = set(a.codes); pairs = [p for p in corp_pairs(con) if p[1] in want or p[0] in want]   # 종목코드 또는 DART 고유번호(비상장은 U+고유번호)
        print(f"[run] 대상 종목 지정: {len(pairs)}사", flush=True)
    if a.new_only and a.job in ("fin", "biz", "rating", "news"):   # mezz는 모듈 내부에서 처리
        tbl = {"fin": "fin_raw", "biz": "business", "rating": "credit_rating", "news": "news"}.get(a.job)   # fin은 fin_raw 기준(quarters가 fin_kpi에 요약행을 먼저 넣을 수 있음)
        done = {r[0] for r in con.execute(f"SELECT DISTINCT corp_code FROM {tbl}")}
        pairs = [p for p in pairs if p[0] not in done]
        print(f"[{a.job}] --new-only: 미수집 {len(pairs)}사", flush=True)
    ccs = [p[0] for p in pairs]
    years = a.years or [a.year - 2, a.year - 1]        # 연간 2개년 (올해분은 분기로)

    if a.job in ("fin", "all"):
        import financials
        financials.run(ccs, years, interim=not a.no_interim)
    if a.job == "kpi":
        import financials
        print("KPI 재계산:", financials.rebuild_kpi_all(con), "건")
        try:   # 진단(2026-10-25): 비상장 최근 연간 KPI에서 영업CF·자산총계·순이익이 빈 회사와 해당 표의 계정명
            for cc, nm, y, fs, cfo, ta, ni in con.execute("""SELECT k.corp_code, c.corp_name, k.bsns_year, k.fs_div, k.cfo, k.total_assets, k.net_income FROM fin_kpi k JOIN company c USING(corp_code)
                                          WHERE c.market='UNLISTED' AND k.reprt_code='11011' AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi WHERE corp_code=k.corp_code AND reprt_code='11011')
                                          AND (k.cfo IS NULL OR k.total_assets IS NULL OR k.net_income IS NULL)""").fetchall():
                miss = [n for n, v in (("CF", cfo), ("BS", ta), ("IS", ni)) if v is None]
                for sj in miss:
                    sjs = ("IS", "CIS") if sj == "IS" else (sj,)
                    names = [r[0] for r in con.execute(f"SELECT account_nm FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code='11011' AND fs_div=? AND sj_div IN ({','.join('?'*len(sjs))}) ORDER BY ord LIMIT 40",
                                                       (cc, y, fs, *sjs)).fetchall()]
                    print(f"[kpi진단] {nm} {y} {fs} {sj} 누락 · 계정 {len(names)}개: {' | '.join(names)[:600]}", flush=True)
        except Exception as e:  # noqa
            print(f"[kpi진단] 오류: {e}", flush=True)
        try:   # 진단: 이자비용은 있는데 총차입금이 비는 연간 행 수와, 그 회사들의 재무상태표 부채 계정명 상위
            miss = con.execute("""SELECT COUNT(*) FROM fin_kpi WHERE reprt_code='11011' AND borrowings IS NULL AND COALESCE(int_exp,0)<>0""").fetchone()[0]
            tot = con.execute("SELECT COUNT(*) FROM fin_kpi WHERE reprt_code='11011'").fetchone()[0]
            print(f"[kpi] 총차입금 미산출(이자비용 있음) 연간 {miss}/{tot}행", flush=True)
            top = con.execute("""SELECT r.account_nm, COUNT(*) n FROM fin_raw r JOIN fin_kpi k ON k.corp_code=r.corp_code AND k.bsns_year=r.bsns_year
                                 AND k.reprt_code=r.reprt_code AND k.fs_div=r.fs_div
                                 WHERE k.reprt_code='11011' AND k.borrowings IS NULL AND COALESCE(k.int_exp,0)<>0 AND r.sj_div='BS'
                                 AND (r.account_nm LIKE '%부채%' OR r.account_nm LIKE '%차입%' OR r.account_nm LIKE '%채무%')
                                 GROUP BY r.account_nm ORDER BY n DESC LIMIT 30""").fetchall()
            print("[kpi] 미산출 회사의 부채 계정명 상위:", ", ".join(f"{a}({n})" for a, n in top), flush=True)
        except Exception as e:  # noqa
            print("[kpi] 진단 실패:", e)
        print("접수번호 보충:", financials.backfill_rcept(con), "건")
    if a.job in ("biz", "all"):
        import business
        business.run(ccs, a.years[-1] if a.years else a.year - 1)   # 최신 사업보고서 = 직전 연도
    if a.job in ("mezz", "all"):
        import mezz
        if a.debug:
            mezz.debug(con, a.debug)
        else:
            mezz.run(set(ccs) if a.limit else None, new_only=a.new_only)
    if a.job == "fin_fast":                  # 연간 재무 빠른 수집(연간 재무 없는 회사만, 회사당 1~2콜)
        import financials
        have = annual_done(con)
        financials.run_annual_fast([c for c in ccs if c not in have], a.year - 1, con=con)
        print("KPI 재계산(계정 매핑 보강 반영):", financials.rebuild_kpi_all(con), "건", flush=True)
    if a.job == "subst":                     # 대용가(KRX 정보데이터시스템 MDCSTAT04001) 1콜
        import subst
        subst.run(start=a.start, con=con)
    if a.job == "quarters":                  # 8분기·LTM 분기 손익(등급사→코스피→코스닥 순, 한도까지)
        import quarters
        quarters.run([p[0] for p in corp_pairs(con)] if not a.codes else ccs, limit=a.limit, con=con)   # --limit = 미수집 회사 중 N사
    if a.job == "unlisted":                  # 비상장 등급사 드라이런(명단 xlsx, DB 변경 없음)
        if a.type == "전체등급":            # 2026-10-25: ICR + 일반사채 + 기업어음 + 전자단기사채 전수(15개월, --limit로 일수 지정)
            import unlisted2
            unlisted2.run(days=a.limit or 456)
        else:
            import unlisted
            unlisted.run(years=(a.years[0] if a.years else 3), max_pages=a.max_pages, ttype=a.type)
    if a.job == "delta":
        import delta
        delta.run(days=a.limit)
    if a.job in ("daily", "newlist"):         # 신규 상장 탐지: KRX 종목기본정보에 있는데 company에 없는 종목 → 개황 적재 후 미니 파이프라인(당일 사이트 반영)
        import listing
        before = {r[0] for r in con.execute("SELECT corp_code FROM company")}
        for m in a.market:
            try:
                listing.run(market=m, skip_existing=True).close()      # 연결을 바로 닫아 잠금이 남지 않게
            except Exception as e:  # noqa
                print(f"[newlist] {m} 상장목록 갱신 실패: {e}", flush=True)
        pairs = corp_pairs(con, a.limit); ccs = [p[0] for p in pairs]          # 신규분 포함해 다시 계산(아래 시세 요약에도 반영)
        new = [c for c in ccs if c not in before]
        if new:
            names = {r[0]: f"{r[1]}({r[2]}·{r[3]})" for r in con.execute("SELECT corp_code, corp_name, stock_code, market FROM company")}
            print(f"[newlist] 신규 상장 {len(new)}사: " + ", ".join(names.get(c, c) for c in new), flush=True)
            import financials, quarters, business, mezz, fss_rating
            for nm, fn in (("fin", lambda: financials.run_annual_fast(new, a.year - 1, con=con)),      # 직전 연도 사업보고서(없으면 skip 기록)
                           ("fin_ofs", lambda: financials.run_ofs(new, a.year - 1, con=con)),
                           ("quarters", lambda: quarters.run(new, con=con)),                          # 상장 후 첫 분기·반기보고서
                           ("biz", lambda: business.run(new, a.year - 1)),                             # 사업내용·주주·감사의견(사업보고서 없으면 fail 기록)
                           ("mezz", lambda: mezz.run(set(new), new_only=True)),
                           ("rating", lambda: fss_rating.run(set(new))),
                           ("summarize", lambda: __import__("summarize").run_all(set(new), model="haiku", force=False))):
                try:
                    print(f"[newlist] {nm} 시작 ({len(new)}사)", flush=True); fn(); con.commit()
                except Exception as e:  # noqa
                    print(f"[newlist] {nm} 오류: {e}", flush=True)
        else:
            print("[newlist] 신규 상장 없음", flush=True)
    if a.job == "unlisted_add":              # 비상장 등급사 추가(사용자 결정 2026-10-25: 1군 일반기업 77사부터): data/unlisted_seed.csv → company(market=UNLISTED, stock_code='U'+고유번호) → 미니 파이프라인
        import csv, listing, financials, quarters, business, mezz, fss_rating
        seed = list(csv.DictReader(open(Path(__file__).resolve().with_name("unlisted_seed.csv"), encoding="utf-8")))
        if a.type == "전체등급":            # 2026-10-25 사용자 결정: 1군 기준(비금융·비공공·비SPC) + ICR·회사채·CP 중 하나라도 있는 비상장사 전부 추가
            import unlisted2
            extra = [{"corp_code": r["DART고유번호"], "corp_name": r["회사명"], "grade": r["대표등급"]}
                     for r in unlisted2.run(days=a.limit or 456) if r["구분"] == "일반기업" and r["DART고유번호"]]
            known = {r["corp_code"] for r in seed}
            for r in extra:                       # 같은 회사가 표기 2가지로 잡히는 경우(대륜E&S·대륜이엔에스) 1번만
                if r["corp_code"] not in known:
                    known.add(r["corp_code"]); seed.append(r)
            print(f"[unlisted_add] 등급 전수 명단 추가 후보 {len(extra)}사 → 시드 합계 {len(seed)}사", flush=True)
        exf = Path(__file__).resolve().with_name("unlisted_exclude.csv")   # 2026-10-25 사용자 결정: 보증부 등급(은행·계열사 지급보증·연대보증)만 있는 회사는 제외 — 등급이 보증인 신용도라서
        excl = {r["corp_code"] for r in csv.DictReader(open(exf, encoding="utf-8"))} if exf.exists() else set()
        if excl:
            seed = [r for r in seed if r["corp_code"] not in excl]
            print(f"[unlisted_add] 제외 명단 {len(excl)}사 적용(보증부 등급만 보유 등)", flush=True)
        if a.type == "수동" and a.codes:      # 2026-10-26 사이트 '회사 추가'(관리자 비밀번호) — 시드에 없는 회사도 고유번호만으로 등록
            from common import dart_get
            known = {r["corp_code"] for r in seed}
            for cc in a.codes:
                if cc in known or cc in excl:
                    continue
                try:
                    nm = (dart_get("company", corp_code=cc).get("corp_name") or "").strip()
                except Exception as e:  # noqa
                    nm = ""; print(f"[unlisted_add] {cc} 개황 조회 실패: {e}", flush=True)
                seed.append({"corp_code": cc, "corp_name": nm or cc, "grade": ""})
            con.execute("CREATE TABLE IF NOT EXISTS manual_company (corp_code TEXT PRIMARY KEY, added_at TEXT)")
            con.executemany("INSERT OR IGNORE INTO manual_company VALUES (?, datetime('now','localtime'))", [(c,) for c in a.codes])
            con.commit()
        if a.type == "사업보고서":            # 2026-10-28 사용자 결정: 사업보고서 제출 비상장 일반기업·금융 중 자본총계 500억 이상(filer_scan 결과) — 큰 회사부터 --limit개씩
            rows = con.execute("""SELECT corp_code, name FROM filer_candidate WHERE grp IN ('일반기업','금융') AND equity >= 5e10
                                  AND corp_code NOT IN (SELECT corp_code FROM company) ORDER BY equity DESC LIMIT ?""", (a.limit or 60,)).fetchall()
            seed = [{"corp_code": r[0], "corp_name": r[1], "grade": ""} for r in rows]
            print(f"[unlisted_add] 사업보고서 제출 비상장(자본총계 500억↑, 금융 포함) 이번 {len(seed)}사", flush=True)
        if a.codes:
            seed = [r for r in seed if r["corp_code"] in set(a.codes)]
        if a.debug:                          # --debug <고유번호…>: 잘못 등록된 비상장사 제거(시드 고유번호 오류 정정용, UNLISTED만) 후 시드대로 재등록
            for cc in a.debug:
                if not con.execute("SELECT 1 FROM company WHERE corp_code=? AND market='UNLISTED'", (cc,)).fetchone():
                    print(f"[unlisted_add] {cc}: 비상장 등록 없음 — 건너뜀", flush=True); continue
                n = 0
                for (tbl,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})")}
                    if "corp_code" in cols:
                        n += con.execute(f"DELETE FROM {tbl} WHERE corp_code=?", (cc,)).rowcount
                print(f"[unlisted_add] {cc} 제거: {n}행", flush=True)
            con.commit()
        have = {r[0] for r in con.execute("SELECT corp_code FROM company")}
        new = []
        for r in seed:
            cc = r["corp_code"].strip()
            if cc in have:
                continue
            nm = r["corp_name"].replace("(주)", "").replace("주식회사", "").strip()
            try:
                listing.upsert_company(con, cc, "U" + cc, {"corp_name": nm, "market": "UNLISTED"})
                new.append(cc)
            except Exception as e:  # noqa
                print(f"[unlisted_add] {nm}({cc}) 개황 실패: {e}", flush=True)
        print(f"[unlisted_add] 신규 {len(new)}사 / 시드 {len(seed)}사 (이미 있음 {len(seed)-len(new)})", flush=True)
        if new:
            for nm, fn in (("fin", lambda: financials.run_annual_fast(new, a.year - 1, con=con)),      # 사업보고서 제출사는 API로 바로(감사보고서만 내는 회사는 skip 기록 → 2차 파서 대상)
                           ("fin_prev", lambda: financials.run_annual_fast(new, a.year - 2, con=con)),
                           ("fin_ofs", lambda: financials.run_ofs(new, a.year - 1, con=con)),
                           ("quarters", lambda: quarters.run(new, con=con)),
                           ("biz", lambda: business.run(new, a.year - 1)),
                           ("mezz", lambda: mezz.run(set(new), new_only=True)),
                           ("rating", lambda: fss_rating.run(set(new))),
                           ("notes", lambda: business.run_notes(con, new)),
                           ("da_notes", lambda: __import__("notes_v2").run(set(new), con=con)),
                           ("summarize", lambda: __import__("summarize").run_all(set(new), model="haiku", force=False))):
                try:
                    print(f"[unlisted_add] {nm} 시작 ({len(new)}사)", flush=True); fn(); con.commit()
                except Exception as e:  # noqa
                    print(f"[unlisted_add] {nm} 오류: {e}", flush=True)
            miss = [c for c in new if not con.execute("SELECT 1 FROM fin_kpi WHERE corp_code=? AND reprt_code='11011' LIMIT 1", (c,)).fetchone()]
            names = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
            print(f"[unlisted_add] 사업보고서 재무 미확보 {len(miss)}사 → 감사보고서 파서(audit_fs): " + ", ".join(names.get(c, c) for c in miss), flush=True)
            if miss:
                try:
                    __import__("audit_fs").run(miss, year=a.year - 1, con=con, years_back=a.years_back or (4 if a.type == "수동" else 2))   # 수동 추가는 5개년(연도별 손익 추이용, 2026-10-26)
                except Exception as e:  # noqa
                    print(f"[unlisted_add] audit_fs 오류: {e}", flush=True)
            try:                              # XBRL 재무가 없는 사업보고서 제출사(당근마켓 등): 분기·반기보고서 본문 → 최근 분기 + 전년 동기(상장사와 같은 양식, 2026-10-26)
                __import__("interim_doc").run(new, con=con)
            except Exception as e:  # noqa
                print(f"[unlisted_add] interim_doc 오류: {e}", flush=True)
            try:                              # 정기보고서 제출사는 최근 분기·반기 검토의견도(감사보고서만 내는 회사는 해당 없음)
                import business
                business.run_review(con, new)
            except Exception as e:  # noqa
                print(f"[unlisted_add] 검토의견 오류: {e}", flush=True)
            if a.type == "수동":             # 감사보고서만 있는 회사의 사업내용 요약(회사의 개요 + 재무 요약, Haiku)
                try:
                    __import__("summarize").run_unlisted(set(new), model="haiku")
                except Exception as e:  # noqa
                    print(f"[unlisted_add] 요약 오류: {e}", flush=True)
    if a.job == "filer_scan":                # 사업보고서 제출 비상장사 전수 명단(드라이런, DB 변경 없음 → unlisted_candidates.csv 아티팩트, 2026-10-28)
        __import__("annual_filers").run(limit=a.limit)
    if a.job == "interim_doc":               # 분기·반기보고서 본문 재무제표 → fin_raw (XBRL 없는 사업보고서 제출 비상장사, 2026-10-26)
        __import__("interim_doc").run([p[0] for p in pairs] if a.codes else None, con=con, dry=a.dry_run)
    if a.job == "review":                    # 최근 분기·반기 검토의견(상장사, 1콜/사) — --codes 지정 시 그 회사만, 기본은 미조회 상장사 전부
        import business
        business.run_review(con, [p[0] for p in pairs] if a.codes else None, verbose=bool(a.codes))
    if a.job == "coverage":                  # 항목별 수록 현황·실패 사유 진단(2026-10-25 사용자 요청: 메자닌·차입금·사업내용 등 미진한 부분 팔로업) — DART 호출 없음
        def has(sql):
            try:
                return {r[0] for r in con.execute(sql)}
            except Exception as e:  # noqa
                print(f"[coverage] 쿼리 오류: {e}", flush=True); return set()
        live = con.execute("SELECT corp_code, market FROM company WHERE delisted_dt IS NULL").fetchall()
        checks = {
            "사업내용 요약": has("SELECT corp_code FROM business WHERE summary_json IS NOT NULL"),
            "사업보고서 원문": has("SELECT corp_code FROM business WHERE full_text IS NOT NULL OR full_z IS NOT NULL"),
            "연간 재무": has("SELECT corp_code FROM fin_kpi WHERE reprt_code='11011'"),
            "총차입금(최근연간)": has("""SELECT k.corp_code FROM fin_kpi k WHERE k.reprt_code='11011' AND k.borrowings IS NOT NULL
                                     AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi WHERE corp_code=k.corp_code AND reprt_code='11011')"""),
            "영업CF(최근연간)": has("""SELECT k.corp_code FROM fin_kpi k WHERE k.reprt_code='11011' AND k.cfo IS NOT NULL
                                     AND k.bsns_year=(SELECT MAX(bsns_year) FROM fin_kpi WHERE corp_code=k.corp_code AND reprt_code='11011')"""),
            "메자닌 점검": has("SELECT corp_code FROM mezz_checked"),
            "주석 원문": has("SELECT corp_code FROM notes_latest"),
            "차입금 구조화": has("SELECT corp_code FROM notes_struct"),
            "감사의견": has("SELECT corp_code FROM audit_opinion"),
        }
        mk = {}
        for cc, m in live:
            mk.setdefault(m, []).append(cc)
        for m, ccs in sorted(mk.items()):
            line = " · ".join(f"{k} {sum(1 for c in ccs if c in v)}/{len(ccs)}" for k, v in checks.items())
            print(f"[coverage] {m}: {line}", flush=True)
        for k in ("사업내용 요약", "총차입금(최근연간)", "메자닌 점검", "차입금 구조화"):
            miss = [cc for cc, m in live if m != "UNLISTED" and cc not in checks[k]]
            nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
            print(f"[coverage] 상장 {k} 누락 {len(miss)}사 예: {', '.join(nm.get(c, c) for c in miss[:25])}", flush=True)
        for job, n in con.execute("SELECT job, COUNT(*) FROM fetch_log WHERE status='fail' GROUP BY job ORDER BY 2 DESC").fetchall():
            top = con.execute("""SELECT substr(COALESCE(msg,''),1,70) m, COUNT(*) n FROM fetch_log WHERE status='fail' AND job=?
                                 GROUP BY m ORDER BY n DESC LIMIT 4""", (job,)).fetchall()
            print(f"[coverage] 실패 {job} {n}건: " + " / ".join(f"{r[0]}({r[1]})" for r in top), flush=True)
    if a.job == "audit_fs":                  # 감사보고서(pblntf_ty F) 본문 표 → fin_raw/KPI. 기본: 비상장 중 연간 KPI 없는 회사. --codes로 지정, --dry-run으로 점검만
        import audit_fs
        mk = a.market[0] if (isinstance(a.market, list) and len(a.market) == 1) else "UNLISTED"   # --market KONEX 처럼 하나만 지정했을 때만(기본값은 3개 시장 목록)
        audit_fs.run([p[0] for p in pairs] if a.codes else None, year=a.year - 1, con=con, dry=a.dry_run, new_only=("all" if a.type == "전체재파싱" else "recheck" if a.force else not a.codes), source="web" if a.web else "api", market=mk, years_back=a.years_back or 2)   # --force: 빈 KPI(CF·순이익) 회사 재파싱
    if a.job == "daily":                     # 평일 아침: 시세 + 수시공시 델타(전환청구·리픽싱·5%보고·주요이벤트)
        import prices, delta, subst
        prices.run(pairs, a.year, markets=a.market)
        try:
            subst.run(con=con)                   # 대용가(KRX, 전종목 1콜) → price_summary.subst_price
        except Exception as e:  # noqa
            print("[daily] 대용가 실패:", e)
        try:
            delta.run()
        except Exception as e:  # noqa
            print("[daily] delta 실패:", e)
        try:                                       # 2026-10-24: 신용평가서 공시 당일 반영(금감원 최근 3일 목록, DART 호출 없음)
            import fss_rating
            fss_rating.run_delta(con=con, days=3)
        except Exception as e:  # noqa
            print("[daily] 신용평가 델타 실패:", e)
    if a.job == "catchup":                   # 야간: 미수집분을 mezz → biz → fin → kpi 순으로, DART 한도 도달 시 중단(다음날 이어서)
        import common, mezz, business, financials, quarters
        done = lambda tbl: {r[0] for r in con.execute(f"SELECT DISTINCT corp_code FROM {tbl}")}
        done_annual = annual_done
        def done_notes(con):
            try:
                return {r[0] for r in con.execute("SELECT corp_code FROM notes_latest")}
            except Exception:  # noqa
                return set()
        def done_invest_latest(con):               # 이미 분기보고서(11012/11013/11014) 기준으로 받은 회사
            try:
                return {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM other_invest WHERE reprt_code IN ('11012','11013','11014')")}
            except Exception:  # noqa
                return set()
        def audit_only(con, _c={}):
            if "v" not in _c:              # 재무가 감사보고서 파서로만 들어온 비상장사(사업보고서 미제출)
                try:
                    _c["v"] = {r[0] for r in con.execute("""SELECT DISTINCT r.corp_code FROM fin_raw r JOIN company c ON c.corp_code=r.corp_code
                               WHERE c.market='UNLISTED' AND r.account_id='-표준계정코드 미사용-'
                               AND r.corp_code NOT IN (SELECT corp_code FROM fin_raw WHERE account_id<>'-표준계정코드 미사용-')""")}
                except Exception:  # noqa
                    _c["v"] = set()
            return _c["v"]
        steps = [("mezz", lambda: mezz.run(ccs, new_only=True)),
                 ("biz", lambda: business.run([c for c in ccs if c not in done("business") and c not in audit_only(con)], a.year - 1)),   # 감사보고서만 내는 비상장사는 사업보고서가 없어 매일 헛조회 → 제외(2026-10-25)
                 ("summarize", lambda: (__import__("summarize").run_all(None, model="haiku", force=False),
                                        __import__("summarize").run_unlisted(None, model="haiku", force=False))),   # 새로 받은 사업보고서만 Haiku 요약 + 사업보고서 없는 비상장사(감사보고서·신평서 기반)
                 ("audit_fix", lambda: [business.fetch_audit_opinion(con, c, str(a.year - 1)) for c in (done("business") - done("audit_opinion"))]),   # 복구로 비워진 감사의견 보강(1콜/사)
                 ("review", lambda: business.run_review(con)),   # 최근 분기·반기 검토의견(미조회분만, 2026-10-26)
                 ("rating_valid", lambda: __import__("rating_valid").run(con=con)),   # 새 신용평가서의 등급 유효기간(미조회분만, 2026-10-26)
                 ("fin", lambda: financials.run_annual_fast([c for c in ccs if c not in done_annual(con)], a.year - 1, con=con)),   # 연간 재무가 아예 없는 회사만(코스피·코넥스 신규) — 분기·누적 보정은 quarters가 담당
                 ("fin_ofs", lambda: financials.run_ofs(ccs, a.year - 1, con=con)),
                 ("fin_yoy", lambda: financials.run_yoy(ccs, con=con)),       # 전년동기 BS 보강   # 별도 재무 보강(연결만 있는 회사)
                 ("quarters", lambda: quarters.run(ccs, con=con)),
                 ("kpi", lambda: print("KPI 재계산:", financials.rebuild_kpi_all(con), "건")),                       # 8분기·LTM용 분기 손익(주요계정 1콜/보고서), 한도까지
                 ("notes", lambda: business.run_notes(con, [c for c in ccs if c not in done_notes(con)])),   # 주석(차입금·우발채무·특수관계자) — 최신 정기보고서(분기 우선) 원문만, 회사당 2콜, 한도까지
                 ("invest", lambda: business.refresh_other_investments(con, [c for c in ccs if c not in done_invest_latest(con)], a.year - 1)),   # 타법인 출자 — 최신 분기보고서 기준으로 갱신
                 ("da", lambda: __import__("da_xbrl").run(con=con)),
                 ("da_notes", lambda: __import__("notes_v2").run(con=con)),   # XBRL에 없는 상각비는 주석 변동표에서(2026-10-24)   # 상각비(XBRL) — 건당 수초로 느려 분기·주석·요약 뒤로(2026-10-23)
                 ("price_backfill", lambda: __import__("prices").backfill(pairs, start="2011-01-01", max_calls=1500))]   # 과거 시세(최근→과거, 시장별 1,500콜/일): 2011년까지(2026-10-23 사용자 요청) — KRX는 연속 호출 수백 건 후 차단되므로 야간 분할
        import os as _os
        _cont = Path(__file__).resolve().with_name("continue.txt")
        if a.hop < 8 and not _os.environ.get("KOSDAQ_IN_CHAIN"):   # 시작 시 미리 기록 → 170분 제한으로 중간에 끊겨도 워크플로가 catchup을 자동 재실행(정상 완료 시 삭제)
            _cont.write_text(f"--hop {a.hop + 1} --steps catchup", encoding="utf-8")
        for name, fn in steps:
            if common.QUOTA_HIT and name not in ("price_backfill", "kpi", "summarize"):      # DART 한도 도달 시 DART 단계만 건너뜀(kpi 재계산·KRX 시세 백필은 진행)
                print(f"[catchup] DART 한도 도달 — {name} 건너뜀(내일)", flush=True); continue
            print(f"[catchup] {name} 시작", flush=True)
            try:
                fn()
            except KeyboardInterrupt:        # 170분 제한(timeout INT) → 커밋·사이트 반영 후 다음 실행에서 catchup 이어하기(단계별 미수집분만 수집하므로 재개 가능)
                con.commit(); print(f"[catchup] 제한 시간 도달 — {name} 중단, 다음 실행에서 자동 이어하기", flush=True)
                publish(f"catchup {name} 부분")
                break
            except Exception as e:  # noqa
                print(f"[catchup] {name} 오류: {e}", flush=True)
            if name in ("biz", "summarize", "fin", "fin_ofs", "fin_yoy", "da", "da_notes", "quarters", "invest", "notes"):    # 단계가 끝날 때마다 사이트 반영
                con.commit(); publish(f"catchup {name}")
        else:   # 모든 단계 정상 완료 → 이어하기 불필요
            if _cont.exists() and not _os.environ.get("KOSDAQ_IN_CHAIN"):
                _cont.unlink()
    if a.job in ("price", "all"):
        import prices
        prices.run(pairs, a.year, start=a.start, markets=a.market)
    if a.job in ("summarize", "all"):
        import summarize
        # 등급 보유사 = 신용평가서+사업보고서 통합(항목별 근거), 나머지 = 사업보고서 — 병렬·재개 가능
        if a.type == "비상장":              # 2026-10-25: 사업보고서 없는 비상장 등급사 = 신용평가서 + 감사보고서 '회사의 개요' + 재무 요약
            summarize.run_unlisted(set(ccs) if a.codes else None, model=a.model, force=a.force)
        else:
            summarize.run_all(set(ccs) if a.codes else None, model=a.model, force=a.force, limit=a.limit)
    if a.job == "notes":                      # 주석 원문(차입금·우발채무·약정·담보·보증·소송·특수관계자·계열) — 최신 정기보고서, 회사당 1~2콜 (chain에서 단독 실행용; catchup의 notes 단계와 동일)
        import business
        have = {r[0] for r in con.execute("SELECT corp_code FROM notes_latest")} if con.execute("SELECT name FROM sqlite_master WHERE name='notes_latest'").fetchone() else set()
        business.run_notes(con, [c for c in ccs if c not in have], limit=a.limit)
    if a.job == "da_notes":                   # 주석 변동표 상각비(유형·무형·사용권) + 주석 표 구조 보존 재수집 — DART만, AI 없음
        import notes_v2
        notes_v2.run(set(ccs) if a.codes else None, limit=a.limit, con=con)
        print("KPI 재계산:", __import__("financials").rebuild_kpi_all(con), "건", flush=True)
    if a.job == "rawdump":                    # 진단: 지정 종목의 fin_raw 손익 행(연결·별도, 최근 분기)을 로그에 출력 — DART 호출 없음
        for cc, sc in pairs:
            print(f"=== {sc} {cc}", flush=True)
            for r in con.execute("""SELECT bsns_year, reprt_code, fs_div, sj_div, account_nm, thstrm_amount, frmtrm_amount, rcept_no FROM fin_raw
                                    WHERE corp_code=? AND sj_div IN ('IS','CIS') AND reprt_code<>'11011' AND bsns_year>='2025'
                                    AND account_nm IN ('매출액','수익(매출액)','영업수익','매출','영업이익','영업이익(손실)','당기순이익','당기순이익(손실)','반기순이익','분기순이익')
                                    ORDER BY bsns_year, reprt_code, fs_div, sj_div, ord""", (cc,)):
                print("  ", dict(r), flush=True)
            for r in con.execute("SELECT * FROM fin_cum_ok WHERE corp_code=?", (cc,)):
                print("   cum_ok", tuple(r), flush=True)
    if a.job == "invest":                     # 타법인 출자 현황 — 최신 분기보고서 기준 갱신
        import business
        have = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM other_invest WHERE reprt_code IN ('11012','11013','11014')")}
        business.refresh_other_investments(con, [c for c in ccs if c not in have], a.year - 1)
    if a.job == "notes_struct":               # 3-1 차입금·우발채무·계열 구조화(Haiku, DART 호출 없음)
        import notes_struct
        notes_struct.run(set(ccs) if a.codes else None, limit=a.limit, con=con, force=a.force)
    if a.job == "fin_yoy":                    # 전년동기 열 재무상태표 보강(최신 분기의 전년 같은 분기 보고서 직접, 연결·별도 각 1콜)
        import financials
        financials.run_yoy(ccs, con=con)
    if a.job == "fin_ofs":                    # 연결만 있는 회사의 별도 재무 보강(회사당 2콜)
        import financials
        financials.run_ofs(ccs, a.year - 1, con=con)
    if a.job == "rating_valid":               # 신용평가서 PDF의 등급 유효기간(2026-10-26) — 회사·평가사·장단기별 최신 평가서, 1회 다운로드/건
        __import__("rating_valid").run(set(ccs) if a.codes else None, limit=a.limit, con=con)
    if a.job == "da":                         # 상각비 보강(XBRL 주석) — da 빈 회사의 최신 연간·분기, 1콜/보고서
        import da_xbrl
        da_xbrl.run(ccs if a.codes else None, limit=a.limit, con=con, debug=a.debug is not None)
    if a.job == "chain":                      # 한 번의 실행에서 여러 작업을 순서대로(대기열 교체·빈 시간 방지)
        import subprocess, shlex, time, os
        me = str(Path(__file__).resolve())
        EVERY = int(os.environ.get("CHAIN_PUBLISH_MIN", "40")) * 60     # 긴 단계는 40분마다 중간 반영
        steps = list(a.steps or [])
        from datetime import datetime, timezone, timedelta
        _h = datetime.now(timezone(timedelta(hours=9))).hour
        if a.hop and steps and all(x.split(":")[0] == "catchup" for x in steps) and 7 <= _h < 20:
            # 2026-10-27: 미수집분 보충(catchup)의 자동 이어하기가 낮 시간에 대기열을 차지해 시세·사용자 작업을 밀어냄 → 낮(07~20시)에는 이어하지 않고 밤 00:10 정기 실행에 맡김
            print(f"[chain] 낮 시간({_h}시) catchup 이어하기 생략 — 오늘 밤 00:10 정기 실행에서 계속", flush=True)
            steps = []
        def save_continue(remaining):
            """170분 제한으로 끊기면 남은 단계를 continue.txt에 남김 → 워크플로 마지막 단계가 같은 chain을 자동으로 다시 실행"""
            if not remaining or a.hop >= 8:
                return
            arg = "--hop " + str(a.hop + 1) + " --steps " + " ".join(shlex.quote(x) for x in remaining)
            Path(me).with_name("continue.txt").write_text(arg, encoding="utf-8")
            print(f"[chain] 제한 시간 도달 — 남은 단계 자동 이어하기 예약: {remaining}", flush=True)
        for si, st in enumerate(steps):
            nm, _, rest = st.partition(":")
            print(f"[chain] ▶ {nm} {rest}", flush=True)
            p = subprocess.Popen([sys.executable, me, nm] + shlex.split(rest), env={**os.environ, "KOSDAQ_IN_CHAIN": "1"})   # 하위 작업은 continue.txt를 직접 쓰지 않음(chain이 관리)
            last = time.time()
            try:
                while p.poll() is None:
                    time.sleep(15)
                    if time.time() - last >= EVERY:
                        publish(f"{nm} 진행 중"); last = time.time()
            except KeyboardInterrupt:          # 170분 제한(timeout INT) → 하위 작업도 정리하고 종료(이후 단계는 워크플로가 저장·배포)
                save_continue(steps[si:])
                p.send_signal(__import__("signal").SIGINT)
                try:
                    p.wait(timeout=120)
                except Exception:  # noqa
                    p.kill()
                raise
            publish(f"{nm} 완료")
    if a.job == "rating_fix":                 # 신용등급 계열사 혼입 정정(완전일치 재조회, 등급 보유사만 ~10분)
        import fss_rating
        fss_rating.fix_mismatch(con)
    if a.job in ("rating", "all"):
        import fss_rating
        fss_rating.run(set(ccs) if (a.limit or a.new_only) else None)
        try:
            __import__("rating_valid").run(con=con)      # 새로 받은 평가서의 유효기간(2026-10-26)
        except Exception as e:  # noqa
            print(f"[rating] 유효기간 보강 오류: {e}", flush=True)
    if a.job in ("news", "all"):
        import news
        news.run(set(ccs) if (a.limit or a.new_only) else None)
    if a.job == "kind":
        import kind_ir
        kind_ir.run()
    if a.job == "hk":
        import hankyung
        hankyung.run(period=a.period, max_pages=a.max_pages, pdf=not a.no_pdf)
    if a.job == "analyze":
        import analyze_report
        analyze_report.run(limit=a.limit)
    if a.job in ("profile", "all"):
        import profile
        profile.export_all()
        print("profiles/ 출력 완료")
    if a.job in ("site", "all"):
        import build_site
        build_site.build()

    record_usage(con, a.job)
    fails = con.execute("SELECT job, COUNT(*) n FROM fetch_log WHERE status='fail' GROUP BY job").fetchall()
    if fails:
        print("실패 요약:", {r["job"]: r["n"] for r in fails})


if __name__ == "__main__":
    main()
