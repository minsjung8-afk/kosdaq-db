"""사업의 내용 → 심사용 정형 요약(JSON + 서술). Claude API 사용(anthropic 패키지).
없거나 키가 없으면 건너뜀. 프롬프트는 심사 관점 필드로 고정해 DB 컬럼처럼 쓴다."""
import json
import os

from common import connect, now

PROMPT = """당신은 여신심사역이다. 아래는 코스닥 상장사 사업보고서 'II. 사업의 내용' 원문이다.
심사 관점에서 다음 JSON 스키마로만 답하라(값이 없으면 null). 추정은 "추정"이라고 표시.
{
 "industry": "업종 한 줄",
 "business_model": "무엇을 누구에게 어떻게 파는지 2~3문장",
 "main_products": [{"name": "", "revenue_share_pct": null, "note": ""}],
 "major_customers": ["고객명/비중"],
 "customer_concentration": "상위 거래처 의존도 평가",
 "raw_material_supply": "원재료·공급망 리스크",
 "competition": "경쟁강도·진입장벽",
 "capacity_utilization": "생산능력·가동률(있으면)",
 "new_business": "신규사업·투자계획",
 "key_risks": ["심사상 유의할 리스크 3~5개"],
 "summary_md": "심사보고서 '사업현황'란에 그대로 쓸 수 있는 10줄 내외 서술"
}

원문:
"""


def summarize_text(text: str, model: str = "claude-sonnet-4-5") -> dict:
    import anthropic  # noqa
    client = anthropic.Anthropic()
    text = text[:120_000]  # 안전 상한
    msg = client.messages.create(model=model, max_tokens=2500,
                                 messages=[{"role": "user", "content": PROMPT + text}])
    out = msg.content[0].text.strip()
    if out.startswith("```"):
        out = out.strip("`").split("\n", 1)[1]
    return json.loads(out)


def run(corp_codes=None, bsns_year=None, force=False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 요약 단계 건너뜀")
        return
    con = connect()
    q = "SELECT corp_code, bsns_year, full_text FROM business WHERE full_text IS NOT NULL"
    if not force:
        q += " AND summary_json IS NULL"
    for r in con.execute(q):
        if corp_codes and r["corp_code"] not in corp_codes:
            continue
        if bsns_year and r["bsns_year"] != str(bsns_year):
            continue
        try:
            js = summarize_text(r["full_text"])
            con.execute("UPDATE business SET summary_json=?, summary_md=?, summarized_at=? WHERE corp_code=? AND bsns_year=?",
                        (json.dumps(js, ensure_ascii=False), js.get("summary_md"), now(), r["corp_code"], r["bsns_year"]))
            con.commit()
        except Exception as e:  # noqa
            print("요약 실패", r["corp_code"], e)
