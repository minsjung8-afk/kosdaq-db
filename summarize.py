"""사업의 내용 → 심사용 정형 요약(JSON + 서술). Claude API 사용(anthropic 패키지).
없거나 키가 없으면 건너뜀. 프롬프트는 심사 관점 필드로 고정해 DB 컬럼처럼 쓴다."""
import json
import os

from common import connect, now

PROMPT = """당신은 여신심사역이다. 아래는 상장사(코스피·코스닥·코넥스) 사업보고서 'II. 사업의 내용' 원문이다.
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


def _parse_json(out: str) -> dict:
    """모델 응답에서 JSON만 안전하게 추출(코드펜스·앞뒤 설명문 제거, 첫 '{'~마지막 '}')"""
    out = out.strip()
    if out.startswith("```"):
        out = out.strip("`").split("\n", 1)[1]
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        out = out[i:j + 1]
    return json.loads(out)

def summarize_text(text: str, model: str = "claude-sonnet-4-5") -> dict:
    import anthropic  # noqa
    client = anthropic.Anthropic()
    text = text[:120_000]  # 안전 상한
    msg = client.messages.create(model=model, max_tokens=4000,
                                 messages=[{"role": "user", "content": PROMPT + text}])
    return _parse_json(msg.content[0].text)


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


RATING_PROMPT = """당신은 여신심사역이다. 아래는 신용평가사가 공시한 기업신용평가서 텍스트다.
평가사 문장을 베끼지 말고 내 말로, 아래 JSON 스키마로만 답하라(없으면 null). 금액은 억원 단위 숫자.
{
 "industry": "업종 한 줄",
 "business_model": "무엇을 누구에게 어떻게 파는지 2~3문장",
 "main_products": [{"name": "", "revenue_share_pct": null}],
 "market_position": "시장지위·경쟁력 한두 문장",
 "group_support": "계열·대주주 지원 가능성(있으면)",
 "rating_rationale": ["등급 논리(평가사가 든 근거) 3~5개, 각 한 줄"],
 "rating_triggers": {"up": ["상향 요인"], "down": ["하향 요인"]},
 "key_financials_view": "평가사의 재무 평가 요지 2~3문장(차입·현금흐름·유동성)",
 "key_risks": ["심사상 유의할 리스크 3~5개"],
 "summary_md": "승인신청서 '사업현황'란에 쓸 10줄 내외 서술. 내 문장으로."
}

신용평가서 텍스트:
"""


def _pdf_text(data: bytes, max_pages=40):
    import io
    from pypdf import PdfReader  # noqa
    rd = PdfReader(io.BytesIO(data))
    n = len(rd.pages)
    return "\n".join((rd.pages[i].extract_text() or "") for i in range(min(n, max_pages))), n


def run_rating(corp_codes=None, force=False, model="claude-sonnet-4-5"):
    """등급 보유사: 최신 기업신용평가(없으면 사채) 평가서 PDF → 사업 요약. PDF는 메모리에서만 처리."""
    import anthropic  # noqa
    import requests  # noqa
    from common import UA  # noqa
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 신용평가서 요약 건너뜀")
        return
    con = connect()
    client = anthropic.Anthropic()
    rows = con.execute("""SELECT r.corp_code, r.agency, r.disc_dt, r.grade, r.file_url,
                          CASE WHEN r.target_type LIKE '%기업신용평가%' THEN 0 ELSE 1 END AS pri
                          FROM credit_rating r WHERE r.file_url IS NOT NULL
                          AND (r.target_type LIKE '%기업신용평가%' OR r.target_type LIKE '%사채%')
                          ORDER BY r.corp_code, pri, r.disc_dt DESC""").fetchall()
    latest = {}
    for r in rows:
        latest.setdefault(r["corp_code"], r)
    n = 0
    net_fail = 0          # 금감원 연속 접속 실패 카운터 — 차단/장애 시 3시간 헛도는 것 방지
    import time
    for cc, r in latest.items():
        if corp_codes and cc not in corp_codes:
            continue
        old = con.execute("SELECT disc_dt FROM rating_summary WHERE corp_code=?", (cc,)).fetchone()
        if old and not force and old["disc_dt"] >= r["disc_dt"]:
            continue
        try:
            try:
                pdf = requests.get(r["file_url"], headers=UA, timeout=(15, 60)).content
                net_fail = 0
            except requests.exceptions.ConnectionError as e:
                net_fail += 1
                print("신용평가서 다운로드 실패", cc, type(e).__name__, flush=True)
                if net_fail >= 5:
                    print(f"[summarize] 금감원 접속 연속 {net_fail}회 실패 — 차단/장애로 보고 중단(다음 실행에서 이어서)", flush=True)
                    break
                time.sleep(30)
                continue
            time.sleep(1.5)   # 금감원 서버 부하 완화(차단 예방)
            if pdf[:4] != b"%PDF":
                continue
            text, pages = _pdf_text(pdf)
            if len(text) < 500:
                continue
            msg = client.messages.create(model=model, max_tokens=4000,
                                         messages=[{"role": "user", "content": RATING_PROMPT + text[:150_000]}])
            js = _parse_json(msg.content[0].text)
            con.execute("INSERT OR REPLACE INTO rating_summary VALUES (?,?,?,?,?,?,?,?,?)",
                        (cc, r["agency"], r["disc_dt"], r["grade"], r["file_url"], json.dumps(js, ensure_ascii=False),
                         js.get("summary_md"), pages, now()))
            con.commit()
            n += 1
        except Exception as e:  # noqa
            print("신용평가서 요약 실패", cc, e)
    print(f"신용평가서 요약 {n}건")
