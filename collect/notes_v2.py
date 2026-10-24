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
NOTE_KEYS_V2 = tuple(NOTE_KEYS) + ("유형자산", "무형자산", "사용권자산", "투자부동산", "현금흐름")
UNIT = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "십억원": 1_000_000_000, "억원": 100_000_000, "만원": 10_000}
CELL_RE = re.compile(r"<T[DHEU]\b[^>]*>(.*?)</T[DHEU]>", re.S | re.I)
TR_RE = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S | re.I)
TABLE_RE = re.compile(r"<TABLE[^>]*>(.*?)</TABLE>", re.S | re.I)
MAX_ROWS, MAX_P, MAX_SEC = 80, 3000, 40


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


def _blocks(sec_xml):
    """절 XML → 문단·표 블록 (표는 행렬 유지)."""
    out = []
    pos = 0
    for tm in TABLE_RE.finditer(sec_xml):
        before = _strip(sec_xml[pos:tm.start()])
        if before:
            out.append({"p": before[:MAX_P]})
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
        out.append({"p": tail[:MAX_P]})
    return out


def extract_notes_v2(xml_text):
    """정기보고서 원문 → [{t, fs, blocks}] (fs: 'CFS'|'OFS'|None — 상위 제목에 '연결'이 있으면 CFS, '별도'/'재무제표 주석'(연결 아님)이면 OFS)."""
    secs = []
    # 상위 섹션(연결재무제표 주석 / 재무제표 주석) 경계 수집
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
    for lv in ("1", "2", "3", "4", "5"):
        for m in re.finditer(rf"<SECTION-{lv}[^>]*>(.*?)</SECTION-{lv}>", xml_text, flags=re.S | re.I):
            sec = m.group(1)
            t = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", sec, flags=re.S | re.I)
            title = _strip(t.group(1)) if t else ""
            if not title or not any(k in title.replace(" ", "") for k in NOTE_KEYS_V2):
                continue
            if re.search(r"<SECTION-\d", sec, flags=re.I):
                continue
            blocks = _blocks(re.sub(r"<TITLE[^>]*>.*?</TITLE>", "", sec, count=1, flags=re.S | re.I))
            if sum(len(b.get("p", "")) + sum(len(" ".join(r)) for r in b.get("tbl", {}).get("rows", [])) for b in blocks) < 60:
                continue
            secs.append({"t": title, "fs": fs_at(m.start()), "blocks": blocks})
            if len(secs) >= MAX_SEC:
                break
    return secs


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


def da_from_secs(secs, fs=None):
    """[{t, fs, blocks}] → {dep, amort, rou, da}. 각 절에서 상각비 행이 있는 첫 표(=당기 변동표)만 사용. fs 지정 시 그 구분 절만(구분 없는 절은 포함)."""
    res = {"dep": None, "amort": None, "rou": None, "inv": None}
    for s in secs:
        if fs and s.get("fs") and s["fs"] != fs:
            continue
        t = s["t"].replace(" ", "")
        key = "rou" if "사용권" in t else "amort" if "무형" in t else "inv" if "투자부동산" in t else "dep" if "유형자산" in t else None
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
    return out


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS da_notes (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT,
                   dep REAL, amort REAL, rou REAL, da REAL, rcept_no TEXT, src TEXT, fetched_at TEXT,
                   PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))""")
    con.execute("""CREATE TABLE IF NOT EXISTS notes_annual (corp_code TEXT PRIMARY KEY, rcept_no TEXT, report_nm TEXT, secs_j BLOB, fetched_at TEXT)""")   # 사업보고서 변동표 절(유형·무형·사용권·투자부동산)만 캐시 — 파서 개선 시 재다운로드 없이 재계산
    try:
        con.execute("ALTER TABLE notes_latest ADD COLUMN notes_j BLOB")
    except Exception:  # noqa
        pass


def _report_key(report_nm):
    """'사업보고서 (2025.12)' → ('2025','11011'); '반기보고서 (2026.06)' → ('2026','11012'); 분기 → 03→11013, 09→11014"""
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm or "")
    if not m:
        return None, None
    y, mm = m.group(1), m.group(2)
    if "사업" in report_nm:
        return y, "11011"
    return y, {"03": "11013", "06": "11012", "09": "11014"}.get(mm)


def _store_da(con, cc, y, rc, secs, rcept, src):
    """연결/별도 구분이 있으면 각각, 없으면 같은 값을 CFS·OFS 양쪽에(폴백용). 반환: 저장 건수"""
    tagged = any(s.get("fs") for s in secs)
    n = 0
    for fs in ("CFS", "OFS"):
        r = da_from_secs(secs, fs=fs) if tagged else da_from_secs(secs)
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
        if (not corp_codes or r[0] in corp_codes) and (r[0], r[1]) not in have_q:
            try:
                secs = json.loads(ztext(r[3]) or "[]")
                y, rc = _report_key(r[2])
                if y and rc and _store_da(con, r[0], y, rc, secs, r[1], "latest"):
                    build_kpi(con, r[0], y, rc); k0 += 1
            except Exception as e:  # noqa
                log(con, r[0], "notes_v2", "fail", e)
    have_a = {(r[0], r[1]) for r in con.execute("SELECT corp_code, rcept_no FROM da_notes WHERE src='annual'")}
    for r in con.execute("SELECT corp_code, rcept_no, report_nm, secs_j FROM notes_annual WHERE secs_j IS NOT NULL"):
        if (not corp_codes or r[0] in corp_codes) and (r[0], r[1]) not in have_a:
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
    latest = {r[0]: (r[1], r[2]) for r in con.execute("SELECT corp_code, rcept_no, report_nm FROM notes_latest WHERE rcept_no IS NOT NULL AND notes_j IS NULL")}
    todo1 = [(cc, latest[cc][0], latest[cc][1], "latest") for cc in cs if cc in latest]
    # ② 연간: 사업보고서 변동표 (da_notes 11011 없는 회사)
    have_ann = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM da_notes WHERE reprt_code='11011'")} | {r[0] for r in con.execute("SELECT corp_code FROM notes_annual")}
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
                        con.execute("UPDATE notes_latest SET notes_j=?, notes_z=?, char_len=? WHERE corp_code=? AND rcept_no=?",
                                    (zpack(json.dumps(secs, ensure_ascii=False)), zpack(to_text(secs)), len(to_text(secs)), cc, rcept))
                        n += 1
                    else:
                        asset = [x for x in secs if re.search(r"유형자산|무형자산|사용권자산|투자부동산", x["t"].replace(" ", ""))]
                        con.execute("INSERT OR REPLACE INTO notes_annual VALUES (?,?,?,?,?)", (cc, rcept, nm, zpack(json.dumps(asset, ensure_ascii=False)), now()))
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
