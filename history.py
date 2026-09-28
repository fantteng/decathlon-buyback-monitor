# -*- coding: utf-8 -*-
"""价格与库存历史 — SQLite 落地（output/history.db）
====================================================
每次全量扫描后调用 record_scan(items)：
- target_hist: 每辆命中目标规则的车一行（ts/sku/city/quality/price）
  → 目标车价格走势 + 在架断档（相邻点间隔>10分钟即离架过）
- city_stats: 每次扫描一行 JSON {city: {S:数量,A:数量,...}}
  → 城市×成色库存趋势（看 S 类消耗速度）
自动保留 7 天。供 webapp /api/history 查询画趋势图。
"""
import json
import sqlite3
import time
from pathlib import Path

from rules import hit_items

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "output" / "history.db"
RETAIN_DAYS = 7
_TS = "%Y-%m-%d %H:%M:%S"


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS target_hist(
      ts TEXT, sku TEXT, city TEXT, title TEXT, quality TEXT, price REAL);
    CREATE INDEX IF NOT EXISTS ix_th_sku ON target_hist(sku, ts);
    CREATE INDEX IF NOT EXISTS ix_th_ts ON target_hist(ts);
    CREATE TABLE IF NOT EXISTS city_stats(ts TEXT PRIMARY KEY, stats TEXT);
    """)


def record_scan(items: list, targets=None) -> int:
    """扫描后记录一轮。返回记录的命中行数（-1=异常）。"""
    try:
        now = time.strftime(_TS)
        c = _conn()
        _init(c)
        hits = hit_items(items, targets)
        for h in hits:
            c.execute("INSERT INTO target_hist VALUES(?,?,?,?,?,?)",
                      (now, h.get("sku", ""), h.get("city", ""), (h.get("title") or "")[:80],
                       h.get("quality", ""), h.get("price")))
        stats = {}
        for it in items:
            q = it.get("quality") or "?"
            stats.setdefault(it.get("city", ""), {}).setdefault(q, 0)
            stats[it["city"]][q] += 1
        c.execute("INSERT OR REPLACE INTO city_stats VALUES(?,?)",
                  (now, json.dumps(stats, ensure_ascii=False)))
        cutoff = time.strftime(_TS, time.localtime(time.time() - RETAIN_DAYS * 86400))
        c.execute("DELETE FROM target_hist WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM city_stats WHERE ts < ?", (cutoff,))
        c.commit()
        c.close()
        return len(hits)
    except Exception as e:
        print(f"[history] 记录异常: {e}")
        return -1


def target_series(sku: str, hours: int = 48, city: str = None) -> list:
    """某 sku 的历史点位 [{ts, city, quality, price}]，时间升序。
    city 可选：sku 全国不唯一（同款同成色跨城同码），不传城市会把多城数据混进一条曲线。"""
    cutoff = time.strftime(_TS, time.localtime(time.time() - hours * 3600))
    c = _conn()
    _init(c)
    if city:
        rows = c.execute(
            "SELECT ts, city, quality, price FROM target_hist WHERE sku=? AND city=? AND ts>=? ORDER BY ts",
            (sku, city, cutoff)).fetchall()
    else:
        rows = c.execute(
            "SELECT ts, city, quality, price FROM target_hist WHERE sku=? AND ts>=? ORDER BY ts",
            (sku, cutoff)).fetchall()
    c.close()
    return [{"ts": r[0], "city": r[1], "quality": r[2], "price": r[3]} for r in rows]


def city_series(city: str, quality: str, hours: int = 48) -> list:
    """某城市某成色的库存数量曲线 [{ts, count}]，时间升序。"""
    cutoff = time.strftime(_TS, time.localtime(time.time() - hours * 3600))
    c = _conn()
    _init(c)
    rows = c.execute("SELECT ts, stats FROM city_stats WHERE ts>=? ORDER BY ts",
                     (cutoff,)).fetchall()
    c.close()
    out = []
    for ts, stats in rows:
        try:
            n = (json.loads(stats).get(city) or {}).get(quality, 0)
        except Exception:
            n = 0
        out.append({"ts": ts, "count": n})
    return out
