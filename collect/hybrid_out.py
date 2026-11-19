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
END_RE = re.compile(r"조건부\s*자본\s*증권\s*미상환|사채관리계약|증권의\s*발행을\s*통해\s*조달된\s*자금")


def _txt(x):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", x or ""))).strip()


def _num(cell):
    c = (cell or "").replace(",", "").replace(" ", "")
    if c in ("-", "", "–", "—", "해당사항없음"):
        return 0.0
    return float(c) if re.fullmatch(r"\d+(\.\d+)?", c) else None


def _table_total(tbl_xml):
    """잔여만기 표 → '합계' 행의 마지막 칸(표 단위 그대로). 합계 행이 없으면 공모+사모. 못 읽으면 None"""
    rows = [[_txt(c) for c in CELL_RE.findall(tr)] for tr in TR_RE.findall(tbl_xml)]
    rows = [r for r in rows if r]
    data = [r for r in rows if not any("잔여만기" in c for c in r)]
    def last_of(label):
        for r in data:
            if any(c.replace(" ", "") == label for c in r[:-1]):
                return _num(r[-1])
        return None
    tot = last_of("합계")
    if tot is None:
        a, b = last_of("공모"), last_of("사모")
        tot = (a or 0) + (b or 0) if (a is not None or b is not None) else None
    return tot


def parse(xml):
    """보고서 원문 → [{label, amt(원), unit, dt}] — 「신종자본증권 미상환 잔액」 제목 뒤 잔여만기 표마다 1건"""
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
        unit, label_parts, dt = None, [], None
        ctx = _ctx_basis(xml, m.start())
        for tm in TABLE_RE.finditer(seg):
            between = _txt(seg[cur:tm.start()])
            cur = tm.end()
            body = tm.group(1)
            flat = _txt(body)
            if "잔여만기" not in flat:                      # 기준일·단위만 담은 작은 표 또는 그 밖의 표
                between = (between + " " + flat).strip()
                tm_is_data = False
            else:
                tm_is_data = True
            u = UNIT_RE.findall(between)
            if u:
                unit = u[-1]
            d = re.search(r"(20\d{2})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})", between)
            if d:
                dt = f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}"
            lab = re.sub(r"\(?\s*기준일\s*[:：]?[^)]*\)?|\(?\s*단위\s*[:：]?\s*[천백십억만]*원[^)]*\)?|[()]", " ", between)
            lab = re.sub(r"20\d{2}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{1,2}\s*일?", " ", lab)
            lab = re.sub(r"\s+", " ", lab).strip()
            if lab:
                label_parts.append(lab)
            if not tm_is_data:
                continue
            if unit is None:
                iu = UNIT_RE.findall(flat)
                unit = iu[-1] if iu else None
            tot = _table_total(body)
            label = " ".join(label_parts)[-60:]
            if tot is not None and unit in UNIT:
                out.append({"label": label, "amt": tot * UNIT[unit], "unit": unit, "dt": dt, "ctx": ctx})
            elif tot is not None:
                out.append({"label": label, "amt": None, "unit": unit, "dt": dt, "raw": tot, "ctx": ctx})   # 단위를 못 읽음 → 저장하지 않음(진단용)
            label_parts = []
    return out


_HEAD_RE = re.compile(r"(?:[가-하]\.|\(\d+\)|\d+\)|\d+\.|[①-⑳]|[□■▣◆▶○●]|\[|<)\s*(연결|별도|개별)\s*(?:재무제표\s*)?기준")
_SEC_RE = re.compile(r"증권의\s*발행을\s*통한\s*자금조달|채무증권의\s*발행\s*등과\s*관련된\s*사항")


