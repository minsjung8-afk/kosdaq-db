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



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["init", "fin", "kpi", "biz", "mezz", "price", "rating", "news", "kind", "hk", "analyze", "summarize", "profile", "site", "all", "delta", "daily", "catchup", "unlisted", "quarters", "subst"])
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
    ap.add_argument("--codes", nargs="*", help="대상 종목코드 지정 (예: --codes 060310 006840) — 모든 작업 공통")
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
    if a.job == "subst":                     # 대용가(KRX 정보데이터시스템 MDCSTAT04001) 1콜
        import subst
        subst.run(start=a.start, con=con)
    if a.job == "quarters":                  # 8분기·LTM 분기 손익(등급사→코스피→코스닥 순, 한도까지)
        import quarters
        quarters.run(ccs, limit=a.limit, con=con)
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
        steps = [("mezz", lambda: mezz.run(ccs, new_only=True)),
                 ("biz", lambda: business.run([c for c in ccs if c not in done("business")], a.year - 1)),
                 ("audit_fix", lambda: [business.fetch_audit_opinion(con, c, str(a.year - 1)) for c in (done("business") - done("audit_opinion"))]),   # 복구로 비워진 감사의견 보강(1콜/사)
                 ("fin", lambda: financials.run([c for c in ccs if c not in done("fin_raw")], years)),
                 ("kpi", lambda: print("KPI 재계산:", financials.rebuild_kpi_all(con), "건")),
                 ("quarters", lambda: quarters.run(ccs, con=con)),                       # 8분기·LTM용 분기 손익(주요계정 1콜/보고서), 한도까지
                 ("price_backfill", lambda: __import__("prices").backfill(pairs, start="2016-01-01", max_calls=600))]   # 과거 시세(최근→과거, 시장별 600콜/일): 차트 5년·전체 기간용
        for name, fn in steps:
            if common.QUOTA_HIT and name != "price_backfill":                 # DART 한도 도달 시 DART 단계는 건너뛰고 KRX 시세 백필만 진행
                print(f"[catchup] DART 한도 도달 — {name} 건너뜀(내일)", flush=True); continue
            print(f"[catchup] {name} 시작", flush=True)
            try:
                fn()
            except Exception as e:  # noqa
                print(f"[catchup] {name} 오류: {e}", flush=True)
    if a.job in ("price", "all"):
        import prices
        prices.run(pairs, a.year, start=a.start, markets=a.market)
    if a.job in ("summarize", "all"):
        import summarize
        if not a.codes:
            summarize.run_rating(set(ccs) if a.limit else None)              # 등급 보유사: 신용평가서 기반 요약(우선)
        summarize.run(ccs, a.years[-1] if a.years else a.year - 1)           # 나머지(또는 --codes 지정분): 사업보고서 기반
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

    fails = con.execute("SELECT job, COUNT(*) n FROM fetch_log WHERE status='fail' GROUP BY job").fetchall()
    if fails:
        print("실패 요약:", {r["job"]: r["n"] for r in fails})


if __name__ == "__main__":
    main()
