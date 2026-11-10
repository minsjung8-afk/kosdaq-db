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
BUDGET = float(os.environ.get("VERIFY_AI_BUDGET", "30"))   # 11-10 $20 단계 감사 10/10 일치·달러당 11.6건 → $30 단계   # 2026-11-09 사용자 "10달러 예산 범위 내에서 한번 해봐"(전 실행 누적) → $10 감사 통과 후 $20 단계(11-09 승인: 20→30→50 순차, 11-10 "유지 + 이중계상 차단")

PROMPT = """당신은 회계 감사인이다. 아래는 한 회사 {period} {fs} 재무제표의 원문 발췌(현금흐름표 본문의 상각 관련 줄, 주석 중 상각이 들어간 표 전체와 사용권·리스·상각 문장)다.
이 기간 누적(분기·반기는 '누적' 열, 연간은 당기 열) 상각비 합계 = 유형자산 감가상각비 + 사용권자산 상각비 + 무형자산 상각비 + 투자부동산 감가상각비 를 구하라.
규칙:
- 주어진 원문 숫자만 쓴다. 추측·외부 지식 금지. 전기(비교기간) 열·3개월 열을 쓰지 않는다.
- 같은 금액이 여러 표에 중복될 수 있다(예: 사용권자산이 유형자산 변동표의 한 종류로 이미 포함). 이중으로 더하지 않는다.
- 대손상각비·손상차손·처분손실·사채할인발행차금 상각·이자비용은 제외.
- 표의 단위(원·천원·백만원)를 확인해 원 단위로 환산한다.
- 사용권자산(리스) 상각비가 원문 어디에든(표의 '상각' 열, 문장) 있으면 반드시 포함한다. 리스 주석·사용권자산 변동표가 있는데 이 기간 사용권 상각비를 찾지 못하면 da_won을 null, verdict를 '판단불가'로 한다.
- 무형자산 상각비도 같다: 무형자산 변동표가 있는데 상각액을 찾지 못하면 null.
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
            # 2026-11-09 개선: 예전엔 첫 칸에 '상각'이 있는 행만 → 머리글에 '상각' 열이 있는 변동표(제이티비씨 '사용권자산 | 기초 | 증가 | 감소 | 상각 | 기말')를 놓쳐 사용권 누락
            #   → 표 어디든 상각·감가가 있으면 표 전체(최대 30행)
            if not any(_KEEP.search(str(c)) for r in rows for c in r):
                continue
            unit = tb.get("unit") or 1
            out.append(f"[{s.get('t')}] (표 단위 배수 {unit}원)\n" + "\n".join(" | ".join(str(c) for c in r) for r in rows[:30]))
        for b in s.get("blocks", []):              # 문장 속 사용권·리스 상각 금액(상보·제이티비씨처럼 표 밖에 적힌 경우)
            p = b.get("p") or ""
            for m in re.finditer(r"[^.。]{0,120}(사용권|리스|상각)[^.。]{0,200}", p):
                seg = m.group(0)
                if re.search(r"상각|감가", seg) and re.search(r"\d{1,3}(,\d{3})+", seg):
                    out.append(f"[{s.get('t')}] 문장: {seg.strip()}")
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


def targets(con, limit=None, min_gap=0.01, since=None, kinds=None, redo=False):
    """사이트 상각비가 주석 직접값보다 1% 넘게 큰 행(아직 판정 안 한 것) — 최근 연도·연간/반기 먼저"""
    con.execute("""CREATE TABLE IF NOT EXISTS ai_verify (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, field TEXT,
                   site_val REAL, alt_val REAL, ai_val REAL, verdict TEXT, evidence TEXT, reason TEXT, comp TEXT, rcept_no TEXT,
                   model TEXT, tin INTEGER, tout INTEGER, at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, field))""")
    rows = con.execute("""SELECT k.corp_code, k.bsns_year, k.reprt_code, k.fs_div, k.da, d.da, COALESCE(d.rcept_no, k.rcept_no), k.da_src
                          FROM fin_kpi k JOIN da_direct d USING(corp_code, bsns_year, reprt_code, fs_div)
                          WHERE k.da > d.da * 1.01 AND d.da > 0
                            AND NOT EXISTS (SELECT 1 FROM ai_verify v WHERE v.corp_code=k.corp_code AND v.bsns_year=k.bsns_year
                                            AND v.reprt_code=k.reprt_code AND v.fs_div=k.fs_div AND v.field='da' AND (? = 0 OR v.reason LIKE '[원문확인]%'))
                          ORDER BY k.bsns_year DESC, CASE k.reprt_code WHEN '11011' THEN 0 WHEN '11012' THEN 1 ELSE 2 END, k.corp_code""", (1 if redo else 0,)).fetchall()
    rows = [r for r in rows if r[6] and ":" not in r[6] and r[4] > r[5] * (1 + min_gap)
            and (not since or r[1] >= since) and (not kinds or r[2] in kinds)]
    return rows[:limit] if limit else rows


def _grounded(js, ctx):
    """AI가 낸 구성항목이 원문 발췌에 실제 있는 숫자인지(원·천원·백만원 환산) + 합계가 구성항목 합과 맞는지 — 둘 다 맞아야 반영 후보"""
    try:
        comp = {k: float(v) for k, v in (js.get("components") or {}).items() if v not in (None, "", 0)}
        tot = float(js.get("da_won"))
    except Exception:  # noqa
        return False
    if not comp or abs(sum(comp.values()) - tot) > max(1.0, tot * 0.002):
        return False
    if (re.search(r"사용권", ctx) and not comp.get("사용권") and not re.search(r"사용권.{0,20}포함", ctx)
            and not (comp.get("유형") and _rou_in_ppe(ctx, comp["유형"]))):
        return False                                        # 2026-11-09: 원문에 사용권자산이 있는데 사용권 상각을 빼면 불완전(제이티비씨·상보 오정정 원인)
        #   단 유형자산 변동표 안에 '사용권자산' 행이 있어 유형 합계에 이미 든 경우(오이솔루션)는 빼는 게 맞음
    nums = {int(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", ctx)}
    for v in comp.values():
        if not any(abs(v / u - n) <= 1 for u in (1, 1e3, 1e6) for n in nums):
            return False
    # 2026-11-10 감사(오이솔루션 반기 별도 14.0억, 원문 13.7억): 유형자산 변동표 합계 감가상각비(1,334,922)에 '사용권자산' 행(23,393)이 이미 들어 있는데
    #   사용권을 따로 또 더함 → 같은 변동표(기초·기말 행이 있는 표) 안에 유형 합계와 '사용권…' 행의 그 금액이 함께 있으면 이중계상으로 보고 반영 안 함
    if comp.get("유형") and comp.get("사용권") and _rou_in_ppe(ctx, comp["유형"], comp["사용권"]):
        return False
    # 2026-11-10 $30 단계 감사(코위버 투자부동산 이중계상 +3%, 디케이락 정부보조금 상계 전 +1.7%): 현금흐름 조정 표가 발췌에 있으면
    #   그 표의 상각 항목 합계(당기 열)와 0.5% 안에서 맞을 때만 반영 — 현금흐름 조정이 손익에 실제 반영된 상각비의 기준
    cf = _cf_sum(ctx)
    if cf and abs(tot - cf) > cf * 0.005:
        return False
    return True


def _cf_sum(ctx):
    """발췌 중 현금흐름 조정 표(제목에 '현금흐름')에서 상각 항목 행의 첫 숫자(당기) 합 × 표 단위. 없으면 None"""
    for blk in ctx.split("\n\n"):
        head = blk.split("\n", 1)[0]
        if "현금흐름" not in head:
            continue
        m = re.search(r"표 단위 배수 ([\d.e+]+)원", head)
        unit = float(m.group(1)) if m else 1.0
        tot, hit = 0.0, 0
        for ln in blk.splitlines()[1:]:
            cells = [c.strip() for c in ln.split("|")]
            lab = cells[0].replace(" ", "")
            if not re.search(r"감가|상각", lab) or re.search(r"대손|할인|금융|손상|후원가|환입|채권|사채|이익", lab):
                continue
            nums = [c for c in cells[1:] if re.fullmatch(r"\(?-?[\d,]+\)?", c or "")]
            if not nums:
                continue
            tot += abs(float(nums[0].strip("()").replace(",", ""))) * unit
            hit += 1
        if hit:
            return tot
    return None


def _has_num(txt, v):
    ns = {int(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", txt)}
    return any(abs(v / u - n) <= 1 for u in (1, 1e3, 1e6) for n in ns)


def _rou_in_ppe(ctx, ppe, rou=None):
    """유형자산 변동표(기초·기말이 있는 표) 안에 유형 합계 금액과 '사용권…' 행이 함께 있으면 사용권 상각은 유형 합계에 이미 포함.
    rou를 주면 그 '사용권' 행에 그 금액이 있을 때만 참"""
    for blk in ctx.split("\n\n"):
        if not re.search(r"기초|기말", blk) or not _has_num(blk, ppe):
            continue
        for ln in blk.splitlines():
            if re.match(r"\s*사용권", ln) and (rou is None or _has_num(ln, rou)):
                return True
    return False


def _spent(con):
    """실제 누적 지출. 2026-11-10: 재판정이 ai_verify 행을 덮어써 예전 호출 토큰이 사라져 누적이 과소(표시 $10.16, 실제 약 $10.61)
    → 실행별 지출 장부(ai_verify_spend)를 따로 두고, 장부가 없던 11-10 이전 지출은 실측 합계 $10.61로 한 번 기록"""
    con.execute("CREATE TABLE IF NOT EXISTS ai_verify_spend (at TEXT, usd REAL, note TEXT)")
    if not con.execute("SELECT 1 FROM ai_verify_spend").fetchone():
        con.execute("INSERT INTO ai_verify_spend VALUES (?, ?, ?)", (now(), 10.61, "11-09 표본 $0.49 + 재표본 $1.06 + 11-10 전체 $9.06(로그 실측)"))
        con.commit()
    return con.execute("SELECT COALESCE(SUM(usd),0) FROM ai_verify_spend").fetchone()[0]


def _cached_secs(con):
    """보관된 최신 보고서 주석(notes_latest.notes_j) — rcept_no → secs (DART 호출 없이 판정할 수 있는 행)"""
    from common import ztext
    out = {}
    try:
        rows = con.execute("SELECT rcept_no, notes_j FROM notes_latest WHERE notes_j IS NOT NULL").fetchall()
    except Exception:  # noqa
        rows = []
    for rn, blob in rows:
        try:
            out[rn] = json.loads(ztext(blob) or "[]")
        except Exception:  # noqa
            pass
    return out


def run(con=None, limit=25, workers=4, min_gap=0.05, since="2025", kinds=("11011", "11012", "11014"), cache_only=False, rejudge=False, max_ratio=None):
    import anthropic
    from concurrent.futures import ThreadPoolExecutor
    from business import fetch_document_xml
    from notes_v2 import extract_notes_v2
    from summarize import _parse_json
    con = con or connect()
    allt = targets(con, None, min_gap, since, kinds)
    if max_ratio:                                          # 2026-11-09: '2배+' 구간은 주석 직접값 쪽 오독이 대부분(표본) → 예산 안에서는 제외
        allt = [r for r in allt if r[4] <= r[5] * max_ratio]
    if rejudge:                                            # 2026-11-09: 발췌 개선 후 지난 표본(원문확인 제외)을 같은 행으로 다시 판정 — 원문 대조 결과와 비교용
        old = {(r[0], r[1], r[2], r[3]) for r in con.execute("SELECT corp_code, bsns_year, reprt_code, fs_div FROM ai_verify WHERE field='da' AND reason NOT LIKE '[원문확인]%'")}
        allt = [r for r in targets(con, None, min_gap, since, kinds, redo=True) if (r[0], r[1], r[2], r[3]) in old]
        limit = None
    cache = _cached_secs(con)
    if cache_only:                                         # 2026-11-09: 오늘 DART 한도 초과 — 주석이 보관된 보고서(주로 최신 반기) 행만으로 표본(DART 0콜)
        allt = [r for r in allt if r[6] in cache]
    # 2026-11-09 사용자: 코스피·코스닥·등급보유사·등급없는 회사 등 골고루 → (시장 3군 × 등급 유무 × 차이 폭 3구간) 18칸에서 같은 수씩
    mk = {r[0]: ("코스피" if r[1] == "KOSPI" else "코스닥·코넥스" if r[1] in ("KOSDAQ", "KONEX") else "비상장") for r in con.execute("SELECT corp_code, market FROM company")}
    rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
    band = lambda r: "5~20%" if r[4] <= r[5] * 1.2 else "20~100%" if r[4] <= r[5] * 2 else "2배+"
    stratum = lambda r: (mk.get(r[0], "비상장"), "등급있음" if r[0] in rated else "등급없음", band(r))
    if limit:
        import collections
        cells = collections.defaultdict(list)
        for r in allt:
            cells[stratum(r)].append(r)
        per = max(1, limit // max(1, len(cells)))
        todo = [r for k in sorted(cells) for r in cells[k][:per]]
        print("[verify_ai] 표본 칸별 " + " · ".join(f"{'/'.join(k)}:{min(per, len(v))}(전체 {len(v)})" for k, v in sorted(cells.items())), flush=True)
    else:
        todo = allt
    _ = todo   # 2026-11-09: 미판정 3,441건 전부는 예산 초과 → 최근(2025~)·연간/반기/3Q·차이 5% 넘는 행만(1~5%는 영업외 상각 가능성 — SGC에너지처럼 원문과 맞는 경우)
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
        if cache_only and rn in cache:                     # 보관 주석 사용(DART 0콜) — 현금흐름표 본문 줄은 없음(표본에서 'AI값없음' 48%의 주원인)
            ctx = _render(cache[rn], fs, "")
        else:                                              # 원문(DART 1콜) — 현금흐름표 본문 포함. 원문 전체는 보관하지 않고 연결·별도 발췌만 메모리에(수천 건 메모리 폭증 방지)
            with lock:
                got = docs.get(rn)
            if got is None:
                xml = fetch_document_xml(rn)
                secs = extract_notes_v2(xml)
                got = {f: _render(secs, f, xml) for f in ("CFS", "OFS")}
                del xml
                with lock:
                    docs[rn] = got
            ctx = got[fs]
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
        if js is not None:
            js["_grounded"] = _grounded(js, ctx)
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
                         ("[근거확인v2] " if (js or {}).get("_grounded") else "[근거불일치] ") + str((js or {}).get("reason") or ""), json.dumps((js or {}).get("components"), ensure_ascii=False), rn, MODEL, tin, tout, now()))
            con.commit()
            res.append((r, aiv, js))
    cost = spent0 + st["tin"] / 1e6 * PRICE_IN + st["tout"] / 1e6 * PRICE_OUT
    con.execute("INSERT INTO ai_verify_spend VALUES (?, ?, ?)", (now(), cost - spent0, f"{len(res)}건"))
    con.commit()
    print(f"[verify_ai] 완료 {len(res)}건 · 이번 입력 {st['tin']:,} / 출력 {st['tout']:,} 토큰 · 이번 ${cost - spent0:.2f} · 누적 ${cost:.2f}", flush=True)
    return res


def report(con, res):
    """판정 요약 — AI 값이 A(사이트)·B(주석 직접값)·둘 다 아님 중 어디에 맞는지(±0.5%)"""
    import collections
    nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    mk = {r[0]: ("코스피" if r[1] == "KOSPI" else "코스닥·코넥스" if r[1] in ("KOSDAQ", "KONEX") else "비상장") for r in con.execute("SELECT corp_code, market FROM company")}
    rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
    c = collections.Counter()
    seg = collections.Counter()
    for r, aiv, js in res:
        site, alt = r[4], r[5]
        k = ("AI값없음" if not aiv else "A(사이트) 일치" if abs(aiv - site) <= site * 0.005 else
             "B(주석) 일치" if abs(aiv - alt) <= alt * 0.005 else "둘다아님")
        g = "근거확인" if (js or {}).get("_grounded") else "근거불일치"
        c[(k, g)] += 1
        seg[(mk.get(r[0], "비상장"), "등급있음" if r[0] in rated else "등급없음", k, g)] += 1
        print(f"  [{k}|{g}|{'5~20%' if r[4] <= r[5]*1.2 else '20~100%' if r[4] <= r[5]*2 else '2배+'}] {nm.get(r[0], r[0])} {r[1]} {r[2]} {r[3]}: 사이트 {site/1e8:,.1f}억[{r[7]}] · 주석 {alt/1e8:,.1f}억 · AI {aiv and round(aiv/1e8,1)}억"
              f" · {(js or {}).get('source')} · {((js or {}).get('evidence') or '')[:160]!r}", flush=True)
    print(f"[verify_ai] 판정 분포(판정, 근거): {dict(c)}", flush=True)
    for k in sorted(seg):
        print(f"  [칸] {'/'.join(k)}: {seg[k]}", flush=True)
