"""기업별 심사참고 프로필(Markdown) 렌더. 틀의 '출력 양식' 역할."""
import json
from pathlib import Path

from common import ROOT, connect


def won(v, unit=1e8, digits=0):
    """원 → 억원"""
    if v is None:
        return "-"
    return f"{v / unit:,.{digits}f}"


def pct(v):
    return "-" if v is None else f"{v:,.1f}%"


def render(con, corp_code: str) -> str:
    c = con.execute("SELECT * FROM company WHERE corp_code=?", (corp_code,)).fetchone()
    if not c:
        return f"# {corp_code}: 개요 없음"
    L = [f"# {c['corp_name']} ({c['stock_code']}, {c['market']})", ""]

    # 1. 개요
    L += ["## 1. 개요", "",
          "| 항목 | 내용 | 항목 | 내용 |", "|---|---|---|---|",
          f"| 대표이사 | {c['ceo_nm'] or '-'} | 설립일 | {c['est_dt'] or '-'} |",
          f"| 상장일 | {c['listed_dt'] or '-'} | 결산월 | {c['acc_mt'] or '-'}월 |",
          f"| KRX 업종 | {c['sector_krx'] or '-'} | KSIC | {c['induty_code'] or '-'} |",
          f"| 본점 | {c['adres'] or '-'} | 홈페이지 | {c['hm_url'] or '-'} |", ""]
    ao = con.execute("SELECT * FROM audit_opinion WHERE corp_code=? ORDER BY bsns_year DESC", (corp_code,)).fetchall()
    if ao:
        L += ["**감사의견**", "", "| 연도 | 감사인 | 의견 | 강조사항 |", "|---|---|---|---|"]
        L += [f"| {r['bsns_year']} | {r['adtor'] or '-'} | {r['adt_opinion'] or '-'} | {(r['emphs_matter'] or '-')[:60]} |" for r in ao]
        L.append("")
    sh = con.execute("""SELECT * FROM shareholder WHERE corp_code=? AND bsns_year=(SELECT MAX(bsns_year) FROM shareholder WHERE corp_code=?)
                        ORDER BY trmend_posesn_stock_qota_rt DESC LIMIT 8""", (corp_code, corp_code)).fetchall()
    if sh:
        L += ["**최대주주 및 특수관계인**", "", "| 성명 | 관계 | 기말주식수 | 지분율 |", "|---|---|---|---|"]
        L += [f"| {r['nm']} | {r['relate'] or '-'} | {int(r['trmend_posesn_stock_co'] or 0):,} | {pct(r['trmend_posesn_stock_qota_rt'])} |" for r in sh]
        L.append("")

    # 2. 사업내용
    b = con.execute("SELECT * FROM business WHERE corp_code=? ORDER BY bsns_year DESC LIMIT 1", (corp_code,)).fetchone()
    L += ["## 2. 사업내용", ""]
    if b and b["summary_json"]:
        js = json.loads(b["summary_json"])
        L += [f"- **업종**: {js.get('industry')}", f"- **사업모델**: {js.get('business_model')}",
              f"- **주요제품**: " + ", ".join(f"{p['name']}({p.get('revenue_share_pct') or '-'}%)" for p in js.get("main_products") or []),
              f"- **주요고객**: " + ", ".join(js.get("major_customers") or []),
              f"- **거래처 집중도**: {js.get('customer_concentration')}",
              f"- **경쟁환경**: {js.get('competition')}",
              f"- **신규사업**: {js.get('new_business')}", "",
              "**심사상 유의 리스크**", ""] + [f"- {x}" for x in js.get("key_risks") or []] + ["", js.get("summary_md") or "", ""]
    elif b and b["overview_text"]:
        L += [f"*(요약 미생성 — 사업보고서 {b['bsns_year']} '사업의 개요' 원문 발췌)*", "", b["overview_text"][:3000], ""]
    else:
        L += ["*(사업내용 미수집)*", ""]

    # 3. 재무현황
    L += ["## 3. 재무현황 (단위: 억원, %)", ""]
    for fs_div, label in (("CFS", "연결"), ("OFS", "별도")):
        ks = con.execute("SELECT * FROM fin_kpi WHERE corp_code=? AND fs_div=? AND reprt_code='11011' ORDER BY bsns_year",
                         (corp_code, fs_div)).fetchall()
        if not ks:
            continue
        L += [f"**{label}**", "", "| 항목 | " + " | ".join(k["bsns_year"] for k in ks) + " |",
              "|---|" + "---|" * len(ks)]
        rows = [("매출액", "revenue", won), ("영업이익", "op_income", won), ("당기순이익", "net_income", won),
                ("자산총계", "total_assets", won), ("부채총계", "total_liab", won), ("자본총계", "equity", won),
                ("총차입금(근사)", "borrowings", won), ("현금성자산", "cash", won), ("영업활동CF", "cfo", won),
                ("부채비율", "debt_ratio", pct), ("영업이익률", "op_margin", pct), ("순차입금/자본", "net_borrow_ratio", pct)]
        for nm, key, f in rows:
            L.append(f"| {nm} | " + " | ".join(f(k[key]) for k in ks) + " |")
        L.append("")

    # 4. 주가추이
    ps = con.execute("SELECT * FROM price_summary WHERE stock_code=? ORDER BY asof DESC LIMIT 1", (c["stock_code"],)).fetchone()
    L += ["## 4. 주가추이 / 담보가치 참고", ""]
    if ps:
        L += ["| 항목 | 값 | 항목 | 값 |", "|---|---|---|---|",
              f"| 기준일 | {ps['asof']} | 종가 | {ps['close']:,.0f}원 |",
              f"| 시가총액 | {won(ps['mcap'])}억 | 발행주식수 | {int(ps['shares_out'] or 0):,} |",
              f"| 52주 고/저 | {ps['high_52w']:,.0f} / {ps['low_52w']:,.0f} | 60일 변동성(연율) | {pct(ps['vol_60d'])} |",
              f"| 1M/3M/1Y 수익률 | {pct(ps['chg_1m'])} / {pct(ps['chg_3m'])} / {pct(ps['chg_1y'])} | 3M 평균 거래대금 | {won(ps['avg_value_3m'])}억/일 |",
              ""]
    else:
        L += ["*(주가 미수집)*", ""]

    # 5. 주요 공시 이벤트
    ev = con.execute("SELECT * FROM disclosure_event WHERE corp_code=? ORDER BY rcept_dt DESC LIMIT 15", (corp_code,)).fetchall()
    L += ["## 5. 주요 공시 이벤트 (심사 관점 필터)", ""]
    if ev:
        L += ["| 일자 | 구분 | 공시명 |", "|---|---|---|"]
        L += [f"| {r['rcept_dt']} | {r['flag']} | {r['report_nm']} |" for r in ev]
    else:
        L.append("*(해당 없음 / 미수집)*")
    L.append("")

    # 6. 리포트
    rp = con.execute("SELECT * FROM report_meta WHERE stock_code=? ORDER BY pub_dt DESC LIMIT 10", (c["stock_code"],)).fetchall()
    L += ["## 6. 외부 리포트 (메타데이터·링크)", ""]
    if rp:
        L += ["| 일자 | 출처 | 발행 | 제목 | 의견/목표가 |", "|---|---|---|---|---|"]
        L += [f"| {r['pub_dt']} | {r['source']} | {r['publisher'] or '-'} | [{r['title']}]({r['url']}) | {r['opinion'] or '-'} / {r['target_price'] or '-'} |" for r in rp]
    else:
        L.append("*(커버리지 없음 / 미수집)*")
    return "\n".join(L)


def export_all(out_dir: Path = ROOT / "profiles"):
    con = connect()
    out_dir.mkdir(exist_ok=True)
    for r in con.execute("SELECT corp_code, corp_name FROM company"):
        (out_dir / f"{r['corp_name']}_{r['corp_code']}.md").write_text(render(con, r["corp_code"]), encoding="utf-8")


if __name__ == "__main__":
    export_all()
