"""사업의 내용 → 심사용 정형 요약(JSON + 서술). Claude API 사용(anthropic 패키지).
없거나 키가 없으면 건너뜀. 프롬프트는 심사 관점 필드로 고정해 DB 컬럼처럼 쓴다."""
import json
import os

from common import connect, now, ztext

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

MODELS = {"sonnet": "claude-sonnet-4-5", "haiku": "claude-haiku-4-5", "opus": "claude-opus-4-1"}
MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "45000"))      # 입력 절단(사업의 개요~생산·매출 절까지면 충분) — 비용 40% 절감


def summarize_text(text: str, model: str = "claude-sonnet-4-5") -> dict:
    import anthropic  # noqa
    client = anthropic.Anthropic()
    model = MODELS.get(model, model)
    text = text[:MAX_CHARS]
    msg = client.messages.create(model=model, max_tokens=4000,
                                 messages=[{"role": "user", "content": PROMPT + text}])
    return _parse_json(msg.content[0].text)


def run(corp_codes=None, bsns_year=None, force=False, model="claude-sonnet-4-5"):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 요약 단계 건너뜀")
        return
    con = connect()
    q = "SELECT corp_code, bsns_year, COALESCE(full_z, full_text) AS full_text FROM business WHERE (full_z IS NOT NULL OR full_text IS NOT NULL)"
    if not force:
        q += " AND summary_json IS NULL"
    for r in con.execute(q):
        if corp_codes and r["corp_code"] not in corp_codes:
            continue
        if bsns_year and r["bsns_year"] != str(bsns_year):
            continue
        try:
            js = summarize_text(ztext(r["full_text"]), model)
            js["_model"] = MODELS.get(model, model)
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
                          AND COALESCE(r.grade,'') NOT IN ('취소','철회','WD','NR','-')          -- 등급취소 공시(평가서에 심사정보 없음)는 요약 대상에서 제외
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%취소%' AND COALESCE(r.eval_kind,'') NOT LIKE '%철회%'
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


# ---------------------------------------------------------------------------
# 통합 요약(2026-10-22): 등급 보유사 = 신용평가서 + 사업보고서를 한 표로(항목별 근거 표시), 비보유사 = 사업보고서.
# 병렬 호출·재개(이미 같은 모델로 만든 건 건너뜀). 원문·PDF는 메모리에서만 처리.
COMBINED_PROMPT = """당신은 여신심사역이다. 아래 [A] 신용평가사 기업신용평가서와 [B] 사업보고서 'II. 사업의 내용'을 함께 읽고,
두 자료를 적절히 섞어 심사용 요약을 아래 JSON으로만 답하라(없으면 null). 평가사·회사 문장을 베끼지 말고 내 말로. 금액은 억원.
각 항목의 근거를 "_src"에 "신용평가서" / "사업보고서" / "양쪽" 중 하나로 적어라. 두 자료가 다르면 최신·구체적인 쪽을 쓰고 차이를 note에 남겨라.
{
 "industry": "업종 한 줄",
 "business_model": "무엇을 누구에게 어떻게 파는지 2~3문장",
 "main_products": [{"name": "", "revenue_share_pct": null, "note": ""}],
 "major_customers": ["고객명/비중"],
 "customer_concentration": "상위 거래처 의존도 평가",
 "raw_material_supply": "원재료·공급망 리스크",
 "competition": "경쟁강도·진입장벽·시장지위",
 "capacity_utilization": "생산능력·가동률(있으면)",
 "new_business": "신규사업·투자계획",
 "group_support": "계열·대주주 지원 가능성(있으면)",
 "key_financials_view": "평가사의 재무 평가 요지 2~3문장(차입·현금흐름·유동성)",
 "rating_rationale": ["등급 논리 3~5개, 각 한 줄"],
 "rating_triggers": {"up": ["상향 요인"], "down": ["하향 요인"]},
 "key_risks": ["심사상 유의할 리스크 3~5개"],
 "summary_md": "승인신청서 '사업현황'란에 쓸 10줄 내외 서술",
 "_src": {"industry": "", "business_model": "", "main_products": "", "major_customers": "", "customer_concentration": "",
          "raw_material_supply": "", "competition": "", "capacity_utilization": "", "new_business": "", "group_support": "",
          "key_financials_view": "", "rating_rationale": "", "rating_triggers": "", "key_risks": ""}
}
"""


