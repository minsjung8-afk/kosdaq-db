"""신종자본증권 공시 잔액 — 정기보고서 「신종자본증권 미상환 잔액」 표(2026-11-19).

배경: 신종자본증권이 있는 회사의 절반 이상(풀무원식품·SK텔레콤·CJ대한통운 등, 151/269건)은 재무상태표에 '신종자본증권' 줄 없이
      기타자본·자본잉여금·비지배지분에 넣는다 → 잔액과 '총차입금(신종포함)'을 낼 수 없었다.
출처: 사업·반기·분기보고서 'III. 재무에 관한 사항 > 증권의 발행을 통한 자금조달' 의 「신종자본증권 미상환 잔액」 표 — 회사가 직접 적는 값(액면, 기준일 현재).
      단위는 표 바로 위 '(단위 : 백만원)' 문구로 확정한다(추정 없음).
판단 절차(DART 원문 33사 대조):
  ① 표 제목·라벨에 '연결'·'개별/별도'가 있으면 그 기준  ('(연결, 개별)' 풀무원식품 2,285억 / '1) 개별기준 2) 연결기준' CJ대한통운 / CJ CGV 연결 9,511억·별도 8,011억)
  ② 라벨에 없으면 제목 앞 머리말 '가. 별도기준 … 나. 연결기준'  (롯데쇼핑·롯데지주·한화에어로스페이스: 표 묶음을 머리말로만 구분)
  ③ 기준 표시가 전혀 없는 표 1개 → ANY(관행상 연결 기준). 별도에는 연결·별도 분배금이 같을 때만 사용  (풀무원 3,985억은 풀무원식품분 포함 → 별도에 쓰면 과대)
  ④ 종속회사 이름만 붙은 표 1개 → 연결만  (이수화학 3분기: '이수건설' 1,500억) / 자기 회사 이름만 붙은 표 1개 → OWN: 별도에 쓰고 연결에는 분배금이 같을 때만 ([SK이노베이션㈜] 7,000억)
  ⑤ 발행회사별 표 여러 개 → 별도 = 자기 회사 표, 연결 = 합계  (SK텔레콤·SK브로드밴드)
  ⑥ 그 밖(기준 없는 표 여러 개인데 금액이 다름) → 저장하지 않음
  0('-')도 저장하되 화면에는 '공시 0'으로만 표시(HMM 2025: 기중 전환으로 기말 잔액 없음).
단위 대조: DART 정형 API(newCaplScritsNrdmpBlce)는 보고서 첫 표의 합계를 원 단위로 준다 → 파서의 첫 표 금액과 다르면 그 보고서는 저장하지 않는다
  (API만 쓰지 않는 이유: 표가 여러 개인 보고서에서 첫 표만 준다 — 롯데쇼핑은 별도 '-'만 나오고 연결 3,500억이 빠짐).
  python run.py rawdump --type 신종잔액점검 --codes ...   # 파싱 결과만 출력
  python run.py rawdump --type 신종잔액                   # 대상 회사 수집 → hybrid_out 저장 → KPI 재계산
"""
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import connect, now  # noqa: E402

TABLE_RE = re.compile(r"<TABLE\b[^>]*>(.*?)</TABLE>", re.S | re.I)
TR_RE = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S | re.I)
CELL_RE = re.compile(r"<T[DHEU]\b[^>]*>(.*?)</T[DHEU]>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
UNIT = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "십억원": 1_000_000_000, "억원": 100_000_000, "만원": 10_000}
UNIT_RE = re.compile(r"단위\s*[:：]?\s*([천백십억만]*원)")
TITLE_RE = re.compile(r"신종\s*자본\s*증권\s*미상환\s*잔액")
END_RE = re.compile(r"조건부\s*자본\s*증권\s*미상환|사채관리계약|증권의\s*발행을\s*통해\s*조달된\s*자금|기업어음증권\s*미상환|단기사채\s*미상환|회사채\s*미상환")   # 목차에 적힌 제목(타임X AI)에서 시작하면 다음 표 제목에서 끊고 진짜 제목을 다시 찾는다


