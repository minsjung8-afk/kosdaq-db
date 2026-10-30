"""재무 특이사항 AI 보강(Haiku) — 종합 의견 + 항목별 원인(손익계산서 계정 근거)  (2026-10-28 사용자 승인: 전체 비용 100달러 이내)

설계
  ① 규칙 엔진(fin_notes)이 뽑은 특이사항 + 핵심 재무 수치 + 최근 기간 손익계산서 계정 전부(fin_raw, 당기·전년동기)를 Haiku에 준다
  ② 모델은 '제공된 숫자'만 근거로 쓴다 — 원인은 계정명·금액으로 확인되는 것만, 아니면 "제공 자료로 판단 불가"
  ③ 캐시: 특이사항·기준기간이 같으면 다시 부르지 않음(key = sha1) → 이후에는 새 공시가 반영된 회사만 재호출
  ④ 예산: 호출마다 실제 사용 토큰으로 비용 누적, FIN_AI_BUDGET(기본 60달러) 넘으면 중단(Haiku 4.5 입력 $1·출력 $5 / 100만 토큰)
  python run.py fin_ai [--limit N] [--force] [--codes ...]
"""
import hashlib
import json
import os
import threading

from common import connect, now

MODEL = os.environ.get("FIN_AI_MODEL", "claude-haiku-4-5")
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-5": (3.0, 15.0)}   # 달러 / 100만 토큰
PRICE_IN, PRICE_OUT = PRICES.get(MODEL, (3.0, 15.0))
# 2026-10-30 사용자 승인(11/8 크레딧): 상위 종목은 Sonnet — 심사 빈도 높은 순(신용등급 보유 → 비상장 → 자산 큰 순). run(tier=N)
PROMPT_SONNET_EXTRA = """
- 심사 논점 중심으로: 순차입금/EBITDA·이자보상배율 추세, 1년 내 상환 부담 대비 현금·영업현금흐름, 운전자본 변동이 현금흐름을 어떻게 눌렀는지, 분기·반기 누적치는 연환산 관점으로.
- 숫자 간 모순(예: 영업이익 흑자인데 영업현금흐름 큰 적자)이 있으면 그 자체를 지적하고 손익계산서 계정에서 확인되는 범위에서만 원인을 적는다.
"""
PROMPT = """당신은 여전사 기업금융 심사역이다. 아래는 한 회사의 재무 요약, 규칙으로 자동 판정한 재무 특이사항, 최근 기간 손익계산서 계정이다.
규칙:
- 아래에 주어진 숫자만 근거로 쓴다. 외부 지식·업황·뉴스·추측으로 원인을 만들지 않는다.
- 원인은 손익계산서 계정명과 금액(억원)으로 확인될 때만 적는다. 확인 안 되면 "제공 자료로 판단 불가".
- 금액은 억원, 비율은 %로. 과장·수식어 없이 심사보고서 문체(~임, ~함)로.
다음 JSON으로만 답하라:
{"summary": "재무 종합 의견 3~4문장(수익성·레버리지·유동성·현금흐름 중 중요한 순서로)",
 "causes": [{"title": "특이사항 제목 그대로", "cause": "원인 한 줄(근거 계정·금액) 또는 '제공 자료로 판단 불가'"}]}
causes는 특이사항 중 손익 관련 항목(순손실·영업적자·수익성 하락·종속회사 손실 등)에만 쓴다.

"""


