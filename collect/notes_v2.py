"""주석 v2 (2026-10-24): 표 구조를 살려 보관 + 유형·무형·사용권자산 변동표에서 상각비 추출 — AI 비용 0, DART 호출만.
  notes_latest.notes_j : zlib(JSON) — [{t: 절 제목, blocks: [{p: 문단} | {tbl: {unit, rows: [[셀…]…]}}]}]
  da_notes             : (corp_code, bsns_year, reprt_code, fs_div, dep, amort, rou, da, rcept_no, src, fetched_at)
    - 연결/별도 구분: 정기보고서 주석은 연결 주석(연결재무제표 주석)과 별도 주석이 따로 있어 절 제목 앞 상위 섹션으로 구분한다.
      구분이 불가능하면 'CFS'로 두고 build_kpi 에서 CFS·OFS 모두의 폴백으로 쓴다.
  python run.py da_notes [--limit N]   # 연간(사업보고서, business.rcept_no) + 최신 분기(notes_latest.rcept_no) 변동표 → da_notes
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, log, now, zpack, ztext  # noqa: E402
from business import _strip, fetch_document_xml, NOTE_KEYS  # noqa: E402

# v2 수집 절: 기존 심사 절 + 상각비용 변동표 절
NOTE_KEYS_V2 = tuple(NOTE_KEYS) + ("유형자산", "무형자산", "사용권자산", "투자부동산", "현금흐름", "비용의성격", "성격별")   # 비용의 성격별 분류(2026-10-26): 상각비 1순위 근거
UNIT = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "십억원": 1_000_000_000, "억원": 100_000_000, "만원": 10_000}
CELL_RE = re.compile(r"<T[DHEU]\b[^>]*>(.*?)</T[DHEU]>", re.S | re.I)
TR_RE = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S | re.I)
TABLE_RE = re.compile(r"<TABLE[^>]*>(.*?)</TABLE>", re.S | re.I)
MAX_ROWS, MAX_P, MAX_SEC = 80, 3000, 90
PARSER_VER = 4          # 4: 비용의 성격별 분류 절 추가 수집(2026-10-26 HL D&I 반기 연결 상각비 공백) / 3: 주석 항목을 TITLE 기준으로 분할(2026-10-24) — 이보다 낮은 버전으로 보관된 notes_j·notes_annual은 재수집


def _unit_of(text):
    m = re.search(r"단위\s*[:：]?\s*([천백십억만]*원)", text or "")
    return UNIT.get(m.group(1), 1) if m else 1


def _num(cell):
    """'(1,234)' '-1,234' '△1,234' → float, 비숫자는 None"""
    c = (cell or "").replace(",", "").replace(" ", "").replace("△", "-").replace("▲", "-")
    neg = c.startswith("(") and c.endswith(")")
    c = c.strip("()")
    if not re.fullmatch(r"-?\d+(\.\d+)?", c):
        return None
    v = float(c)
    return -v if neg else v


def _blocks(sec_xml, cap=True):
    """절 XML → 문단·표 블록 (표는 행렬 유지). cap=False면 문단을 자르지 않음(주석 항목 분할용)."""
    out = []
    pos = 0
    for tm in TABLE_RE.finditer(sec_xml):
        before = _strip(sec_xml[pos:tm.start()])
        if before:
            out.append({"p": before[:MAX_P] if cap else before})
        rows = []
        for tr in TR_RE.findall(tm.group(1)):
            cells = [_strip(c).replace("\n", " ").strip() for c in CELL_RE.findall(tr)]
            if any(cells):
                rows.append(cells)
            if len(rows) >= MAX_ROWS:
                break
        if rows:
            unit = _unit_of(_strip(sec_xml[max(0, tm.start() - 600):tm.start()]))
            out.append({"tbl": {"unit": unit, "rows": rows}})
        pos = tm.end()
    tail = _strip(sec_xml[pos:])
    if tail:
        out.append({"p": tail[:MAX_P] if cap else tail})
    return out


# 주석 항목 제목: 'N. 제목' 한 줄(문장 아님) — DART 원문에서 주석 항목(1. 회사의 개요 … 15. 유형자산 …)은 별도 SECTION이 아니라
# '재무제표 주석' 절 본문의 <P> 제목으로만 구분된다(2026-10-24 확인: SECTION 제목만 보면 주석 항목이 하나도 안 잡힘).
_HEAD_RE = re.compile(r"^\s*(\d{1,2})\s*[.．]\s*(?![\d.)])(\S.{0,70}?)\s*$")
_NOTES_TITLE_RE = re.compile(r"((연결|별도)?재무제표(에대한)?주석|^(\d+\.)?주석)$")


def _split_notes(sec_xml, fs):
    """주석 절 본문 → 항목별 [{t, fs, blocks}].
    ① 본문 안에 <TITLE>(ATOC 목차 항목: '12. 유형자산 (연결)', '17-1. 차입금 (연결)')이 3개 이상이면 그 제목 사이로 분할(DART 표준 — 항목은 SECTION이 아니라 TITLE만 있음)
    ② 없으면 연번(직전+1~2)으로 이어지는 'N. 제목' 문단으로 분할(구형 원문)"""
    titles = list(re.finditer(r"<TITLE[^>]*>(.*?)</TITLE>", sec_xml, flags=re.S | re.I))
    if len(titles) >= 3:
        secs = []
        for i, m in enumerate(titles):
            end = titles[i + 1].start() if i + 1 < len(titles) else len(sec_xml)
            t = _strip(m.group(1)).replace("\n", " ").strip()
            if not t:
                continue
            blocks = _blocks(sec_xml[m.end():end])
            secs.append({"t": t, "fs": fs, "blocks": blocks})
        return secs
    secs, cur, last = [], None, 0
    for b in _blocks(sec_xml, cap=False):
        if "tbl" in b:
            if cur is not None:
                cur["blocks"].append(b)
            continue
        buf = []
        for line in b["p"].split("\n"):
            m = _HEAD_RE.match(line)
            n = int(m.group(1)) if m else None
            if m and last < n <= last + 2 and not re.search(r"(니다|습니다|음|함|임)\s*[.．]?$", m.group(2)):
                if cur is not None:
                    if buf:
                        cur["blocks"].append({"p": "\n".join(buf)[:MAX_P]})
                    secs.append(cur)
                buf = []
                cur = {"t": f"{n}. {m.group(2).strip()}", "fs": fs, "blocks": []}
                last = n
            else:
                buf.append(line)
        if buf and cur is not None:
            cur["blocks"].append({"p": "\n".join(buf)[:MAX_P]})
    if cur is not None:
        secs.append(cur)
    return secs


def _sec_size(blocks):
    return sum(len(b.get("p", "")) + sum(len(" ".join(r)) for r in b.get("tbl", {}).get("rows", [])) for b in blocks)


def extract_notes_v2(xml_text):
    """정기보고서 원문 → [{t, fs, blocks}].
    ① '연결재무제표 주석'/'재무제표 주석' 절 본문을 항목(N. 제목)으로 분할해 심사·상각비 관련 항목만(fs: CFS/OFS)
    ② 그 밖의 제목 있는 절(재무에 관한 사항의 차입금·우발부채 등)은 종전처럼 SECTION 단위(fs: 상위 제목으로 추정)"""
    secs = []
    bounds = []
    for m in re.finditer(r"<TITLE[^>]*>(.*?)</TITLE>", xml_text, flags=re.S | re.I):
        t = _strip(m.group(1)).replace(" ", "")
        if re.search(r"연결재무제표주석|연결재무제표에대한주석", t):
            bounds.append((m.start(), "CFS"))
        elif re.search(r"^(\d+\.)?(별도)?재무제표주석|재무제표에대한주석", t):
            bounds.append((m.start(), "OFS"))
    def fs_at(pos):
        cur = None
        for b, fs in bounds:
            if b <= pos:
                cur = fs
            else:
                break
        return cur
    others = []
    for lv in ("1", "2", "3", "4", "5"):
        for m in re.finditer(rf"<SECTION-{lv}[^>]*>(.*?)</SECTION-{lv}>", xml_text, flags=re.S | re.I):
            sec = m.group(1)
            if re.search(r"<SECTION-\d", sec, flags=re.I):
                continue
            t = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", sec, flags=re.S | re.I)
            title = _strip(t.group(1)) if t else ""
            tt = title.replace(" ", "")
            if not title:
                continue
            body = re.sub(r"<TITLE[^>]*>.*?</TITLE>", "", sec, count=1, flags=re.S | re.I)
            if _NOTES_TITLE_RE.search(tt):
                fs = "CFS" if "연결" in tt else "OFS"
                for s in _split_notes(body, fs):
                    if any(k in s["t"].replace(" ", "") for k in NOTE_KEYS_V2) and _sec_size(s["blocks"]) >= 60:
                        secs.append(s)
                continue
            if any(k in tt for k in NOTE_KEYS_V2):
                blocks = _blocks(body)
                if _sec_size(blocks) >= 60:
                    others.append({"t": title, "fs": None, "blocks": blocks})          # 재무에 관한 사항 등 — 연결·별도 구분 없음
    secs += others
    return secs[:MAX_SEC]


def to_text(secs):
    """v2 블록 → 기존 notes_z 호환 평문(### 제목 + 표는 ' | ' 구분)"""
    out = []
    for s in secs:
        lines = []
        for b in s["blocks"]:
            if "p" in b:
                lines.append(b["p"])
            else:
                lines += [" | ".join(r) for r in b["tbl"]["rows"]]
        out.append(f"### {s['t']}\n" + "\n".join(lines))
    return "\n\n".join(out)


# ---------- 상각비(변동표) ----------
_ROW_DA = re.compile(r"(감가상각|상각비|상각액|상각)")
_ROW_EX = re.compile(r"(누계|충당|손상|환입|누적|취득|처분|대체|기초|기말|평가|재분류|환율|외화|매각|계정|이연)")


def _hdr_match(cell):
    c = (cell or "").replace(" ", "")
    return bool(_ROW_DA.search(c)) and not _ROW_EX.search(c)


def _da_from_table(tbl):
    """변동표 1개 → 상각비(원 단위 절대값) 또는 None.
    ① 열 방향(흔함): 머리글(1~2행)에 '감가상각비/상각' 열이 있고 행이 자산 종류 → '합계/계' 행의 그 열, 없으면 자산 행 합산
    ② 행 방향: 첫 열이 '감가상각비/상각' 인 행 → '합계/계' 열, 없으면 마지막 숫자 열"""
    rows, unit = tbl["rows"], tbl.get("unit", 1) or 1
    if not rows:
        return None
    # ① 열 방향
    for hi in (0, 1):
        if hi >= len(rows):
            break
        hdr = rows[hi]
        cols = [i for i, h in enumerate(hdr) if _hdr_match(h)]
        if not cols:
            continue
        ci = cols[0]
        body = rows[hi + 1:]
        tot = next((r for r in body if r and re.search(r"^(합\s*계|총\s*계|계)$", r[0].replace(" ", "")) or (r and re.search(r"합계|총계", r[0].replace(" ", "")))), None)
        if tot and ci < len(tot) and _num(tot[ci]) is not None:
            return abs(_num(tot[ci]) * unit)
        vals = [_num(r[ci]) for r in body if ci < len(r) and _num(r[ci]) is not None and not re.search(r"기초|기말|취득원가|장부금액", (r[0] or "").replace(" ", ""))]
        if vals:
            return abs(sum(vals) * unit)
    # ② 행 방향
    hdr = rows[0]
    tot_idx = next((i for i, h in enumerate(hdr) if i > 0 and re.search(r"합계|총계|^계$", h.replace(" ", ""))), None)
    for r in rows[1:]:
        if not r or not _hdr_match(r[0]):
            continue
        if tot_idx is not None and tot_idx < len(r) and _num(r[tot_idx]) is not None:
            return abs(_num(r[tot_idx]) * unit)
        nums = [_num(c) for c in r[1:] if _num(c) is not None]
        if nums:
            return abs(nums[-1] * unit)
    return None


_NAT_ROW = re.compile(r"(감가상각|상각비)")
_NAT_EX = re.compile(r"(대손|손상|환입|충당|누계|합계|총계)")


def _da_from_nature(tbl, interim=False):
    """'비용의 성격별 분류' 표 1개 → 상각비 합(원). 행: 감가상각비·무형자산상각비·사용권자산상각비·투자부동산상각비(대손상각비 제외).
    열: 머리글에 '누적'이 있으면 그 열(분기·반기 누적), 없으면 첫 숫자 열(당기). 2026-10-26 HL D&I 2026 반기 연결: 83.1+2.9+2.4=88.5억 확인"""
    rows, unit = tbl["rows"], tbl.get("unit", 1) or 1
    if not rows:
        return None
    ci = None
    q3 = any("3개월" in (h or "").replace(" ", "") for hdr in rows[:4] for h in hdr)
    for hdr in rows[:4]:
        for i, h in enumerate(hdr):
            if "누적" in (h or "").replace(" ", ""):
                ci = i
                break
        if ci is not None:
            break
    tot, hit = 0.0, False
    for r in rows:
        if not r or not _NAT_ROW.search((r[0] or "").replace(" ", "")) or _NAT_EX.search((r[0] or "").replace(" ", "")):
            continue
        nums = [(i, _num(c)) for i, c in enumerate(r) if i > 0 and _num(c) is not None]
        if not nums:
            continue
        if ci is not None:
            v = next((x for i, x in nums if i == ci), None)
            if v is None and interim and q3 and len(nums) >= 2:
                v = nums[-1][1]
        else:                                          # '누적' 머리글이 없으면: 분기·반기에 '3개월' 열이 있을 때만 마지막 열(누적), 그 외엔 첫 열(당기·당반기 누적)
            v = nums[-1][1] if (interim and q3 and len(nums) >= 2) else nums[0][1]
        if v is not None:
            tot += abs(v); hit = True
    return tot * unit if hit and tot else None


_CF_ROW = re.compile(r"(감가상각|무형자산상각|사용권자산상각|투자부동산상각|^상각비)")
_CF_EX = re.compile(r"(대손|손상|환입|처분|충당|누계|채권|할인|현재가치|평가|이연)")


def _da_from_cf(tbl):
    """'현금흐름표' 주석의 '당기순이익 조정' 표 1개 → 상각비 합(원). 행: 감가상각비(에 대한 조정)·무형자산상각비·사용권자산상각비 등, 열: 첫 숫자 열(당기·당반기 누적).
    2026-10-29 동부건설 2026 반기: 현금흐름표 본문은 '영업에서 창출된 현금' 한 줄 · 변동표 없음 → 주석 37 '감가상각비에 대한 조정 7,408 + 무형자산상각비 255'(백만원)"""
    rows, unit = tbl["rows"], tbl.get("unit", 1) or 1
    tot, hit = 0.0, False
    for r in rows or []:
        nm = (r[0] or "").replace(" ", "") if r else ""
        if not _CF_ROW.search(nm) or _CF_EX.search(nm):
            continue
        nums = [_num(c) for c in r[1:] if _num(c) is not None]
        if nums:
            tot += abs(nums[0]); hit = True
    return tot * unit if hit and tot else None


def da_from_secs(secs, fs=None, interim=False):
    """[{t, fs, blocks}] → {dep, amort, rou, da}. 각 절에서 상각비 행이 있는 첫 표(=당기 변동표)만 사용. fs 지정 시 그 구분 절만(구분 없는 절은 포함)."""
    res = {"dep": None, "amort": None, "rou": None, "inv": None}
    for s in secs:
        if fs and s.get("fs") and s["fs"] != fs:
            continue
        t = s["t"].replace(" ", "")
        key = ("rou" if ("사용권" in t or (re.search(r"리스", t) and "리스부채" not in t))
               else "amort" if "무형" in t else "inv" if "투자부동산" in t else "dep" if "유형자산" in t else None)
        if not key or res[key] is not None:
            continue
        for b in s["blocks"]:
            if "tbl" in b:
                v = _da_from_table(b["tbl"])
                if v:
                    res[key] = v
                    break
    dep = (res["dep"] or 0) + (res["inv"] or 0)
    out = {"dep": dep or None, "amort": res["amort"], "rou": res["rou"]}
    parts = [v for v in out.values() if v]
    out["da"] = sum(parts) if parts else None
    # 현금흐름표 주석의 조정 항목(감가상각비 등)이 있으면 변동표보다 우선(현금흐름 조정 = 당기 비용으로 확정된 금액, 2026-10-29)
    for s in secs:
        if fs and s.get("fs") and s["fs"] != fs:
            continue
        if "현금흐름" in s["t"].replace(" ", ""):
            for b in s["blocks"]:
                if "tbl" in b:
                    v = _da_from_cf(b["tbl"])
                    if v:
                        out["da"], out["src"] = v, "현금흐름"
                        break
            if out.get("src"):
                break
    # 비용의 성격별 분류가 있으면 그 값을 최우선(손익계산서 비용 기준으로 명시된 금액 — 변동표 오독 위험이 없음)
    for s in secs:
        if fs and s.get("fs") and s["fs"] != fs:
            continue
        if re.search(r"비용의성격|성격별", s["t"].replace(" ", "")):
            for b in s["blocks"]:
                if "tbl" in b:
                    v = _da_from_nature(b["tbl"], interim)
                    if v:
                        out["da"], out["src"] = v, "성격별"
                        break
            if out.get("src") == "성격별":
                break
    return out


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS da_notes (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT,
                   dep REAL, amort REAL, rou REAL, da REAL, rcept_no TEXT, src TEXT, fetched_at TEXT,
                   PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))""")
    con.execute("""CREATE TABLE IF NOT EXISTS notes_annual (corp_code TEXT PRIMARY KEY, rcept_no TEXT, report_nm TEXT, secs_j BLOB, fetched_at TEXT)""")   # 사업보고서 변동표 절(유형·무형·사용권·투자부동산)만 캐시 — 파서 개선 시 재다운로드 없이 재계산
    for sql in ("ALTER TABLE notes_latest ADD COLUMN notes_j BLOB", "ALTER TABLE notes_latest ADD COLUMN notes_v INTEGER",
                "ALTER TABLE notes_annual ADD COLUMN ver INTEGER"):
        try:
            con.execute(sql)
        except Exception:  # noqa
            pass


def _report_key(report_nm):
    """'사업보고서 (2025.12)' → ('2025','11011'); '반기보고서 (2026.06)' → ('2026','11012'); 분기 → 03→11013, 09→11014"""
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm or "")
    if not m:
        return None, None
    y, mm = m.group(1), m.group(2)
    if "사업" in report_nm or "감사보고서" in report_nm:      # 감사보고서(비상장사 주석) = 연간
        return y, "11011"
    return y, {"03": "11013", "06": "11012", "09": "11014"}.get(mm)


def _store_da(con, cc, y, rc, secs, rcept, src):
    """연결/별도 구분이 있으면 각각, 없으면 같은 값을 CFS·OFS 양쪽에(폴백용). 반환: 저장 건수"""
    tagged = any(s.get("fs") for s in secs)
    n = 0
    for fs in ("CFS", "OFS"):
        r = da_from_secs(secs, fs=fs, interim=rc != "11011") if tagged else da_from_secs(secs, interim=rc != "11011")
        if r["da"]:
            con.execute("INSERT OR REPLACE INTO da_notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cc, y, rc, fs, r["dep"], r["amort"], r["rou"], r["da"], rcept, src, now()))
            n += 1
    return n


def _worker(cc, rcept):
    xml = fetch_document_xml(rcept)
    return extract_notes_v2(xml)


def run(corp_codes=None, limit=None, con=None, workers=4):
    """① notes_latest(최신 정기보고서)를 v2(표 보존)로 재수집·상각비 추출 ② 연간 상각비가 없는 회사는 사업보고서(business.rcept_no)에서 변동표만 추출.
    회사당 1~2콜. 이미 v2(notes_j)가 있고 같은 보고서면 건너뜀."""
    import common
    from concurrent.futures import ThreadPoolExecutor
    from financials import build_kpi
    con = con or connect()
    ensure(con)
    cs = corp_codes or [r[0] for r in con.execute("SELECT corp_code FROM company ORDER BY corp_name")]
    # ①a 이미 v2(notes_j)로 보관된 최신 보고서 → 다운로드 없이 상각비만 계산
    have_q = {(r[0], r[1]) for r in con.execute("SELECT corp_code, rcept_no FROM da_notes")}
    k0 = 0
    for r in con.execute("SELECT corp_code, rcept_no, report_nm, notes_j FROM notes_latest WHERE notes_j IS NOT NULL"):
        if (not corp_codes or r[0] in corp_codes) and (corp_codes or (r[0], r[1]) not in have_q):   # --codes 지정 시 파서 개선분 반영 위해 다시 계산
            try:
                secs = json.loads(ztext(r[3]) or "[]")
                y, rc = _report_key(r[2])
                if y and rc and _store_da(con, r[0], y, rc, secs, r[1], "latest"):
                    build_kpi(con, r[0], y, rc); k0 += 1
            except Exception as e:  # noqa
                log(con, r[0], "notes_v2", "fail", e)
    have_a = {(r[0], r[1]) for r in con.execute("SELECT corp_code, rcept_no FROM da_notes WHERE src='annual'")}
    for r in con.execute("SELECT corp_code, rcept_no, report_nm, secs_j FROM notes_annual WHERE secs_j IS NOT NULL"):
        if (not corp_codes or r[0] in corp_codes) and (corp_codes or (r[0], r[1]) not in have_a):
            try:
                secs = json.loads(ztext(r[3]) or "[]")
                y, rc = _report_key(r[2])
                if y and rc and _store_da(con, r[0], y, rc, secs, r[1], "annual"):
                    build_kpi(con, r[0], y, rc); k0 += 1
            except Exception as e:  # noqa
                log(con, r[0], "notes_v2", "fail", e)
    con.commit()
    if k0:
        print(f"[notes-v2] 보관된 주석에서 상각비 {k0}건 계산(호출 0)", flush=True)
    # ①b 최신 정기보고서 v2 재수집(notes_j 없는 회사)
    latest = {r[0]: (r[1], r[2]) for r in con.execute("SELECT corp_code, rcept_no, report_nm FROM notes_latest WHERE rcept_no IS NOT NULL AND (notes_j IS NULL OR COALESCE(notes_v,0) < ?)", (PARSER_VER,))}
    todo1 = [(cc, latest[cc][0], latest[cc][1], "latest") for cc in cs if cc in latest]
    # ② 연간: 사업보고서 변동표 (da_notes 11011 없는 회사)
    have_ann = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM da_notes WHERE reprt_code='11011'")} | {r[0] for r in con.execute("SELECT corp_code FROM notes_annual WHERE COALESCE(ver,0) >= ?", (PARSER_VER,))}
    ann = {}
    for r in con.execute("SELECT corp_code, bsns_year, rcept_no FROM business WHERE rcept_no IS NOT NULL ORDER BY bsns_year"):
        ann[r[0]] = (r[1], r[2])
    todo2 = [(cc, ann[cc][1], f"사업보고서 ({ann[cc][0]}.12)", "annual") for cc in cs if cc in ann and cc not in have_ann
             and con.execute("SELECT 1 FROM fin_kpi WHERE corp_code=? AND reprt_code='11011' AND da IS NULL AND op_income IS NOT NULL LIMIT 1", (cc,)).fetchone()]
    todo = (todo1 + todo2)[:limit] if limit else todo1 + todo2
    print(f"[notes-v2] 최신보고서 표보존 {len(todo1)}건 + 연간 변동표 {len(todo2)}건 = {len(todo)} · 병렬 {workers}", flush=True)
    n = got = i = 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(todo), workers * 5):
            batch = todo[b:b + workers * 5]
            futs = [(t, ex.submit(_worker, t[0], t[1])) for t in batch]
            stop = False
            for (cc, rcept, nm, kind), f in futs:
                i += 1
                try:
                    secs = f.result()
                    y, rc = _report_key(nm)
                    if kind == "latest":
                        con.execute("UPDATE notes_latest SET notes_j=?, notes_z=?, char_len=?, notes_v=? WHERE corp_code=? AND rcept_no=?",
                                    (zpack(json.dumps(secs, ensure_ascii=False)), zpack(to_text(secs)), len(to_text(secs)), PARSER_VER, cc, rcept))
                        n += 1
                    else:
                        asset = [x for x in secs if re.search(r"유형자산|무형자산|사용권자산|투자부동산|비용의성격|성격별", x["t"].replace(" ", ""))]
                        con.execute("INSERT OR REPLACE INTO notes_annual (corp_code, rcept_no, report_nm, secs_j, fetched_at, ver) VALUES (?,?,?,?,?,?)",
                                    (cc, rcept, nm, zpack(json.dumps(asset, ensure_ascii=False)), now(), PARSER_VER))
                    if y and rc and _store_da(con, cc, y, rc, secs, rcept, kind):
                        got += 1
                        build_kpi(con, cc, y, rc)
                except Exception as e:  # noqa
                    log(con, cc, "notes_v2", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 200 < workers * 5:
                print(f"[notes-v2] {i}/{len(todo)} · 표보존 {n} · 상각비 {got} · DART {common.CALLS}콜", flush=True)
            if stop:
                print(f"[notes-v2] DART 한도 도달 — {i}/{len(todo)}에서 중단", flush=True); break
    print(f"[notes-v2] 완료 {i}/{len(todo)} · 표보존 {n} · 상각비 확보 {got}", flush=True)
    return got
