"""당사 양식 재무현황 각주 — 변동 사유 AI 판독(2026-11-19 사용자 승인 ②, 예산 FN_AI_BUDGET 기본 35달러).

대상: 당사 양식(build_site._ibk_fin)에서 전년 대비 큰 변동(±30% 이면서 그룹 합계의 5% 이상)인 세부 계정 — 회사당 최대 6건.
근거: 해당 기간 보고서의 주석(연간 쌍은 사업보고서, 분기 쌍은 최신 분기보고서 = notes_latest) 중 계정 관련 절만 발췌(절당 1,500자).
규칙(할루시네이션 방지): 모델이 '사유'와 함께 주석에서 **그대로 베낀 문장(quote)**을 내야 하고, 그 문장이 발췌문에 실제로 있어야만 저장. 없으면 각주 없음.
비용: 회사당 1콜, 입력 5~8천 토큰(Haiku 4.5 $1/M) → 약 0.7~1센트 · 3,500사 ≈ 25~35달러.
  python run.py fn_ai [--limit N] [--codes ...]
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
from common import connect, now, ztext  # noqa: E402
from business import fetch_document_xml  # noqa: E402
import notes_v2  # noqa: E402

MODEL = os.environ.get("FN_AI_MODEL", "claude-haiku-4-5")
VER = "v2"               # 2026-11-19 표본 20사(v1) 점검: 저장 20건 중 틀리거나 애매한 것이 절반(단위 환산 오류 3건 — 천원 표를 억으로 잘못 환산, 방향 모순 2건, 다른 계정 표 인용 1건, 현금 항목 0/11 유효)
                         #   → v2: 금액 환산 금지 + 숫자 대조, 변동 방향 대조, 차입·리스 절 분리, 현금 항목 제외. 저장 행의 model에 '|v2'를 붙여 사이트는 v2만 표시(build_site.FN_AI_VER)
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-5": (3.0, 15.0)}
BUDGET = float(os.environ.get("FN_AI_BUDGET", "35"))
MAX_ITEMS = 4            # 회사당 변동 항목 수(금액 큰 순) — 예산 35달러 안에서 3,500사를 돌리려면 4건 × 2절 × 1,200자 ≈ 입력 8천 토큰
EXCERPT = 1200
# 주석 절 수집 키(2026-11-19): 기본 수집(notes_v2.NOTE_KEYS_V2 = 차입·우발·리스·유무형 등)에는 현금·매출채권·재고·관계기업 절이 없어 사유를 못 찾는다 → 각주용으로 넓힌다
WIDE_KEYS = tuple(notes_v2.NOTE_KEYS_V2) + ("현금", "매출채권", "기타채권", "수취채권", "재고", "관계기업", "종속기업", "공동기업", "금융자산", "금융상품", "공정가치", "법인세",
                                            "충당", "퇴직", "확정급여", "종업원급여", "계약자산", "계약부채", "영업권", "선수", "기타자산", "기타부채", "기타유동", "기타비유동", "자본금", "미지급", "예수")

# 계정명 → 관련 주석 절 제목 패턴
_SEC = [
    (r"리스", r"리스|사용권"),                          # v2: 차입·사채와 분리(DRB동일: 장기차입금 항목에 사용권자산 표가 근거로 붙음)
    (r"차입|사채|CB|BW", r"차입|사채|금융부채|전환사채|신주인수권"),
    (r"매출채권|채권|계약자산|미청구", r"매출채권|채권|계약자산|미청구|손실충당"),
    (r"재고", r"재고"),
    (r"매입채무|채무|계약부채|선수", r"매입채무|채무|계약부채|선수"),
    (r"유형자산|건설중", r"유형자산"),
    (r"투자부동산", r"투자부동산"),
    (r"무형자산|영업권", r"무형자산|영업권"),
    (r"사용권", r"리스|사용권"),
    (r"관계기업|종속기업|공동기업|지분증권|투자주식", r"관계기업|종속기업|공동기업|지분|투자"),
    (r"금융자산|금융상품|파생", r"금융자산|금융상품|파생|공정가치"),
    (r"이연법인세", r"법인세"),
    (r"충당부채|확정급여", r"충당|퇴직|확정급여"),
    (r"기타", r"기타"),
]

PROMPT = """당신은 여신심사역을 돕는 회계 보조원이다. 아래는 한 회사의 재무상태표 세부 계정 중 전년 대비 크게 변동한 항목과, 그 보고서 주석에서 관련 절을 발췌한 것이다.
각 항목에 대해 **주석 발췌문에 적힌 내용만으로** 변동 사유를 한 줄(40자 이내)로 쓰고, 근거가 된 문장 또는 표의 행 하나를 발췌문에서 **글자 그대로 복사**해 quote에 넣어라.
규칙:
- 발췌문에 사유(신규 차입·상환·차환, 취득·처분, 발행·전환, 손상, 거래처·대손, 계약 변동, 합병·연결범위 변동, 유상증자, 배당 등)가 드러나지 않으면 reason=null, quote=null. 추정·일반론 금지.
- 숫자의 단순 증감 서술("증가했다")은 사유가 아니다. 구성 변화(예: 산업은행 운영자금 차입 300억 신규)처럼 무엇이 바뀌었는지가 보여야 한다.
- quote는 발췌문의 한 줄을 그대로(앞뒤 생략 가능, 최대 160자). 요약·의역 금지. 발췌문에 없는 문장을 만들면 안 된다.
- 금액을 환산하지 마라(천원·백만원 표의 숫자를 억 단위로 바꾸지 말 것). reason에는 되도록 숫자를 쓰지 말고, 꼭 필요하면 quote에 있는 숫자를 글자 그대로만 쓴다.
- 변동 방향과 맞아야 한다. 증가한 항목에 '상환·처분·감소'만, 감소한 항목에 '신규·취득·증가'만 쓰면 안 된다.
- 항목과 다른 계정의 표는 근거가 아니다(예: 차입금 항목에 사용권자산·리스 표, 매출채권 항목에 선수금 표).
- 표의 두 숫자를 비교해 짐작한 인과(만기 연장·재분류·대체 등)는 쓰지 말 것. 주석 문장이 직접 말하거나, 표의 행 이름이 그 사실(차입처·회차·취득·처분·합병 등)을 나타낼 때만 쓴다.
JSON 배열로만 답하라: [{{"i": 항목번호, "reason": "…" | null, "quote": "…" | null}}, …]