def _ctx_basis(xml, at):
    """제목 위치(at) 앞에서 가장 가까운 '가. 별도기준 / (2) 연결기준' 머리말 → 'C'|'O'|None.
    롯데쇼핑·롯데지주처럼 별도 표 묶음과 연결 표 묶음을 머리말로만 나누는 보고서용. 본문 속 '(연결 기준)'·'※ 별도 기준 발행 한도' 같은 언급은 번호·도형 기호가 앞에 없으므로 잡지 않는다."""
    lo = max(0, at - 900_000)
    head = xml[lo:at]
    ms = list(_SEC_RE.finditer(head))
    if ms:
        head = head[ms[0].start():] if len(ms) == 1 else head[ms[-2].start():] if at - (lo + ms[-1].start()) < 400 else head[ms[-1].start():]
    hs = _HEAD_RE.findall(_txt(head))
    return None if not hs else ("C" if hs[-1] == "연결" else "O")


_GENERIC = re.compile(r"신종\s*자본\s*증권|미상환|잔액|기준일?|개별|별도|연결|동일|재무제표|주식회사|㈜|\(주\)|당사|지배회사|[\s\[\]()<>·.,:\-\d]|[가-하]\.")


def _issuer(label):
    """라벨에서 일반 단어를 뺀 나머지(발행회사 이름으로 볼 만한 것) — 없으면 ''"""
    r = _GENERIC.sub("", label or "")
    return r[:-1] if r.endswith("주") and len(r) > 2 else r


def _basis(x):
    l = x.get("label") or ""
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
    if not t:
        return {**res, "how": "표 없음" if not tables else "단위 미확인"}
    own = re.sub(r"주식회사|㈜|\(주\)|\s", "", corp_name or "")
    def is_own(x):
        i = _issuer(x["label"])
        return bool(i) and len(i) >= 2 and (i in own or own in i)
    def is_other(x):
        i = _issuer(x["label"])
        return bool(i) and len(i) >= 2 and not (i in own or own in i)
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
    return {**res, "how": f"분류 불가(표 {len(t)}개, 라벨: " + " / ".join(x["label"][:14] or "-" for x in t) + ")"}


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


def run(con=None, year0=None, codes=None, limit=None, dry=False):
    from business import fetch_document_xml
    con = con or connect()
    _ensure(con)
    from datetime import date
    year0 = year0 or date.today().year - 2
    tg = targets(con, year0, codes)
    done = {r[0] for r in con.execute("SELECT rcept_no FROM hybrid_out_log")}
    todo = [t for t in tg if dry or t[4] not in done]
    print(f"[신종잔액] 대상 {len({t[0] for t in tg})}사 · {len(tg)}기간 · 이번 실행 {min(len(todo), limit or len(todo))}기간" + (" (점검만)" if dry else ""), flush=True)
    n_ok = n_set = 0
    changed = set()
    for cc, name, y, rc, rcept in todo[: (limit or len(todo))]:
        try:
            tables = parse(fetch_document_xml(rcept))
        except Exception as e:  # noqa
            print(f"[신종잔액] {name} {y} {rc} 원문 실패 {str(e)[:70]}", flush=True)
            continue
        res = classify(tables, name)
        n_ok += 1
        if dry or len(todo) <= 60:
            print(f"[신종잔액] {name} {y} {rc}: 연결 {_e(res['CFS'])} · 별도 {_e(res['OFS'])} · 기준없음 {_e(res['ANY'])} · 자기회사 {_e(res['OWN'])} · {res['how']} · 표 "
                  + " / ".join(f"[{t['label'][:18] or '-'}|{t.get('ctx') or '-'}] {_e(t.get('amt'))}({t.get('unit')})" for t in tables), flush=True)
        if dry:
            continue
        con.execute("DELETE FROM hybrid_out WHERE corp_code=? AND bsns_year=? AND reprt_code=?", (cc, y, rc))
        for fs in ("CFS", "OFS", "ANY", "OWN"):
            if res[fs] is not None:                       # 0도 저장(= 표상 기말 잔액 없음 — 화면에는 '공시 0'으로만 표시, HMM 2025: 기중 전환)
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
    print(f"[신종잔액] 원문 {n_ok}건 · 저장 {n_set}건(연결/별도) · KPI 재계산 {len(changed)}기간", flush=True)
    return n_set


def _e(v):
    return "-" if v is None else f"{v / 1e8:,.0f}억"
