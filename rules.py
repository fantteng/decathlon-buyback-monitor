# -*- coding: utf-8 -*-
"""目标车型规则 — 服务端存储（targets.json）
=============================================
规则格式（与网页前端一致）:
[{"kws": ["14寸|14''", "900", "青玉"], "min": 500, "max": 800}]
- kws: 每个关键词用 | 分隔多个别名，车名需满足全部关键词（任一别名命中即可）
- min/max: 价格区间（可选）
守护线程用它在服务端判断命中并推送，不再依赖浏览器 localStorage。
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
TARGETS_PATH = BASE / "targets.json"

DEFAULT = [{"kws": ["14寸|14''", "900", "青玉"]}]


def load_targets() -> list:
    if not TARGETS_PATH.exists():
        return [dict(r) for r in DEFAULT]
    try:
        t = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
        if isinstance(t, list) and t:
            return t
    except Exception:
        pass
    return [dict(r) for r in DEFAULT]


def save_targets(targets: list):
    TARGETS_PATH.write_text(json.dumps(targets, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def _kws_match(kw: str, title: str) -> bool:
    return any(a in title for a in kw.split("|") if a)


def match_rules(item: dict, targets: list):
    """返回命中的规则，未命中返回 None。item 需含 title / price。"""
    title = item.get("title", "")
    try:
        p = float(item.get("price"))
    except (TypeError, ValueError):
        p = None
    for r in targets:
        if not all(_kws_match(k, title) for k in (r.get("kws") or [])):
            continue
        rc = r.get("city")
        if rc and item.get("city") != rc:
            continue  # 规则限定城市时，城市不符不命中
        if p is None:
            continue  # 价格未知时不命中：宁可漏报，不可推假警报
        if r.get("min") is not None and p < r["min"]:
            continue
        if r.get("max") is not None and p > r["max"]:
            continue
        return r
    return None


def hit_items(items: list, targets: list = None) -> list:
    """从全量列表中筛出命中目标规则的车辆。"""
    targets = targets or load_targets()
    return [it for it in items if match_rules(it, targets)]
