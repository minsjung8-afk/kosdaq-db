"""KRX Open API(data.krx.co.kr) 래퍼.
- 인증: 헤더 AUTH_KEY. 환경변수 KRX_API_KEY 또는 config.json(krx_api_key)
- 특성: 모든 조회가 '기준일(basDd) 1일 × 전종목' 형태. 종목별 히스토리 API는 없음.
  → 1년치 시세 = 거래일 수(약 250회) 호출로 전시장 적재.
필드명은 KRX 공식 명세 기준이며, 명세 개정 시 FIELD_* 매핑만 수정."""
import json
import os
import time
from datetime import date, timedelta

import requests

from common import ROOT, UA

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"

# 코스닥 종목기본정보 / 일별매매정보 (시장 바꾸면 stk_* 코스피, knx_* 코넥스)
EP_BASE_INFO = {"KOSDAQ": "sto/ksq_isu_base_info", "KOSPI": "sto/stk_isu_base_info", "KONEX": "sto/knx_isu_base_info"}
EP_DAILY = {"KOSDAQ": "sto/ksq_bydd_trd", "KOSPI": "sto/stk_bydd_trd", "KONEX": "sto/knx_bydd_trd"}

FIELD_BASE = {"stock_code": "ISU_SRT_CD", "corp_name": "ISU_ABBRV", "full_name": "ISU_NM", "listed_dt": "LIST_DD",
              "sect_tp": "SECT_TP_NM", "sec_group": "SECUGRP_NM", "stock_type": "KIND_STKCERT_TP_NM",
              "shares": "LIST_SHRS", "parval": "PARVAL"}
FIELD_DAILY = {"stock_code": "ISU_CD", "dt": "BAS_DD", "open": "TDD_OPNPRC", "high": "TDD_HGPRC", "low": "TDD_LWPRC",
               "close": "TDD_CLSPRC", "volume": "ACC_TRDVOL", "value": "ACC_TRDVAL", "mcap": "MKTCAP", "shares": "LIST_SHRS"}


def krx_key() -> str:
    key = os.environ.get("KRX_API_KEY")
    if not key:
        cfg = ROOT / "config.json"
        if cfg.exists():
            key = json.loads(cfg.read_text(encoding="utf-8")).get("krx_api_key")
    if not key:
        raise SystemExit("KRX_API_KEY 환경변수 또는 config.json(krx_api_key)이 필요합니다")
    return key


def krx_get(endpoint: str, bas_dd: str, retry: int = 3) -> list:
    for i in range(retry):
        r = requests.get(f"{KRX_BASE}/{endpoint}", params={"basDd": bas_dd},
                         headers={**UA, "AUTH_KEY": krx_key()}, timeout=60)
        if r.status_code == 200:
            js = r.json()
            # 응답 루트 키는 OutBlock_1. 다른 키로 오면 첫 list 값 사용
            if "OutBlock_1" in js:
                return js["OutBlock_1"]
            for v in js.values():
                if isinstance(v, list):
                    return v
            return []
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"KRX {endpoint} {bas_dd} HTTP {r.status_code}: {r.text[:200]}")


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def last_trading_day(market="KOSDAQ", max_back=10) -> str:
    """오늘부터 거슬러 올라가 데이터가 있는 첫 기준일(YYYYMMDD)"""
    d = date.today()
    for _ in range(max_back):
        s = d.strftime("%Y%m%d")
        if d.weekday() < 5 and krx_get(EP_DAILY[market], s):
            return s
        d -= timedelta(days=1)
    raise RuntimeError("최근 거래일을 찾지 못함")


def base_info(market="KOSDAQ", bas_dd=None) -> list:
    """상장 종목기본정보 → [{stock_code, corp_name, listed_dt, sector_krx, shares, ...}]. 보통주만."""
    bas_dd = bas_dd or last_trading_day(market)
    out = []
    for r in krx_get(EP_BASE_INFO[market], bas_dd):
        row = {k: r.get(v) for k, v in FIELD_BASE.items()}
        if row["stock_type"] and "보통주" not in row["stock_type"]:
            continue  # 우선주·리츠 등 제외(필요 시 완화)
        row["stock_code"] = str(row["stock_code"]).zfill(6)
        row["listed_dt"] = (row["listed_dt"] or "").replace("/", "").replace("-", "")
        row["shares"] = _num(row["shares"])
        row["parval"] = _num(row["parval"])
        out.append(row)
    return out


def daily_all(market="KOSDAQ", bas_dd=None) -> list:
    """기준일 전종목 시세 → [{stock_code, dt(YYYY-MM-DD), open, high, low, close, volume, value, mcap, shares}]"""
    bas_dd = bas_dd or last_trading_day(market)
    out = []
    for r in krx_get(EP_DAILY[market], bas_dd):
        row = {k: r.get(v) for k, v in FIELD_DAILY.items()}
        if not row["stock_code"]:
            continue
        row["stock_code"] = str(row["stock_code"]).zfill(6)
        d = str(row["dt"] or bas_dd).replace("/", "").replace("-", "")
        row["dt"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        for k in ("open", "high", "low", "close", "volume", "value", "mcap", "shares"):
            row[k] = _num(row[k])
        out.append(row)
    return out


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d.strftime("%Y%m%d")
        d += timedelta(days=1)
