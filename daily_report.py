# -*- coding: utf-8 -*-
"""每日日报 — 汇总当日库存变动 + 目标车现况，推送到全部微信/短信通道
================================================================
由任务计划/自动化每天 21:00 调用（python daily_report.py），也可手动运行。
数据源：webapp API（优先，活着就有最新缓存）→ output/latest.json 兜底；
变动日志 output/changes.json；目标匹配 rules.py。
"""
import json
import time
import urllib.request
from collections import Counter
from pathlib import Path

from rules import hit_items, match_rules, load_targets
from wechat_push import send

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def _current_hits() -> list:
    """当前在售目标车：优先 webapp 实时缓存，兜底 monitor 的 latest.json。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        d = json.load(op.open("http://localhost:8787/api/query", timeout=15))
        return hit_items(d.get("items") or [])
    except Exception:
        pass
    try:
        latest = json.loads((OUT / "latest.json").read_text(encoding="utf-8"))
        return latest.get("hits") or []
    except Exception:
        return []


def _fmt_hit(h: dict) -> str:
    if "城市" in h:  # monitor 格式
        return f"{h.get('城市')}·{h.get('门店名') or '门店详见小程序'} {h.get('成色')}类 ¥{h.get('价格')}（{h.get('车辆编码')}）"
    return f"{h.get('city')} {h.get('quality')}类 ¥{h.get('price')}（{h.get('sku')}）"


def run() -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    today = time.strftime("%Y-%m-%d")
    try:
        changes = json.loads((OUT / "changes.json").read_text(encoding="utf-8")) if (OUT / "changes.json").exists() else []
    except Exception:
        changes = []
    today_ev = [e for e in changes if str(e.get("time", "")).startswith(today)]
    adds = [e for e in today_ev if e.get("type") == "上架"]
    dels = [e for e in today_ev if e.get("type") == "下架"]
    prc = [e for e in today_ev if e.get("type") == "改价"]
    targets = load_targets()
    hit_ev = [e for e in today_ev if match_rules(e, targets)]

    hits = _current_hits()

    lines = [f"📊 迪卡侬二手车日报 ｜ {now[5:]}", ""]
    lines.append(f"📦 今日全国变动：上架 {len(adds)} ｜ 下架 {len(dels)} ｜ 改价 {len(prc)}")
    lines.append(f"🎯 目标相关变动：{len(hit_ev)} 条" + ("" if not hit_ev else
                 "".join(f"\n  - {e['time'][11:16]} {e['type']} {e.get('city')} {e.get('title', '')[:24]} ¥{e.get('price')}"
                         + (f"（原¥{e['old_price']}）" if e.get("old_price") is not None else "")
                         for e in hit_ev[:5])))
    lines.append("")
    if hits:
        lines.append(f"🚗 目标车现况：在售 {len(hits)} 辆")
        for h in hits[:5]:
            lines.append(f"  - {_fmt_hit(h)}")
        lines.append("💡 心动就尽快下单：二手车单件售罄即无（下单步骤见《抢车下单步骤清单.md》）")
    else:
        lines.append("🚗 目标车现况：全国暂无在售")
        lines.append("💡 保持等待：命中瞬间会实时推送，无需盯着看板")
    content = "\n".join(lines)
    ok, info = send(f"📊 迪卡侬二手车日报 {now[5:10]}", content)
    print(content)
    print(f"\n[日报推送] {'✅ ' + info if ok else '✗ ' + info}")
    return content


if __name__ == "__main__":
    run()
