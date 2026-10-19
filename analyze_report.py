"""리포트 분석: PDF 텍스트 → Claude 정형 추출 → report_analysis.
원문·문단은 저장하지 않는다. 숫자(전망치·가정)와 자체 재서술 요약만 남긴다.
설치: pip install pypdf anthropic
"""
import json
import os
from pathlib import Path

from common import ROOT, connect, now

PROMPT = """당신은 여신심사역이다. 아래는 증권사 애널리스트 리포트 텍스트다.
리포트 문장을 베끼지 말고, 아래 JSON 스키마로 사실·수치·판단만 추출하라. 없으면 null. 금액 단위는 원(억원이면 ×1e8).
{
 "company": "", "stock_code": "6자리 or null",
 "opinion": "", "target_price": null,
 "forecast": [{"year": "2025", "revenue": null, "op_income": null, "net_income": null, "is_estimate": true}],
 "valuation": {"method": "PER/EV·EBITDA/SOTP/DCF 등", "multiple": null, "base_eps": null, "base_year": "", "note": "한 줄"},
 "points": [{"point": "투자포인트를 내 말로 한 줄", "evidence": "근거 수치 한 줄"}],
 "risks": ["리스크 한 줄"],
 "assumptions": ["실적 전망의 핵심 가정 한 줄"],
 "memo_md": "심사 관점 3~6줄: 애널리스트 논리의 핵심, 전망의 공격성, 차주 신용에 유의할 점. 내 문장으로."
}

리포트 텍스트:
"""


def pdf_text(path: Path, max_pages=30) -> tuple[str, int]:
    from pypdf import PdfReader  # noqa
    rd = PdfReader(str(path))
    n = len(rd.pages)
    txt = "\n".join((rd.pages[i].extract_text() or "") for i in range(min(n, max_pages)))
    return txt, n


def analyze_text(text: str, model="claude-sonnet-4-5") -> dict:
    import anthropic  # noqa
    client = anthropic.Anthropic()
    msg = client.messages.create(model=model, max_tokens=2500,
                                 messages=[{"role": "user", "content": PROMPT + text[:150_000]}])
    out = msg.content[0].text.strip()
    if out.startswith("```"):
        out = out.strip("`").split("\n", 1)[1]
    return json.loads(out)


def analyze_one(con, report_id: int) -> dict | None:
    r = con.execute("SELECT * FROM report_meta WHERE id=?", (report_id,)).fetchone()
    if not r or not r["local_path"]:
        return None
    path = ROOT / r["local_path"]
    if not path.exists():
        return None
    text, pages = pdf_text(path)
    if len(text) < 300:
        return None  # 스캔본/추출 실패
    js = analyze_text(text)
    con.execute("""INSERT OR REPLACE INTO report_analysis VALUES (?,?,?,?,?,?,?,?,?)""",
                (report_id, r["stock_code"], json.dumps(js.get("forecast"), ensure_ascii=False),
                 json.dumps(js.get("valuation"), ensure_ascii=False), json.dumps(js.get("points"), ensure_ascii=False),
                 json.dumps(js.get("risks"), ensure_ascii=False), js.get("memo_md"), pages, now()))
    con.commit()
    return js


def forecast_vs_actual(con, stock_code: str) -> list[dict]:
    """리포트 전망치 vs DB 실제(연결 우선) 대조. 달성률(%)과 전망 발행 시점 포함."""
    actual = {}
    for fs in ("CFS", "OFS"):
        for k in con.execute("SELECT bsns_year, revenue, op_income, net_income FROM fin_kpi WHERE corp_code=(SELECT corp_code FROM company WHERE stock_code=?) AND fs_div=? AND reprt_code='11011'", (stock_code, fs)):
            actual.setdefault(k["bsns_year"], dict(k))
    out = []
    for r in con.execute("""SELECT m.pub_dt, m.publisher, a.forecast_json FROM report_analysis a JOIN report_meta m ON m.id=a.report_id
                            WHERE a.stock_code=? ORDER BY m.pub_dt""", (stock_code,)):
        for f in json.loads(r["forecast_json"] or "[]"):
            y = str(f.get("year") or "")[:4]
            a = actual.get(y)
            row = {"pub_dt": r["pub_dt"], "publisher": r["publisher"], "year": y,
                   "f_rev": f.get("revenue"), "f_op": f.get("op_income"), "a_rev": a and a["revenue"], "a_op": a and a["op_income"]}
            row["ach_rev"] = round(row["a_rev"] / row["f_rev"] * 100, 1) if row["a_rev"] and row["f_rev"] else None
            row["ach_op"] = round(row["a_op"] / row["f_op"] * 100, 1) if row["a_op"] and row["f_op"] else None
            out.append(row)
    return out


def run(stock_codes=None, limit=None, force=False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 리포트 분석 건너뜀")
        return
    con = connect()
    q = "SELECT m.id, m.stock_code FROM report_meta m LEFT JOIN report_analysis a ON a.report_id=m.id WHERE m.local_path IS NOT NULL"
    if not force:
        q += " AND a.report_id IS NULL"
    q += " ORDER BY m.pub_dt DESC"
    rows = con.execute(q).fetchall()
    if stock_codes:
        rows = [r for r in rows if r["stock_code"] in stock_codes]
    if limit:
        rows = rows[:limit]
    n = 0
    for r in rows:
        try:
            if analyze_one(con, r["id"]):
                n += 1
        except Exception as e:  # noqa
            print("분석 실패", r["id"], e)
    print(f"리포트 분석 {n}건")


if __name__ == "__main__":
    run(limit=3)