"""


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS fn_ai (corp_code TEXT, fs TEXT, nm TEXT, label TEXT, reason TEXT, quote TEXT, rcept_no TEXT,
                   model TEXT, at TEXT, PRIMARY KEY (corp_code, fs, nm, label))""")
    con.execute("""CREATE TABLE IF NOT EXISTS fn_ai_log (corp_code TEXT PRIMARY KEY, n_items INTEGER, n_found INTEGER, tin INTEGER, tout INTEGER,
                   model TEXT, at TEXT, key TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fn_ai_src (rcept_no TEXT PRIMARY KEY, secs_z BLOB, fetched_at TEXT)""")   # 사업보고서 주석(절별) 캐시
    con.commit()


def _spent(con):
    tot = 0.0
    for m, tin, tout in con.execute("SELECT model, COALESCE(SUM(tin),0), COALESCE(SUM(tout),0) FROM fn_ai_log GROUP BY model"):
        pi, po = PRICES.get(m or "", (3.0, 15.0))
        tot += tin / 1e6 * pi + tout / 1e6 * po
    return tot


def _changes(X):
    """build_site._ibk_fin 결과 한 기준(fs)에서 큰 변동 항목 [(nm, a_idx, b_idx, p, c, r)] — 화면(ibkNotes)과 같은 규칙"""
    C, G = X["cols"], X["groups"]
    ann = [i for i, c in enumerate(C) if c["kind"] == "annual"]
    q = [i for i, c in enumerate(C) if c["kind"] != "annual"]
    pairs = [(ann[k - 1], ann[k]) for k in range(1, len(ann))] + ([(q[-2], q[-1])] if len(q) >= 2 else [])
    out = []
    for g, gg in G.items():
        T = gg.get("total") or []
        for it in gg["items"]:
            for a, b in pairs:
                p, c = it["v"][a], it["v"][b]
                if p in (None, 0) or c is None:
                    continue
                d = c - p; r = d / abs(p); base = (T[b] if b < len(T) else None) or (T[a] if a < len(T) else None)
                if abs(r) < 0.3 or (base and abs(d) < 0.05 * abs(base)):
                    continue
                out.append((it["nm"], a, b, p, c, r))
    out = [x for x in out if not re.search(r"현금", x[0])]      # v2: 현금 항목은 AI에 묻지 않는다(표본 11건 중 유효 0 — 현금 증감은 화면의 현금흐름 구성 근거가 더 정확)
    out.sort(key=lambda x: -abs(x[4] - x[3]))
    return out[:MAX_ITEMS]


