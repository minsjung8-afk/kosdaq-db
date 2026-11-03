"""상각비(D&A) 보강: DART '재무제표 원본파일(XBRL)'(fnlttXbrl, 1콜/보고서)에서 주석까지 포함된 XBRL 인스턴스를 읽어
감가상각비·무형자산상각비·사용권자산상각비를 뽑는다. 원본 zip은 메모리에서만 처리하고 저장하지 않는다(R2 무료 한도).

왜: 본문 재무제표 API(fnlttSinglAcntAll)는 현금흐름표가 '영업에서 창출된 현금' 한 줄이면 상각비가 없다
    (주석 '영업활동현금흐름 조정'·'비용의 성격별 분류'에만 있음) → EBITDA 산출 불가 회사가 약 30%.

- 대상: fin_kpi에서 da가 비어 있는 (회사, 연도, 보고서) 중 최신 연간 + 최신 분기(누적). 접수번호는 fin_raw.rcept_no.
- 결과: da_xbrl 테이블(dep, amort, rou, da, 근거 태그) → build_kpi가 본문에서 못 찾으면 이 값을 씀(재계산해도 유지).
- 기간: 보고서 기간 말일로 끝나는 '가장 긴' 기간(연간=1년, 분기=누적) · 연결/별도는 ConsolidatedAndSeparateFinancialStatementsAxis로 구분
  python run.py da [--limit N] [--codes 011170 ...] [--debug]
"""
import io
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import DART_BASE, UA, api_key, connect, log, now  # noqa: E402

# 우선순위: 합산 태그 → (감가 + 무형 + 사용권) 합
COMBINED = ["AdjustmentsForDepreciationAndAmortisationExpense", "DepreciationAndAmortisationExpense",
            "AdjustmentsForDepreciationAndAmortisationExpenseAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"]
DEP = ["AdjustmentsForDepreciationExpense", "DepreciationExpense", "DepreciationPropertyPlantAndEquipment",
       "AdjustmentsForDepreciationPropertyPlantAndEquipment"]
AMO = ["AdjustmentsForAmortisationExpense", "AmortisationExpense", "AmortisationIntangibleAssetsOtherThanGoodwill",
       "AdjustmentsForAmortisationIntangibleAssetsOtherThanGoodwill"]
ROU = ["DepreciationRightofuseAssets", "AdjustmentsForDepreciationRightofuseAssets", "DepreciationOfRightofuseAssets"]
INVP = ["DepreciationInvestmentProperty", "AdjustmentsForDepreciationInvestmentProperty"]
EXCL = re.compile(r"Accumulated|Impairment|Reversal|Gain|Loss|Discount|Premium|Bond|Deferred|Tax|Allowance|Ratio", re.I)


def _fetch_zip(rcept_no, reprt_code):
    """HTTP만(스레드 안전). 반환: 인스턴스 XML bytes 또는 None. DART 오류(한도 등)는 예외."""
    with common._LOCK:
        common.CALLS += 1
    r = requests.get(f"{DART_BASE}/fnlttXbrl.xml", params={"crtfc_key": api_key(), "rcept_no": rcept_no, "reprt_code": reprt_code},
                     headers=UA, timeout=90)
    r.raise_for_status()
    b = r.content
    if not b.startswith(b"PK"):                     # zip이 아니면 오류 XML(<status>…)
        txt = b[:600].decode("utf-8", "ignore")
        st = re.search(r"<status>(\d+)</status>", txt)
        if st and st.group(1) in ("020", "021"):
            common.QUOTA_HIT = True
            raise RuntimeError("DART 한도 초과")
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    names = [n for n in z.namelist() if n.lower().endswith((".xbrl", ".xml")) and not re.search(r"(_lab|_pre|_cal|_def|_ref)[-_.]", n.lower())]
    names.sort(key=lambda n: (not n.lower().endswith(".xbrl"), -z.getinfo(n).file_size))
    return z.read(names[0]) if names else None


def _contexts(root):
    """id → (start, end, member) — member: 'C'(연결)/'S'(별도)/None(구분 없음)/'X'(그 외 차원: 주석 세부 → 제외)"""
    out = {}
    for c in root.iter():
        if not c.tag.endswith("}context"):
            continue
        cid = c.get("id")
        st = en = None
        mem, other = None, False
        for e in c.iter():
            t = e.tag.split("}")[-1]
            if t == "startDate":
                st = (e.text or "").strip()
            elif t == "endDate":
                en = (e.text or "").strip()
            elif t == "instant":
                st = en = (e.text or "").strip()
            elif t == "explicitMember":
                dim, val = e.get("dimension") or "", (e.text or "")
                if "ConsolidatedAndSeparate" in dim:
                    mem = "S" if "Separate" in val else "C"
                else:
                    other = True
            elif t == "typedMember":
                other = True
        out[cid] = (st, en, "X" if other else mem)
    return out