def _call(client, model, content, tries=4):
    import time
    for k in range(tries):
        try:
            msg = client.messages.create(model=model, max_tokens=4000, messages=[{"role": "user", "content": content}])
            return _parse_json(msg.content[0].text)
        except Exception as e:  # noqa
            if k == tries - 1:
                raise
            time.sleep(20 * (k + 1) if "429" in str(e) or "overloaded" in str(e).lower() else 5)


def _latest_ratings(con, include_cp=False):
    """include_cp: 기업어음만 있는 회사도(비상장 요약용 — 에스피엘 등 CP만 보유, 2026-10-25)"""
    cp = " OR r.target_type LIKE '%기업어음%'" if include_cp else ""
    rows = con.execute("""SELECT r.corp_code, r.agency, r.disc_dt, r.grade, r.file_url,
                          CASE WHEN r.target_type LIKE '%기업신용평가%' THEN 0 WHEN r.target_type LIKE '%사채%' THEN 1 ELSE 2 END AS pri
                          FROM credit_rating r WHERE r.file_url IS NOT NULL
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%정정%'
                          AND (r.target_type LIKE '%기업신용평가%' OR r.target_type LIKE '%사채%'""" + cp + """)
                          AND COALESCE(r.grade,'') NOT IN ('취소','철회','WD','NR','-')
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%취소%' AND COALESCE(r.eval_kind,'') NOT LIKE '%철회%'
                          ORDER BY r.corp_code, pri, r.disc_dt DESC""").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["corp_code"], dict(r))
    return out


def run_all(corp_codes=None, model="haiku", force=False, workers=10, limit=None):
    """전 종목 요약. force=True여도 '같은 모델·같은 근거'로 이미 만든 건 건너뜀(타임아웃 후 재개)."""
    import time
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from common import UA
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 요약 건너뜀"); return
    import anthropic  # noqa
    client = anthropic.Anthropic()
    mid = MODELS.get(model, model)
    con = connect()
    rat = _latest_ratings(con)
    biz = {}
    for r in con.execute("""SELECT corp_code, bsns_year, rcept_no, COALESCE(full_z, full_text) t, summary_json FROM business
                            WHERE (full_z IS NOT NULL OR full_text IS NOT NULL) ORDER BY corp_code, bsns_year"""):
        biz[r["corp_code"]] = dict(r)                # 최신 연도가 마지막에 덮어씀
    todo = []
    for cc, b in biz.items():
        if corp_codes and cc not in corp_codes:
            continue
        old = json.loads(b["summary_json"]) if b["summary_json"] else None
        rr = rat.get(cc)
        want_kind = "combined" if rr else "biz"
        if old and old.get("_model") == mid and old.get("_kind", "biz") == want_kind and \
           (not rr or (old.get("_rating") or {}).get("disc_dt") == rr["disc_dt"]):
            continue                                  # 이미 최신 근거로 같은 모델 요약 완료
        if old and not force:
            continue
        todo.append((cc, b, rr))
    todo.sort(key=lambda x: x[2] is None)            # 등급 보유사 먼저
    if limit:
        todo = todo[:limit]
    print(f"[summarize] 대상 {len(todo)}사 (등급 보유 {sum(1 for t in todo if t[2])}) · 모델 {mid} · 병렬 {workers}", flush=True)

    def work(item):
        cc, b, rr = item
        btxt = (ztext(b["t"]) or "")
        if rr:
            pdf_txt = ""
            try:
                pdf = requests.get(rr["file_url"], headers=UA, timeout=(15, 60)).content
                if pdf[:4] == b"%PDF":
                    pdf_txt, _ = _pdf_text(pdf)
            except Exception:  # noqa
                pdf_txt = ""
            if len(pdf_txt) >= 500:
                js = _call(client, mid, COMBINED_PROMPT + "\n[A] 신용평가서(" + rr["agency"] + " " + rr["disc_dt"] + " " + str(rr["grade"]) + "):\n"
                           + pdf_txt[:45000] + "\n\n[B] 사업보고서(" + str(b["bsns_year"]) + "):\n" + btxt[:35000])
                js["_kind"] = "combined"
                js["_rating"] = {k: rr[k] for k in ("agency", "disc_dt", "grade", "file_url")}
                return cc, b, js
        js = _call(client, mid, PROMPT + btxt[:MAX_CHARS])
        js["_kind"] = "biz"
        return cc, b, js

    n = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for i, fut in enumerate([ex.submit(work, it) for it in todo], 1):
            try:
                cc, b, js = fut.result()
                js["_model"] = mid
                con.execute("UPDATE business SET summary_json=?, summary_md=?, summarized_at=? WHERE corp_code=? AND bsns_year=?",
                            (json.dumps(js, ensure_ascii=False), js.get("summary_md"), now(), cc, b["bsns_year"]))
                n += 1
            except Exception as e:  # noqa
                fail += 1
                print("요약 실패", todo[i - 1][0], str(e)[:150], flush=True)
            if i % 25 == 0:
                con.commit()
                print(f"[summarize] {i}/{len(todo)} · 성공 {n} · 실패 {fail} · {int(time.time() - t0)}초", flush=True)
    con.commit()
    print(f"[summarize] 완료 {n}/{len(todo)} · 실패 {fail}", flush=True)