def _txt(x):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", x or ""))).strip()


def _num(cell):
    c = (cell or "").replace(",", "").replace(" ", "")
    if c in ("-", "", "–", "—", "해당사항없음"):
        return 0.0
    return float(c) if re.fullmatch(r"\d+(\.\d+)?", c) else None


def _table_total(tbl_xml):
    """잔여만기 표 → (합계 금액(표 단위 그대로) | None, 메모). '합계' 행의 '합 계' 칸을 쓰되,
    그 칸이 비었는데 만기 구간 칸에 금액이 있으면 구간 합을 쓴다(롯데지주 2025 사업보고서 별도: 20~30년 칸 625,000 · 합 계 칸 '-').
    합 계 칸과 구간 합이 서로 다르면 읽지 않는다(None, 메모)."""
    rows = [[_txt(c) for c in CELL_RE.findall(tr)] for tr in TR_RE.findall(tbl_xml)]
    rows = [r for r in rows if r]
    hdr = next((r for r in rows if any("잔여만기" in c for c in r)), None)
    has_tot = bool(hdr) and hdr[-1].replace(" ", "") in ("합계", "계", "총계")
    data = [r for r in rows if r is not hdr]

    def vals_of(label):
        for r in data:
            for k, c in enumerate(r[:-1]):
                if c.replace(" ", "") == label:
                    return [_num(x) for x in r[k + 1:]]
        return None

    def total(vals):
        if vals is None or not [v for v in vals if v is not None]:
            return None, None
        if not has_tot:
            return sum(v for v in vals if v is not None), None
        last, buckets = vals[-1], sum(v for v in vals[:-1] if v is not None)
        if not last:
            return buckets, ("합계 칸 공란 → 만기 구간 합" if buckets else None)
        if not buckets or abs(buckets - last) < 0.5:
            return last, None
        return None, f"합계 칸 {last:,.0f} ≠ 만기 구간 합 {buckets:,.0f}"

    tot, note = total(vals_of("합계"))
    if tot is None and note is None:
        (a, an), (b, bn) = total(vals_of("공모")), total(vals_of("사모"))
        if an and a is None or bn and b is None:
            return None, an or bn
        if a is not None or b is not None:
            tot, note = (a or 0) + (b or 0), an or bn
    return tot, note


_PIECE_RE = re.compile(r"</?(?:P|TITLE|PGBRK|BR|DIV|TD|TH|TE|TU|TR|LI|SPAN)\b[^>]*>", re.I)
_SHORT = 80          # 이보다 긴 문단·표는 설명문·발행조건 표 → 라벨·단위 근거로 쓰지 않는다


def _clean_label(t):
    lab = re.sub(r"\(?\s*기준일\s*[:：]?[^)]*\)?|\(?\s*단위\s*[:：]?\s*[천백십억만]*원[^)]*\)?|[()]", " ", t)
    lab = re.sub(r"20\d{2}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{1,2}\s*일?", " ", lab)
    return re.sub(r"\s+", " ", lab).strip()