def _secs_for_report(con, rcept_no, cc):
    """보고서 주석 절(v2, WIDE_KEYS). 보고서 원문 1콜 → fn_ai_src에 캐시(같은 보고서는 다시 받지 않음).
    notes_latest 저장본은 좁은 키로 걸러져 있어 쓰지 않는다."""
    r = con.execute("SELECT secs_z FROM fn_ai_src WHERE rcept_no=?", (rcept_no,)).fetchone()
    if r and r[0]:
        try:
            return json.loads(ztext(r[0]) or "[]")
        except Exception:  # noqa
            pass
    xml = fetch_document_xml(rcept_no)
    secs = notes_v2.extract_notes_v2(xml, keys=WIDE_KEYS, max_sec=220)
    con.execute("INSERT OR REPLACE INTO fn_ai_src VALUES (?,?,?)", (rcept_no, common.zpack(json.dumps(secs, ensure_ascii=False)), now()))
    con.commit()
    return secs


def _excerpt(secs, nm, fs):
    """계정 관련 주석 절 2개까지 발췌. 제목에 계정의 핵심어가 들어간 절을 먼저, 절 안에서는 문단(서술)을 먼저 싣고 남는 분량에 표 행을 싣는다
    (변동 사유는 대개 문단이나 표의 '취득·처분·차입·상환' 행에 있다 — 표 머리만 잘려 들어가는 것을 줄임)."""
    pat = next((s for k, s in _SEC if re.search(k, nm)), None)
    if not pat:
        return ""
    rx = re.compile(pat)
    core = re.sub(r"^(유동성|비유동|유동|단기|장기|기타)+", "", nm)
    core = re.sub(r"(및.*|\(.*)$", "", core)[:6]
    def title(s):
        return s["t"].replace(" ", "")
    cand = [s for s in secs if rx.search(title(s)) and (s.get("fs") in (fs, None, ""))] or [s for s in secs if rx.search(title(s))]
    cand.sort(key=lambda s: (0 if (core and core in title(s)) else 1, len(title(s))))
    out = []
    for s in cand[:2]:
        ps = [b["p"] for b in s["blocks"] if "p" in b]
        rows = [" | ".join(r) for b in s["blocks"] if "tbl" in b for r in b["tbl"]["rows"]]
        txt = f"### {s['t']}\n" + "\n".join(ps)[: EXCERPT * 2 // 3]
        txt += "\n" + "\n".join(rows)
        out.append(txt[:EXCERPT])
    return "\n".join(out)


def _rcept_for_pair(con, cc, kpi_rows, b):
    """변동 쌍의 '당기'(b) 기간 보고서 접수번호"""
    r = kpi_rows[b]
    return (r.get("rcept_no") or "").split(":")[0]      # '…:전기'(다음 보고서의 전기 열로 채운 행) → 그 보고서 번호


def _norm(s):
    return re.sub(r"\s+", "", s or "")


_UP = re.compile(r"신규|증가|발행|취득|확대|추가|인수|유입|편입|증자|조달|합병")
_DOWN = re.compile(r"상환|감소|처분|매각|소멸|손상|축소|유출|제외|감자|회수|해지|제거")      # 환입·상각·대손은 계정에 따라 방향이 달라 제외(충당금 환입 → 채권 순액 증가)
_PAREN_NUM = re.compile(r"\s*\([^()]*\d[^()]*\)")


def _nums_ok(reason, ex):
    """reason의 숫자가 발췌문에 '글자 그대로' 있는지(쉼표 무시, 더 긴 숫자의 일부로 맞는 것은 불인정: 335억 vs 335,529,483). 억·조 단위 금액은 발췌문에 같은 표기가 있어야 한다"""
    exn = re.sub(r"(?<=\d),(?=\d)", "", ex or "")
    exs = _norm(exn)
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", reason or ""):
        n = m.group(0).replace(",", "")
        tail = _norm((reason or "")[m.end():])[:2]
        if tail[:1] in ("억", "조") or tail[:2] in ("천억", "백억", "십억", "천만", "백만"):
            if (n + tail[:1]) not in exs:
                return False
            continue
        if len(re.sub(r"\D", "", n)) < 2:
            continue                                    # 한 자리 숫자(회차 가지번호 등)는 대조하지 않음
        if not re.search(r"(?<![\d.])" + re.escape(n) + r"(?!\d)", exn):
            return False
    return True


def _check(reason, ex, r):
    """v2 결정적 검증 → (통과한 reason | None, 탈락 사유). ① 숫자 대조(괄호 속 숫자는 떼어 내고 한 번 더) ② 변동 방향과 사유의 방향어"""
    if not _nums_ok(reason, ex):
        cut = _PAREN_NUM.sub("", reason).strip()
        if not cut or cut == reason or not _nums_ok(cut, ex):
            return None, "숫자 불일치(환산·발췌문에 없는 숫자)"
        reason = cut
    up, down = bool(_UP.search(reason)), bool(_DOWN.search(reason))
    if r > 0 and down and not up:
        return None, "방향 불일치(증가 항목에 감소 사유)"
    if r < 0 and up and not down:
        return None, "방향 불일치(감소 항목에 증가 사유)"
    return reason, ""


def run(con=None, limit=None, codes=None, dry=False):
    import build_site  # noqa — 사이트 빌드와 같은 규칙으로 변동 항목 산출
    if not dry and os.environ.get("FN_AI_ENABLED") != "1":     # 2026-11-19 사용자 '각주 관련 상세 주석은 일단 반영하지 말고': 표본 정밀도(20건 중 유효 9건)가 낮아 보류 — 켤 때만 실행
        print("[fn_ai] 보류 중 — 실행하지 않음(사용자 결정 2026-11-19). 다시 켜려면 FN_AI_ENABLED=1", flush=True)
        return 0
    con = con or connect()
    _ensure(con)
    spent = _spent(con)
    print(f"[fn_ai] 모델 {MODEL} · 누적 비용 ${spent:.2f} / 예산 ${BUDGET}", flush=True)
    if spent >= BUDGET:
        print("[fn_ai] 예산 소진 — 중단", flush=True); return 0
    cos = [dict(r) for r in con.execute("SELECT * FROM company ORDER BY CASE market WHEN 'KOSPI' THEN 0 WHEN 'KOSDAQ' THEN 1 WHEN 'KONEX' THEN 2 ELSE 3 END, corp_name")]
    if codes:
        cos = [c for c in cos if c["stock_code"] in codes or c["corp_code"] in codes]
    done = {r[0]: r[1] for r in con.execute("SELECT corp_code, key FROM fn_ai_log")}
    import anthropic  # noqa
    client = anthropic.Anthropic()
    n_co = n_items = n_found = 0
    for c in cos:
        if limit and n_co >= limit:
            break
        cc = c["corp_code"]
        kpi = build_site.kpi_periods(con, c)
        if not kpi:
            continue
        ib = build_site._ibk_fin(con, cc, kpi)
        if not ib:
            continue
        fs = "CFS" if "CFS" in ib else "OFS"
        X = ib[fs]
        ch = _changes(X)
        key = VER + "|" + fs + "|" + "|".join(f"{nm}:{X['cols'][b]['label']}" for nm, a, b, *_ in ch)
        if done.get(cc) == key:
            continue                                    # 같은 변동 세트는 다시 묻지 않음(새 보고서가 들어오면 key가 바뀜)
        if not ch:
            prev = con.execute("SELECT tin, tout FROM fn_ai_log WHERE corp_code=? AND model=?", (cc, MODEL)).fetchone()
            con.execute("DELETE FROM fn_ai WHERE corp_code=? AND fs=?", (cc, fs))      # 예전 판(v1)에서 저장한 사유가 남지 않게
            con.execute("INSERT OR REPLACE INTO fn_ai_log VALUES (?,?,?,?,?,?,?,?)", (cc, 0, 0, prev[0] if prev else 0, prev[1] if prev else 0, MODEL, now(), key)); con.commit()
            continue
        rows = kpi[fs]
        parts, meta = [], []
        for i, (nm, a, b, p, cv, r) in enumerate(ch, 1):
            rc = _rcept_for_pair(con, cc, rows, b)
            try:
                secs = _secs_for_report(con, rc, cc) if rc else []
            except Exception as e:  # noqa
                print(f"[fn_ai] {c['corp_name']} 주석 실패 {rc} {str(e)[:60]}", flush=True); secs = []
            ex = _excerpt(secs, nm, fs)
            meta.append((nm, X["cols"][b]["label"], rc, ex, r))
            parts.append(f"[항목 {i}] {nm}: {X['cols'][a]['label']} {p/1e8:,.0f}억 → {X['cols'][b]['label']} {cv/1e8:,.0f}억 ({'+' if r > 0 else '−'}{abs(r)*100:.0f}%)\n--- 주석 발췌 ---\n{ex or '(관련 절 없음)'}\n")
        if dry:
            print(f"[fn_ai] {c['corp_name']} {fs} 항목 {len(ch)}: " + " · ".join(f"{m[0]}@{m[1]}({len(m[3])}자)" for m in meta), flush=True)
            n_co += 1; continue
        if _spent(con) >= BUDGET:
            print("[fn_ai] 예산 도달 — 중단", flush=True); break
        try:
            msg = client.messages.create(model=MODEL, max_tokens=700, messages=[{"role": "user", "content": PROMPT + "\n".join(parts)}])
            txt = msg.content[0].text
            tin, tout = msg.usage.input_tokens, msg.usage.output_tokens
        except Exception as e:  # noqa
            print(f"[fn_ai] {c['corp_name']} API 실패 {str(e)[:80]}", flush=True); continue
        found = 0
        try:
            m = re.search(r"\[.*\]", txt, re.S)
            arr = json.loads(m.group(0)) if m else []
        except Exception:  # noqa
            arr = []
        con.execute("DELETE FROM fn_ai WHERE corp_code=? AND fs=?", (cc, fs))
        verbose = bool(limit and limit <= 50)                 # 표본 실행: 항목별 결과(저장·사유 없음·인용 불일치)를 로그로 남겨 품질을 눈으로 확인
        if verbose:
            print(f"[fn_ai] ▶ {c['corp_name']} {fs} · 입력 {tin:,}토큰 · 출력 {tout:,}토큰 · 발췌 " + "/".join(f"{len(m[3])}자" for m in meta), flush=True)
        for o in arr:
            try:
                i = int(o.get("i")) - 1
            except Exception:  # noqa
                continue
            if i < 0 or i >= len(meta):
                continue
            nm, label, rc, ex, r = meta[i]
            if not o.get("reason") or not o.get("quote"):
                if verbose:
                    print(f"[fn_ai]    – {nm}@{label}: 사유 없음" + ("" if ex else " (관련 절 없음)"), flush=True)
                continue
            qn = _norm(o["quote"])
            if len(qn) < 8 or qn not in _norm(ex):            # 인용이 발췌문에 그대로 없으면 버림(할루시네이션 방지)
                if verbose:
                    print(f"[fn_ai]    ✗ {nm}@{label}: 인용 불일치로 버림 — {str(o['reason'])[:50]} / “{str(o['quote'])[:70]}”", flush=True)
                continue
            reason, why_not = _check(str(o["reason"]), ex, r)
            if not reason:
                if verbose:
                    print(f"[fn_ai]    ✗ {nm}@{label}: {why_not} — {str(o['reason'])[:60]} / “{str(o['quote'])[:60]}”", flush=True)
                continue
            if verbose:
                print(f"[fn_ai]    ✓ {nm}@{label}({'+' if r > 0 else '−'}{abs(r) * 100:.0f}%): {reason[:80]} / “{str(o['quote'])[:110]}”", flush=True)
            con.execute("INSERT OR REPLACE INTO fn_ai VALUES (?,?,?,?,?,?,?,?,?)", (cc, fs, nm, label, reason[:80], str(o["quote"])[:200], rc, MODEL + "|" + VER, now()))
            found += 1
        prev = con.execute("SELECT tin, tout FROM fn_ai_log WHERE corp_code=? AND model=?", (cc, MODEL)).fetchone()   # 다시 묻는 회사: 앞서 쓴 토큰도 누적(예산 집계가 줄어들지 않게)
        con.execute("INSERT OR REPLACE INTO fn_ai_log VALUES (?,?,?,?,?,?,?,?)", (cc, len(ch), found, tin + (prev[0] if prev else 0), tout + (prev[1] if prev else 0), MODEL, now(), key))
        con.commit()
        n_co += 1; n_items += len(ch); n_found += found
        if n_co % 50 == 0:
            print(f"[fn_ai] {n_co}사 · 항목 {n_items} · 사유 확인 {n_found} · ${_spent(con):.2f}", flush=True)
    print(f"[fn_ai] 완료 {n_co}사 · 항목 {n_items} · 사유 확인 {n_found} · 누적 ${_spent(con):.2f}", flush=True)
    return n_co
