# -*- coding: utf-8 -*-
"""output/ 目录自动清理 —— 解决「长期挂机磁盘只增不减」

背景：每轮巡检/兜底扫描都会往 output/ 落一份 report_*.md，7×24 跑一天就是几十份，
几个月下来纯文件堆积 + history.db 只删行不回收空间（SQLite 删完文件不缩小）。

清理规则：
- report_*.md    保留最近 KEEP_REPORTS 份；且超过 KEEP_DAYS 天的一律删
- _t_snap*.json  回归测试残留，直接删
- history.db     按 RETAIN_DAYS 清超期行 + VACUUM 真正回收磁盘
- changes.log    超过 MAX_LOG_BYTES 后截断，只留尾部

用法：
    python cleanup.py          # 真跑
    python cleanup.py --dry    # 只报数不删（先看会删什么）
服务运行时由守护线程每天自动跑一次，无需手动。
"""
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"

KEEP_REPORTS = 60      # 日报最多保留份数
KEEP_DAYS = 7          # 巡检日报最多保留天数
DAILY_KEEP_DAYS = 30   # 「每天最新一份」的保护期（日汇总，通常是最有价值的 21 点那份）
RETAIN_DAYS = 7        # history.db 保留天数（与 history.py 一致）
MAX_LOG_BYTES = 5 * 1024 * 1024     # changes.log 超过这个体积才动
LOG_KEEP_BYTES = 1024 * 1024        # 截断后保留的尾部大小
_TS = "%Y-%m-%d %H:%M:%S"


def _rm(p: Path) -> bool:
    try:
        p.unlink()
        return True
    except Exception:
        return False


def clean_reports(dry=False, keep=KEEP_REPORTS, days=KEEP_DAYS) -> dict:
    """日报：按「份数 + 天数」双上限清理，最新在前。

    保护规则：每天的最新一份豁免「超额」删除（21 点汇总日报通常是一天的最后一份，
    比每小时的巡检日报有价值），但仍受天数上限约束。
    """
    files = sorted(OUT.glob("report_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - days * 86400
    daily_cutoff = time.time() - DAILY_KEEP_DAYS * 86400
    daily_last = {}
    for p in files:                       # files 已按新→旧，首个即当天最新
        day = time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))
        daily_last.setdefault(day, p)
    protected = set(daily_last.values())
    r = {"total": len(files), "del_by_count": 0, "del_by_days": 0,
         "kept": 0, "kept_daily": len(protected)}
    for i, p in enumerate(files):
        mt = p.stat().st_mtime
        expired = mt < (daily_cutoff if p in protected else cutoff)
        over = i >= keep and p not in protected
        if expired or over:
            r["del_by_days" if expired else "del_by_count"] += 1
            if not dry:
                _rm(p)
        else:
            r["kept"] += 1
    return r


def clean_temp(dry=False) -> int:
    """回归测试残留的临时快照等。"""
    n = 0
    for pat in ("_t_snap*.json", "*.tmp", "q*.json", "p_check.html", "served_check.html",
                "page.html", "wd_tmp.txt", "wd_info.txt", "gh_cleanup.txt"):
        for p in OUT.glob(pat):
            n += 1
            if not dry:
                _rm(p)
    return n


def clean_db(dry=False) -> dict:
    """history.db：清超期行 + VACUUM（不 VACUUM 的话删完文件体积不变）。"""
    db = OUT / "history.db"
    if not db.exists():
        return {"skipped": True}
    before = db.stat().st_size
    if before < 1024 * 1024:      # 小于 1MB 不折腾
        return {"skipped": True, "size": before}
    if dry:
        return {"before": before}
    try:
        c = sqlite3.connect(db, timeout=15)
        cutoff = time.strftime(_TS, time.localtime(time.time() - RETAIN_DAYS * 86400))
        try:
            c.execute("DELETE FROM target_hist WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM city_stats WHERE ts < ?", (cutoff,))
            c.commit()
        except sqlite3.OperationalError:
            pass  # 表还没建
        c.execute("VACUUM")
        c.close()
    except Exception as e:
        return {"error": str(e), "before": before}
    return {"before": before, "after": db.stat().st_size,
            "freed": before - db.stat().st_size}


def clean_log(dry=False) -> dict:
    """changes.log 只追加不轮转，超限后保留尾部。"""
    p = OUT / "changes.log"
    if not p.exists():
        return {"skipped": True}
    size = p.stat().st_size
    if size <= MAX_LOG_BYTES:
        return {"skipped": True, "size": size}
    if dry:
        return {"before": size}
    data = p.read_bytes()[-LOG_KEEP_BYTES:]
    nl = data.find(b"\n")
    if nl >= 0:
        data = data[nl + 1:]      # 丢弃半行，避免首行残缺
    p.write_bytes(data)
    return {"before": size, "after": p.stat().st_size}


def run(dry=False) -> dict:
    return {"reports": clean_reports(dry), "temp": clean_temp(dry),
            "db": clean_db(dry), "log": clean_log(dry)}


def _human(n):
    return f"{n/1024/1024:.1f}MB" if n >= 1024 * 1024 else f"{n/1024:.0f}KB"


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    r = run(dry=dry)
    tag = "[试运行] " if dry else ""
    rep = r["reports"]
    print(f"{tag}日报: 共{rep['total']}份 → 留{rep['kept']}份（含每天最新{rep['kept_daily']}份），"
          f"删{rep['del_by_count']}份(超额)+{rep['del_by_days']}份(过期>7天)")
    print(f"{tag}临时文件: 删 {r['temp']} 个")
    if r["db"].get("skipped"):
        print(f"{tag}history.db: 跳过（{_human(r['db'].get('size', 0))}）")
    elif r["db"].get("error"):
        print(f"{tag}history.db: ✗ {r['db']['error']}")
    else:
        print(f"{tag}history.db: {_human(r['db']['before'])} → "
              f"{_human(r['db'].get('after', r['db']['before']))}"
              f"（回收 {_human(r['db'].get('freed', 0))}）")
    print(f"{tag}changes.log: {'跳过' if r['log'].get('skipped') else _human(r['log']['before']) + ' → ' + _human(r['log']['after'])}")
