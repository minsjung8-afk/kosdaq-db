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
    ap.add_argument("job", choices=["init", "fin", "kpi", "biz", "mezz", "price", "rating", "news", "kind", "hk", "analyze", "summarize", "profile", "site", "all", "delta", "daily", "catchup", "unlisted", "quarters", "subst", "fin_fast", "rating_fix", "da", "chain", "fin_ofs", "fin_yoy", "notes_struct"])
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
    ap.add_argument("--new-only", action="store_true", help="init: 기존 종목 생략 / fin·biz·rating·news: 해당 데이터가 없는 회사만")
    a = ap.parse_args()

    if a.job in ("init", "all"):
        import listing
        for m in a.market:
            listing.run(limit=a.limit, market=m, skip_existing=a.new_only)
    con = connect()
    pairs = corp_pairs(con, a.limit)
    if a.codes:
        want = set(a.codes); pairs = [p for p in corp_pairs(con) if p[1] in want]
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
        import unlisted
        unlisted.run(years=(a.years[0] if a.years else 3), max_pages=a.max_pages, ttype=a.type)
    if a.job == "delta":
        import delta
        delta.run(days=a.limit)
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
        steps = [("mezz", lambda: mezz.run(ccs, new_only=True)),
                 ("biz", lambda: business.run([c for c in ccs if c not in done("business")], a.year - 1)),
                 ("summarize", lambda: __import__("summarize").run_all(None, model="haiku", force=False)),   # 새로 받은 사업보고서만 Haiku 요약(기존은 건너뜀)
                 ("audit_fix", lambda: [business.fetch_audit_opinion(con, c, str(a.year - 1)) for c in (done("business") - done("audit_opinion"))]),   # 복구로 비워진 감사의견 보강(1콜/사)
                 ("fin", lambda: financials.run_annual_fast([c for c in ccs if c not in done_annual(con)], a.year - 1, con=con)),   # 연간 재무가 아예 없는 회사만(코스피·코넥스 신규) — 분기·누적 보정은 quarters가 담당
                 ("fin_ofs", lambda: financials.run_ofs(ccs, a.year - 1, con=con)),
                 ("fin_yoy", lambda: financials.run_yoy(ccs, con=con)),       # 전년동기 BS 보강   # 별도 재무 보강(연결만 있는 회사)
                 ("da", lambda: __import__("da_xbrl").run(con=con)),   # 상각비(XBRL 주석) — EBITDA 산출 불가 회사 보강
                 ("quarters", lambda: quarters.run(ccs, con=con)),
                 ("kpi", lambda: print("KPI 재계산:", financials.rebuild_kpi_all(con), "건")),                       # 8분기·LTM용 분기 손익(주요계정 1콜/보고서), 한도까지
                 ("notes", lambda: business.run_notes(con, [c for c in ccs if c not in done_notes(con)])),   # 주석(차입금·우발채무·특수관계자) — 최신 정기보고서(분기 우선) 원문만, 회사당 2콜, 한도까지
                 ("invest", lambda: business.refresh_other_investments(con, [c for c in ccs if c not in done_invest_latest(con)], a.year - 1)),   # 타법인 출자 — 최신 분기보고서 기준으로 갱신
                 ("price_backfill", lambda: __import__("prices").backfill(pairs, start="2016-01-01", max_calls=1500))]   # 과거 시세(최근→과거, 시장별 1,500콜/일): 차트 5년·전체 기간용
        for name, fn in steps:
            if common.QUOTA_HIT and name not in ("price_backfill", "kpi", "summarize"):      # DART 한도 도달 시 DART 단계만 건너뜀(kpi 재계산·KRX 시세 백필은 진행)
                print(f"[catchup] DART 한도 도달 — {name} 건너뜀(내일)", flush=True); continue
            print(f"[catchup] {name} 시작", flush=True)
            try:
                fn()
            except Exception as e:  # noqa
                print(f"[catchup] {name} 오류: {e}", flush=True)
            if name in ("biz", "summarize", "fin", "fin_ofs", "fin_yoy", "da", "quarters", "invest", "notes"):    # 단계가 끝날 때마다 사이트 반영
                con.commit(); publish(f"catchup {name}")
    if a.job in ("price", "all"):
        import prices
        prices.run(pairs, a.year, start=a.start, markets=a.market)
    if a.job in ("summarize", "all"):
        import summarize
        # 등급 보유사 = 신용평가서+사업보고서 통합(항목별 근거), 나머지 = 사업보고서 — 병렬·재개 가능
        summarize.run_all(set(ccs) if a.codes else None, model=a.model, force=a.force, limit=a.limit)
    if a.job == "notes_struct":               # 3-1 차입금·우발채무·계열 구조화(Haiku, DART 호출 없음)
        import notes_struct
        notes_struct.run(set(ccs) if a.codes else None, limit=a.limit, con=con, force=a.force)
    if a.job == "fin_yoy":                    # 전년동기 열 재무상태표 보강(최신 분기의 전년 같은 분기 보고서 직접, 연결·별도 각 1콜)
        import financials
        financials.run_yoy(ccs, con=con)
    if a.job == "fin_ofs":                    # 연결만 있는 회사의 별도 재무 보강(회사당 2콜)
        import financials
        financials.run_ofs(ccs, a.year - 1, con=con)
    if a.job == "da":                         # 상각비 보강(XBRL 주석) — da 빈 회사의 최신 연간·분기, 1콜/보고서
        import da_xbrl
        da_xbrl.run(ccs if a.codes else None, limit=a.limit, con=con, debug=a.debug is not None)
    if a.job == "chain":                      # 한 번의 실행에서 여러 작업을 순서대로(대기열 교체·빈 시간 방지)
        import subprocess, shlex, time, os
        me = str(Path(__file__).resolve())
        EVERY = int(os.environ.get("CHAIN_PUBLISH_MIN", "40")) * 60     # 긴 단계는 40분마다 중간 반영
        steps = list(a.steps or [])
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
            p = subprocess.Popen([sys.executable, me, nm] + shlex.split(rest))
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
