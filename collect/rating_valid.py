"""신용평가서 PDF에서 등급 유효기간 읽기 (2026-10-26)
배경: 사이트의 '유효기간 만료'는 공시일+1년 추정이었다. 기업어음·전단채는 평가서에 유효기간이 명시되고 6개월인 경우도 있다
(예: 씨제이포디플렉스 CP 6개월 → 1년 추정으로는 A3-가 살아 있는 것처럼 보임). 평가서 원문 앞부분에서 '유효기간' 날짜를 읽어 저장한다.
  rating_valid(file_url PK, valid_to 'YYYY-MM-DD' | NULL, snip, fetched_at, ver)  — 못 찾으면 valid_to NULL(판독 규칙 VER이 오르면 다시 시도)
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
    if "ver" not in [r[1] for r in con.execute("PRAGMA table_info(rating_valid)")]:
        con.execute("ALTER TABLE rating_valid ADD COLUMN ver INTEGER DEFAULT 1")


def _d(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


VER = 3      # 2026-10-27 v3: pypdf로 못 찾으면 PyMuPDF·pdfminer 글자 추출로 다시 시도(추출기마다 글자 순서가 달라 한신평·한기평 CP 일부 누락) / 2026-10-26 v2: 나이스 '등급유효기한'·숫자 사이 공백('202 6 .6.30'), 한신평 '값 먼저·제목 나중' 표 배치 대응


def extract_valid_to(text, disc_dt=None):
    """평가서 텍스트 → (유효기간 종료일 'YYYY-MM-DD' | None, 근거 문구). 판단 순서:
    ① '유효기간'·'유효기한' 뒤 160자(공백 제거 후)에 날짜가 있으면 그중 가장 늦은 날짜(한기평 '등 급 유 효 기 간 2026년 06월 30일', 나이스 '등급유효기한 2026.6.30')
    ② 제목만 모아 둔 표('재무제표기준일 평가일 유효기간 용도' — 값은 제목보다 앞에 나옴, 한신평)면 제목 앞 300자의 날짜 중 가장 늦은 날짜
    ③ 'YYYY.MM.DD까지 유효' 문구
    공시일 달보다 이르거나 3년 넘게 뒤인 날짜는 버림(평가일·발행일·만기 오인 방지)."""
    t = re.sub(r"[ \t　]+", " ", text or "")
    base = None
    try:
        base = datetime.strptime((disc_dt or "")[:10], "%Y-%m-%d").date()
    except Exception:  # noqa
        pass

    def ok(dd):
        return dd and (not base or (dd >= base.replace(day=1) and (dd - base).days <= 3 * 366))

    def dates(w):
        w = re.sub(r"\s+", "", w)                       # 나이스 PDF는 숫자 사이에도 공백('202 6 .6.30')
        return [x for x in (_d(*g) for g in re.findall(DATE, w)) if ok(x)]

    def snip(a, b):
        return re.sub(r"\s+", " ", t[max(0, a): b]).strip()[:160]
    for m in re.finditer(r"유\s*효\s*기\s*[간한]", t):
        ds = dates(t[m.end(): m.end() + 160])
        if ds:
            return max(ds).isoformat(), snip(m.start(), m.end() + 80)
        around = re.sub(r"\s+", "", t[max(0, m.start() - 40): m.end() + 30])
        if "평가일" in around and "용도" in around:          # 제목 묶음 → 값은 앞에
            ds = dates(t[max(0, m.start() - 300): m.start()])
            if ds:
                return max(ds).isoformat(), snip(m.start() - 160, m.end())
    for m in re.finditer(DATE + r"\s*(까지|이내)\s*(유효|적용)", t):
        dd = _d(m.group(1), m.group(2), m.group(3))
        if ok(dd):
            return dd.isoformat(), snip(m.start() - 40, m.end() + 10)
    return None, None


def _pdf_text(data, pages=5):
    from pypdf import PdfReader  # noqa
    rd = PdfReader(io.BytesIO(data))
    return "\n".join((rd.pages[i].extract_text() or "") for i in range(min(len(rd.pages), pages)))


def _pdf_texts(data, pages=5):
    """글자 추출기 3종을 차례로(앞의 것에서 유효기간을 못 찾을 때만 다음 것) — pypdf → PyMuPDF → pdfminer"""
    yield "pypdf", lambda: _pdf_text(data, pages)
    def mu():
        import pymupdf  # noqa
        doc = pymupdf.open(stream=data, filetype="pdf")
        return "\n".join(doc[i].get_text() for i in range(min(len(doc), pages)))
    yield "pymupdf", mu
    def pm():
        from pdfminer.high_level import extract_text  # noqa
        return extract_text(io.BytesIO(data), maxpages=pages)
    yield "pdfminer", pm


def extract_from_pdf(data, disc_dt=None):
    """→ (valid_to, snip, 첫 추출기 텍스트 앞부분). 추출기 하나라도 날짜를 찾으면 그 결과."""
    head = None
    for name, fn in _pdf_texts(data):
        try:
            t = fn()
        except Exception:  # noqa
            continue
        if head is None:
            head = re.sub(r"\s+", " ", t or "")[:200]
        vt, snip = extract_valid_to(t, disc_dt)
        if vt:
            return vt, (snip if name == "pypdf" else f"[{name}] {snip}")[:160], head
    return None, None, head


def targets(con, corp_codes=None):
    ensure(con)
    rows = con.execute("""SELECT r.corp_code, r.agency, r.target_type, r.eval_kind, r.disc_dt, r.grade, r.file_url FROM credit_rating r
                          WHERE r.file_url IS NOT NULL AND r.disc_dt >= date('now','-3 years')
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%정정%' AND COALESCE(r.grade,'') NOT IN ('취소','철회','WD','NR','-')
                          AND r.file_url NOT IN (SELECT file_url FROM rating_valid WHERE valid_to IS NOT NULL OR COALESCE(ver,1)>=?)
                          ORDER BY r.corp_code, r.disc_dt DESC""", (VER,)).fetchall()
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
        return extract_from_pdf(data, r[4])

    got = miss = fail = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = [(r, ex.submit(work, r)) for r in todo]
        for i, (r, f) in enumerate(futs, 1):
            try:
                vt, snip, head = f.result()
                con.execute("INSERT OR REPLACE INTO rating_valid (file_url, valid_to, snip, fetched_at, ver) VALUES (?,?,?,?,?)", (r[6], vt, snip, now(), VER))
                if vt:
                    got += 1
                else:
                    miss += 1
                if i <= 15 or (vt is None and miss <= 10):       # 처음 몇 건과 못 찾은 건은 로그로(패턴 보완용)
                    print(f"  {r[1]} {r[2]} {r[5]} {r[4]} → {vt or '못 찾음'} | {snip or ('앞부분: ' + (head or '(글자 없음 — 그림 PDF)'))}", flush=True)
            except Exception as e:  # noqa
                fail += 1
                log(con, r[0], "rating_valid", "fail", e)
            if i % 50 == 0:
                con.commit()
                print(f"[rating_valid] {i}/{len(todo)} · 찾음 {got} · 못 찾음 {miss} · 실패 {fail}", flush=True)
    con.commit()
    print(f"[rating_valid] 완료 {len(todo)}건 · 유효기간 찾음 {got} · 못 찾음 {miss} · 실패 {fail}", flush=True)
    return got