def parse(xml):
    """보고서 원문 → [{label, amt(원)|None, unit, dt, ctx, note}] — 「신종자본증권 미상환 잔액」 제목 뒤 잔여만기 표마다 1건.
    라벨·단위는 표 바로 앞의 '짧은' 문단·작은 표에서만 읽는다. 사이에 긴 설명문·발행조건 표가 끼면 그 앞의 라벨·단위는 버린다
    (2026-11-19 표본 점검: SK이노베이션 — 발행조건 표의 '연결 기준 부채비율…' 문구가 라벨로 들어가 종속회사 표를 '연결'로 오판, 발행조건 표의 '(단위: 원)'이 다음 표 단위로 넘어감).
    단위를 못 읽었거나 합계가 맞지 않는 표는 amt=None(→ 그 보고서는 저장하지 않음)."""
    out = []
    pos = 0
    while True:
        m = TITLE_RE.search(xml, pos)
        if not m:
            break
        e = END_RE.search(xml, m.end())
        end = e.start() if e and e.start() - m.end() < 80_000 else min(len(xml), m.end() + 30_000)
        seg = xml[m.end():end]
        pos = max(end, m.end() + 1)
        cur = 0
        st = {"unit": None, "dt": None, "heads": []}

        def absorb(t):
            if len(t) > _SHORT:                              # 긴 설명문·발행조건 표 → 앞의 라벨·단위 무효
                st["unit"], st["heads"] = None, []
                return
            u = UNIT_RE.findall(t)
            if u:
                st["unit"] = u[-1]
            d = re.search(r"(20\d{2})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})", t)
            if d:
                st["dt"] = f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}"
            lab = _clean_label(t)
            if lab:
                st["heads"].append(lab)

        for tm in TABLE_RE.finditer(seg):
            body = tm.group(1)
            flat = _txt(body)
            for t in (_txt(x) for x in _PIECE_RE.split(seg[cur:tm.start()])):
                if t:
                    absorb(t)
            cur = tm.end()
            if "잔여만기" not in flat:                       # 기준일·단위만 담은 작은 표(짧음) 또는 발행조건 표(김)
                absorb(flat)
                continue
            unit = st["unit"]
            if unit is None:
                iu = UNIT_RE.findall(flat)
                unit = iu[-1] if iu else None
            tot, note = _table_total(body)
            hs_ = st["heads"]                                # 라벨 = 표에 가장 가까운 짧은 문단 1개(발행회사 이름 판정용), blabel = 가까운 2개(연결·별도 판정용)
            rec = {"label": (hs_[-1] if hs_ else "")[-60:], "blabel": " ".join(hs_[-2:])[-80:], "amt": None, "unit": unit, "dt": st["dt"], "ctx": _ctx_basis(xml, m.end() + tm.start()), "note": note}
            if tot is not None and unit in UNIT and (0 < tot * UNIT[unit] < 1e8 or tot * UNIT[unit] > 5e13):
                rec["note"] = f"합계 {tot:,.0f}{unit} = 1억원 미만 또는 50조원 초과 — 단위 표기 오류 의심"      # SK이노베이션 2026 1분기 SK온 표: 단위 '원'에 500,000(실제 백만원) → 읽지 않는다
            elif tot is not None and unit in UNIT:
                rec["amt"] = tot * UNIT[unit]
            else:
                rec["note"] = note or ("단위 문구 없음" if tot is not None else "합계 행 없음")
            out.append(rec)
            st["unit"], st["heads"] = None, []               # 단위·라벨은 다음 표로 넘기지 않는다
    return out


_HEAD_RE = re.compile(r"(?:[가-하]\.|\(\d+\)|\d+\)|\d+\.|[①-⑳]|[□■▣◆▶○●]|\[|<|【)\s*(연결|별도|개별)\s*(?:재무제표\s*)?기준|(?:<|【)\s*(재무제표)\s*기준\s*(?:>|】)")
_ANGLE_RE = re.compile(r"<\s*((?:연결\s*)?재무제표\s*기준)\s*>")      # 원문에 태그처럼 적힌 '<연결재무제표 기준>' — 태그 제거에 쓸려 나가지 않게 먼저 바꿔 둔다
_SEC_RE = re.compile(r"증권의\s*발행을\s*통한\s*자금조달|채무증권의\s*발행\s*등과\s*관련된\s*사항")


