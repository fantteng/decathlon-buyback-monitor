# -*- coding: utf-8 -*-
"""库存变动追踪 — 快照对比，记录上架/下架/改价事件
================================================
多个扫描源共用（webapp 全量 / decathlon_monitor 14寸筛选），各自独立快照文件，
事件统一追加到 changes.json / changes.log。注意：不同扫描范围必须用不同快照，
否则会产生大量虚假"下架"事件。

防抖（2026-09-15）：接口偶发不返回 randomItemCode 等字段抖动，会让同一辆车
在"sku键"与"fallback键"之间切换，产生成对的假"下架/上架"。15 分钟内
同 city+title+quality+price 的反向事件自动配对抵消，不写入日志。
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

DEBOUNCE_SEC = 900  # 15 分钟内的反向事件视为抖动

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _atomic_write(path: Path, text: str):
    """原子写入：先写 .tmp 再 replace，防进程中断损坏文件（损坏的快照会被当成
    首次运行重建基线，静默丢失全部变动记录）。"""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _item_key(it: dict) -> str:
    """唯一键必须含城市：sku 是「车型编码@成色」，全国同款同成色同 sku（165+ 组跨城重复），
    裸 sku 做 key 会让同款跨城互相覆盖 → 变动被吞（2026-09-16 北京 ¥569.9 目标车出现/消失零记录的根因）。"""
    sku = it.get("sku") or f"{it.get('title')}|{it.get('quality')}|{it.get('price')}"
    return f"{it.get('city')}|{sku}"


def _ts(ts: str):
    try:
        return datetime.strptime(ts, _TS_FMT)
    except Exception:
        return None


_OPP = {"上架": "下架", "下架": "上架"}


def _identity(e: dict):
    """事件身份：sku 优先精确配对（必须含城市——裸 sku 全国不唯一，跨城同款会错误配对抵消）；
    sku 缺失时退化到 城市+车名+成色+价格 模糊四元组。"""
    sku = e.get("sku") or ""
    if sku:
        return ("sku", e.get("city"), sku)
    return ("fz", e.get("city"), e.get("title"), e.get("quality"), e.get("price"))


def _debounce(events: list, log: list):
    """新事件与既有日志做抖动抵消。返回 (kept_events, cleaned_log)：配对成功的双方都不保留。
    - 上架↔下架：15 分钟内同身份反向事件配对抵消（接口抖动/下单未付款超时）
    - 改价：15 分钟内价格回到原值的往返配对抵消（价格闪变刷屏）"""
    kept, cleaned = [], list(log)
    for e in events:
        typ = e.get("type")
        t_new = _ts(e.get("time", ""))
        cancelled = False
        for i, old in enumerate(cleaned):
            t_old = _ts(old.get("time", ""))
            if not (t_new and t_old and abs((t_new - t_old).total_seconds()) <= DEBOUNCE_SEC):
                continue
            if typ in _OPP and old.get("type") == _OPP[typ] and _identity(old) == _identity(e):
                cancelled = True
                cleaned.pop(i)
                break
            if typ == "改价" and old.get("type") == "改价" and _identity(old) == _identity(e) \
                    and old.get("price") == e.get("old_price") and old.get("old_price") == e.get("price"):
                cancelled = True
                cleaned.pop(i)
                break
        if not cancelled:
            kept.append(e)
    return kept, cleaned


def diff_and_record(items: list, snap_path: Path, changes_path: Path, log_human: bool = True):
    """与上次快照对比，记录上架/下架/改价事件。首次运行只建基线。
    返回 (added, removed, price_changed) 数量三元组（已抵消抖动）。"""
    now = time.strftime(_TS_FMT)
    try:
        prev = json.loads(Path(snap_path).read_text(encoding="utf-8")) if Path(snap_path).exists() else {}
    except Exception:
        prev = {}
    cur = {}
    for it in items:
        cur[_item_key(it)] = {"city": it.get("city", ""), "title": it.get("title", ""),
                              "quality": it.get("quality", ""), "price": it.get("price"),
                              "retail": it.get("retail"), "sku": it.get("sku", ""),
                              "image": it.get("image", ""), "dsm": it.get("dsm", ""),
                              "model": it.get("model", "")}  # 图片/门店码供只读模式展示
    Path(snap_path).parent.mkdir(exist_ok=True)
    if not prev:  # 首次运行：只建基线，不产生事件
        _atomic_write(Path(snap_path), json.dumps(cur, ensure_ascii=False))
        return 0, 0, 0
    added = [{"sku": k, **v} for k, v in cur.items() if k not in prev]
    removed = [{"sku": k, **prev[k]} for k in prev if k not in cur]
    changed = [{"sku": k, **cur[k], "old_price": prev[k].get("price")}
               for k in cur if k in prev and prev[k].get("price") != cur[k].get("price")]
    events = ([{"time": now, "type": "上架", **a} for a in added]
              + [{"time": now, "type": "下架", **r} for r in removed]
              + [{"time": now, "type": "改价", **c} for c in changed])
    _atomic_write(Path(snap_path), json.dumps(cur, ensure_ascii=False))
    if not events:
        return 0, 0, 0
    try:
        log = json.loads(Path(changes_path).read_text(encoding="utf-8")) if Path(changes_path).exists() else []
    except Exception:
        log = []
    kept, log = _debounce(events, log)
    final = (kept + log)[:500]
    _atomic_write(Path(changes_path), json.dumps(final, ensure_ascii=False))
    if log_human:
        # 全量重写 human 日志（而非追加），使抵消同步生效
        lines = []
        for e in reversed(final):  # 旧→新
            lines.append(f"[{e['time']}] {e['type']} {e['city']} {e['title'][:40]} ¥{e.get('price')}"
                         + (f" (原¥{e['old_price']})" if e.get("old_price") else "")
                         + f" 编码{e.get('sku', '')}")
        _atomic_write(Path(changes_path).parent / "changes.log", "\n".join(lines) + "\n")
    return (sum(1 for e in kept if e["type"] == "上架"),
            sum(1 for e in kept if e["type"] == "下架"),
            sum(1 for e in kept if e["type"] == "改价"))