def _e(v):
    return "-" if v is None else f"{v / 1e8:,.0f}"


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS fin_ai (corp_code TEXT PRIMARY KEY, key TEXT, summary TEXT, causes_json TEXT,
                   model TEXT, tin INTEGER, tout INTEGER, created TEXT)""")


def _key(notes, kpi_rows):
    last = kpi_rows[-1] if kpi_rows else {}
    s = json.dumps([notes, last.get("label"), last.get("rcept_no")], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _is_lines(con, cc, row, fs):
    """최근 기간 손익계산서 계정: 당기 누적 vs 전년 동기 누적(같은 보고서 종류의 전년 행) — 분기·반기 손익은 누적으로 저장(quarters.py)"""
    if not row:
        return []
    q = """SELECT account_nm, thstrm_amount FROM fin_raw WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? AND sj_div IN ('IS','CIS') ORDER BY sj_div, ord LIMIT 60"""
    prev = {r["account_nm"]: r["thstrm_amount"] for r in con.execute(q, (cc, str(int(row["bsns_year"]) - 1), row["reprt_code"], fs))}
    out = []
    for r in con.execute(q, (cc, row["bsns_year"], row["reprt_code"], fs)):
        cur, prv = r["thstrm_amount"], prev.get(r["account_nm"])
        if cur is None and prv is None:
            continue
        out.append(f"{r['account_nm']}: 당기 {_e(cur)} / 전년 동기 {_e(prv)}")
    return out


def _payload(con, c, d):
    kpi = d.get("kpi") or {}
    fs = "CFS" if kpi.get("CFS") else "OFS"
    rows = kpi.get(fs) or []
    F = [("매출", "revenue"), ("영업이익", "op_income"), ("순이익", "net_income"), ("EBITDA", "ebitda"), ("자산", "total_assets"), ("부채", "total_liab"),
         ("자본", "equity"), ("현금", "cash"), ("총차입금", "borrowings"), ("순차입금", "net_borrowings"), ("영업CF", "cfo"), ("FCF", "fcf")]
    tbl = ["기간 | " + " | ".join(n for n, _ in F) + " | 부채비율 | 유동비율"]
    for r in rows:
        tbl.append(f"{r.get('label')}{'(누적)' if r.get('kind') != 'annual' else ''} | " + " | ".join(_e(r.get(k)) for _, k in F)
                   + f" | {r.get('debt_ratio') or '-'}% | {r.get('cur_ratio') or '-'}%")
    last = next((r for r in rows if r.get("kind") == "interim"), rows[-1] if rows else None)
    lines = _is_lines(con, c["corp_code"], last, fs)
    biz = ((d.get("business") or {}).get("summary") or {}).get("industry") or ""
    notes = "\n".join(f"- [{n['sev']}] {n['title']}: {n['text']}" for n in d.get("fin_notes") or [])
    return (f"회사: {c['corp_name']} ({c.get('market')}) · 업종: {biz} · 기준: {'연결' if fs == 'CFS' else '별도'} · 단위 억원\n\n[재무 요약]\n" + "\n".join(tbl)
            + f"\n\n[재무 특이사항(규칙 판정)]\n{notes or '- 없음'}\n\n[손익계산서 {last.get('label') if last else ''} 계정(당기/전기, 누적)]\n" + "\n".join(lines[:60]))


def _tier_order(con, n):
    """Sonnet 대상 상위 n사: 신용등급 보유사 → 비상장 → 최근 연간 자산 큰 순"""
    rows = con.execute("""SELECT c.corp_code,
                            CASE WHEN EXISTS(SELECT 1 FROM credit_rating r WHERE r.corp_code=c.corp_code) THEN 0 WHEN c.market='UNLISTED' THEN 1 ELSE 2 END AS pri,
                            COALESCE((SELECT MAX(total_assets) FROM fin_kpi k WHERE k.corp_code=c.corp_code AND k.reprt_code='11011'), 0) AS ta
                          FROM company c ORDER BY pri, ta DESC""").fetchall()
    return [r[0] for r in rows[:n]]


def run(corp_codes=None, limit=None, force=False, workers=8, tier=None, model=None, budget=None):
    global MODEL, PRICE_IN, PRICE_OUT
    if model:
        MODEL = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-5"}.get(model, model)
        PRICE_IN, PRICE_OUT = PRICES.get(MODEL, (3.0, 15.0))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[fin_ai] ANTHROPIC_API_KEY 없음 — 건너뜀"); return
    import anthropic  # noqa
    from concurrent.futures import ThreadPoolExecutor
    import build_site
    import fin_notes
    budget = float(budget or os.environ.get("FIN_AI_BUDGET", "60"))
    con = connect()
    _ensure(con)
    have = {r["corp_code"]: r["key"] for r in con.execute("SELECT corp_code, key FROM fin_ai")}
    used = con.execute("SELECT COALESCE(SUM(tin),0), COALESCE(SUM(tout),0) FROM fin_ai WHERE model=?", (MODEL,)).fetchone()   # 예산은 모델별 누적
    spent0 = used[0] / 1e6 * PRICE_IN + used[1] / 1e6 * PRICE_OUT
    todo = []
    if tier:
        corp_codes = set(_tier_order(con, tier))
        force = True                                   # 상위 종목은 모델을 올려 다시 만든다(같은 모델로 이미 있으면 아래에서 건너뜀)
    have_model = {r["corp_code"]: r["model"] for r in con.execute("SELECT corp_code, model FROM fin_ai")}
    for c in [dict(r) for r in con.execute("SELECT * FROM company ORDER BY corp_name")]:
        if corp_codes and c["corp_code"] not in corp_codes and c["stock_code"] not in corp_codes:
            continue
        if tier and have_model.get(c["corp_code"]) == MODEL and have.get(c["corp_code"]):
            continue                                   # 이미 이 모델로 만든 것은 그대로(키가 바뀐 건 일반 경로가 갱신)
        d = build_site.company_detail(con, c)
        if not (d.get("kpi") or {}):
            continue
        fin_notes.fix_ltm(d["kpi"])
        d["fin_notes"] = fin_notes.build(d, c)
        rows = d["kpi"].get("CFS") or d["kpi"].get("OFS") or []
        k = _key(d["fin_notes"], rows)
        if not force and have.get(c["corp_code"]) == k:
            continue
        todo.append((c, k, _payload(con, c, d)))
        if limit and len(todo) >= limit:
            break
    print(f"[fin_ai] 대상 {len(todo)}사 · 누적 사용 ${spent0:.2f} · 예산 ${budget:.0f} · 모델 {MODEL}", flush=True)
    client = anthropic.Anthropic()
    lock = threading.Lock()
    st = {"tin": 0, "tout": 0, "ok": 0, "fail": 0, "stop": False}
    res = []

    def work(item):
        c, k, text = item
        with lock:
            if st["stop"]:
                return
        try:
            msg = client.messages.create(model=MODEL, max_tokens=1100, messages=[{"role": "user", "content": PROMPT + (PROMPT_SONNET_EXTRA if "sonnet" in MODEL else "") + text}])
            out = msg.content[0].text.strip()
            i, j = out.find("{"), out.rfind("}")
            js = json.loads(out[i:j + 1])
            with lock:
                st["tin"] += msg.usage.input_tokens; st["tout"] += msg.usage.output_tokens; st["ok"] += 1
                res.append((c["corp_code"], k, js.get("summary"), json.dumps(js.get("causes") or [], ensure_ascii=False), MODEL,
                            msg.usage.input_tokens, msg.usage.output_tokens, now()))
                cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
                if cost >= budget:
                    st["stop"] = True
                if st["ok"] % 100 == 0:
                    print(f"[fin_ai] {st['ok']}/{len(todo)} · 누적 ${cost:.2f}", flush=True)
        except Exception as e:  # noqa
            with lock:
                st["fail"] += 1
                if st["fail"] <= 5:
                    print(f"[fin_ai] 실패 {c['corp_name']}: {str(e)[:120]}", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        for i in range(0, len(todo), 200):            # 200사마다 저장(타임아웃 대비)
            list(ex.map(work, todo[i:i + 200]))
            with lock:
                batch, res[:] = list(res), []
            con.executemany("INSERT OR REPLACE INTO fin_ai VALUES (?,?,?,?,?,?,?,?)", batch)
            con.commit()
            if st["stop"]:
                print("[fin_ai] 예산 도달 — 중단", flush=True)
                break
    cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
    print(f"[fin_ai] 완료 {st['ok']} · 실패 {st['fail']} · 이번 입력 {st['tin']:,} / 출력 {st['tout']:,} 토큰 · 누적 ${cost:.2f}", flush=True)


def view(con, corp_code, notes, kpi_rows):
    """사이트용: 현재 특이사항과 같은 키로 만든 AI 결과만 반환(숫자가 바뀌었으면 숨김)"""
    try:
        r = con.execute("SELECT key, summary, causes_json, model, created FROM fin_ai WHERE corp_code=?", (corp_code,)).fetchone()
    except Exception:  # noqa
        return None
    if not r or r["key"] != _key(notes, kpi_rows):
        return None
    return {"summary": r["summary"], "causes": json.loads(r["causes_json"] or "[]"), "model": r["model"], "at": r["created"]}
