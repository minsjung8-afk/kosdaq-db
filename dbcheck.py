"""DB 무결성 점검·복구 도구 (Actions에서 복원 직후·업로드 직전에 실행)
  python dbcheck.py check   kosdaq.db     → quick_check 결과 'ok'면 0, 아니면 2
  python dbcheck.py recover kosdaq.db     → WAL 체크포인트 → quick_check → 실패 시 sqlite3 .recover로 재구성(테이블 단위 복구) → 결과 0/2
  python dbcheck.py pack    kosdaq.db     → 체크포인트 + 무결성 확인 + VACUUM (업로드 전)"""
import os
import shutil
import sqlite3
import subprocess
import sys


def quick_check(path):
    try:
        c = sqlite3.connect(path)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        r = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        return r if r == "ok" else r.splitlines()[0][:120]
    except sqlite3.DatabaseError as e:
        return f"error: {e}"


def _py_recover(path, out):
    """sqlite3 CLI가 없을 때: 테이블별로 읽을 수 있는 행만 새 DB로 복사(rowid 구간 단위로 손상 페이지 건너뜀)"""
    src = sqlite3.connect(path)
    dst = sqlite3.connect(out)
    for (name, sql) in src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL").fetchall():
        try:
            dst.execute(sql)
        except sqlite3.DatabaseError:
            continue
        cols = len(src.execute(f"PRAGMA table_info('{name}')").fetchall())
        ins = f"INSERT OR IGNORE INTO '{name}' VALUES ({','.join('?' * cols)})"
        try:
            mx = src.execute(f"SELECT MAX(rowid) FROM '{name}'").fetchone()[0] or 0
        except sqlite3.DatabaseError:
            mx = 0
        lo, step, got = 0, 5000, 0
        while lo <= mx:
            try:
                rows = src.execute(f"SELECT * FROM '{name}' WHERE rowid BETWEEN ? AND ?", (lo, lo + step - 1)).fetchall()
                dst.executemany(ins, rows); got += len(rows)
            except sqlite3.DatabaseError:
                if step > 50:
                    step //= 10; continue      # 손상 구간은 더 잘게
            lo += step
        print(f"[dbcheck]   {name}: {got}행")
    for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall():
        try:
            dst.execute(sql)
        except sqlite3.DatabaseError:
            pass
    dst.commit(); dst.close(); src.close()
    return True


def recover(path):
    """sqlite3 CLI .recover → 새 파일(없으면 파이썬 복구). 성공 시 원본 교체. 복구 전 원본은 .bad 로 보관"""
    out = path + ".recovered"
    if os.path.exists(out):
        os.remove(out)
    try:
        dump = subprocess.run(["sqlite3", path, ".recover"], capture_output=True, text=False)
        ok = dump.returncode in (0, 1) and bool(dump.stdout)
        if ok:
            load = subprocess.run(["sqlite3", out], input=dump.stdout, capture_output=True)
            ok = load.returncode == 0
        if not ok:
            print("[dbcheck] .recover 실패 → 파이썬 복구로 전환")
    except FileNotFoundError:
        ok = False
    if not ok:
        if os.path.exists(out):
            os.remove(out)
        _py_recover(path, out)
    r = quick_check(out)
    if r != "ok":
        print("[dbcheck] 복구본도 비정상:", r)
        return False
    c = sqlite3.connect(out)
    n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('company','fin_kpi','mezz_issue','mezz_outstanding','business','credit_rating','price_daily','rating_summary')")}
    c.close()
    shutil.move(path, path + ".bad")
    shutil.move(out, path)
    print("[dbcheck] 복구 완료 — 행 수:", n)
    return True


def main():
    cmd, path = sys.argv[1], sys.argv[2]
    r = quick_check(path)
    print(f"[dbcheck] {cmd} {path}: quick_check = {r}")
    if cmd == "check":
        sys.exit(0 if r == "ok" else 2)
    if cmd == "recover":
        if r == "ok":
            sys.exit(0)
        sys.exit(0 if recover(path) else 2)
    if cmd == "pack":
        if r != "ok":
            print("[dbcheck] 무결성 실패 — 업로드 중단(이전 R2 본 유지)")
            sys.exit(2)
        for f in (path + "-wal", path + "-shm"):
            if os.path.exists(f):
                os.remove(f)
        c = sqlite3.connect(path)
        c.execute("VACUUM")
        c.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
