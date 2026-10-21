#!/usr/bin/env python3
"""자택 Mac(국내 IP)에서 KRX 대용가 1콜 → JSON → R2 업로드(subst/latest.json, subst/YYYYMMDD.json).
GitHub Actions 러너는 해외 IP라 KRX가 400으로 막기 때문에 이 파일을 거쳐 간다.
필요: python3 + requests + boto3, ~/.kosdaq_r2.env (R2 키 + KRX_ID/KRX_PW: Data Marketplace 계정). launchd가 평일 06:50에 실행.
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


LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd?locale=ko_KR"
LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"


def login(s, env):
    """KRX Data Marketplace 로그인(전종목 대용가 조회는 로그인 세션 필요 — 미로그인 시 400 'LOGOUT'). 반환 True/False"""
    if not env.get("KRX_ID") or not env.get("KRX_PW"):
        return False
    s.get(LOGIN_PAGE, headers={"User-Agent": UA}, timeout=30)
    s.get("https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc", headers={"User-Agent": UA, "Referer": LOGIN_PAGE}, timeout=30)
    r = s.post(LOGIN_URL, data={"mbrId": env["KRX_ID"], "pw": env["KRX_PW"], "mbrNm": "", "telNo": "", "di": "", "certType": ""},
               headers={"User-Agent": UA, "Referer": LOGIN_PAGE, "Origin": "https://data.krx.co.kr", "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01"}, timeout=30)
    txt = r.text
    try:
        js = r.json()
    except Exception:
        js = {}
    ok = r.status_code == 200 and bool(js.get("MBR_NO")) and not js.get("errorCode") and not js.get("ERROR_CODE")
    if not ok or "--debug" in sys.argv:
        safe = {k: ("***" if any(x in k.lower() for x in ("pw", "tel", "nm", "email", "di")) else v) for k, v in (js.items() if isinstance(js, dict) else [])}
        print(f"[login] HTTP {r.status_code} ok={ok} keys={list(js.keys())[:12] if isinstance(js, dict) else '-'} body={str(safe)[:300] if js else txt[:200]}")
        print(f"[login] cookies={[c.name for c in s.cookies]}")
    return ok


def fetch(trd_dd, s=None):
    s = s or requests.Session()
    s.get(REF, headers={"User-Agent": UA}, timeout=30)
    if "--debug" in sys.argv:
        print(f"[fetch] cookies={[c.name for c in s.cookies]}")
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
    env = load_env()
    sess = requests.Session()
    if not login(sess, env):
        say("KRX 로그인 실패 또는 KRX_ID/KRX_PW 미설정(~/.kosdaq_r2.env) — 전종목 대용가는 로그인 필요"); return 3
    for back in range(0, 8):
        d = d0 - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            rows = fetch(d.strftime("%Y%m%d"), sess)
        except Exception as e:
            say(f"{d} 조회 실패: {e}"); continue
        if rows:
            used = d; break
    if not rows:
        say("대용가 자료 없음 — 종료"); return 1
    js = {"trd_dd": rows[0]["trd_dd"], "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(rows), "rows": rows}
    f = OUT / f"subst_{used.strftime('%Y%m%d')}.json"
    f.write_text(json.dumps(js, ensure_ascii=False))
    ep = f"https://{env['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    keys = ("subst/latest.json", f"subst/{used.strftime('%Y%m%d')}.json")
    try:                                                   # 1) boto3 (pip) — Homebrew/aws cli 없는 Mac
        import boto3
        from botocore.config import Config
        s3 = boto3.client("s3", endpoint_url=ep, aws_access_key_id=env["R2_ACCESS_KEY_ID"], aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                          region_name="auto", config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required"))
        for key in keys:
            s3.upload_file(str(f), env["R2_BUCKET"], key, ExtraArgs={"ContentType": "application/json"})
    except ImportError:                                    # 2) aws cli
        import shutil, glob
        aws = shutil.which("aws") or next(iter(glob.glob(str(Path.home() / "Library/Python/*/bin/aws"))), None)
        if not aws:
            say("boto3도 aws cli도 없음 — python3 -m pip install --user boto3"); return 2
        e = {**os.environ, "AWS_ACCESS_KEY_ID": env["R2_ACCESS_KEY_ID"], "AWS_SECRET_ACCESS_KEY": env["R2_SECRET_ACCESS_KEY"], "AWS_DEFAULT_REGION": "auto",
             "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}
        for key in keys:
            subprocess.run([aws, "s3", "cp", str(f), f"s3://{env['R2_BUCKET']}/{key}", "--endpoint-url", ep, "--only-show-errors"], check=True, env=e)
    say(f"OK 기준 {used} · {len(rows):,}종목 → R2 업로드")
    return 0


if __name__ == "__main__":
    sys.exit(main())
