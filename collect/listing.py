"""코스닥 상장사 목록(KRX API) + DART corp_code 매핑 + 기업개황(company)"""
import io
import zipfile
import xml.etree.ElementTree as ET

from common import connect, dart_get, dart_get_binary, log, now
from krx import base_info


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


def upsert_company(con, corp_code, stock_code, base):
    js = dart_get("company", corp_code=corp_code)
    con.execute("""INSERT INTO company (corp_code, stock_code, corp_name, market, sector_krx, induty_code,
        ceo_nm, est_dt, listed_dt, acc_mt, adres, hm_url, phn_no, jurir_no, bizr_no, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corp_code) DO UPDATE SET corp_name=excluded.corp_name, market=excluded.market,
        sector_krx=excluded.sector_krx, induty_code=excluded.induty_code, ceo_nm=excluded.ceo_nm,
        est_dt=excluded.est_dt, listed_dt=excluded.listed_dt, acc_mt=excluded.acc_mt, adres=excluded.adres,
        hm_url=excluded.hm_url, phn_no=excluded.phn_no, jurir_no=excluded.jurir_no, bizr_no=excluded.bizr_no,
        updated_at=excluded.updated_at""",
        (corp_code, stock_code, js.get("corp_name") or base["corp_name"], base.get("market"),
         base.get("sector_krx"), js.get("induty_code"), js.get("ceo_nm"), js.get("est_dt"),
         base.get("listed_dt"), js.get("acc_mt"), js.get("adres"), js.get("hm_url"), js.get("phn_no"),
         js.get("jurir_no"), js.get("bizr_no"), now()))
    con.commit()


def run(limit=None, market="KOSDAQ"):
    con = connect()
    rows = base_info(market)
    rows.sort(key=lambda r: r["corp_name"])
    if limit:
        rows = rows[:limit]
    codes = dart_corp_codes()
    for r in rows:
        cc = codes.get(r["stock_code"])
        if not cc:
            log(con, r["stock_code"], "listing", "skip", "DART corp_code 없음")
            continue
        try:
            upsert_company(con, cc, r["stock_code"], {**r, "market": market})
            log(con, cc, "listing", "ok")
        except Exception as e:  # noqa
            log(con, cc, "listing", "fail", e)
    return con


if __name__ == "__main__":
    run(limit=5)