# ---------------------------------------------------------------------------
# 비상장 감사보고서 제출사 요약(2026-10-25 사용자 요청): 사업보고서가 없어 'II. 사업의 내용'이 없는 회사 →
# [A] 신용평가서 PDF(사업·재무 서술이 가장 풍부) + [B] 감사보고서 주석 '회사의 개요' + [C] 감사보고서 재무 요약(KPI)로 같은 스키마 요약.
# 결과는 business 행(rcept_no=감사보고서, overview_text=회사의 개요)으로 저장 → 사이트 '사업현황'에 그대로 표시, 야간 사업보고서 재조회 대상에서도 빠짐.
UNLISTED_PROMPT = COMBINED_PROMPT.replace(
    "[B] 사업보고서 'II. 사업의 내용'을 함께 읽고",
    "[B] 감사보고서 주석 '회사의 개요'와 [C] 감사보고서 재무 요약을 함께 읽고(이 회사는 비상장사로 사업보고서가 없다)").replace(
    '"사업보고서" / "양쪽"', '"감사보고서" / "양쪽"')

_OV_RE = __import__("re").compile(r"(일반\s*사항|회사의\s*개요|회사\s*개요|일반적\s*사항)")

# 감사보고서만 있는 회사(2026-10-26): '회사의 개요' 외에 사업 실체를 보여 주는 주석을 함께 넣는다
_EXTRA_NOTES = (("매출 구성", r"고객과의\s*계약|수익의\s*구분|수익\s*\(?매출|매출액|영업수익|수익인식"),
                ("특수관계자 거래", r"특수관계자"),
                ("영업부문", r"영업\s*부문|부문\s*정보"),
                ("우발부채·약정", r"우발|약정|담보|지급보증"),
                ("건설·공사계약", r"건설계약|공사계약|수주"))


def _audit_extra(xml, per=4000, total=16000):
    """감사보고서 원문 → 주석 중 매출 구성·특수관계자·영업부문·우발/약정·건설계약 절 평문(절당 4천자, 합계 1만6천자)"""
    import re
    from business import _strip
    t = _strip(xml)
    i = t.find("주석")
    t = t[i:] if i >= 0 else t
    heads = [(m.start(), m.group(0)) for m in re.finditer(r"(?m)^\s*\d{1,2}\s*[.．]\s*[^\n]{1,60}$", t)]
    out, used = [], 0
    for label, pat in _EXTRA_NOTES:
        for k, (pos, h) in enumerate(heads):
            if re.search(pat, h) and not re.search(r"정책|추정|기준", h):
                end = heads[k + 1][0] if k + 1 < len(heads) else len(t)
                body = t[pos:end].strip()[:per]
                if len(body) > 80 and used < total:
                    out.append(f"<{label}> {body}")
                    used += len(body)
                break
    return "\n\n".join(out)[:total]


def _company_basics(con, cc):
    """DART 기업개황(company 테이블) — 업종 오인 방지용"""
    try:
        r = con.execute("SELECT corp_name, corp_name_full, ceo_nm, est_dt, adres, hm_url, induty_code, sector_krx FROM company WHERE corp_code=?", (cc,)).fetchone()
    except Exception:  # noqa
        return ""
    if not r:
        return ""
    lab = ("회사명", "정식명칭", "대표자", "설립일", "주소", "홈페이지", "업종코드(KSIC)", "업종")
    return " · ".join(f"{a} {b}" for a, b in zip(lab, tuple(r)) if b)


