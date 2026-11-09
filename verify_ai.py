"""의심 숫자 AI 원문 판정(Sonnet) — 2026-11-09 사용자 승인: '1번(의심 숫자 원문 대조)부터, 예산 50달러 이내'

설계
  ① 대상은 규칙으로 이미 걸러 둔 의심 행만: 사이트 상각비가 주석 '비용의 성격별 분류/현금흐름 조정' 직접값보다 1% 넘게 큰 행
     (보정 규칙은 '작으면 주석값으로'만 하고 큰 쪽은 그대로 뒀음 — 영업외 상각인지 오독인지 판단 불가였던 행)
  ② 보고서 원문(DART 1콜)에서 현금흐름표 본문의 상각 줄 + 주석(현금흐름·성격별·유형/무형/사용권/투자부동산 변동표)의 상각 행만 추려 Sonnet에 준다
  ③ 모델은 주어진 원문 숫자만으로 그 기간 누적 상각비 합계(유형 감가 + 사용권 + 무형 + 투자부동산)와 근거 문구를 JSON으로 낸다
  ④ 결과는 ai_verify 표에만 기록 — 사이트 값은 자동으로 바꾸지 않음(표본 검토 후 반영 여부를 사용자와 정함)
  ⑤ 예산: 호출마다 실제 토큰으로 비용 누적, VERIFY_AI_BUDGET(기본 50달러, 전 실행 누적) 넘으면 시작/계속하지 않음
"""
import json
import os
import re
import threading

from common import connect, now

MODEL = os.environ.get("VERIFY_AI_MODEL", "claude-sonnet-4-5")
PRICE_IN, PRICE_OUT = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-5": (3.0, 15.0)}.get(MODEL, (3.0, 15.0))
BUDGET = float(os.environ.get("VERIFY_AI_BUDGET", "50"))

PROMPT = """당신은 회계 감사인이다. 아래는 한 회사 {period} {fs} 재무제표의 원문 발췌(현금흐름표 본문의 상각 관련 줄, 주석의 상각 관련 표 행)다.
이 기간 누적(분기·반기는 '누적' 열, 연간은 당기 열) 상각비 합계 = 유형자산 감가상각비 + 사용권자산 상각비 + 무형자산 상각비 + 투자부동산 감가상각비 를 구하라.
규칙:
- 주어진 원문 숫자만 쓴다. 추측·외부 지식 금지. 전기(비교기간) 열·3개월 열을 쓰지 않는다.
- 같은 금액이 여러 표에 중복될 수 있다(예: 사용권자산이 유형자산 변동표의 한 종류로 이미 포함). 이중으로 더하지 않는다.
- 대손상각비·손상차손·처분손실·사채할인발행차금 상각·이자비용은 제외.
- 표의 단위(원·천원·백만원)를 확인해 원 단위로 환산한다.
- 참고로 사이트 값 A={a}원, 주석 성격별/현금흐름 표 값 B={b}원이다. 둘 중 하나에 맞추려 하지 말고 원문으로 독립적으로 계산하라.
다음 JSON으로만 답하라:
{{"da_won": 숫자 또는 null, "components": {{"유형": 숫자|null, "사용권": 숫자|null, "무형": 숫자|null, "투자부동산": 숫자|null}},
 "source": "현금흐름표 본문|현금흐름 주석|성격별 주석|변동표", "evidence": "근거가 된 원문 행을 그대로 1~3줄",
 "verdict": "A|B|둘다아님|판단불가", "reason": "한 문장"}}

원문 발췌:
"""

_KEEP = re.compile(r"상각|감가")
_DROP = re.compile(r"대손|손상|처분|이자|사채|할인|누계액")


