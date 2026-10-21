"""공통: 설정, DB 연결, DART 호출 래퍼"""
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "kosdaq.db"
DART_BASE = "https://opendart.fss.or.kr/api"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}


def api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key:
        cfg = ROOT / "config.json"
        if cfg.exists():
            key = json.loads(cfg.read_text(encoding="utf-8")).get("dart_api_key")
    if not key:
        raise SystemExit("DART_API_KEY 환경변수 또는 config.json(dart_api_key)이 필요합니다")
    return key


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")      # 로컬 디스크: 빠름
    except sqlite3.OperationalError:
        con.execute("PRAGMA journal_mode=DELETE")   # 네트워크/FUSE 마운트: WAL 불가
    con.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    migrations = [("company", "sect_tp TEXT"), ("company", "corp_name_full TEXT"), ("company", "parval REAL")] + \
                 [("price_summary", c) for c in ("chg_1w REAL", "chg_6m REAL", "chg_2y REAL", "chg_3y REAL")] + \
                 [("fin_kpi", c) for c in ("net_borrowings REAL", "da REAL", "ebitda REAL", "int_exp REAL",
                                           "interest_cover REAL", "borrow_dep REAL", "rcept_no TEXT",
                                           "cur_assets REAL", "cur_liab REAL", "receivables REAL", "inventory REAL", "capital REAL",
                                           "cur_ratio REAL", "recv_turn REAL", "inv_turn REAL", "nd_ebitda REAL",
                                           "payables REAL", "nwc REAL", "net_margin REAL", "borrow_op REAL",
                                           "contract_assets REAL", "contract_liab REAL", "advances REAL", "nwc_proj REAL")] + \
                 [("fin_raw", "rcept_no TEXT"), ("business", "rating_text TEXT"), ("business", "full_z BLOB"),
                  ("other_invest", "reprt_code TEXT"), ("other_invest", "rcept_no TEXT")]
    for tbl, col in migrations:                                   # 기존 DB 마이그레이션
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    _compress_business(con)
    return con


def zpack(text):
    """긴 원문 → zlib BLOB (DB·아티팩트 용량 1/4)"""
    import zlib
    return zlib.compress(text.encode("utf-8"), 6) if text else None


def ztext(v):
    """full_z(BLOB) 또는 예전 full_text(str) → str"""
    import zlib
    if v is None:
        return None
    if isinstance(v, (bytes, memoryview)):
        return zlib.decompress(bytes(v)).decode("utf-8")
    return v


def _compress_business(con):
    """1회성: 기존 business.full_text(평문) → full_z(zlib)로 옮기고 평문은 비움"""
    try:
        n = con.execute("SELECT COUNT(*) FROM business WHERE full_text IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        return
    if not n:
        return
    rows = con.execute("SELECT corp_code, bsns_year, full_text FROM business WHERE full_text IS NOT NULL").fetchall()
    con.executemany("UPDATE business SET full_z=?, full_text=NULL WHERE corp_code=? AND bsns_year=?",
                    [(zpack(r["full_text"]), r["corp_code"], r["bsns_year"]) for r in rows])
    con.commit()
    print(f"[db] 사업내용 원문 {n}건 압축 저장(full_text → full_z)", flush=True)


def log(con, corp_code, job, status, msg=""):
    con.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)", (corp_code, job, status, str(msg)[:500], now()))
    con.commit()


_last_call = 0.0


QUOTA_HIT = False   # DART 일일 한도 도달 여부(catchup 체인 중단용)


def dart_get(endpoint: str, retry: int = 3, **params):
    """DART JSON API 호출. status 000 이외는 예외. 초당 호출 간격 유지."""
    global _last_call
    params = {"crtfc_key": api_key(), **params}
    for i in range(retry):
        gap = 0.2 - (time.time() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.time()
        r = requests.get(f"{DART_BASE}/{endpoint}.json", params=params, headers=UA, timeout=30)
        if r.status_code != 200:
            time.sleep(2 * (i + 1))
            continue
        js = r.json()
        st = js.get("status")
        if st == "000":
            return js
        if st == "013":          # 조회 데이터 없음
            return {"status": "013", "list": []}
        if st in ("020", "021"):  # 한도 초과
            global QUOTA_HIT
            QUOTA_HIT = True
            raise RuntimeError(f"DART 한도 초과: {js.get('message')}")
        raise RuntimeError(f"DART {endpoint} 오류 {st}: {js.get('message')}")
    raise RuntimeError(f"DART {endpoint} HTTP 실패")


def dart_get_binary(endpoint: str, **params) -> bytes:
    params = {"crtfc_key": api_key(), **params}
    r = requests.get(f"{DART_BASE}/{endpoint}.xml", params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return r.content


def to_num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None
