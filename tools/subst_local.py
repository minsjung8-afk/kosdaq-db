#!/usr/bin/env python3
"""자택 Mac(국내 IP)에서 KRX 대용가 1콜 → JSON → R2 업로드(subst/latest.json, subst/YYYYMMDD.json).
GitHub Actions 러너는 해외 IP라 KRX가 400으로 막기 때문에 이 파일을 거쳐 간다.
필요: python3 + requests, aws cli, ~/.kosdaq_r2.env (R2 키). launchd가 평일 06:50에 실행.
"""
import json, os, subprocess, sys, time
from datetime import date, timedelta
from pathlib import Path

import requests

URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
BLD = "dbms/MDC/STAT/standard/MDCSTAT04001"
REF = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC020102050701"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ENV = Path.home() / ".kosdaq_r2.env"
OUT = Path.home() / "kosdaq_subst"
LOG = OUT / "subst_local.log"


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def fetch(trd_dd):
    s = requests.Session()
    s.get(REF, headers={"User-Agent": UA}, timeout=30)
    r = s.post(URL, data={"bld": BLD, "locale": "ko_KR", "searchType": "1", "mktId": "ALL", "trdDd": trd_dd, "csvxls_isNo": "false"},
               headers={"User-Agent": UA, "Referer": REF, "Origin": "https://data.krx.co.kr", "X-Requested-With": "XMLHttpRequest"}, timeout=60)
    r.raise_for_status()
    rows = []
    for x in r.json().get("block1", []):
        v = num(x.get("SB_PRC"))
        if x.get("ISU_SRT_CD") and v:
            rows.append({"stock_code": x["ISU_SRT_CD"], "sb_prc": v, "trd_dd": (x.get("TRD_DD") or "").replace("/", "-"),
                         "mk_date": (x.get("MK_DATE") or "").replace("/", "-"), "prev_close": num(x.get("PREVDD_CLSPRC"))})
    return rows


def load_env():
    env = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"')
    return env


def main():
    OUT.mkdir(exist_ok=True)
    log = open(LOG, "a")
    say = lambda m: (print(m), log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}\n"), log.flush())
    d0 = date.today()
    rows, used = [], None
    for back in range(0, 8):
        d = d0 - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            rows = fetch(d.strftime("%Y%m%d"))
        except Exception as e:
            say(f"{d} 조회 실패: {e}"); continue
        if rows:
            used = d; break
    if not rows:
        say("대용가 자료 없음 — 종료"); return 1
    js = {"trd_dd": rows[0]["trd_dd"], "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(rows), "rows": rows}
    f = OUT / f"subst_{used.strftime('%Y%m%d')}.json"
    f.write_text(json.dumps(js, ensure_ascii=False))
    env = load_env()
    ep = f"https://{env['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    e = {**os.environ, "AWS_ACCESS_KEY_ID": env["R2_ACCESS_KEY_ID"], "AWS_SECRET_ACCESS_KEY": env["R2_SECRET_ACCESS_KEY"], "AWS_DEFAULT_REGION": "auto",
         "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}
    for key in ("subst/latest.json", f"subst/{used.strftime('%Y%m%d')}.json"):
        subprocess.run(["aws", "s3", "cp", str(f), f"s3://{env['R2_BUCKET']}/{key}", "--endpoint-url", ep, "--only-show-errors"], check=True, env=e)
    say(f"OK 기준 {used} · {len(rows):,}종목 → R2 업로드")
    return 0


if __name__ == "__main__":
    sys.exit(main())
