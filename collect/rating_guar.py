"""신용평가서 PDF 본문에서 '타사 보증 기준 등급' 찾기 (2026-11-18)
배경: 사용자 지적 — NICE인프라 기업어음 A1은 신한은행 보증 기준 등급인데, 금감원 공시의 평가대상은 '기업어음'뿐이라
      build_site._guar(평가대상 이름의 '보증'·'신용보강' 등)로는 못 잡아 대표 등급으로 표시됐다. 보증 사실은 평가서 본문에만 있다.
방법: 단기(기업어음·전자단기사채) 평가서 중 회사·평가사별 최신 1건의 앞 3쪽 글자에서 보증·매입보장·신용공여 문구를 찾아
      rating_guar(file_url PK, hits, snip, fetched_at, ver)에 저장. PDF는 메모리에서만 처리(rating_valid와 같은 방식).
      판정(사이트 반영)은 표본 확인 뒤 build_site에서 — 이 모듈은 문구만 모은다.
  python run.py rawdump --type 보증스캔 [--limit N] [--codes ...]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, log, now, UA  # noqa: E402

VER = 1
KW = re.compile(r"지\s*급\s*보\s*증|보\s*증\s*기\s*관|보\s*증\s*인|보\s*증\s*처|매\s*입\s*보\s*장|매\s*입\s*약\s*정|신\s*용\s*공\s*여|신\s*용\s*보\s*강|유\s*동\s*성\s*공\s*여|"
                r"보\s*증\s*부|보\s*증\s*에\s*의|보\s*증\s*을\s*바\s*탕|보\s*증\s*(?:한|하는|하고)")


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS rating_guar (file_url TEXT PRIMARY KEY, corp_code TEXT, hits TEXT, snip TEXT, fetched_at TEXT, ver INTEGER)""")


def targets(con, corp_codes=None):
    ensure(con)
    rows = con.execute("""SELECT r.corp_code, r.agency, r.target_type, r.eval_kind, r.disc_dt, r.grade, r.file_url FROM credit_rating r
                          WHERE r.file_url IS NOT NULL AND r.disc_dt >= date('now','-3 years')
                          AND (r.target_type LIKE '%기업어음%' OR r.target_type LIKE '%단기사채%')
                          AND COALESCE(r.eval_kind,'') NOT LIKE '%정정%' AND COALESCE(r.grade,'') NOT IN ('취소','철회','WD','NR','-')
                          AND r.file_url NOT IN (SELECT file_url FROM rating_guar WHERE ver>=?)
                          ORDER BY r.corp_code, r.disc_dt DESC""", (VER,)).fetchall()
    seen, out = set(), []
    for r in rows:
        if corp_codes and r[0] not in corp_codes:
            continue
        if (r[0], r[1]) in seen:
            continue
        seen.add((r[0], r[1]))
        out.append(r)
    return out


def find(text):
    """→ (문구 목록, 앞뒤 문맥 최대 3개)"""
    t = re.sub(r"\s+", " ", text or "")
    hits, snips = [], []
    for m in KW.finditer(t):
        hits.append(re.sub(r"\s", "", m.group(0)))
        if len(snips) < 3:
            snips.append(t[max(0, m.start() - 70): m.end() + 70])
    return hits, snips


def scan(con=None, corp_codes=None, limit=None, workers=4):
    import requests
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from rating_valid import _pdf_texts
    con = con or connect()
    ensure(con)
    todo = targets(con, corp_codes)
    if limit:
        todo = todo[:limit]
    nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    print(f"[보증스캔] 대상 {len(todo)}건(단기 평가서, 회사·평가사별 최신)", flush=True)

    def work(r):
        data = requests.get(r[6], headers=UA, timeout=(15, 60)).content
        if data[:4] != b"%PDF":
            raise RuntimeError("PDF 아님")
        best = ([], [], "")
        for name, fn in _pdf_texts(data, pages=3):
            try:
                t = fn()
            except Exception:  # noqa
                continue
            h, s = find(t)
            if len(t or "") > 50 and (h or not best[2]):
                best = (h, s, name)
            if h:
                break
        return best

    n_hit = n_no = fail = 0
    kc = Counter()
    with ThreadPoolExecutor(workers) as ex:
        futs = [(r, ex.submit(work, r)) for r in todo]
        for i, (r, f) in enumerate(futs, 1):
            try:
                h, s, name = f.result()
                con.execute("INSERT OR REPLACE INTO rating_guar (file_url, corp_code, hits, snip, fetched_at, ver) VALUES (?,?,?,?,?,?)",
                            (r[6], r[0], ",".join(h), " ‖ ".join(s)[:900], now(), VER))
                if h:
                    n_hit += 1
                    kc.update(set(h))
                    print(f"[보증스캔] 문구 {nm.get(r[0], r[0])} | {r[1]} {r[2]} {r[5]} {r[4]} | {sorted(set(h))} | {(' ‖ '.join(s))[:420]}", flush=True)
                else:
                    n_no += 1
            except Exception as e:  # noqa
                fail += 1
                log(con, r[0], "rating_guar", "fail", e)
            if i % 50 == 0:
                con.commit()
                print(f"[보증스캔] {i}/{len(todo)} · 문구 있음 {n_hit} · 없음 {n_no} · 실패 {fail}", flush=True)
    con.commit()
    print(f"[보증스캔] 완료 {len(todo)}건 · 문구 있음 {n_hit} · 없음 {n_no} · 실패 {fail} · 문구별 {dict(kc.most_common())}", flush=True)
    return n_hit
