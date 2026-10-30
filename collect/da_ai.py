"""상각비 AI 보완(2026-10-30 사용자 승인, 11/8까지 크레딧) — 규칙 파서(notes_v2.backfill)가 주석은 찾았는데 상각비 행을 못 집은 보고서를 Haiku로 읽는다.

대상: fin_kpi에서 영업이익은 있고 상각비가 빈 행(2024년 이후·사이트에 보이는 기간) 중, da_try에 '보고서는 받았으나 실패(ok=0, 접수번호 있음)'로 기록된 것.
절차: 원문 1콜 → 상각 관련 주석 절(유형·무형·사용권·투자부동산·성격별·현금흐름 조정)만 추려 1~1.2만 자 → Haiku가 연결/별도 × 당기(또는 당반기 누적)
      감가상각비·무형자산상각비·사용권자산상각비·투자부동산상각비를 '원' 단위로 환산해 JSON → 합계를 da_notes(src='ai') → build_kpi(상식 점검은 그대로 적용).
비용: 건당 약 1만 토큰 → 600건 약 6달러(Haiku). DA_AI_BUDGET(기본 15달러) 초과 시 중단.
  python run.py da_ai [--limit N] [--codes ...]
"""
import json
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import connect, now  # noqa: E402
from business import _strip, fetch_document_xml  # noqa: E402
import notes_v2  # noqa: E402

MODEL = os.environ.get("DA_AI_MODEL", "claude-sonnet-4-5")   # 2026-10-30 21:48 사용자: 예산 안에서 정확도 우선 → Sonnet(건당 약 1.5센트, Haiku의 3배)
PRICES = {"claude-haiku-4-5": (1.0, 5.0)}
PRICE_IN, PRICE_OUT = PRICES.get(MODEL, (3.0, 15.0))


def _spent(con):
    """da_ai_log 누적 비용 — 행마다 그 행의 모델 단가로(2026-10-31: Haiku 1,440건을 Sonnet 단가로 셈해 $46로 부풀려 예산이 즉시 소진됐던 오류 수정)"""
    tot = 0.0
    for m, tin, tout in con.execute("SELECT model, COALESCE(SUM(tin),0), COALESCE(SUM(tout),0) FROM da_ai_log GROUP BY model"):
        pi, po = PRICES.get(m or "", (3.0, 15.0))
        tot += tin / 1e6 * pi + tout / 1e6 * po
    return tot