def _render(secs, fs, xml_text):
    """원문에서 상각 관련 줄만 — 현금흐름표 본문(재무제표 절) + 주석 표(fs 일치)"""
    from business import _strip
    out = []
    # 현금흐름표 본문: 제목에 '현금흐름표'가 있고 '주석'은 없는 절
    for m in re.finditer(r"<SECTION-(\d)[^>]*>(.*?)</SECTION-\1>", xml_text, flags=re.S | re.I):
        sec = m.group(2)
        t = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", sec, flags=re.S | re.I)
        tt = _strip(t.group(1)).replace(" ", "") if t else ""
        if "현금흐름표" not in tt or "주석" in tt or (("연결" in tt) != (fs == "CFS")):
            continue
        body = _strip(sec)
        lines = body.splitlines()
        head = [ln for ln in lines[:12] if re.search(r"단위|기\s|당|전|\d{4}", ln)][:4]
        keep = [ln for ln in lines if _KEEP.search(ln) and not _DROP.search(ln) and re.search(r"\d", ln)]
        if keep:
            out.append(f"[{_strip(t.group(1))}]\n" + "\n".join(head + keep[:20]))
    for s in secs:
        if s.get("fs") != fs:
            continue
        tt = s.get("t", "").replace(" ", "")
        if not re.search(r"현금흐름|성격|유형자산|무형자산|사용권|리스|투자부동산", tt):
            continue
        for b in s.get("blocks", []):
            tb = b.get("tbl")
            if not tb:
                continue
            rows = tb.get("rows", [])
            hit = [r for r in rows if r and _KEEP.search(str(r[0])) and not _DROP.search(str(r[0]))]
            if not hit:
                continue
            hdr = [r for r in rows[:4] if not any(re.search(r"\d{2,}", str(c)) for c in r[1:])][:3]
            unit = tb.get("unit") or 1
            out.append(f"[{s.get('t')}] (표 단위 배수 {unit}원)\n" + "\n".join(" | ".join(str(c) for c in r) for r in hdr + hit[:8]))
    txt = "\n\n".join(out)
    return txt[:30000]


def dist(con):
    """대상 분포(비용 0) — 연도·보고서·차이 폭별"""
    import collections
    c = collections.Counter()
    for r in targets(con):
        g = r[4] / r[5] - 1
        c[(r[1], {"11011": "연간", "11012": "반기", "11013": "1Q", "11014": "3Q"}.get(r[2], r[2]),
           "1~5%" if g <= 0.05 else "5~20%" if g <= 0.2 else "20~100%" if g <= 1 else "2배+")] += 1
    for k in sorted(c, reverse=True):
        print(f"  [분포] {k[0]} {k[1]} {k[2]}: {c[k]}", flush=True)
    return c


def targets(con, limit=None, min_gap=0.01, since=None, kinds=None):
    """사이트 상각비가 주석 직접값보다 1% 넘게 큰 행(아직 판정 안 한 것) — 최근 연도·연간/반기 먼저"""
    con.execute("""CREATE TABLE IF NOT EXISTS ai_verify (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, field TEXT,
                   site_val REAL, alt_val REAL, ai_val REAL, verdict TEXT, evidence TEXT, reason TEXT, comp TEXT, rcept_no TEXT,
                   model TEXT, tin INTEGER, tout INTEGER, at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, field))""")
    rows = con.execute("""SELECT k.corp_code, k.bsns_year, k.reprt_code, k.fs_div, k.da, d.da, COALESCE(d.rcept_no, k.rcept_no), k.da_src
                          FROM fin_kpi k JOIN da_direct d USING(corp_code, bsns_year, reprt_code, fs_div)
                          WHERE k.da > d.da * 1.01 AND d.da > 0
                            AND NOT EXISTS (SELECT 1 FROM ai_verify v WHERE v.corp_code=k.corp_code AND v.bsns_year=k.bsns_year
                                            AND v.reprt_code=k.reprt_code AND v.fs_div=k.fs_div AND v.field='da')
                          ORDER BY k.bsns_year DESC, CASE k.reprt_code WHEN '11011' THEN 0 WHEN '11012' THEN 1 ELSE 2 END, k.corp_code""").fetchall()
    rows = [r for r in rows if r[6] and ":" not in r[6] and r[4] > r[5] * (1 + min_gap)
            and (not since or r[1] >= since) and (not kinds or r[2] in kinds)]
    return rows[:limit] if limit else rows


def _spent(con):
    r = con.execute("SELECT COALESCE(SUM(tin),0), COALESCE(SUM(tout),0) FROM ai_verify").fetchone()
    return r[0] / 1e6 * PRICE_IN + r[1] / 1e6 * PRICE_OUT


