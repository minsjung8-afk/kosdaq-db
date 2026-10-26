"""신용평가서 PDF에서 등급 유효기간 읽기 (2026-10-26)
배경: 사이트의 '유효기간 만료'는 공시일+1년 추정이었다. 기업어음·전단채는 평가서에 유효기간이 명시되고 6개월인 경우도 있다
(예: 씨제이포디플렉스 CP 6개월 → 1년 추정으로는 A3-가 살아 있는 것처럼 보임). 평가서 원문 앞부분에서 '유효기간' 날짜를 읽어 저장한다.
  rating_valid(file_url PK, valid_to 'YYYY-MM-DD' | NULL, snip, fetched_at)  — 못 찾으면 valid_to NULL(재시도 안 함)
  python run.py rating_valid [--codes ...] [--limit N]
PDF는 메모리에서만 처리(저장 안 함). 대상: 최근 3년 공시 중 회사·평가사·장단기별 최신 1건(정정·취소 제외).
"""
import io
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, log, now, UA  # noqa: E402

DATE = r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?"


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS rating_valid (file_url TEXT PRIMARY KEY, valid_to TEXT, snip TEXT, fetched_at TEXT)""")


def _d(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def extract_valid_to(text, disc_dt=None):
    """평가서 텍스트 → (유효기간 종료일 'YYYY-MM-DD' | None, 근거 문구).
    ① '유효기간' 뒤 120자 안의 날짜 중 가장 늦은 날짜(시작~종료가 함께 적힌 경우 종료일) ② 'YYYY.MM.DD까지 유효' 문구.
    공시일보다 이르거나 3년 넘게 뒤인 날짜는 버림(평가일·발행일 등 오인 방지)."""
    t = re.sub(r"[ \t　]+", " ", text or "")
    base = None
    try:
        base = datetime.strptime((disc_dt or "")[:10], "%Y-%m-%d").date()
    except Exception:  # noqa
        pass

    def ok(dd):
        return dd and (not base or (dd >= base.replace(day=1) and (dd - base).days <= 3 * 366))
    for m in re.finditer(r"유\s*효\s*기\s*간", t):
        w = t[m.end(): m.end() + 120]
        ds = [x for x in (_d(*g) for g in re.findall(DATE, w)) if ok(x)]
        if ds:
            return max(ds).isoformat(), re.sub(r"\s+", " ", t[m.start(): m.end() + 80]).strip()[:160]
    for m in re.finditer(DATE + r"\s*(까지|이내)\s*(유효|적용)", t):
        dd = _d(m.group(1), m.group(2), m.group(3))
        if ok(dd):
            return dd.isoformat(), re.sub(r"\s+", " ", t[max(0, m.start() - 40): m.end() + 10]).strip()[:160]
    return None, None


def _pdf_text(data, pages=5):
    from pypdf import PdfReader  # noqa
    rd = PdfReader(io.BytesIO(data))
    return "\n".join((rd.pages[i].extract_text() or "") for i in range(min(len(rd.pages), pages)))


def targets(con, corp_codes=None):
    ensure(con)
    rows = con.execute("""SELECT r.corp_code, r.agency, r.target_type, r.eval_kind, r.disc_dt, r.grade, r.file_url FROM credit_rating r
                          WHERE r.file_url IS NOT NULL AND r.disc_dt >= date('now','-3 years')
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%정정%' AND COALESCE(r.grade,'') NOT IN ('취소','철회','WD','NR','-')
                          AND r.file_url NOT IN (SELECT file_url FROM rating_valid)
                          ORDER BY r.corp_code, r.disc_dt DESC""").fetchall()
    seen, out = set(), []
    for r in rows:
        if corp_codes and r[0] not in corp_codes:
            continue
        t = r[2] or ""
        kind = "L" if (re.search(r"기업신용평가|사채", t) and "단기사채" not in t) else "S"
        key = (r[0], r[1], kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def run(corp_codes=None, limit=None, con=None, workers=4):
    import requests
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    ensure(con)
    todo = targets(con, corp_codes)
    if limit:
        todo = todo[:limit]
    print(f"[rating_valid] 대상 {len(todo)}건(회사·평가사·장단기별 최신 평가서)", flush=True)

    def work(r):
        data = requests.get(r[6], headers=UA, timeout=(15, 60)).content
        if data[:4] != b"%PDF":
            raise RuntimeError("PDF 아님")
        return extract_valid_to(_pdf_text(data), r[4])

    got = miss = fail = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = [(r, ex.submit(work, r)) for r in todo]
        for i, (r, f) in enumerate(futs, 1):
            try:
                vt, snip = f.result()
                con.execute("INSERT OR REPLACE INTO rating_valid VALUES (?,?,?,?)", (r[6], vt, snip, now()))
                if vt:
                    got += 1
                else:
                    miss += 1
                if i <= 15 or (vt is None and miss <= 10):       # 처음 몇 건과 못 찾은 건은 로그로(패턴 보완용)
                    print(f"  {r[1]} {r[2]} {r[5]} {r[4]} → {vt or '못 찾음'} | {snip or ''}", flush=True)
            except Exception as e:  # noqa
                fail += 1
                log(con, r[0], "rating_valid", "fail", e)
            if i % 50 == 0:
                con.commit()
                print(f"[rating_valid] {i}/{len(todo)} · 찾음 {got} · 못 찾음 {miss} · 실패 {fail}", flush=True)
    con.commit()
    print(f"[rating_valid] 완료 {len(todo)}건 · 유효기간 찾음 {got} · 못 찾음 {miss} · 실패 {fail}", flush=True)
    return got