def _ctx_basis(xml, at):
    """표 위치(at) 앞에서 가장 가까운 '가. 별도기준 / (2) 연결기준 / <연결재무제표 기준>' 머리말 → 'C'|'O'|None.
    롯데쇼핑·롯데지주처럼 별도 표 묶음과 연결 표 묶음을 머리말로만 나누는 보고서용. 본문 속 '(연결 기준)'·'※ 별도 기준 발행 한도' 같은 언급은 번호·도형 기호가 앞에 없으므로 잡지 않는다."""
    lo = max(0, at - 900_000)
    head = xml[lo:at]
    ms = list(_SEC_RE.finditer(head))
    if ms:
        head = head[ms[0].start():] if len(ms) == 1 else head[ms[-2].start():] if at - (lo + ms[-1].start()) < 400 else head[ms[-1].start():]
    hs = _HEAD_RE.findall(_txt(_ANGLE_RE.sub(r"【\1】", head)))
    return None if not hs else ("C" if hs[-1][0] == "연결" else "O")          # '<재무제표 기준>'(연결 표기 없음) = 별도(대한항공: <재무제표 기준> / <연결재무제표 기준> 한 쌍)


_GENERIC = re.compile(r"신종\s*자본\s*증권|미상환|잔액|기준일?|개별|별도|연결|동일|재무제표|주식회사|㈜|\(주\)|당사|지배회사|[\s\[\]()<>·.,:\-\d]|[가-하]\.")


def _issuer(label):
    """라벨에서 일반 단어를 뺀 나머지(발행회사 이름으로 볼 만한 것) — 없으면 ''"""
    r = _GENERIC.sub("", label or "")
    return r[:-1] if r.endswith("주") and len(r) > 2 else r


def _basis(x):
    l = x.get("blabel") or x.get("label") or ""
    c, o = "연결" in l, bool(re.search(r"개별|별도", l))
    if c and o:
        return "B"
    if c:
        return "C"
    if o:
        return "O"
    return x.get("ctx")