def run(con=None, limit=25, workers=4, min_gap=0.05, since="2025", kinds=("11011", "11012", "11014")):
    import anthropic
    from concurrent.futures import ThreadPoolExecutor
    from business import fetch_document_xml
    from notes_v2 import extract_notes_v2
    from summarize import _parse_json
    con = con or connect()
    todo = targets(con, limit, min_gap, since, kinds)   # 2026-11-09: 미판정 3,441건 전부는 예산 초과 → 최근(2025~)·연간/반기/3Q·차이 5% 넘는 행만(1~5%는 영업외 상각 가능성 — SGC에너지처럼 원문과 맞는 경우)
    spent0 = _spent(con)
    print(f"[verify_ai] 대상 {len(todo)}건(조건 충족 {len(targets(con, None, min_gap, since, kinds))} · 전체 미판정 {len(targets(con))}) · 누적 ${spent0:.2f} · 예산 ${BUDGET:.0f} · {MODEL}", flush=True)
    if spent0 >= BUDGET:
        print("[verify_ai] 예산 소진 — 실행 안 함", flush=True)
        return []
    client = anthropic.Anthropic()
    lock = threading.Lock()
    st = {"tin": 0, "tout": 0, "stop": False}
    docs = {}

    def one(r):
        cc, y, rc, fs, site, alt, rn, src = r
        if st["stop"]:
            return None
        with lock:
            xml = docs.get(rn)
        if xml is None:
            xml = fetch_document_xml(rn)
            with lock:
                docs[rn] = xml
        ctx = _render(extract_notes_v2(xml), fs, xml)
        if len(ctx) < 50:
            return (r, None, "원문 발췌 없음", 0, 0)
        period = f"{y}년 " + {"11011": "연간", "11012": "반기(1~6월 누적)", "11013": "1분기", "11014": "3분기(1~9월 누적)"}.get(rc, rc)
        msg = client.messages.create(model=MODEL, max_tokens=900, messages=[{"role": "user", "content":
              PROMPT.format(period=period, fs="연결" if fs == "CFS" else "별도", a=int(site), b=int(alt)) + ctx}])
        out = "".join(getattr(b, "text", "") for b in msg.content)
        try:
            js = _parse_json(out)
        except Exception:  # noqa
            js = None
        return (r, js, out[:300], msg.usage.input_tokens, msg.usage.output_tokens)

    res = []
    with ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(one, r) for r in todo]:
            try:
                x = f.result()
            except Exception as e:  # noqa
                print(f"[verify_ai] 실패: {e}", flush=True)
                continue
            if not x:
                continue
            r, js, raw, tin, tout = x
            cc, y, rc, fs, site, alt, rn, src = r
            with lock:
                st["tin"] += tin; st["tout"] += tout
                cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
                if cost >= BUDGET:
                    st["stop"] = True
            aiv = (js or {}).get("da_won")
            try:
                aiv = float(aiv) if aiv is not None else None
            except (TypeError, ValueError):
                aiv = None
            con.execute("INSERT OR REPLACE INTO ai_verify VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cc, y, rc, fs, "da", site, alt, aiv, (js or {}).get("verdict") or "응답오류", (js or {}).get("evidence") or raw,
                         (js or {}).get("reason"), json.dumps((js or {}).get("components"), ensure_ascii=False), rn, MODEL, tin, tout, now()))
            con.commit()
            res.append((r, aiv, js))
    cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
    print(f"[verify_ai] 완료 {len(res)}건 · 이번 입력 {st['tin']:,} / 출력 {st['tout']:,} 토큰 · 이번 ${cost - spent0:.2f} · 누적 ${cost:.2f}", flush=True)
    return res


def report(con, res):
    """판정 요약 — AI 값이 A(사이트)·B(주석 직접값)·둘 다 아님 중 어디에 맞는지(±0.5%)"""
    import collections
    nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    c = collections.Counter()
    for r, aiv, js in res:
        site, alt = r[4], r[5]
        k = ("AI값없음" if not aiv else "A(사이트) 일치" if abs(aiv - site) <= site * 0.005 else
             "B(주석) 일치" if abs(aiv - alt) <= alt * 0.005 else "둘다아님")
        c[k] += 1
        print(f"  [{k}] {nm.get(r[0], r[0])} {r[1]} {r[2]} {r[3]}: 사이트 {site/1e8:,.1f}억[{r[7]}] · 주석 {alt/1e8:,.1f}억 · AI {aiv and round(aiv/1e8,1)}억"
              f" · {(js or {}).get('source')} · {((js or {}).get('evidence') or '')[:160]!r}", flush=True)
    print(f"[verify_ai] 판정 분포: {dict(c)}", flush=True)