def parse_da(xml_bytes, want_fs="CFS", debug=False):
    """XBRL 인스턴스 → {'dep','amort','rou','da','tag','period'} (당기 누적, 원 단위 절대값)"""
    root = ET.fromstring(xml_bytes)
    ctx = _contexts(root)
    want = "C" if want_fs == "CFS" else "S"
    facts = {}                                       # local → [(start,end,mem,val)]
    for e in root.iter():
        tag = e.tag
        if "}" not in tag:
            continue
        local = tag.split("}")[-1]
        if not re.search(r"Depreciation|Amorti[sz]ation", local) or EXCL.search(local.replace("Rightofuse", "")):
            continue
        c = ctx.get(e.get("contextRef"))
        if not c or c[2] == "X" or not c[0] or c[0] == c[1]:
            continue
        try:
            v = abs(float((e.text or "").strip()))
        except ValueError:
            continue
        facts.setdefault(local, []).append((c[0], c[1], c[2], v))
    if not facts:
        return None
    ends = [f[1] for fs in facts.values() for f in fs]
    pend = max(ends)                                 # 보고기간 말
    def pick(local):
        rows = [f for f in facts.get(local, []) if f[1] == pend]
        if not rows:
            return None
        pref = [f for f in rows if f[2] == want] or [f for f in rows if f[2] is None]
        if not pref:
            return None
        pref.sort(key=lambda f: f[0])                # 가장 이른 시작 = 누적(연간/분기 누적)
        return pref[0][3], pref[0][0]
    def first(names):
        for n in names:
            # 표준 태그 + 확장 태그(entity…_DepreciationExpense 등)는 같은 로컬명 꼬리로 매칭
            for local in facts:
                if local == n or local.endswith("_" + n):
                    p = pick(local)
                    if p:
                        return p[0], local, p[1]
        return None
    res = {"period": pend}
    comb = first(COMBINED)
    if comb:
        res.update(da=comb[0], dep=None, amort=None, rou=None, tag=comb[1], start=comb[2])
    else:
        d, a, r, ip = first(DEP), first(AMO), first(ROU), first(INVP)
        ppe = first(["DepreciationPropertyPlantAndEquipment", "AdjustmentsForDepreciationPropertyPlantAndEquipment"])
        chk = None
        # 2026-11-03 사용자 지적(SGC에너지 별도): '감가상각비'(DepreciationExpense 355.4억)가 유형자산분(355.4억)만이고 사용권 22.2억은 따로인데
        # 예전 규칙('사용권은 AdjustmentsFor 태그일 때만 더함')에 걸려 빠짐 → 감가상각비가 어떤 항목의 합인지 유형자산 감가상각비로 맞춰 보고 빠진 항목만 더함:
        #   감가 = 유형 → 사용권·투자부동산 더함 / 감가 = 유형+사용권 → 투자부동산만 / 감가 = 유형+투자부동산(SGC 연결 566.4 = 530.2+36.3) → 사용권만 …(0.5% 이내)
        if d and ppe:
            dv, pv = d[0], ppe[0]
            rv, iv = (r[0] if r else 0), (ip[0] if ip else 0)
            # 오차가 가장 작은 조합을 쓰되, 다른 조합도 0.5% 안에 들면 가장 작은 오차가 그 1/3 미만일 때만 인정(SGC 연결: 유형+투자부동산 오차 0.01억 vs 유형+사용권 2.0억)
            errs = sorted((abs(dv - (pv + incR * rv + incI * iv)) / dv, incR, incI) for incR, incI in ((0, 0), (1, 0), (0, 1), (1, 1))
                          if not ((incR and not rv) or (incI and not iv)))
            if errs and errs[0][0] <= 0.005 and (len(errs) < 2 or errs[1][0] > 0.005 or errs[0][0] < errs[1][0] / 3):
                _, incR, incI = errs[0]
                chk = ("P" + ("+R" if incR else "") + ("+I" if incI else ""))
                r = None if incR else r          # 이미 들어 있는 항목은 더하지 않음
                ip = None if incI else ip
        if chk is None:
            # 맞는 조합이 없으면(유형자산 감가상각비 미공시 등) 예전 규칙 유지: 사용권은 현금흐름 조정 태그로 따로 공시된 경우만 더함(이중계상 방지)
            if r and d and "AdjustmentsFor" not in r[1]:
                r = None
        if not d:
            # 표준 태그가 없으면 주석 '유형·무형자산 변동내역' 확장 태그(…Depreciation…/…Amortization…, 자산종류 구분 없는 합계 행)에서
            # 기간 말에 끝나는 누적값 중 '가장 큰 값'을 각각 채택(합계 행 = 최대) — 표가 여럿이면 이중계상 방지 위해 합산하지 않음
            def best(rx):
                c = []
                for local in facts:
                    if re.search(rx, local) and not re.search(r"Rightofuse|RightOfUse|InvestmentProperty|Goodwill", local, re.I):
                        p = pick(local)
                        if p:
                            c.append((p[0], local, p[1]))
                return max(c) if c else None
            d2, a2 = best(r"Depreciation"), (a or best(r"Amorti[sz]ation"))
            if d2:
                d, a, r, ip = d2, a2, None, None
        if not d:                     # 감가상각비를 못 찾으면(사용권·무형만 있는 경우 포함) 채택하지 않음 → 과소계상 방지
            if debug:
                print("   후보 태그:", sorted(facts)[:30], flush=True)
            return None
        res.update(dep=(d[0] if d else 0) + (ip[0] if ip else 0) or None, amort=a[0] if a else None, rou=r[0] if r else None,
                   tag="+".join(x[1] for x in (d, a, r, ip) if x) + (f"|감가={chk}" if chk else "|감가구성미확인"), start=(d or a or r)[2])
        res["da"] = sum(x for x in (res["dep"], res["amort"], res["rou"]) if x)
    if debug:
        print(f"   기간 {res.get('start')}~{pend} · 감가 {res.get('dep')} · 무형 {res.get('amort')} · 사용권 {res.get('rou')} · 합계 {res['da']} · 태그 {res['tag']}", flush=True)
    return res


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS da_xbrl (corp_code TEXT, bsns_year TEXT, reprt_code TEXT, fs_div TEXT,
                   dep REAL, amort REAL, rou REAL, da REAL, tag TEXT, rcept_no TEXT, status TEXT, fetched_at TEXT,
                   PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div))""")


def targets(con, corp_codes=None):
    """da가 빈 fin_kpi 중 회사별 최신 연간 1건 + 최신 분기 1건(주 재무제표 fs). 이미 시도한 건 제외."""
    ensure(con)
    rows = con.execute("""SELECT k.corp_code, k.bsns_year, k.reprt_code, k.fs_div,
                                 (SELECT r.rcept_no FROM fin_raw r WHERE r.corp_code=k.corp_code AND r.bsns_year=k.bsns_year AND r.reprt_code=k.reprt_code
                                   AND r.fs_div=k.fs_div AND r.rcept_no IS NOT NULL AND r.rcept_no NOT LIKE '%:전기' LIMIT 1) rc
                          FROM fin_kpi k WHERE k.da IS NULL AND k.op_income IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM da_xbrl x WHERE x.corp_code=k.corp_code AND x.bsns_year=k.bsns_year AND x.reprt_code=k.reprt_code AND x.fs_div=k.fs_div)
                          ORDER BY k.corp_code, k.bsns_year DESC""").fetchall()
    pick = {}
    for r in rows:
        if not r["rc"] or (corp_codes and r["corp_code"] not in corp_codes):
            continue
        kind = "A" if r["reprt_code"] == "11011" else "Q"
        key = (r["corp_code"], kind)
        cur = pick.get(key)
        order = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
        if not cur or (r["bsns_year"], order[r["reprt_code"]], r["fs_div"] == "CFS") > (cur["bsns_year"], order[cur["reprt_code"]], cur["fs_div"] == "CFS"):
            pick[key] = r
    return list(pick.values())


def _fetch_parse(rc, reprt_code, fs_div, debug=False):
    """워커 스레드: 다운로드 + XML 파싱까지(메인 스레드는 DB 기록만) → 2026-10-23 속도 개선(15초/건 → 목표 5초/건)
    반환: (xbrl 존재 여부, {fs_div: 결과}) — 같은 XBRL에 연결·별도가 모두 들어 있어 한 번 내려받아 둘 다 파싱(다운로드 절반)"""
    xml = _fetch_zip(rc, reprt_code)
    if not xml:
        return False, {}
    out = {}
    for fs in ("CFS", "OFS"):
        try:
            out[fs] = parse_da(xml, fs, debug=debug and fs == fs_div)
        except Exception:  # noqa
            out[fs] = None
    return True, out


def run(corp_codes=None, limit=None, con=None, workers=8, debug=False):
    from concurrent.futures import ThreadPoolExecutor
    from financials import build_kpi
    con = con or connect()
    ensure(con)
    # 정정(2026-10-22 v2): 사용권자산상각비만 잡힌 과소 결과 삭제·재계산, 표준 태그 없던 건은 변동내역 태그 보강 로직으로 1회 재시도
    bad = con.execute("""SELECT corp_code, bsns_year, reprt_code FROM da_xbrl WHERE status='ok' AND dep IS NULL
                         AND tag NOT LIKE '%DepreciationAndAmortisation%'""").fetchall()
    con.execute("""DELETE FROM da_xbrl WHERE (status='ok' AND dep IS NULL AND tag NOT LIKE '%DepreciationAndAmortisation%')
                   OR (status='notag' AND fetched_at < '2026-10-22 11:45')""")
    for b in bad:
        build_kpi(con, b[0], b[1], b[2])
    con.commit()
    ts = targets(con, set(corp_codes) if corp_codes else None)
    try:   # 우선순위: 신용등급 보유 → 코스피 → 코스닥
        rated = {r[0] for r in con.execute("SELECT DISTINCT corp_code FROM credit_rating")}
        mk = {r[0]: r[1] for r in con.execute("SELECT corp_code, market FROM company")}
        ts.sort(key=lambda r: (r["corp_code"] not in rated, {"KOSPI": 1, "KOSDAQ": 2}.get(mk.get(r["corp_code"]), 3), r["reprt_code"] != "11011"))
    except Exception:  # noqa
        pass
    if limit:
        ts = ts[:limit]
    print(f"[da] XBRL 상각비 대상 {len(ts)}건(연간·최신분기) · 병렬 {workers}", flush=True)
    ok = i = 0
    c0 = common.CALLS
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(ts), workers * 5):
            batch = ts[b:b + workers * 5]
            futs = [(r, ex.submit(_fetch_parse, r["rc"], r["reprt_code"], r["fs_div"], debug)) for r in batch]
            stop = False
            for r, f in futs:
                i += 1
                key = (r["corp_code"], r["bsns_year"], r["reprt_code"], r["fs_div"])
                try:
                    xml, both = f.result()         # xml: XBRL 존재 여부(bool), both: {CFS: 결과, OFS: 결과}
                    res = both.get(r["fs_div"])
                    other = "OFS" if r["fs_div"] == "CFS" else "CFS"
                    if xml and con.execute("SELECT 1 FROM fin_kpi WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=? AND da IS NULL",
                                           (r["corp_code"], r["bsns_year"], r["reprt_code"], other)).fetchone():   # 반대편 재무제표도 같은 보고서면 함께 기록
                        o = both.get(other)
                        con.execute("INSERT OR REPLACE INTO da_xbrl VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (r["corp_code"], r["bsns_year"], r["reprt_code"], other, *((o["dep"], o["amort"], o["rou"], o["da"], o["tag"]) if o else (None,) * 5),
                                     r["rc"], "ok" if o else "notag", now()))
                        if o:
                            ok += 1
                            build_kpi(con, r["corp_code"], r["bsns_year"], r["reprt_code"])
                    if debug:
                        print(f"  {r['corp_code']} {r['bsns_year']} {r['reprt_code']} {r['fs_div']} rcept {r['rc']} → {'OK' if res else '없음'}", flush=True)
                    con.execute("INSERT OR REPLACE INTO da_xbrl VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (*key, *( (res["dep"], res["amort"], res["rou"], res["da"], res["tag"]) if res else (None,) * 5),
                                 r["rc"], "ok" if res else ("noxbrl" if not xml else "notag"), now()))
                    if res:
                        ok += 1
                        build_kpi(con, r["corp_code"], r["bsns_year"], r["reprt_code"])
                except Exception as e:  # noqa
                    log(con, r["corp_code"], "da_xbrl", "fail", e)
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
            con.commit()
            if i % 100 < workers * 5:
                print(f"[da] {i}/{len(ts)} · 확보 {ok} · DART {common.CALLS - c0}콜", flush=True)
            if stop:
                print(f"[da] DART 한도 도달 — {i}/{len(ts)}에서 중단(다음 실행에서 이어서)", flush=True)
                break
    print(f"[da] 완료 {i}/{len(ts)} · 상각비 확보 {ok} · DART {common.CALLS - c0}콜", flush=True)
    return ok


def redo_rou(con, limit=None, workers=8):
    """2026-11-03: 사용권자산·투자부동산 상각비 구성 확인 규칙 적용 전에 저장된 XBRL 결과(합산 태그 아닌 'ok' 행)를 다시 내려받아 재계산.
    같은 보고서의 연결·별도를 함께 갱신하고 KPI 재계산. 바뀐 행은 전·후 금액을 출력(검증용)."""
    from concurrent.futures import ThreadPoolExecutor
    from financials import build_kpi
    ensure(con)
    reps = con.execute("""SELECT rcept_no, reprt_code, MIN(corp_code), MIN(bsns_year) FROM da_xbrl
                          WHERE status='ok' AND tag NOT LIKE '%DepreciationAndAmortisation%' AND tag NOT LIKE '%|감가%' AND rcept_no IS NOT NULL
                          GROUP BY rcept_no, reprt_code ORDER BY MIN(corp_code) <> (SELECT corp_code FROM company WHERE stock_code='005090'), MIN(corp_code)   -- SGC에너지(사용자 지적 사례) 먼저""").fetchall()
    if limit:
        reps = reps[:limit]
    nm = {r[0]: r[1] for r in con.execute("SELECT corp_code, corp_name FROM company")}
    print(f"[da-rou] 재계산 대상 보고서 {len(reps)}건 · 병렬 {workers}", flush=True)
    c0, chg, same, nomatch, done = common.CALLS, [], 0, 0, 0
    with ThreadPoolExecutor(workers) as ex:
        for b in range(0, len(reps), workers * 5):
            batch = reps[b:b + workers * 5]
            futs = [(rp, ex.submit(_fetch_parse, rp[0], rp[1], "CFS")) for rp in batch]
            stop = False
            for rp, f in futs:
                done += 1
                try:
                    _, both = f.result()
                except Exception as e:  # noqa
                    if common.QUOTA_HIT or "한도" in str(e):
                        stop = True
                    continue
                for old in con.execute("SELECT corp_code, bsns_year, reprt_code, fs_div, da, tag FROM da_xbrl WHERE rcept_no=? AND reprt_code=? AND status='ok'",
                                       (rp[0], rp[1])).fetchall():
                    o = both.get(old[3])
                    if not o:
                        continue
                    con.execute("UPDATE da_xbrl SET dep=?, amort=?, rou=?, da=?, tag=?, fetched_at=? WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?",
                                (o["dep"], o["amort"], o["rou"], o["da"], o["tag"], now(), old[0], old[1], old[2], old[3]))
                    if "미확인" in (o["tag"] or ""):
                        nomatch += 1
                    if old[4] and abs(o["da"] - old[4]) > 0.001 * old[4]:
                        chg.append((nm.get(old[0], old[0]), old[1], old[2], old[3], old[4], o["da"], o["tag"]))
                    else:
                        same += 1
                    build_kpi(con, old[0], old[1], old[2])
            con.commit()
            print(f"[da-rou] {done}/{len(reps)} · 변경 {len(chg)} · 동일 {same} · DART {common.CALLS - c0}콜", flush=True)
            if stop:
                print("[da-rou] DART 한도 — 중단(다음 실행에서 이어서)", flush=True)
                break
    print(f"[da-rou] 완료 {done}/{len(reps)} · 상각비 변경 {len(chg)}행 · 동일 {same}행 · 구성 미확인(예전 규칙 유지) {nomatch}행 · DART {common.CALLS - c0}콜", flush=True)
    import collections as _co
    bins = _co.Counter()
    for c in chg:
        pct = (c[5] - c[4]) / c[4] * 100
        bins["+0~5%" if 0 < pct <= 5 else "+5~15%" if 0 < pct <= 15 else "+15%초과" if pct > 15 else "감소"] += 1
    print(f"[da-rou] 변화 폭 분포 {dict(bins)}", flush=True)
    for c in sorted(chg, key=lambda c: -abs(c[5] - c[4]) / c[4])[:40]:
        print(f"  {c[0]} {c[1]} {c[2]} {c[3]}: {c[4]/1e8:,.1f}억 → {c[5]/1e8:,.1f}억 ({(c[5]-c[4])/c[4]*100:+.1f}%) [{c[6]}]", flush=True)
    return chg