def classify(tables, corp_name):
    """표 목록 → {'CFS','OFS','ANY','OWN': 금액|None, 'how': 설명}. OWN = 자기 회사 이름만 붙은 표 1개([SK이노베이션㈜]) → 별도에 쓰고, 연결에는 분배금이 같을 때만.
    ANY = 기준 표시가 없는 표 1개(관행상 연결 기준 — 풀무원 3,985억 = 자기 발행분 + 풀무원식품 2,285억). 연결에는 그대로 쓰고,
    별도에는 KPI 단계에서 '연결과 별도의 분배금이 같을 때만'(= 같은 증권일 때만) 쓴다."""
    t = [x for x in tables if x.get("amt") is not None]
    res = {"CFS": None, "OFS": None, "ANY": None, "OWN": None}
    if not t or len(t) < len(tables):                          # 표 하나라도 단위·합계를 못 읽으면 그 보고서 전체를 저장하지 않는다(일부만 더하면 과소 — SK이노베이션 2026 1분기)
        bad = [x for x in tables if x.get("amt") is None]
        return {**res, "how": "표 없음" if not tables else "표 읽기 실패(" + " / ".join((x.get("note") or "?")[:30] for x in bad[:3]) + ")"}
    own = re.sub(r"주식회사|㈜|\(주\)|\s", "", corp_name or "")
    def is_own(x):
        i = _issuer(x["label"])
        return 2 <= len(i) <= 20 and (i in own or own in i)
    def is_other(x):                                           # 회사 이름으로 볼 만한 짧은 라벨만(긴 주석 문구가 '다른 회사'로 잡혀 합산되는 것 방지)
        i = _issuer(x["label"])
        return 2 <= len(i) <= 20 and not (i in own or own in i)
    if len({x.get("ctx") for x in t if x.get("ctx")}) < 2:      # 머리말 기준은 '별도 묶음·연결 묶음'이 둘 다 있을 때만 믿는다(표 1개짜리 보고서에서 엉뚱한 머리말을 집는 것 방지 — HL D&I 3분기)
        t = [{**x, "ctx": None} for x in t]
    bs = [_basis(x) for x in t]
    if any(bs):
        for x, b in zip(t, bs):
            if b in ("C", "B") and res["CFS"] is None:
                res["CFS"] = x["amt"]
            if b in ("O", "B") and res["OFS"] is None:
                res["OFS"] = x["amt"]
        return {**res, "how": "연결·별도 표시(" + "/".join(b or "-" for b in bs) + ")"}
    if len(t) == 1 or len({x["amt"] for x in t}) == 1:
        if len(t) == 1 and is_other(t[0]):
            return {**res, "CFS": t[0]["amt"], "how": f"종속회사 표 1개({_issuer(t[0]['label'])[:10]}) → 연결만"}
        if len(t) == 1 and is_own(t[0]):
            return {**res, "OWN": t[0]["amt"], "how": "자기 회사 이름 표 1개 → 별도(연결은 분배금이 같을 때만)"}
        return {**res, "ANY": t[0]["amt"], "how": f"기준 표시 없는 표 {len(t)}개"}
    mine = [x for x in t if is_own(x)]
    if len(mine) == 1 and all(is_own(x) or is_other(x) for x in t):
        return {**res, "CFS": sum(x["amt"] for x in t), "OFS": mine[0]["amt"], "how": f"발행회사별 표 {len(t)}개(별도=자기 회사, 연결=합계)"}
    if not mine and all(is_other(x) for x in t):               # 종속회사 표만 여러 개(SK이노베이션 2024 반기: SK인천석유화학·SK온) → 연결만
        return {**res, "CFS": sum(x["amt"] for x in t), "how": f"종속회사 표 {len(t)}개 → 연결만(합계)"}
    # 2026-11-20 사용자 '분류 불가 19건(현대차·AK홀딩스·대동) 보완': 기준 표시도 발행회사 이름도 없는 표 2개는 정기보고서 관행상 연결→별도 순서
    # (CJ CGV 등 라벨이 있는 2표 사례 전부 그 순서). 첫 표 ≥ 둘째 표일 때만 순서로 추정하고 '순서 추정'을 남긴다(API 대조는 호출부에서 첫 표와 비교).
    if len(t) == 2 and not any(bs) and not any(is_own(x) or is_other(x) for x in t) and t[0]["amt"] >= t[1]["amt"] >= 0:
        return {**res, "CFS": t[0]["amt"], "OFS": t[1]["amt"], "how": "표 2개 순서 추정(연결→별도, 기준 표시 없음)"}
    return {**res, "how": f"분류 불가(표 {len(t)}개, 라벨: " + " / ".join(x["label"][:14] or "-" for x in t) + ")"}