UNLISTED_RULE = ("\n\n[작성 원칙 — 감사보고서만 있는 회사] ① 사업모델·주요 제품·매출 비중은 [E] '매출 구성' 주석에 있는 것만 쓴다. "
                 "② 주요 거래처·계열 의존도는 [E] '특수관계자 거래'의 매출·매입 금액으로 판단하고 금액을 적는다. "
                 "③ 원재료·경쟁·가동률·신규사업처럼 근거 자료에 없는 항목은 지어내지 말고 '감사보고서상 확인 불가'라고 쓴다. "
                 "④ 업종은 [D] 기업개황과 [B] 회사의 개요로 판단한다. ⑤ 업종 일반론(예: '건설업은 경쟁이 치열하다')을 회사 사실처럼 쓰지 않는다.")


def _audit_overview(xml):
    """감사보고서 원문 → 주석 첫 항목(회사의 개요·일반사항) 평문(최대 6천자)"""
    import re
    from business import _strip
    t = _strip(xml)
    i = t.find("주석")
    m = _OV_RE.search(t, i if i >= 0 else 0)
    if not m:
        return ""
    ls = t.rfind("\n", max(0, m.start() - 15), m.start())     # 제목 줄 시작('1. 일반사항')부터 — 앞 표 잔재('…도건설 |') 제외(2026-10-25)
    s = t[ls + 1 if ls >= 0 else m.start():]
    nxt = re.search(r"\n\s*2\s*[.．]\s*\S", s[200:])          # 다음 주석 항목('2. 재무제표 작성기준' 등) 전까지
    return s[:200 + nxt.start()][:6000] if nxt else s[:6000]


def _fin_digest(con, cc):
    rows = con.execute("""SELECT bsns_year, fs_div, revenue, op_income, net_income, total_assets, total_liab, equity, borrowings, cash, cfo, ebitda, debt_ratio
                          FROM fin_kpi WHERE corp_code=? AND reprt_code='11011' ORDER BY bsns_year DESC, fs_div LIMIT 6""", (cc,)).fetchall()
    eok = lambda v: "-" if v is None else f"{v / 1e8:,.0f}"
    out = []
    for r in rows:
        out.append(f"{r['bsns_year']} {'연결' if r['fs_div'] == 'CFS' else '별도'}: 매출 {eok(r['revenue'])}억 · 영업이익 {eok(r['op_income'])}억 · 순이익 {eok(r['net_income'])}억 · "
                   f"자산 {eok(r['total_assets'])}억 · 부채 {eok(r['total_liab'])}억 · 자본 {eok(r['equity'])}억 · 총차입금 {eok(r['borrowings'])}억 · "
                   f"현금 {eok(r['cash'])}억 · 영업CF {eok(r['cfo'])}억 · EBITDA {eok(r['ebitda'])}억 · 부채비율 {r['debt_ratio'] if r['debt_ratio'] is not None else '-'}%")
    return "\n".join(out)


