"""3-1 차입금·우발채무·계열 구조화: notes_latest(최신 정기보고서 주석 원문)를 Haiku로 정형 JSON화 → notes_struct 테이블.
DART 호출 없음. 원문은 이미 압축 보관(notes_latest.notes_z). 같은 보고서·같은 모델이면 건너뜀(재개 가능).
  python run.py notes_struct [--codes 079550 006840 ...] [--limit N]
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, now, ztext  # noqa: E402

MODEL = "claude-haiku-4-5"
MAX_CHARS = 45000

PROMPT = """당신은 여신심사역이다. 아래는 상장사 최신 정기보고서의 재무제표 주석 중 차입금·사채·우발부채·약정·담보·보증·소송·특수관계자·계열회사 관련 절이다.
심사 관점에서 아래 JSON으로만 답하라. 없으면 빈 배열/null. 금액은 모두 '억원' 숫자(원문 단위를 환산, 소수 1자리). 연결 기준 주석이 있으면 연결 우선.
원문에 없는 내용은 만들지 말 것. 각 표 항목은 원문 그대로의 기관·회사명을 쓸 것.
{
 "basis": "연결|별도",
 "as_of": "YYYY-MM-DD (보고기간 말)",
 "borrowings": [{"type": "단기차입금|장기차입금|사채|CP·전단채|리스|기타", "lender": "", "rate_pct": null, "maturity": "YYYY-MM 또는 기간", "amount": null, "secured": "담보/무담보/보증 등"}],
 "maturity_profile": {"within_1y": null, "y1_2": null, "y2_5": null, "over_5y": null},
 "credit_lines": [{"bank": "", "type": "한도·당좌·무역금융 등", "limit": null, "used": null}],
 "collateral_provided": [{"asset": "제공자산", "secured_party": "담보권자", "amount": null, "purpose": "차입 등"}],
 "guarantees_provided": [{"beneficiary": "피보증인", "creditor": "채권자", "amount": null, "note": ""}],
 "guarantees_received": [{"guarantor": "", "amount": null, "note": ""}],
 "litigation": [{"role": "피고|원고", "desc": "", "amount": null, "status": ""}],
 "other_commitments": ["주요 약정·제재·우발 내용 한 줄씩"],
 "related_party": {"parent": "", "ultimate_controller": "", "receivables": null, "payables": null, "loans_to_related": null, "guarantees_for_related": null},
 "group": {"group_name": "", "listed_affiliates": [""], "unlisted_affiliates_count": null},
 "key_risks": ["심사상 유의 3~5개 (예: 1년 내 만기 집중, 계열사 지급보증 과다, 담보 대부분 제공, 진행 중 대형 소송)"],
 "summary_md": "승인신청서 '차입금·우발채무 현황'에 쓸 5~8줄 서술"
}

주석 원문:
"""


def _parse(out):
    out = out.strip()
    if out.startswith("```"):
        out = out.strip("`").split("\n", 1)[1]
    i, j = out.find("{"), out.rfind("}")
    return json.loads(out[i:j + 1])


def run(corp_codes=None, limit=None, con=None, workers=8, force=False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음 - notes_struct 건너뜀"); return
    import anthropic  # noqa
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    con.execute("""CREATE TABLE IF NOT EXISTS notes_struct (corp_code TEXT PRIMARY KEY, rcept_no TEXT, report_nm TEXT, json TEXT,
                   model TEXT, created_at TEXT)""")
    done = {r[0]: r[1] for r in con.execute("SELECT corp_code, rcept_no FROM notes_struct WHERE model=?", (MODEL,))}
    rows = [dict(r) for r in con.execute("SELECT corp_code, rcept_no, report_nm, notes_z FROM notes_latest WHERE notes_z IS NOT NULL")]
    todo = [r for r in rows if (not corp_codes or r["corp_code"] in corp_codes) and (force or done.get(r["corp_code"]) != r["rcept_no"])]
    try:   # 등급 보유 → 코스피 → 코스닥
        rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
        mk = {r[0]: r[1] for r in con.execute("SELECT corp_code, market FROM company")}
        todo.sort(key=lambda r: (r["corp_code"] not in rated, {"KOSPI": 1, "KOSDAQ": 2}.get(mk.get(r["corp_code"]), 3)))
    except Exception:  # noqa
        pass
    if limit:
        todo = todo[:limit]
    print(f"[notes-struct] 대상 {len(todo)}사 · 모델 {MODEL} · 병렬 {workers}", flush=True)
    client = anthropic.Anthropic()

    def work(r):
        txt = ztext(r["notes_z"]) or ""
        for k in range(4):
            try:
                msg = client.messages.create(model=MODEL, max_tokens=4000, messages=[{"role": "user", "content": PROMPT + txt[:MAX_CHARS]}])
                return r, _parse(msg.content[0].text)
            except Exception as e:  # noqa
                if k == 3:
                    raise
                time.sleep(15 * (k + 1) if "429" in str(e) or "overloaded" in str(e).lower() else 3)

    n = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for i, fut in enumerate([ex.submit(work, r) for r in todo], 1):
            try:
                r, js = fut.result()
                con.execute("INSERT OR REPLACE INTO notes_struct VALUES (?,?,?,?,?,?)",
                            (r["corp_code"], r["rcept_no"], r["report_nm"], json.dumps(js, ensure_ascii=False), MODEL, now()))
                n += 1
            except Exception as e:  # noqa
                fail += 1
                print("구조화 실패", todo[i - 1]["corp_code"], str(e)[:120], flush=True)
            if i % 25 == 0:
                con.commit()
                print(f"[notes-struct] {i}/{len(todo)} · 성공 {n} · 실패 {fail} · {int(time.time() - t0)}초", flush=True)
    con.commit()
    print(f"[notes-struct] 완료 {n}/{len(todo)} · 실패 {fail}", flush=True)
    return n