PROMPT = """당신은 회계 주석에서 숫자를 뽑는 심사 보조원이다. 아래는 한 회사 정기보고서의 상각비 관련 주석 발췌다(연결·별도 주석이 섞여 있을 수 있고, 각 절 앞에 [연결]/[별도]/[구분없음] 표시).
찾을 것: {기간} 기준 손익에 반영된 ① 감가상각비(유형자산) ② 무형자산상각비 ③ 사용권자산 상각비 ④ 투자부동산 감가상각비.
규칙:
- 표의 '(단위: 천원/백만원)'을 반드시 적용해 '원' 단위 정수로 환산한다. 단위 표시가 없으면 원으로 본다.
- '비용의 성격별 분류' 표가 있으면 그 표를 최우선으로, 없으면 자산 변동표의 당기 상각 행(기초·기말·취득·처분 행이 아님), 없으면 현금흐름표 주석의 조정 항목.
- 전기(전년) 숫자, 상각누계액, 손상차손, 대손상각비는 제외. 분기·반기 보고서면 당기 누적(3개월 아님) 값.
- 확인되지 않으면 null. 추정하지 않는다.
JSON으로만 답하라: {{"CFS": {{"dep": null, "amort": null, "rou": null, "inv": null, "unit_seen": ""}}, "OFS": {{"dep": null, "amort": null, "rou": null, "inv": null, "unit_seen": ""}}}}
연결 주석이 없는 회사면 CFS를 null로 두고 OFS만 채운다. [구분없음] 절만 있으면 OFS에 넣고 CFS는 null.

"""
_KEY = re.compile(r"유형자산|무형자산|사용권|투자부동산|성격별|비용의성격|현금흐름")


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS da_ai_log (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, rcept_no TEXT, ok INTEGER,
                   tin INTEGER, tout INTEGER, model TEXT, at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code))""")


def _excerpt(xml, limit=12000):
    """상각 관련 절만, 표는 행 텍스트로. 절이 하나도 안 잡히면 '감가상각' 문구 주변 창"""
    secs = [s for s in notes_v2.extract_notes_v2(xml) if _KEY.search(s["t"].replace(" ", ""))]
    out, n = [], 0
    for s in secs:
        head = f"\n### [{'연결' if s.get('fs') == 'CFS' else '별도' if s.get('fs') == 'OFS' else '구분없음'}] {s['t']}\n"
        body = []
        for b in s["blocks"]:
            if "tbl" in b:
                u = b["tbl"].get("unit") or 1
                body.append(f"(표 단위 배수 {u})\n" + "\n".join(" | ".join(c for c in r) for r in b["tbl"]["rows"][:45]))
            else:
                body.append(b["p"][:400])
        txt = head + "\n".join(body)
        if n + len(txt) > limit:
            txt = txt[:max(0, limit - n)]
        out.append(txt); n += len(txt)
        if n >= limit:
            break
    if not out:
        plain = _strip(xml)
        last = -1
        for m in re.finditer(r"감가상각|상각비", plain):
            if m.start() < last or len(out) >= 6:
                continue
            out.append("\n### [구분없음] 발췌\n" + plain[max(0, m.start() - 900):m.start() + 900]); last = m.start() + 900
    return "".join(out)[:limit]


def _targets(con, codes=None, limit=None):
    rows = con.execute("""SELECT k.corp_code, k.bsns_year, k.reprt_code, t.rcept_no
                          FROM fin_kpi k JOIN da_try t ON t.corp_code=k.corp_code AND t.bsns_year=k.bsns_year AND t.reprt_code=k.reprt_code
                          WHERE k.da IS NULL AND k.op_income IS NOT NULL AND k.bsns_year >= '2024' AND t.ok=0 AND t.rcept_no<>'' AND t.at >= '2026-10-30 23:30'
                          AND NOT EXISTS (SELECT 1 FROM da_ai_log a WHERE a.corp_code=k.corp_code AND a.bsns_year=k.bsns_year AND a.reprt_code=k.reprt_code)
                          GROUP BY k.corp_code, k.bsns_year, k.reprt_code
                          ORDER BY (k.reprt_code NOT IN ('11011','11012')), k.bsns_year DESC, k.reprt_code""").fetchall()   # 사이트에 보이는 연간·반기 먼저, 분기(LTM용)는 뒤(2026-10-30 19:48)
    out = [tuple(r) for r in rows if not codes or r[0] in codes]
    return out[:limit] if limit else out


def run(con=None, limit=None, codes=None, workers=6):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[da_ai] ANTHROPIC_API_KEY 없음 — 건너뜀"); return 0
    import anthropic  # noqa
    from concurrent.futures import ThreadPoolExecutor
    from financials import build_kpi
    con = con or connect()
    _ensure(con); notes_v2.ensure(con)
    budget = float(os.environ.get("DA_AI_BUDGET", "90"))   # 2026-10-30 16:59 15→40, 10-31 사용자 "예산 올려줄게·정확도 우선" → 90(전 모델 누적 기준)
    spent0 = _spent(con)
    todo = _targets(con, codes, limit)
    print(f"[da_ai] 대상 {len(todo)}건 · 누적 ${spent0:.2f} · 예산 ${budget:.0f} · {MODEL}", flush=True)
    client = anthropic.Anthropic()
    lock = threading.Lock()
    st = {"tin": 0, "tout": 0, "ok": 0, "fail": 0, "none": 0, "stop": False}

    def work(t):
        cc, y, rc, rn = t
        if st["stop"] or common.QUOTA_HIT:
            return t, None, None, 0, 0
        try:
            xml = fetch_document_xml(rn)
        except Exception as e:  # noqa
            if common.QUOTA_HIT or "한도" in str(e):
                return t, None, None, 0, 0
            return t, "", None, 0, 0
        ex = _excerpt(xml)
        if len(ex) < 200:
            return t, "", None, 0, 0
        period = "당기(사업연도)" if rc == "11011" else "당기 누적(분기·반기)"
        for k in range(3):
            try:
                msg = client.messages.create(model=MODEL, max_tokens=400, messages=[{"role": "user", "content": PROMPT.replace("{기간}", period) + ex}])
                out = msg.content[0].text
                i, j = out.find("{"), out.rfind("}")
                return t, rn, json.loads(out[i:j + 1]), msg.usage.input_tokens, msg.usage.output_tokens
            except Exception as e:  # noqa
                if k == 2:
                    return t, "", None, 0, 0
                import time; time.sleep(15 if "429" in str(e) else 3)

    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(todo), 60):
            for t, rn, js, tin, tout in ex.map(work, todo[b:b + 60]):
                cc, y, rc, _ = t
                if rn is None:
                    continue
                ok = 0
                if js:
                    ta = {r[0]: r[1] for r in con.execute("SELECT fs_div, total_assets FROM fin_kpi WHERE corp_code=? AND bsns_year=? AND reprt_code=?", (cc, y, rc))}
                    for fs in ("CFS", "OFS"):
                        v = js.get(fs) or {}
                        parts = [abs(float(v[k])) for k in ("dep", "amort", "rou", "inv") if isinstance(v.get(k), (int, float)) and v[k]]
                        if not parts or fs not in ta:
                            continue
                        da = sum(parts)
                        if ta[fs] and (da > ta[fs] * 0.25 or da < ta[fs] * 0.0002):
                            continue                                  # 단위·오독 의심 → 저장 안 함
                        con.execute("INSERT OR REPLACE INTO da_notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                    (cc, y, rc, fs, (abs(float(v["dep"])) if v.get("dep") else 0) + (abs(float(v["inv"])) if v.get("inv") else 0) or None,
                                     abs(float(v["amort"])) if v.get("amort") else None, abs(float(v["rou"])) if v.get("rou") else None, da, rn, "ai", now()))
                        ok = 1
                    if ok:
                        build_kpi(con, cc, y, rc)
                st["ok" if ok else "none"] += 1
                st["tin"] += tin; st["tout"] += tout
                con.execute("INSERT OR REPLACE INTO da_ai_log VALUES (?,?,?,?,?,?,?,?,datetime('now'))", (cc, y, rc, rn, ok, tin, tout, MODEL))
            con.commit()
            cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
            print(f"[da_ai] {min(b + 60, len(todo))}/{len(todo)} · 확보 {st['ok']} · 못 찾음 {st['none']} · 누적 ${cost:.2f} · DART {common.CALLS}콜", flush=True)
            if cost >= budget:
                print("[da_ai] 예산 도달 — 중단", flush=True); st["stop"] = True; break
            if common.QUOTA_HIT:
                print("[da_ai] DART 한도 — 중단", flush=True); break
    print(f"[da_ai] 완료 · 확보 {st['ok']} · 못 찾음 {st['none']} · 입력 {st['tin']:,} / 출력 {st['tout']:,} 토큰", flush=True)
    return st["ok"]
