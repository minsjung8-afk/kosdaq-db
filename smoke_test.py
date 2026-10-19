"""오프라인 스모크 테스트: DART 호출 없이 파서·KPI·요약·렌더 검증"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "collect")]

import common  # noqa
common.DB_PATH = ROOT / "test_smoke.db"
if common.DB_PATH.exists():
    common.DB_PATH.unlink()

from common import connect, now  # noqa
import business, financials, prices, profile  # noqa

con = connect()
CC, SC = "00999999", "999999"
con.execute("INSERT INTO company (corp_code, stock_code, corp_name, market, sector_krx, induty_code, ceo_nm, est_dt, listed_dt, acc_mt, adres, hm_url, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CC, SC, "테스트전자", "KOSDAQ", "반도체", "26110", "홍길동", "19990301", "20100615", "12", "경기 성남시", "https://x.test", now()))

# 1) 사업의 내용 파서 (DART 원문 구조 모사)
xml = """<DOCUMENT><BODY>
<SECTION-1><TITLE>I. 회사의 개요</TITLE><P>생략</P></SECTION-1>
<SECTION-1><TITLE>II. 사업의 내용</TITLE>
<SECTION-2><TITLE>1. 사업의 개요</TITLE><P>당사는 반도체 &amp; 디스플레이용 검사장비를 제조합니다.</P>
<TABLE><TR><TD>제품</TD><TD>비중</TD></TR><TR><TD>검사장비</TD><TD>82%</TD></TR></TABLE></SECTION-2>
<SECTION-2><TITLE>2. 주요 제품 및 서비스</TITLE><P>주요 고객은 S사, L사입니다.</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>생략</P></SECTION-1>
</BODY></DOCUMENT>"""
full, ov = business.extract_business_section(xml)
assert full and "검사장비" in full and "재무에 관한" not in full, full
assert ov and "82%" in ov, ov
con.execute("INSERT INTO business (corp_code,bsns_year,rcept_no,reprt_code,overview_text,full_text,char_len,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            (CC, "2025", "20260315000001", "11011", ov, full, len(full), now()))
print("[OK] 사업의 내용 파서:", len(full), "chars / 개요:", ov.replace("\n", " ")[:60])

# 2) KPI (fin_raw 모사)
raw = [("BS", "ifrs-full_Assets", "자산총계", 1500e8), ("BS", "ifrs-full_Liabilities", "부채총계", 900e8),
       ("BS", "ifrs-full_Equity", "자본총계", 600e8), ("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", 100e8),
       ("BS", "-표준계정코드 미사용-", "단기차입금", 300e8), ("BS", "-표준계정코드 미사용-", "장기차입금", 200e8),
       ("IS", "ifrs-full_Revenue", "매출액", 2000e8), ("IS", "dart_OperatingIncomeLoss", "영업이익", 150e8),
       ("IS", "ifrs-full_ProfitLoss", "당기순이익", 100e8), ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", 180e8)]
for y in ("2024", "2025"):
    for sj, aid, nm, amt in raw:
        con.execute("INSERT INTO fin_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (CC, y, "11011", "CFS", sj, aid, nm, amt * (1.1 if y == "2025" else 1), None, None, 0, None))
con.commit()
for y in ("2024", "2025"):
    financials.build_kpi(con, CC, y)
k = con.execute("SELECT * FROM fin_kpi WHERE corp_code=? AND bsns_year='2025'", (CC,)).fetchone()
assert abs(k["debt_ratio"] - 150.0) < 0.1 and abs(k["borrowings"] - 550e8) < 1, dict(k)
assert abs(k["net_borrow_ratio"] - 66.7) < 0.1  # (550-110)/660
print("[OK] KPI: 부채비율", k["debt_ratio"], "순차입금/자본", k["net_borrow_ratio"], "영업이익률", k["op_margin"])

# 3) 주가 요약
import random, datetime  # noqa
random.seed(1)
d, p = datetime.date(2025, 10, 20), 10000.0
rows = []
while d <= datetime.date(2026, 10, 16):
    if d.weekday() < 5:
        p *= 1 + random.gauss(0, 0.02)
        rows.append((SC, d.isoformat(), p, p * 1.01, p * 0.99, p, random.randint(50000, 300000)))
    d += datetime.timedelta(days=1)
con.executemany("INSERT INTO price_daily VALUES (?,?,?,?,?,?,?)", rows)
con.commit()
asof = prices.build_summary(con, SC, shares_out=20_000_000)
ps = con.execute("SELECT * FROM price_summary WHERE stock_code=?", (SC,)).fetchone()
assert ps["mcap"] and ps["high_52w"] >= ps["close"] >= ps["low_52w"] and ps["vol_60d"]
print("[OK] 주가요약:", asof, "시총", round(ps["mcap"] / 1e8), "억, 1Y", ps["chg_1y"], "%, vol60", ps["vol_60d"])

# 4) 이벤트/감사의견/주주
con.execute("INSERT INTO audit_opinion VALUES (?,?,?,?,?,?,?)", (CC, "2025", "삼일회계법인", "적정", None, "계속기업 관련 중요한 불확실성", None))
con.execute("INSERT INTO shareholder VALUES (?,?,?,?,?,?,?,?)", (CC, "2025", "홍길동", "본인", "보통주", 6000000, 6000000, 30.0))
con.execute("INSERT INTO disclosure_event VALUES (?,?,?,?,?)", (CC, "20260701000001", "20260701", "주요사항보고서(전환사채권발행결정)", "CB"))
con.commit()

# 5) 렌더
md = profile.render(con, CC)
out = ROOT / "profiles"; out.mkdir(exist_ok=True)
(out / "SAMPLE_테스트전자.md").write_text(md, encoding="utf-8")
assert "## 3. 재무현황" in md and "150.0%" in md and "전환사채" in md
print("[OK] 프로필 렌더 → profiles/SAMPLE_테스트전자.md", len(md), "chars")
con.execute("""INSERT INTO report_meta (stock_code, source, source_id, title, publisher, analyst, pub_dt, opinion, target_price, old_target_price, url, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (SC, "HK", "1", "테스트전자(999999) 수주 회복", "테스트증권", "김분석", "20250315", "Buy", 15000, 13000, "https://example.test/1", now()))
rid = con.execute("SELECT id FROM report_meta").fetchone()[0]
con.execute("INSERT INTO report_analysis VALUES (?,?,?,?,?,?,?,?,?)", (rid, SC,
    '[{"year":"2025","revenue":2.3e11,"op_income":2.0e10,"net_income":1.5e10,"is_estimate":true}]',
    '{"method":"PER","multiple":12,"base_eps":1250,"base_year":"2025"}',
    '[{"point":"검사장비 수주 회복","evidence":"수주잔고 전년比 +30%"}]', '["고객 집중"]',
    "애널리스트는 2025년 영업이익 200억을 전망. 실제 165억으로 달성률 83%.", 12, now()))
con.commit()
from analyze_report import forecast_vs_actual  # noqa
fva = forecast_vs_actual(con, SC)
assert fva and fva[0]["ach_op"] == 82.5, fva
print("[OK] 전망 vs 실적:", fva[0]["ach_rev"], fva[0]["ach_op"])
import build_site  # noqa
build_site.build()
assert (ROOT / "site" / "data" / "c" / f"{SC}.json").exists()
print("[OK] site/ 빌드")
common.DB_PATH.unlink()
