"""상장사(코스닥·코스피·코넥스) 목록(KRX API) + DART corp_code 매핑 + 기업개황(company)"""
import io
import zipfile
import xml.etree.ElementTree as ET

from common import connect, dart_get, dart_get_binary, log, now
from krx import base_info
from ksic import ksic_name


def dart_corp_codes():
    """corpCode.xml → {stock_code: corp_code}. 상장사만."""
    raw = dart_get_binary("corpCode")
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for e in root.iter("list"):
        sc = (e.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = e.findtext("corp_code")
    return m


def upsert_company(con, corp_code, stock_code, base, js=None):
    js = js or dart_get("company", corp_code=corp_code)
    name = base.get("corp_name") or js.get("stock_name") or js.get("corp_name")   # KRX 약식명 우선
    con.execute("""INSERT INTO company (corp_code, stock_code, corp_name, market, sector_krx, induty_code,
        ceo_nm, est_dt, listed_dt, acc_mt, adres, hm_url, phn_no, jurir_no, bizr_no, updated_at, sect_tp, corp_name_full, parval)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corp_code) DO UPDATE SET corp_name=excluded.corp_name, market=excluded.market,
        sector_krx=excluded.sector_krx, induty_code=excluded.induty_code, ceo_nm=excluded.ceo_nm,
        est_dt=excluded.est_dt, listed_dt=excluded.listed_dt, acc_mt=excluded.acc_mt, adres=excluded.adres,
        hm_url=excluded.hm_url, phn_no=excluded.phn_no, jurir_no=excluded.jurir_no, bizr_no=excluded.bizr_no,
        updated_at=excluded.updated_at, sect_tp=excluded.sect_tp, corp_name_full=excluded.corp_name_full,
        parval=COALESCE(excluded.parval, company.parval)""",
        (corp_code, stock_code, name, base.get("market"),
         ksic_name(js.get("induty_code")), js.get("induty_code"), js.get("ceo_nm"), js.get("est_dt"),
         base.get("listed_dt"), js.get("acc_mt"), js.get("adres"), js.get("hm_url"), js.get("phn_no"),
         js.get("jurir_no"), js.get("bizr_no"), now(), base.get("sect_tp"), js.get("corp_name"), base.get("parval")))
    con.commit()


CORP_CLS = {"KOSDAQ": "K", "KOSPI": "Y", "KONEX": "N"}


def run(limit=None, market="KOSDAQ", skip_existing=False):
    """1) KRX 종목기본정보로 시장 목록 확보 → DART 개황 보강.
       2) KRX 실패(미승인 등) 시 DART만으로: corpCode 전체 상장사 → company.corp_cls로 시장 필터.
       skip_existing: 이미 company에 있는 종목은 DART 개황 호출 생략(신규 상장분만)."""
    con = connect()
    codes = dart_corp_codes()
    existing = {r[0] for r in con.execute("SELECT stock_code FROM company")} if skip_existing else set()
    try:
        rows = base_info(market)
        rows.sort(key=lambda r: r["corp_name"])
        log(con, "-", "krx_listing", "ok", f"{len(rows)} rows")
        krx_ok = True
    except Exception as e:  # noqa
        log(con, "-", "krx_listing", "fail", f"KRX 실패 → DART corp_cls 필터로 대체: {e}")
        rows = [{"stock_code": sc, "corp_name": "", "sect_tp": None, "listed_dt": None} for sc in sorted(codes)]
        krx_ok = False
    if limit:
        rows = rows[:limit] if krx_ok else rows  # DART 경로는 전체를 훑어야 코스닥만 걸러짐
    n = 0
    print(f"[init] {market} 대상 {len(rows)}개 (KRX {'ok' if krx_ok else 'fail→DART'})", flush=True)
    if skip_existing and krx_ok:   # 이미 있는 종목의 시장 이전(코스닥→코스피 등)·종목명 변경만 반영 (DART 호출 없음)
        moved = 0
        for r in rows:
            if r["stock_code"] in existing:
                cur = con.execute("UPDATE company SET market=?, corp_name=COALESCE(NULLIF(?,''), corp_name), updated_at=? WHERE stock_code=? AND (market IS NULL OR market<>? OR corp_name<>COALESCE(NULLIF(?,''), corp_name))",
                                  (market, r.get("corp_name"), now(), r["stock_code"], market, r.get("corp_name")))
                moved += cur.rowcount
        if moved:
            con.commit(); print(f"[init] {market} 기존 종목 시장/명칭 갱신 {moved}건", flush=True)
    for i, r in enumerate(rows, 1):
        if i % 100 == 0:
            print(f"[init] {i}/{len(rows)} 처리, 적재 {n}", flush=True)
        cc = codes.get(r["stock_code"])
        if r["stock_code"] in existing:
            continue
        if not cc:
            log(con, r["stock_code"], "listing", "skip", "DART corp_code 없음")
            continue
        try:
            js = dart_get("company", corp_code=cc)
            if not krx_ok and js.get("corp_cls") != CORP_CLS[market]:
                continue
            upsert_company(con, cc, r["stock_code"], {**r, "market": market}, js)
            n += 1
            log(con, cc, "listing", "ok")
            if limit and not krx_ok and n >= limit:
                break
        except Exception as e:  # noqa
            log(con, cc, "listing", "fail", e)
    return con


if __name__ == "__main__":
    run(limit=5)