def run_unlisted(corp_codes=None, model="haiku", force=False, workers=6):
    """대상: market=UNLISTED 중 사업보고서 원문(business.full_text)이 없는 회사. 감사보고서 접수번호는 fin_raw(감사보고서 파서 적재분)에서."""
    import time
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from common import UA
    from business import fetch_document_xml
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - 요약 건너뜀"); return
    import anthropic  # noqa
    client = anthropic.Anthropic()
    mid = MODELS.get(model, model)
    con = connect()
    rat = _latest_ratings(con, include_cp=True)
    q = """SELECT c.corp_code, c.corp_name,
                  (SELECT r.rcept_no FROM fin_raw r WHERE r.corp_code=c.corp_code AND r.reprt_code='11011' AND r.account_id='-표준계정코드 미사용-'
                    AND r.rcept_no NOT LIKE '%:전기' ORDER BY r.bsns_year DESC, r.fs_div LIMIT 1) AS arc,
                  (SELECT MAX(r.bsns_year) FROM fin_raw r WHERE r.corp_code=c.corp_code AND r.reprt_code='11011' AND r.account_id='-표준계정코드 미사용-'
                    AND r.rcept_no NOT LIKE '%:전기') AS ay
           FROM company c WHERE c.market='UNLISTED'
             AND c.corp_code NOT IN (SELECT corp_code FROM business WHERE full_z IS NOT NULL OR full_text IS NOT NULL)"""
    todo = []
    for r in con.execute(q):
        cc = r["corp_code"]
        if corp_codes and cc not in corp_codes:
            continue
        old = con.execute("SELECT summary_json FROM business WHERE corp_code=? AND summary_json IS NOT NULL", (cc,)).fetchone()
        if old and not force:
            continue
        if not r["arc"] and cc not in rat:
            continue                                   # 감사보고서도 등급서도 없으면 요약 근거 없음
        todo.append((cc, r["corp_name"], r["arc"], r["ay"], rat.get(cc)))
    print(f"[summarize_unlisted] 대상 {len(todo)}사 (신용평가서 {sum(1 for t in todo if t[4])}) · 모델 {mid} · 병렬 {workers}", flush=True)

    def work(item):
        cc, nm, arc, ay, rr = item
        ov, axml = "", ""
        if arc:
            try:
                axml = fetch_document_xml(arc)
                ov = _audit_overview(axml)
            except Exception as e:  # noqa
                ov = ""
        pdf_txt = ""
        if rr:
            try:
                pdf = requests.get(rr["file_url"], headers=UA, timeout=(15, 60)).content
                if pdf[:4] == b"%PDF":
                    pdf_txt, _ = _pdf_text(pdf)
            except Exception:  # noqa
                pdf_txt = ""
        fin = _fin_digest(con_local(), cc)
        basics = _company_basics(con_local(), cc)
        extra = ""
        if axml and len(pdf_txt) < 500:                # 신용평가서가 없을 때만(있으면 평가서가 더 정확·풍부)
            try:
                extra = _audit_extra(axml)
            except Exception:  # noqa
                extra = ""
        if len(pdf_txt) < 500 and len(ov) < 200 and len(extra) < 300:
            raise RuntimeError("근거 부족(신용평가서·회사의 개요·주요 주석 모두 없음)")
        content = (UNLISTED_PROMPT + (UNLISTED_RULE if len(pdf_txt) < 500 else "")
                   + ("\n[A] 신용평가서(" + rr["agency"] + " " + rr["disc_dt"] + " " + str(rr["grade"]) + "):\n" + pdf_txt[:45000] if len(pdf_txt) >= 500 else "\n[A] 신용평가서: 없음")
                   + "\n\n[B] 감사보고서 주석 '회사의 개요'(" + str(ay or "") + "):\n" + (ov or "없음")
                   + "\n\n[C] 감사보고서 재무 요약(억원):\n" + (fin or "없음")
                   + "\n\n[D] DART 기업개황: " + (basics or "없음")
                   + ("\n\n[E] 감사보고서 주요 주석(매출 구성·특수관계자·영업부문·우발/약정·건설계약):\n" + extra if extra else ""))
        js = _call(client, mid, content)
        js["_kind"] = "unlisted_audit"
        if rr:
            js["_rating"] = {k: rr[k] for k in ("agency", "disc_dt", "grade", "file_url")}
        return cc, arc, ay, ov, js

    import threading
    _tl = threading.local()

    def con_local():
        if not hasattr(_tl, "con"):
            _tl.con = connect()
        return _tl.con

    n = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for i, fut in enumerate([ex.submit(work, it) for it in todo], 1):
            try:
                cc, arc, ay, ov, js = fut.result()
                js["_model"] = mid
                y = str(ay or (time.localtime().tm_year - 1))
                con.execute("""INSERT INTO business (corp_code, bsns_year, rcept_no, reprt_code, overview_text, summary_json, summary_md, summarized_at, fetched_at)
                               VALUES (?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(corp_code, bsns_year) DO UPDATE SET summary_json=excluded.summary_json, summary_md=excluded.summary_md,
                               summarized_at=excluded.summarized_at, overview_text=COALESCE(excluded.overview_text, business.overview_text),
                               rcept_no=COALESCE(business.rcept_no, excluded.rcept_no)""",
                            (cc, y, arc, "11011", ov or None, json.dumps(js, ensure_ascii=False), js.get("summary_md"), now(), now()))
                n += 1
            except Exception as e:  # noqa
                fail += 1
                print("요약 실패", todo[i - 1][0], todo[i - 1][1], str(e)[:120], flush=True)
            if i % 20 == 0:
                con.commit()
                print(f"[summarize_unlisted] {i}/{len(todo)} · 성공 {n} · 실패 {fail} · {int(time.time() - t0)}초", flush=True)
    con.commit()
    print(f"[summarize_unlisted] 완료 {n}/{len(todo)} · 실패 {fail}", flush=True)
