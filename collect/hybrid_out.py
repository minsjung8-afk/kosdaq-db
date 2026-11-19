"""신종자본증권 공시 잔액 — 정기보고서 「신종자본증권 미상환 잔액」 표(2026-11-19).

배경: 신종자본증권이 있는 회사의 절반 이상(풀무원식품·SK텔레콤·CJ대한통운 등, 151/269건)은 재무상태표에 '신종자본증권' 줄 없이
      기타자본·자본잉여금·비지배지분에 넣는다 → 잔액과 '총차입금(신종포함)'을 낼 수 없었다.
출처: 사업·반기·분기보고서 'III. 재무에 관한 사항 > 증권의 발행을 통한 자금조달' 의 「신종자본증권 미상환 잔액」 표 — 회사가 직접 적는 값(액면, 기준일 현재).
      단위는 표 바로 위 '(단위 : 백만원)' 문구로 확정한다(추정 없음).
실제 표기(원문 5건 확인):
  ① 표 1개, 라벨 없음 또는 '(연결, 개별)'         → 연결·별도 같은 값            (풀무원식품 2,285억)
  ② '1) 개별기준' '2) 연결기준' 표 2개            → 각각                          (CJ대한통운 4,000억/4,000억)
  ③ 발행회사별 표: '(1) SK텔레콤' '(2) SK브로드밴드' → 별도 = 자기 회사 표, 연결 = 표 합계 (SK텔레콤 4,000억)
  ④ '[SK이노베이션㈜]' 자기 회사 표 1개            → 연결·별도 같은 값(종속회사 발행분은 표에 없으면 미포함)
  ⑤ 전부 '-'                                      → 0 (한국전력: 2025년 상환 완료)
분류가 안 되는 경우(라벨 없는 표 여러 개인데 금액이 다름)는 저장하지 않는다.
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
                out.append({"label": label, "amt": tot * UNIT[unit], "unit": unit, "dt": dt})
            elif tot is not None:
                out.append({"label": label, "amt": None, "unit": unit, "dt": dt, "raw": tot})   # 단위를 못 읽음 → 저장하지 않음(진단용)
            label_parts = []
    return out


def _nm(s):
    return re.sub(r"주식회사|㈜|\(주\)|[\s\[\]()·.,\d]|기준|개별|별도|연결", "", s or "")


def classify(tables, corp_name):
    """표 목록 → {'CFS': 금액|None, 'OFS': 금액|None, 'how': 설명}. 판단 절차는 모듈 설명의 ①~⑤"""
    t = [x for x in tables if x.get("amt") is not None]
    if not t:
        return {"CFS": None, "OFS": None, "how": "표 없음" if not tables else "단위 미확인"}
    has_c = lambda l: "연결" in l
    has_o = lambda l: bool(re.search(r"개별|별도", l))
    if len(t) == 1:
        l = t[0]["label"]
        if has_c(l) and not has_o(l):
            return {"CFS": t[0]["amt"], "OFS": None, "how": "연결 표 1개"}
        if has_o(l) and not has_c(l):
            return {"CFS": None, "OFS": t[0]["amt"], "how": "별도 표 1개"}
        return {"CFS": t[0]["amt"], "OFS": t[0]["amt"], "how": "표 1개(연결·별도 공통)"}
    c = [x for x in t if has_c(x["label"]) and not has_o(x["label"])]
    o = [x for x in t if has_o(x["label"]) and not has_c(x["label"])]
    if c or o:
        return {"CFS": c[0]["amt"] if c else None, "OFS": o[0]["amt"] if o else None, "how": "연결·개별 표 구분"}
    if len({x["amt"] for x in t}) == 1:
        return {"CFS": t[0]["amt"], "OFS": t[0]["amt"], "how": f"표 {len(t)}개 금액 동일"}
    own = _nm(corp_name)
    mine = [x for x in t if own and len(_nm(x["label"])) >= 2 and (_nm(x["label"]) in own or own in _nm(x["label"]))]
    if len(mine) == 1:
        return {"CFS": sum(x["amt"] for x in t), "OFS": mine[0]["amt"], "how": f"발행회사별 표 {len(t)}개(별도=자기 회사, 연결=합계)"}
    return {"CFS": None, "OFS": None, "how": f"분류 불가(표 {len(t)}개, 라벨: " + " / ".join(x["label"][:14] or "-" for x in t) + ")"}


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
            print(f"[신종잔액] {name} {y} {rc}: 연결 {_e(res['CFS'])} · 별도 {_e(res['OFS'])} · {res['how']} · 표 "
                  + " / ".join(f"[{t['label'][:18] or '-'}] {_e(t.get('amt'))}({t.get('unit')})" for t in tables), flush=True)
        if dry:
            continue
        for fs in ("CFS", "OFS"):
            if res[fs]:                                   # 0은 저장하지 않는다 — 표가 지배회사 발행분만 다루면 종속회사 신종(연결)이 있어도 '-'로 나오므로 '없음'의 근거가 못 됨(한국전력 2024)
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