def _api_total(cc, y, rc):
    """DART 정형 API(newCaplScritsNrdmpBlce: 신종자본증권 미상환 잔액) — 보고서 **첫 표**의 합계를 DART가 원 단위로 정규화해 준다(표가 여러 개여도 첫 표만: 롯데쇼핑 = 별도 '-').
    파서가 읽은 단위가 맞는지 대조하는 용도. 실패·자료 없음은 None"""
    try:
        from common import dart_get
        js = dart_get("newCaplScritsNrdmpBlce", corp_code=cc[1:] if cc.startswith("U") else cc, bsns_year=str(y), reprt_code=rc)
    except Exception:  # noqa
        return None
    for r in (js or {}).get("list", []) or []:
        if (r.get("remndr_exprtn2") or "").replace(" ", "") == "합계":
            sm = _num(r.get("sm"))
            if sm:
                return sm
            bk = [_num(v) for k, v in r.items() if k.startswith("yy")]
            return sum(v for v in bk if v is not None) if any(v is not None for v in bk) else sm   # 합계 칸 공란(롯데지주) → 만기 구간 합
    return None


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS hybrid_out (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT, amt REAL, how TEXT,
                   rcept_no TEXT, fetched_at TEXT, PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))""")
    con.execute("CREATE TABLE IF NOT EXISTS hybrid_out_log (rcept_no TEXT PRIMARY KEY, n_tbl INTEGER, how TEXT, fetched_at TEXT)")   # 같은 보고서를 다시 받지 않기 위한 기록
    con.commit()


def targets(con, year0, codes=None):
    """잔액이 재무상태표에 없는(hybrid_note에 '잔액 … 미분리') 회사의 최근 기간 [(corp_code, corp_name, bsns_year, reprt_code, rcept_no)]"""
    q = """SELECT DISTINCT k.corp_code, c.corp_name, k.bsns_year, k.reprt_code, k.rcept_no FROM fin_kpi k JOIN company c ON c.corp_code=k.corp_code
           WHERE k.bsns_year>=? AND k.rcept_no IS NOT NULL AND k.rcept_no NOT LIKE '%:전기'
             AND k.corp_code IN (SELECT corp_code FROM fin_kpi WHERE bsns_year>=? AND (hybrid_note LIKE '%잔액%미분리%' OR hybrid_note LIKE '%공시%'))
           ORDER BY c.corp_name, k.bsns_year, k.reprt_code"""
    rows = [tuple(r) for r in con.execute(q, (str(year0), str(year0)))]
    if codes:
        rows = [r for r in rows if r[0] in codes]
    return rows


def _fetch_one(item):
    """(병렬 작업) 원문 내려받기 → 표 읽기 → DART 정형 API 대조값. DB에는 손대지 않는다."""
    import time
    from business import fetch_document_xml
    cc, name, y, rc, rcept = item
    t0 = time.time()
    try:
        tables = parse(fetch_document_xml(rcept))
    except Exception as e:  # noqa
        return {"err": str(e)[:70], "tf": time.time() - t0, "ta": 0.0}
    t1 = time.time()
    api = _api_total(cc, y, rc) if tables else None        # 단위 대조: DART가 원 단위로 정규화한 첫 표 합계
    return {"tables": tables, "api": api, "tf": t1 - t0, "ta": time.time() - t1}


def run(con=None, year0=None, codes=None, limit=None, dry=False, workers=6, budget_sec=110 * 60):
    """workers: 원문·API를 동시에 받는 수(2026-11-19 표본 168건이 순차로 건당 4~78초 — DART 응답 지연 — 걸려 병렬화).
    budget_sec: 워크플로 제한(170분) 안에 끝나도록 이 시간을 넘기면 남은 보고서는 다음 실행으로 넘긴다(hybrid_out_log로 이어받기)."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    import common
    con = con or connect()
    _ensure(con)
    from datetime import date
    year0 = year0 or date.today().year - 2
    tg = targets(con, year0, codes)
    done = {r[0] for r in con.execute("SELECT rcept_no FROM hybrid_out_log WHERE how NOT LIKE 'API 불일치%' AND how NOT LIKE '%분류 불가%'")}      # 불일치·분류 불가로 미저장한 보고서는 규칙이 바뀌면 다시 시도(2026-11-20 순서 추정 규칙 추가)
    todo = [t for t in tg if dry or t[4] not in done]
    work = todo[: (limit or len(todo))]
    print(f"[신종잔액] 대상 {len({t[0] for t in tg})}사 · {len(tg)}기간 · 이번 실행 {len(work)}기간 · 동시 {workers}" + (" (점검만)" if dry else ""), flush=True)
    n_ok = n_set = n_bad = left = 0
    tf = ta = 0.0
    t_start = time.time()
    changed = set()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for b in range(0, len(work), 48):
            if common.QUOTA_HIT:
                left = len(work) - b
                print(f"[신종잔액] DART 한도 도달 — 중단(남은 {left}기간은 다음 실행에서)", flush=True)
                break
            if time.time() - t_start > budget_sec:
                left = len(work) - b
                print(f"[신종잔액] 시간 예산 {budget_sec // 60}분 도달 — 남은 {left}기간은 다음 실행에서", flush=True)
                break
            batch = work[b:b + 48]
            for (cc, name, y, rc, rcept), r in zip(batch, ex.map(_fetch_one, batch)):
                tf += r["tf"]; ta += r["ta"]
                if r.get("err"):
                    print(f"[신종잔액] {name} {y} {rc} 원문 실패 {r['err']}", flush=True)
                    continue
                tables, api = r["tables"], r["api"]
                res = classify(tables, name)
                n_ok += 1
                amts = [t.get("amt") for t in tables if t.get("amt") is not None]
                first = amts[0] if amts else None
                chk = "미대조" if api is None or not amts else ("일치" if any(abs(api - x) < 1 for x in amts) else "불일치")   # 정형 API는 '첫 표'가 아니라 개별(자기 회사) 표를 주기도 한다(콘텐트리중앙·SKC: 연결 표가 앞, API는 뒤의 별도 표) → 어느 표든 맞으면 일치
                n_bad += chk == "불일치"
                print(f"[신종잔액] {name} {y} {rc}: 연결 {_e(res['CFS'])} · 별도 {_e(res['OFS'])} · 기준없음 {_e(res['ANY'])} · 자기회사 {_e(res['OWN'])} · {res['how']} · API {_e(api)} {chk} · 표 "
                      + " / ".join(f"[{t['label'][:18] or '-'}|{t.get('ctx') or '-'}] {_e(t.get('amt'))}({t.get('unit')}{'·' + t['note'] if t.get('note') else ''})" for t in tables), flush=True)
                if dry:
                    continue
                con.execute("DELETE FROM hybrid_out WHERE corp_code=? AND bsns_year=? AND reprt_code=?", (cc, y, rc))
                if chk == "불일치":                            # 단위·표 인식이 DART 값과 다르면 저장하지 않는다(화면은 '있음' 유지) — 기록만 남겨 매일 다시 받지 않게
                    con.execute("INSERT OR REPLACE INTO hybrid_out_log VALUES (?,?,?,?)", (rcept, len(tables), f"API 불일치(API {_e(api)} vs 첫 표 {_e(first)}) · " + res["how"], now()))
                    con.commit()
                    changed.add((cc, y, rc))
                    continue
                for fs in ("CFS", "OFS", "ANY", "OWN"):
                    if res[fs] is not None:               # 0도 저장(= 표상 기말 잔액 없음 — 화면에는 '공시 0'으로만 표시, HMM 2025: 기중 전환)
                        con.execute("INSERT OR REPLACE INTO hybrid_out VALUES (?,?,?,?,?,?,?,?)", (cc, y, rc, fs, res[fs], res["how"], rcept, now()))
                        n_set += 1
                con.execute("INSERT OR REPLACE INTO hybrid_out_log VALUES (?,?,?,?)", (rcept, len(tables), res["how"], now()))
                con.commit()
                changed.add((cc, y, rc))
    if not dry and changed:
        import financials
        for cc, y, rc in sorted(changed):
            try:
                financials.build_kpi(con, cc, y, rc)
            except Exception as e:  # noqa
                print(f"[신종잔액] KPI 재계산 실패 {cc} {y} {rc} {str(e)[:60]}", flush=True)
        con.commit()
    print(f"[신종잔액] 원문 {n_ok}건 · 저장 {n_set}건(연결/별도) · DART 정형 API와 불일치(저장 안 함) {n_bad}건 · KPI 재계산 {len(changed)}기간 · 남은 {left}기간"
          f" · 경과 {time.time() - t_start:.0f}초(내려받기 {tf:.0f}초 · API {ta:.0f}초 — 작업 시간 합)", flush=True)
    return n_set


def _e(v):
    return "-" if v is None else f"{v / 1e8:,.0f}억"
