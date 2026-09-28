# -*- coding: utf-8 -*-
"""
迪卡侬二手车全国库存查询 — 本地网页版（支持自定义目标车型）
==========================================================
启动: python webapp.py   (端口 8787)
访问: http://localhost:8787
依赖: 仅 Python 标准库
"""
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from avatar import fetch_avatar, fetch_commodity
from tracker import diff_and_record
from rules import load_targets, save_targets, hit_items
from history import record_scan, target_series, city_series
from wechat_push import push_content, send as wx_send, load_channels, save_channels, send_channel, _dep_bad

BASE = Path(__file__).resolve().parent

# ---- 图片代理：服务端缩放后再给浏览器（防大图解码卡整机；Pillow 可选，缺失时原样直出） ----
try:
    from PIL import Image
    import io as _io
    PIL_OK = True
except Exception:
    PIL_OK = False
import hashlib as _hashlib
import os as _os

IMG_CACHE_DIR = BASE / "imgcache"
_IMG_HOSTS = {"pixl.decathlon.com.cn"}

def _img_proxy(u: str, w: int):
    """下载 pixl 原图 → 缩放到指定宽度 → 落盘缓存。失败返回 None。"""
    sp = urlparse(u)
    if sp.netloc not in _IMG_HOSTS or sp.scheme not in ("http", "https"):
        return None
    cache_path = IMG_CACHE_DIR / (_hashlib.md5(f"{w}|{u}".encode("utf-8")).hexdigest() + ".jpg")
    try:
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path.read_bytes()
    except Exception:
        pass
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0",
                                             "Referer": "https://www.decathlon.com.cn/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    if not raw:
        return None
    if PIL_OK:
        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        data = buf.getvalue()
    else:
        data = raw
    try:
        IMG_CACHE_DIR.mkdir(exist_ok=True)
        if len(_os.listdir(IMG_CACHE_DIR)) > 3000:
            for f in _os.listdir(IMG_CACHE_DIR):
                try:
                    (IMG_CACHE_DIR / f).unlink()
                except Exception:
                    pass
        cache_path.write_bytes(data)
    except Exception:
        pass
    return data

# 小程序直达单车页配置（path 待抓包单车详情页后更新，query 支持 {commodityId}/{city} 占位）
MP_CFG_PATH = BASE / "mpconfig.json"
MP_DEFAULT = {"path": "sportlover/zero-waste-bike/pages/home/index",
              "query": "commodityId={commodityId}"}


def load_mp_cfg() -> dict:
    try:
        c = json.loads(MP_CFG_PATH.read_text(encoding="utf-8"))
        if isinstance(c, dict) and c.get("path"):
            return c
    except Exception:
        pass
    return dict(MP_DEFAULT)


def save_mp_cfg(cfg: dict):
    MP_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

CITIES_PATH = BASE / "cities.json"
PORT = 8787
CACHE_TTL = 120  # 秒
# 只读模式（环境变量 DT_READONLY=1）：不扫官方接口、不推送，仅展示 output/snapshot.json 快照
READONLY = _os.environ.get("DT_READONLY") == "1"
API_URL = "https://buyback.decathlon.com.cn/recycle-gateway/recycle/v2/open/customer/commodity"

# 免疫抓包工具（Reqable）退出后的系统代理残留：所有外呼一律直连
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

_cache = {"time": 0, "items": [], "cities": 0}
_scan_lock = threading.Lock()

# ---------- 库存变动追踪（逻辑见 tracker.py） ----------

SNAP_PATH = BASE / "output" / "snapshot.json"
CHANGES_PATH = BASE / "output" / "changes.json"

# ---------- 数据抓取 ----------

def fetch_city(city: str, store_map: dict):
    body = {"city": city, "longitude": 108.888916, "latitude": 34.193462,
            "productType": "CHILD_BIKE", "filters": [], "sort": ""}
    out, page = [], 0
    try:
        while page <= 20:  # 翻页抓全（上海等大城市>200辆，单页会漏、边界车时进时出）
            url = f"{API_URL}?pageIndex={page}&pageSize=200"
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("code") != "OK":
                break
            c = data.get("content") or {}
            results = c.get("results") or []
            for it in results:
                out.append({
                    "city": city,
                    "store": store_map.get(str(it.get("dsmCode")), ""),  # 列表接口dsmCode非门店码，通常匹配不到，显示留空
                    "title": it.get("dsmTitle", "").strip('"'),
                    "quality": it.get("quality", ""),
                    "price": it.get("sellingPrice"),
                    "retail": it.get("retailPrice"),
                    "sku": it.get("randomItemCode", ""),
                    "image": it.get("mainImage", ""),
                    "dsm": str(it.get("dsmCode", "")),
                    "model": str(it.get("modelCode", "")),
                })
            total = c.get("totalRecords")
            page += 1
            if not results or (isinstance(total, int) and len(out) >= total):
                break
        return out
    except Exception:
        return out  # 翻页中途失败时保留已抓到的部分

def scan_all():
    cities = {}
    if CITIES_PATH.exists():
        all_c = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
        cities = {c: {str(k): v for k, v in stores} for c, stores in all_c.items() if stores}
    items, n_ok = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_city, c, m): c for c, m in cities.items()}
        for f in as_completed(futs):
            r = f.result()
            if r:
                n_ok += 1
                items += r
    items.sort(key=lambda x: (x["city"], x["price"] or 0))
    return items, n_ok

FORCE_MIN_INTERVAL = 25  # 强刷节流：强刷请求在25s内直接回缓存（防多标签页/自动化循环触发扫描风暴，保护迪卡侬接口）

def get_data(force=False):
    if READONLY:  # 只读模式：任何请求都不触发扫描
        return _cache
    if force and _cache["time"] and time.time() - _cache["time"] < FORCE_MIN_INTERVAL:
        force = False  # 距上次扫描不足25s，视为命中缓存
    if force or time.time() - _cache["time"] > CACHE_TTL:
        with _scan_lock:  # 多线程服务器下防止并发重复扫描
            # 锁内复检：并发强刷请求排队期间，只要有线程刚完成过扫描（25s内）全部回缓存
            # （2026-09-15 踩坑：旧写法 force 绕过复检，10个并发强刷=10连扫，6秒一行假风暴）
            if _cache["time"] and time.time() - _cache["time"] < FORCE_MIN_INTERVAL:
                return _cache
            items, n_ok = scan_all()
            try:
                added, removed, changed = diff_and_record(items, SNAP_PATH, CHANGES_PATH)
            except Exception as e:  # 变动记录失败不应拖垮数据缓存（2026-09-15 教训）
                print(f"[tracker] diff_and_record 异常: {e}")
                added, removed, changed = 0, 0, 0
            _cache.update({"time": time.time(), "items": items, "cities": n_ok,
                           "added": added, "removed": removed, "price_changed": changed})
            try:  # 价格/库存历史落库（失败不影响主流程）
                record_scan(items)
            except Exception as e:
                print(f"[history] 异常: {e}")
    return _cache

# ---------- 服务端守护推送（2026-09-15：推送不再依赖浏览器标签页） ----------
# 后台线程每 30s 唤醒一次；数据缓存 120s（CACHE_TTL），即实际约 2 分钟一轮扫描：
#   1. 命中目标车 → 服务端直接推微信/短信（per-channel 去重，失败通道自动补发）
#   2. 目标车被抢(下架)/改价/重新上架 → 变动告警
#   3. 连续 3 轮扫到 0 辆 → 接口可能变更告警（每小时最多提醒 1 次）

_guard = {"fail_streak": 0, "last_alert": 0.0, "tstate": {},
          "schema": {"ok": True}, "schema_alert": 0.0}
GUARD_INTERVAL = 30

PUSH_STATE_PATH = BASE / "output" / "push_state.json"


def _hit_push_content(h: dict):
    """单辆命中车的推送文案（附门店/车况/实拍）。"""
    lines = [f"🚨 {h['city']}·{h.get('store') or '查看门店'}\n{h['title']}\n"
             f"成色 {h.get('quality')}类 ｜ ¥{h.get('price')}（原价 ¥{h.get('retail')}）\n"
             f"编码 {h.get('sku')}\n"
             f"👉 极速下单：微信 → 文件传输助手 → 点收藏的「二手车卡片」直达\n（没收藏就搜小程序「二手童车」切{h['city']}，单件售罄即无）"]
    fields = {"city": h.get("city", ""), "store": h.get("store", ""),
              "title": h.get("title", ""), "price": h.get("price", ""),
              "quality": h.get("quality", ""), "sku": h.get("sku", ""), "count": 1}
    content = lines[0]
    try:
        det = fetch_commodity(h.get("dsm", ""), h.get("model", ""),
                              h.get("quality", ""), h.get("city", ""))
        st = det.get("store") or {}
        if st.get("name"):
            content += f"\n\n🏬 {st['name']}"
            if st.get("address"):
                content += f"\n📍 {st['address']}"
        dep = det.get("deprecation") or {}
        items = [x for g in (dep.get("children") or []) for x in (g.get("children") or []) if x.get("desc")]
        if items:
            bad = [x for x in items if _dep_bad(x)]
            content += f"\n\n📋 官方车况：{len(items)} 项检测，" + ("全部通过 ✅" if not bad else f"{len(bad)} 项需注意 ⚠️")
            content += "".join(f"\n- {x.get('desc')}：{x.get('additionalDesc')}" for x in bad[:3])
        pics = (det.get("pics") or [])[:3] or ([det["avatar"]] if det.get("avatar") else [])
        if pics:
            content += f"\n\n📸 车辆成色实拍（共{len(det.get('pics') or pics)}张，附前{len(pics)}张）："
            content += "\n\n".join(f"![实拍{i+1}]({p})" for i, p in enumerate(pics))
    except Exception:
        pass
    return content, fields


def server_push_hits(hits: list) -> int:
    """逐辆推送新命中的目标车（key=sku|city|price 与 monitor 统一去重）。
    返回实际推送的车数。"""
    n = 0
    for h in hits:
        key = f"{h.get('sku')}|{h.get('city')}|{h.get('price')}"
        content, fields = _hit_push_content(h)
        ok, info, _ = push_content(key, f"🚨 命中目标车！{h.get('city')} ¥{h.get('price')}", content, fields)
        if ok:
            n += 1
            print(f"[守护推送] ✅ {h.get('city')} {h.get('title', '')[:30]} → {info}")
    if n:
        try:
            PUSH_STATE_PATH.write_text(
                json.dumps({"time": time.strftime("%H:%M"), "pushed": n}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass
    return n


# 规则变更后重扫存量车的推送上限（防止规则写太宽时一瞬间刷屏）
RULE_RESCAN_MAX = 10

# ---------- 接口健康检查：官方改字段名会「静默停摆」 ----------
# 典型场景：官方把 sellingPrice 改名 → 车还在、列表非空、fail_streak 不涨，
# 但价格全空 → 规则永不命中 → 用户以为没车，其实是坏了。现有告警覆盖不到。
SCHEMA_MIN_ITEMS = 50        # 样本太小时不判断（少量空值属正常，避免误报）
SCHEMA_MISS_RATE = 0.9       # 价格缺失率 ≥ 此值判定为「接口字段变更」
SCHEMA_ALERT_COOLDOWN = 6 * 3600   # 同类告警冷却 6 小时


def schema_health_check(items: list) -> dict:
    """检测接口字段是否还认得：价格大面积缺失 = 官方改字段名/改结构。"""
    res = {"ok": True, "total": len(items), "miss_price": 0, "rate": 0.0}
    if len(items) < SCHEMA_MIN_ITEMS:
        return res
    miss = 0
    for it in items:
        p = it.get("price")
        try:
            v = float(p) if p not in (None, "", "null") else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            miss += 1
    rate = miss / len(items)
    res.update(miss_price=miss, rate=round(rate, 3))
    if rate >= SCHEMA_MISS_RATE:
        res["ok"] = False
    return res


def _schema_alert(schema: dict):
    """字段异常告警（带冷却 + 每轮打印）。"""
    _guard["schema"] = schema
    if schema.get("ok"):
        return
    now = time.time()
    print(f"[健康检查] ⚠️ 价格缺失率 {schema['rate']:.0%}"
          f"（{schema['miss_price']}/{schema['total']}）——接口字段可能已变更")
    if now - _guard.get("schema_alert", 0) < SCHEMA_ALERT_COOLDOWN:
        return
    _guard["schema_alert"] = now
    try:
        wx_send("⚠️ 迪卡侬监控数据异常（疑似接口改版）",
                f"本轮扫到 {schema['total']} 辆，其中 {schema['miss_price']} 辆读不到价格"
                f"（缺失率 {schema['rate']:.0%}）。\n\n"
                "车还在架，但价格字段失效会导致目标规则永不命中——\n"
                "请打开看板核对，若列表价格全空说明官方接口已改版，需要更新解析代码。\n"
                "（此类告警每 6 小时最多一次）",
                {"count": schema["total"]})
        print("[健康检查] ✅ 已推送接口改版告警")
    except Exception as e:
        print(f"[健康检查] 告警推送失败: {e}")


def _bg_push(hits: list):
    """后台线程推送（每条推送约 1 秒网络请求，不能阻塞 HTTP 响应）。"""
    try:
        n = server_push_hits(hits)
        print(f"[规则重扫] 后台补推完成：{n}/{len(hits)} 辆")
    except Exception as e:
        print(f"[规则重扫] 后台推送失败: {e}")


def rescan_existing_hits(targets: list = None, background: bool = False) -> dict:
    """规则变更后，用当前数据重新匹配一遍存量车并补推。

    解决的问题：规则只在「新事件」触发时才推送，改完规则后已存在的命中车
    不会重新报警 —— 用户会误以为没车，其实早就在架。
    本函数用缓存数据（不额外请求接口）重跑匹配，靠 pushed.json 的
    sku|city|price 去重，历史已推过的不会重推。

    background=True 时只同步做匹配（纯内存，毫秒级），推送丢后台线程，
    HTTP 立即返回；返回 pushed=-1 表示「后台推送中」。
    """
    if READONLY:
        return {"skipped": "readonly"}
    try:
        d = get_data()
        items = d.get("items") or []
        if not items:
            return {"ok": False, "info": "暂无数据"}
        hits = hit_items(items, targets)
        check_target_changes(hits)  # 只播种/同步生命周期基线，不产生告警
        capped = hits[:RULE_RESCAN_MAX]
        if hits:
            print(f"[规则重扫] 存量命中 {len(hits)} 辆"
                  + (f"（超出上限 {RULE_RESCAN_MAX}，仅推前 {RULE_RESCAN_MAX} 辆）" if len(hits) > RULE_RESCAN_MAX else ""))
        if background:
            if capped:
                threading.Thread(target=_bg_push, args=(capped,),
                                 daemon=True, name="rule-rescan").start()
                return {"ok": True, "total": len(hits), "pushed": -1}
            return {"ok": True, "total": len(hits), "pushed": 0}
        n = server_push_hits(capped) if capped else 0
        return {"ok": True, "total": len(hits), "pushed": n}
    except Exception as e:
        print(f"[规则重扫] ✗ {e}")
        return {"ok": False, "info": str(e)}


def check_target_changes(cur_hits: list) -> list:
    """目标车生命周期检测，返回 [(type, item)]，type ∈ 上架(重新上架)/下架(被抢)/改价。
    键 = 城市|sku：裸 sku 全国不唯一（同款同成色跨城同码），会互相覆盖漏报（2026-09-16 坑）。"""
    alerts = []
    cur = {f"{h.get('city')}|{h['sku']}": h for h in cur_hits if h.get("sku")}
    tstate = _guard["tstate"]
    for sku, h in cur.items():
        old = tstate.get(sku)
        if old is None:
            tstate[sku] = {"price": h.get("price"), "gone": False, "info": h}
        elif old.get("gone"):
            alerts.append(("重新上架", h))
            old.update(price=h.get("price"), gone=False, info=h)
        elif old.get("price") != h.get("price"):
            alerts.append(("改价", {**h, "old_price": old.get("price")}))
            old.update(price=h.get("price"), info=h)
    for sku, old in tstate.items():
        if sku not in cur and not old.get("gone"):
            alerts.append(("下架", old.get("info") or {}))
            old["gone"] = True
    return alerts


def _push_change_alerts(alerts: list):
    for typ, h in alerts:
        title = {"下架": "🔴 目标车被抢/下架！", "改价": "💲 目标车改价", "重新上架": "🟢 目标车重新上架！"}.get(typ, typ)
        old = f"（原 ¥{h['old_price']}）" if typ == "改价" and h.get("old_price") is not None else ""
        content = (f"{title}\n{h.get('city', '')} · {h.get('store') or '查看门店'}\n{h.get('title', '')}\n"
                   f"成色 {h.get('quality', '')}类 ｜ 现价 ¥{h.get('price', '?')}{old}\n编码 {h.get('sku', '')}")
        if typ == "下架":
            content += "\n\n⚠️ 这辆已不在售。若全国无同类目标车，建议尽快考虑其他城市/成色。"
        else:
            content += "\n\n👉 极速下单：微信 → 文件传输助手 → 点收藏的「二手车卡片」直达（没收藏就搜小程序「二手童车」切城市）"
        try:
            push_content(f"chg|{typ}|{h.get('city')}|{h.get('sku')}|{h.get('price')}|{old}",
                         f"{title} {h.get('city', '')} ¥{h.get('price', '?')}", content,
                         {"city": h.get("city", ""), "title": h.get("title", ""), "price": h.get("price", ""),
                          "quality": h.get("quality", ""), "sku": h.get("sku", ""), "count": 1})
            print(f"[变动告警] {typ} {h.get('city')} {h.get('title', '')[:30]}")
        except Exception as e:
            print(f"[变动告警] ✗ {e}")


def _daily_cleanup():
    """每天跑一次 output/ 清理（日报留 7 天、日汇总留 30 天、db 回收空间）。"""
    today = time.strftime("%Y-%m-%d")
    if _guard.get("cleanup_day") == today:
        return
    _guard["cleanup_day"] = today
    try:
        import cleanup
        r = cleanup.run()
        rep = r["reports"]
        freed = r["db"].get("freed") or 0
        print(f"[清理] 日报留 {rep['kept']} 份（删 {rep['del_by_count'] + rep['del_by_days']} 份）"
              f"｜临时文件 {r['temp']} 个"
              + (f"｜history.db 回收 {freed // 1024}KB" if freed else ""))
    except Exception as e:
        print(f"[清理] ✗ {e}")


def guardian():
    """服务端守护线程：浏览器关闭也能继续监控+推送。"""
    first = True
    while True:
        try:
            _daily_cleanup()
            d = get_data()
            items = d.get("items") or []
            if not items:
                _guard["fail_streak"] += 1
                if _guard["fail_streak"] >= 3 and time.time() - _guard["last_alert"] > 3600:
                    _guard["last_alert"] = time.time()
                    try:
                        wx_send("⚠️ 迪卡侬监控接口异常",
                                f"连续 {_guard['fail_streak']} 轮扫描结果为 0 辆，\n"
                                "接口可能已变更或网络故障，请打开看板检查。\n（每小时最多提醒一次）",
                                {"count": 0})
                        print(f"[守护] ⚠️ 接口异常告警已推送（连续 {_guard['fail_streak']} 轮 0 辆）")
                    except Exception as e:
                        print(f"[守护] 告警推送失败: {e}")
            else:
                _guard["fail_streak"] = 0
                _schema_alert(schema_health_check(items))   # 字段改名静默停摆检测
                hits = hit_items(items)
                if first:
                    # 首轮：播种生命周期基线 + 当前命中车走 push_content 去重后推送
                    # （pushed.json 已有的不会重推），之后每轮只推新增。
                    first = False
                    check_target_changes(hits)
                    server_push_hits(hits)
                else:
                    # 新增命中（不在生命周期基线里的新车）
                    # 基线键是 城市|sku（裸 sku 全国不唯一），必须同构比较
                    known = set(_guard["tstate"].keys())
                    fresh = [h for h in hits
                             if h.get("sku") and f"{h.get('city')}|{h['sku']}" not in known]
                    if fresh:
                        server_push_hits(fresh)
                    alerts = check_target_changes(hits)
                    if alerts:
                        _push_change_alerts(alerts)
        except Exception as e:
            print(f"[守护] 轮询异常: {e}")
        time.sleep(GUARD_INTERVAL)

# ---------- 成色实拍图（v3 详情接口，见 avatar.py） ----------

# ---------- 页面 ----------

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🚲 迪卡侬二手车全国库存查询</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--card:#1e222b;--border:#2a2f3a;--txt:#e6e9ef;--sub:#9aa3b2;--acc:#4f8cff;--gold:#ffc53d;--green:#3fbf7f;--red:#ff6b6b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:20px}
.wrap{max-width:1280px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--sub);font-size:12px;margin-bottom:2px}
.headerline{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.hdr-status{color:var(--sub);font-size:12px;text-align:right;max-width:460px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
.panel h3{font-size:14px;margin-bottom:10px}
.panel .hint{color:var(--sub);font-size:12px;font-weight:400}
.panel-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:920px){.panel-grid{grid-template-columns:1fr}}
.rowline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.rowline:last-child{margin-bottom:0}
#kw{width:230px}
.stats{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 18px;min-width:120px}
.stat b{font-size:22px;display:block}
.stat span{color:var(--sub);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
input,select,button{background:var(--card);color:var(--txt);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px}
input{width:180px}
button{cursor:pointer;background:var(--acc);border:none;font-weight:600}
button:hover{opacity:.88}
button.ghost{background:var(--card);border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden}
th{background:#232834;color:var(--sub);font-size:12px;text-align:left;padding:9px 10px;position:sticky;top:44px}
.quicknav{position:sticky;top:0;z-index:20;display:flex;gap:8px;flex-wrap:wrap;align-items:center;background:var(--bg);padding:9px 0;margin-bottom:10px;border-bottom:1px solid var(--border)}
.quicknav button{padding:5px 12px;font-size:12px;font-weight:400}
.quicknav .qn-label{color:var(--sub);font-size:12px}
td{padding:8px 10px;border-top:1px solid var(--border);vertical-align:middle;font-size:13px}
tr.hit td{background:rgba(255,197,61,.08)}
tr:hover td{background:rgba(79,140,255,.06)}
img{width:52px;height:40px;object-fit:cover;border-radius:6px;background:#2a2f3a}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.q-S{background:#1d3a2a;color:var(--green)}.q-A{background:#1d2f4a;color:#6fa8ff}.q-B{background:#3a3320;color:var(--gold)}.q-C{background:#3a2222;color:var(--red)}
.price{color:var(--gold);font-weight:700}
.retail{color:var(--sub);font-size:12px;text-decoration:line-through;margin-left:4px}
.hitmark{color:var(--gold);font-weight:700}
.empty{text-align:center;color:var(--sub);padding:40px}
#hitbox{display:none;margin-bottom:14px;background:linear-gradient(135deg,#3a2f10,#241d0a);border:1px solid var(--gold);border-radius:12px;padding:12px;animation:flash 1.2s infinite}
@keyframes flash{0%,100%{box-shadow:0 0 4px rgba(255,197,61,.4)}50%{box-shadow:0 0 18px rgba(255,197,61,.9)}}
.hb-title{color:var(--gold);font-weight:800;font-size:16px;margin-bottom:8px}
.hb-cards{display:flex;gap:10px;flex-wrap:wrap}
.hb-card{background:rgba(0,0,0,.4);border:1px solid var(--gold);border-radius:10px;padding:10px;display:flex;gap:10px;min-width:300px;align-items:center}
.hb-card img{width:132px;height:99px;object-fit:cover;border-radius:8px}
.hb-price{color:var(--gold);font-size:22px;font-weight:800}
.hb-city{font-size:17px;font-weight:800;color:var(--txt)}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--gold);color:var(--gold);border-radius:16px;padding:3px 10px;font-size:12px;font-weight:600}
.chip b{cursor:pointer;color:var(--sub);font-weight:400}
.chip b:hover{color:var(--red)}
.mpbtn{display:inline-block;margin-top:6px;margin-right:8px;padding:6px 14px;border-radius:9px;background:var(--green);color:#fff;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer;border:none}
.mpbtn:hover{filter:brightness(1.15)}
.mpbtn.alt{background:transparent;border:1px solid var(--green);color:var(--green)}
.tgl{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--sub);cursor:pointer;user-select:none;white-space:nowrap}
.tgl input{accent-color:var(--green);cursor:pointer}
.chrow{display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}
.chrow .mini{padding:3px 10px;font-size:12px}
.chtype{color:var(--sub);font-size:12px;flex:1}
#chFields input{width:100%;margin:4px 0}
.m-sec{margin:12px 0 6px;color:var(--sub);font-size:13px;font-weight:600}
.tlabel{color:var(--sub);font-size:13px;font-weight:600}
.modal-mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:99;align-items:center;justify-content:center}
.modal{background:linear-gradient(160deg,#2b230d,#1a1608);border:2px solid var(--gold);border-radius:16px;padding:20px;max-width:540px;width:92%;max-height:85vh;overflow:auto;animation:pop .35s ease}
@keyframes pop{0%{transform:scale(.7);opacity:0}100%{transform:scale(1);opacity:1}}
.m-close{float:right;cursor:pointer;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--sub);padding:2px 10px;font-size:14px}
.m-close:hover{color:var(--txt)}
.m-title{color:var(--gold);font-weight:800;font-size:18px;margin-bottom:12px}
.m-card{display:flex;gap:12px;background:rgba(0,0,0,.35);border:1px solid var(--gold);border-radius:12px;padding:12px;margin-bottom:10px;align-items:center}
.m-card img{width:150px;height:113px;object-fit:cover;border-radius:8px}
.m-city{font-size:20px;font-weight:800}
.m-price{color:var(--gold);font-size:26px;font-weight:800}
.legend{color:var(--sub);font-size:12px;margin-bottom:8px}
.legend b{font-weight:600}
.legend .q-S{padding:1px 8px;margin:0 2px}
.lbmask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:98;align-items:center;justify-content:center;cursor:zoom-out}
.rpt-card{background:var(--bg,#fff);color:var(--fg,#111);border-radius:12px;padding:16px 18px;max-width:640px;width:min(92vw,640px);max-height:86vh;cursor:default;box-shadow:0 0 40px rgba(0,0,0,.5)}
.rpt-body{max-height:66vh;overflow:auto;font-size:13px}
.rpt-head{padding:8px 10px;background:rgba(127,127,127,.08);border-radius:8px;margin-bottom:10px;line-height:1.6}
.rpt-group{margin-bottom:10px}
.rpt-gt{font-weight:600;font-size:12px;color:var(--sub,#666);padding:2px 0;border-bottom:1px solid rgba(127,127,127,.25);margin-bottom:4px}
.rpt-row{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
.rpt-row span{color:var(--sub,#444)}
.rpt-row.bad span,.rpt-row.bad b{color:#d97706}
.lbmask img{max-width:92vw;max-height:88vh;border-radius:10px;box-shadow:0 0 40px rgba(0,0,0,.8)}
.lbnav{width:46px;height:46px;border-radius:50%;border:none;background:rgba(255,255,255,.16);color:#fff;font-size:30px;line-height:1;cursor:pointer}
.lbnav:hover{background:rgba(255,255,255,.3)}
#lbnav{position:fixed;bottom:3vh;left:0;right:0;display:none;align-items:center;justify-content:center;gap:16px;z-index:99}
#lbidx{color:#fff;font-size:14px;text-shadow:0 1px 4px #000}
td img{cursor:zoom-in;width:160px;height:auto;border-radius:8px;display:block}
.noimg{width:160px;height:100px;border:1px dashed var(--border);border-radius:8px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--sub);font-size:12px;gap:2px}
#tb tr{content-visibility:auto;contain-intrinsic-size:auto 210px}
.chg-row{display:flex;gap:8px;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px}
.chg-time{color:var(--sub);flex:none}
.chg-type{flex:none;padding:0 8px;border-radius:8px;font-weight:600}
.t-add{background:#1d3a2a;color:var(--green)}
.t-del{background:#3a2222;color:var(--red)}
.t-pr{background:#3a3320;color:var(--gold)}
.chg-city{flex:none;color:var(--txt);font-weight:600}
.chg-title{flex:1;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chg-price{flex:none;color:var(--gold)}
.chg-hit{background:rgba(255,197,61,.1);border-left:3px solid var(--gold)}
.help-btn{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:50%;border:1px solid var(--sub,#888);color:var(--sub,#aaa);font-size:11px;cursor:pointer;vertical-align:middle;margin-left:6px;user-select:none;flex:none;font-weight:400}
.help-btn:hover{border-color:var(--acc,#4ea1ff);color:var(--acc,#4ea1ff)}
.help-box{background:rgba(78,161,255,.06);border:1px solid rgba(78,161,255,.25);border-radius:8px;padding:8px 12px;margin:6px 0;color:var(--sub,#9ab);font-size:12px;line-height:1.8}
.help-box b{color:var(--gold)}
.chg-hit .chg-title{color:var(--gold)}
#chglist{max-height:240px;overflow:auto}
details.sec{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin-bottom:12px}
details.sec>summary{cursor:pointer;list-style:none;padding:12px 14px;font-weight:700;font-size:14px;display:flex;align-items:center;gap:6px;user-select:none}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:"▸";color:var(--sub);flex:none;transition:transform .15s}
details.sec[open]>summary::before{transform:rotate(90deg)}
details.sec>summary:hover::before{color:var(--acc)}
details.sec .hint{color:var(--sub);font-size:12px;font-weight:400}
details.sec .sec-body{padding:2px 14px 14px}
.cfg-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:920px){.cfg-grid{grid-template-columns:1fr}}
.subpanel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}
.subpanel h3{font-size:14px;margin-bottom:10px}
.subpanel .hint{color:var(--sub);font-size:12px;font-weight:400}
</style></head><body><div class="wrap">
<div class="headerline">
  <div>
    <h1>🚲 迪卡侬二手车全国库存监控 <span class="help-btn" onclick="tgHelp('hp1')">?</span></h1>
    <div class="sub">数据来源：迪卡侬官方回收小程序接口 ｜ 自动缓存 2 分钟 ｜ 🎯 金色高亮 = 命中目标车型</div>
  </div>
  <div id="status" class="hdr-status">加载中…</div>
</div>
<div id="hp1" class="help-box" style="display:none"><b>这个看板是什么？</b><br>数据来自迪卡侬官方回收小程序接口，后台守护线程每 30 秒自动巡检一次，页面数据缓存 2 分钟；右上角显示最后更新时间和当前筛选结果数。若右上角出现「⚠️ 接口连续异常」= 官方接口临时故障，系统会自动重试，无需处理。所有数据和规则都保存在本机，仅自己可见。</div>
<div class="stats">
  <div class="stat"><b id="st-total">-</b><span>全国在售</span></div>
  <div class="stat"><b id="st-city">-</b><span>覆盖城市</span></div>
  <div class="stat"><b id="st-hit" style="color:var(--gold)">-</b><span>🎯 目标命中 <span class="help-btn" onclick="tgHelp('hp3')">?</span></span></div>
</div>
<div id="hp3" class="help-box" style="display:none"><b>三个数字的含义</b><br>全国在售 = 当前扫到的全国二手车总数；覆盖城市 = 有货的城市数量；🎯 目标命中 = 符合你规则的车辆数。命中大于 0 时上方会出现红色横幅、同时微信推送；0 = 暂时没有符合预算的同型号，继续挂机等。</div>
<div class="bar" style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px;margin-bottom:12px">
  <input id="kw" placeholder="🔍 列表筛选：青玉 / 900 / 独角兽…">
  <select id="fsize"><option value="">全部尺寸</option></select>
  <select id="fq"><option value="">全部成色</option><option>S</option><option>A</option><option>B</option><option>C</option></select>
  <select id="fcity"><option value="">全部城市</option></select>
  <span style="flex:1"></span>
  <button onclick="load(true)">🔄 立即查询</button>
  <button class="ghost" onclick="reset()">清空筛选</button>
  <span class="help-btn" onclick="tgHelp('hp4')">?</span>
</div>
<div id="hp4" class="help-box" style="display:none"><b>筛选工具栏</b><br>这里的筛选只改变「下方列表显示哪些车」，<b>不影响警报规则</b>（警报看的是「🎯 目标车型规则」）。「🔄 立即查询」会绕过 2 分钟缓存强制拉最新数据——别高频狂点，容易触发官方接口限流；「清空筛选」恢复显示全部车辆。</div>
<div class="quicknav"><span class="qn-label">⚡ 快捷跳转：</span><button class="ghost" onclick="jumpSec(0)">🎯 规则与推送</button><button class="ghost" onclick="jumpSec(1)">📦 变动日志</button><button class="ghost" onclick="jumpSec(2)">📈 趋势</button><button class="ghost" onclick="window.scrollTo({top:0,behavior:'smooth'})">⬆ 回顶部</button><span id="qn-note" class="qn-label">在页面任意位置一键展开对应板块</span></div>
<details class="sec"><summary>🎯 规则与推送 <span class="hint">目标车型规则 · 自动刷新 · 微信推送通道（点开展开）</span> <span class="help-btn" onclick="event.stopPropagation();tgHelp('hp6')">?</span></summary><div class="sec-body"><div class="cfg-grid">
  <div class="subpanel">
    <h3>🎯 目标车型规则 <span class="hint">车名含全部关键词 + 价格区间 + 城市（不限=全国）= 命中报警</span></h3>
    <div id="hp6" class="help-box" style="display:none"><b>规则怎么写？</b><br>一条规则 = 车名里<b>同时包含</b>全部关键词（且关系）+ 价格落在区间（留空 = 不限）+ <b>城市</b>（默认不限 = 全国 66 城都报；选了城市就只报那一城）。当前规则「14寸|14'' + 900 + 青玉色 + ≤650」= 全国任何城市出现 14寸900青玉色且 ≤650 就报警。「快捷添加」从下拉里选（尺寸/系列/颜色/城市）；「关键词添加」输入 a,b,c 逗号分隔一次加一条多关键词规则（无城市限定，全国生效）；点规则标签上的 ✕ 删除。规则存服务端，换设备打开看板也生效。⚠️ 上限设太低会把想买的车排除在警报外。</div>
    <div class="rowline"><span id="chips"></span></div>
    <div class="rowline">
      <span class="tlabel">快捷添加</span>
      <select id="qaSize" title="尺寸"><option value="">尺寸不限</option></select>
      <select id="qaSeries" title="系列"><option value="">系列不限</option></select>
      <select id="qaColor" title="颜色"><option value="">颜色不限</option></select>
      <select id="qaCity" title="城市"><option value="">城市不限</option></select>
      <input id="qaMin" type="number" placeholder="¥最低" style="width:72px">
      <input id="qaMax" type="number" placeholder="¥最高" style="width:72px">
      <button class="ghost" onclick="quickAdd()">＋ 添加规则</button>
    </div>
    <div id="ruletip" style="color:var(--gold);font-size:12px;min-height:16px;margin-bottom:6px"></div>
    <div class="rowline">
      <span class="tlabel">关键词添加</span>
      <input id="addKw" placeholder="多个关键词用逗号分隔，如：900,青玉" style="width:280px">
      <button class="ghost" onclick="kwAdd()">＋ 关键词规则</button>
    </div>
  </div>
  <div class="subpanel">
    <h3>🔔 提醒与推送</h3>
    <div id="hp7" class="help-box" style="display:none"><b>哪里是全局、哪里只管本页？</b><br>自动刷新、🪟 弹窗、🔔 提示音——只作用于当前浏览器页面（关了就没）；推送通道（Server酱/短信）是<b>全局的</b>：关掉浏览器、电脑睡眠前照样发到微信（后台守护线程在跑）。点「📲 推送通道」可添加多个通道，命中时全部广播；「📲 测试推送」会真实给你的微信发一条测试消息。</div>
    <div class="rowline">
      <span class="tlabel">自动刷新</span>
      <select id="fauto" title="自动定时刷新">
        <option value="0">关闭</option>
        <option value="30">每 30 秒</option>
        <option value="60" selected>每 1 分钟</option>
        <option value="120">每 2 分钟</option>
        <option value="300">每 5 分钟</option>
      </select>
      <span id="cd" style="color:var(--sub);font-size:12px"></span>
    </div>
    <div class="rowline">
      <span class="tlabel">命中提醒</span>
      <label class="tgl"><input type="checkbox" id="tmodal">🪟 弹窗</label>
      <label class="tgl"><input type="checkbox" id="tsound">🔔 提示音</label>
      <span class="help-btn" onclick="tgHelp('hp7')">?</span>
    </div>
    <div class="rowline">
      <span class="tlabel">微信/短信</span>
      <span id="wxchip" class="chip" onclick="openCfg()" title="点击管理推送通道（可添加多个微信/短信）" style="border-color:var(--green);color:var(--green);cursor:pointer">📲 推送通道：检测中… ✎</span>
      <button class="ghost" onclick="testPush(this)" style="padding:4px 12px;font-size:12px">📲 测试推送</button>
    </div>
  </div>
</div></div></details>
<details class="sec"><summary>📦 库存变动日志 <span class="hint">上下架 / 改价，每次刷新自动对比</span><span id="chg-sum" style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap;font-weight:400"></span><span class="help-btn" onclick="event.stopPropagation();tgHelp('hp5')">?</span></summary><div class="sec-body">
  <div id="hp5" class="help-box" style="display:none"><b>变动日志怎么看？</b><br>每次数据刷新都会和上次对比：🟢 上架 = 新出现在架；🔴 下架 = 被买走或撤牌；💲 改价 = 价格变化（括号里是原价）。按时间倒序，最新在最上面。若某辆命中你规则的车辆发生变动，除了记在这里还会额外推送微信。</div>
  <div id="chglist"><div class="empty" style="padding:16px">加载中…</div></div>
</div></details>
<details class="sec"><summary>📈 趋势（48小时，红色区间=目标车离架）<span id="histMeta" style="margin-left:auto;color:var(--sub);font-size:12px;font-weight:400"></span><span class="help-btn" onclick="event.stopPropagation();tgHelp('hp8')">?</span></summary><div class="sec-body">
  <div id="hp8" class="help-box" style="display:none"><b>趋势图怎么读？</b><br>记录近 48 小时的价格与在架状态（原始数据保留 7 天）。下拉可切换「目标车价格走势」或「某城市某成色的在架数量」。折线水平 = 价格没变；<b>红色阴影区间 = 目标车离架</b>（被买走或下架的时间段）。</div>
  <div class="rowline"><select id="histSel" style="width:360px"></select><button class="ghost" onclick="loadHist()">🔄 查看走势</button></div>
  <div id="histBox" style="min-height:40px"></div>
</div></details>
<div id="lb" class="lbmask" onclick="if(event.target===this)closeLb()"><img id="lbimg" alt="" decoding="async"><div id="lbnav"><button class="lbnav" onclick="event.stopPropagation();lbGo(-1)">‹</button><span id="lbidx"></span><button class="lbnav" onclick="event.stopPropagation();lbGo(1)">›</button><a id="lbori" href="#" target="_blank" onclick="event.stopPropagation()" style="color:#fff;font-size:13px;text-decoration:underline">🔗 原图</a></div></div>
<div id="rpt" class="lbmask" onclick="if(event.target===this)closeRpt()"><div class="rpt-card" onclick="event.stopPropagation()"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b style="font-size:16px">📋 官方车况报告</b><button class="ghost" onclick="closeRpt()">✕ 关闭</button></div><div id="rptbody" class="rpt-body">加载中…</div></div></div>
<div id="hitbox"><div class="hb-title" id="hbtitle">🚨 命中目标车！手慢无！</div><div class="hb-cards" id="hbcards"></div><div style="text-align:right;margin-top:4px"><span class="help-btn" onclick="tgHelp('hp2')">?</span></div><div id="hp2" class="help-box" style="display:none"><b>命中横幅怎么用？</b><br>当某辆车同时满足「🎯 目标车型规则」里的全部关键词和价格区间，就会出现在这里并金色高亮。每张卡片可以：📷 看门店实拍（最真实）、📋 看官方车况、📋 车源直达指引（复制车源+操作引导）。命中的同时会自动推送到你配置的微信/短信——关掉浏览器也照推。</div></div>
<div id="hitmodal" class="modal-mask" onclick="if(event.target===this)closeHitModal()"><div class="modal"><button class="m-close" onclick="closeHitModal()">✕ 关闭</button><div class="m-title">🚨🚨 命中目标车！手慢无！</div><div id="hm-body"></div><div style="color:var(--sub);font-size:12px;margin-top:6px">极速路径（约10秒）：微信 → 文件传输助手 → 点收藏的「二手车卡片」直达（目标车详情卡片=改价/重新上架时直接下单；新车用首页卡片切城市找车）。首次收藏：小程序打开目标车详情页 → 右上角「…」→ 转发给「文件传输助手」。二手车单件售罄即无！</div></div></div>
<div id="cfgmodal" class="modal-mask" onclick="if(event.target===this)closeCfg()"><div class="modal" style="border-color:var(--acc)"><button class="m-close" onclick="closeCfg()">✕ 关闭</button><div class="m-title" style="color:var(--acc)">📲 推送通道管理（命中时广播到全部通道）</div><div id="chlist"></div><div class="m-sec">➕ 添加新通道</div><select id="ctType" onchange="renderChFields()" style="width:100%"><option value="serverchan">Server酱（微信服务号）</option><option value="pushplus">PushPlus（微信公众号）</option><option value="aliyun_sms">阿里云短信</option><option value="tencent_sms">腾讯云短信</option></select><div id="chFields"></div><div style="color:var(--sub);font-size:12px;margin-top:4px">短信变量占位符：{city} {store} {title} {price} {quality} {sku} {count}；短信需已在云厂商备案的签名和模板，按量计费（约0.05元/条）</div><button onclick="addChannel()" style="width:100%;margin-top:10px">＋ 添加通道</button></div></div>
<div style="font-weight:700;margin:14px 0 6px">🚲 全部在售车辆（金色行 = 命中你的规则） <span class="help-btn" onclick="tgHelp('hp9')">?</span></div>
<div id="hp9" class="help-box" style="display:none"><b>列表怎么用？</b><br>每行一辆：图片点击放大（服务端已压缩，秒开不卡；灯箱右下角「🔗 原图」可看原始分辨率）；<b>📷 实拍图</b> = 门店实拍，判断真实车况的首选；<b>📋 车况</b> = 官方检测报告（含需注意项）；编码 = 车辆唯一标识（和微信推送、收藏卡片核对用）。金色整行 = 命中你的目标规则，配合上方横幅操作。</div>
<div class="legend">📖 成色参考：<span class="badge q-S">S类</span>≈未使用/展示样品 <span class="badge q-A">A类</span>≈轻微使用痕迹 <span class="badge q-B">B类</span>≈明显使用痕迹 <span class="badge q-C">C类</span>≈较重磨损 ｜ 车辆图片为官方车型图，实际车况以门店实车为准</div>
<table><thead><tr><th>图片</th><th>车型名称</th><th>城市</th><th>门店</th><th>成色</th><th>二手价</th><th>编码</th></tr></thead>
<tbody id="tb"><tr><td colspan="7" class="empty">加载中…</td></tr></tbody></table>
<div id="pager" style="display:none;align-items:center;gap:10px;margin:8px 0 14px;flex-wrap:wrap"><button class="ghost" id="pgPrev" onclick="pgGo(-1)">‹ 上一页</button><span id="pgInfo" style="font-size:13px;color:var(--sub)"></span><button class="ghost" id="pgNext" onclick="pgGo(1)">下一页 ›</button><select id="pgSize" onchange="pgResize()" style="font-size:13px"><option value="50">50/页</option><option value="100" selected>100/页</option><option value="200">200/页</option><option value="99999">全部</option></select><span style="font-size:12px;color:var(--sub)">分页只为流畅，命中车永远置顶在第一页</span></div>
</div>
<script>
let DATA=[],TS=0,lastHits=0,FAILN=0;
/* ---- 目标规则（服务端 targets.json 为准，localStorage 仅镜像） ---- */
let targets=[{kws:["14寸|14''","900","青玉"]}],targetsReady=false;
function ruleTip(t){const el=document.getElementById('ruletip');if(el){el.textContent=t||'';if(t)setTimeout(()=>{if(el.textContent===t)el.textContent=''},8000)}}
function saveTargets(){localStorage.setItem('dt_targets',JSON.stringify(targets));
 if(!targetsReady)return;
 fetch('/api/targets_save',{method:'POST',headers:{'Content-Type':'application/json','X-Dbk':'1'},body:JSON.stringify({targets})})
  .then(r=>r.json()).then(j=>{const rs=j&&j.rescan;
   if(rs&&rs.ok&&rs.total>0){const p=rs.pushed;
    ruleTip(p<0?`🔁 已重扫存量车：命中 ${rs.total} 辆，正在后台补推送…`
                :`🔁 已重扫存量车：当前命中 ${rs.total} 辆，补推送 ${p} 辆`+(rs.total>p?'（其余此前已推过）':''));}
   else if(rs&&rs.ok)ruleTip('🔁 已重扫存量车：当前无命中');
  }).catch(()=>{})}
async function initTargets(){try{const j=await(await fetch('/api/targets',{cache:'no-store'})).json();if(j.targets&&j.targets.length){targets=j.targets}else{const ls=JSON.parse(localStorage.getItem('dt_targets')||'null');if(ls&&ls.length)targets=ls.map(r=>Array.isArray(r)?{kws:r}:r);await saveTargets()}}catch(e){}targetsReady=true;renderChips();render()}
/* ---- 车源快捷操作（注：微信2023.12起要求明文scheme在目标小程序后台白名单声明，第三方小程序无法外链直达，改走「卡片书签」方案） ---- */
let MPC={path:'sportlover/zero-waste-bike/pages/home/index',query:'commodityId={commodityId}'};
async function initMpCfg(){try{MPC=await(await fetch('/api/mpconfig',{cache:'no-store'})).json()}catch(e){}}
const MP_GUIDE='📌 极速路径（约10秒）\\n① 微信 → 文件传输助手 → 点收藏的「二手车卡片」\\n  · 目标车详情卡片 → 改价/重新上架时直达详情页直接下单\\n  · 二手车首页卡片 → 新车时切换城市找这辆车\\n\\n首次收藏：小程序打开目标车详情页 → 右上角「…」→ 转发给「文件传输助手」（首页卡片同理）';
function copyCar(btn,city,title,price,quality){
 const txt='迪卡侬二手车 '+city+' '+title+' ¥'+price+' '+quality+'成色';
 (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).catch(()=>{});
 btn.textContent='✅ 车源已复制';setTimeout(()=>{btn.textContent='📋 车源直达指引'},2500);
 alert(MP_GUIDE+'\\n\\n车源信息已复制：\\n'+txt);
}
function itemBtn(d){const t=(d.title||'').replace(/['"]/g,'');return `<button class="mpbtn alt" style="margin-top:6px" onclick="copyCar(this,'${d.city||''}','${t}','${d.price||''}','${d.quality||''}')">📋 车源直达指引</button>`}
function kmatch(k,t){return k.split('|').some(a=>t.includes(a))}
function priceOK(d,r){const p=parseFloat(d.price);if(isNaN(p))return false;return (r.min==null||p>=r.min)&&(r.max==null||p<=r.max)}
function priceTxt(r){const a=[];if(r.min!=null)a.push('≥¥'+r.min);if(r.max!=null)a.push('≤¥'+r.max);return a.length?' '+a.join(''):''}
function ruleMatch(d){return targets.find(r=>(!r.city||d.city===r.city)&&r.kws.every(k=>kmatch(k,d.title))&&priceOK(d,r))||null}
function isHit(d){return !!ruleMatch(d)}
/* ---- 小程序入口指引（明文 scheme 被微信白名单封锁，不再用协议链接） ---- */
const MP_APPID='wxdbc3f1ac061903dd';
function mpBtn(extra){return '<button class="mpbtn" onclick="alert(MP_GUIDE)">🚀 打开小程序·极速指引</button>'+(extra||'')}
function copySearchTip(btn){
 const txt='迪卡侬 小程序 二手童车';
 (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(()=>{btn.textContent='✅ 已复制';
  setTimeout(()=>btn.textContent='📋 复制搜索口令',1500)}).catch(()=>{alert('复制失败，请手动输入：'+txt)});
}
function renderChips(){
 document.getElementById('chips').innerHTML=targets.length?
  targets.map((r,i)=>`<span class="chip">🎯 ${r.city?'📍'+r.city+' · ':''}${r.kws.join(' + ')||'全部车型'}${priceTxt(r)} <b onclick="delTarget(${i})">✕</b></span>`).join(' ')
  :'<span style="color:var(--sub);font-size:12px">（无规则，点右侧添加）</span>';
}
function delTarget(i){targets.splice(i,1);saveTargets();renderChips();render()}
function getPrice(){
 const mn=parseFloat(document.getElementById('qaMin').value),
       mx=parseFloat(document.getElementById('qaMax').value);
 const r={};
 if(!isNaN(mn))r.min=mn;
 if(!isNaN(mx))r.max=mx;
 return r;
}
function sameRule(a,b){return a.kws.join()===b.kws.join()&&a.min===b.min&&a.max===b.max&&(a.city||'')===(b.city||'')}
function quickAdd(){
 const s=document.getElementById('qaSize').value,
       se=document.getElementById('qaSeries').value,
       c=document.getElementById('qaColor').value,
       cy=document.getElementById('qaCity').value,
       pr=getPrice();
 if(!s&&!se&&!c&&!cy&&!Object.keys(pr).length)return;
 const r={kws:[]};
 if(cy)r.city=cy;
 if(s)r.kws.push(s+"寸|"+s+"''");
 if(se)r.kws.push(se);
 if(c)r.kws.push(c);
 Object.assign(r,pr);
 if(!targets.some(x=>sameRule(x,r))){targets.push(r);saveTargets()}
 renderChips();render();
}
function kwAdd(){
 const v=document.getElementById('addKw').value.trim(),pr=getPrice();
 if(!v&&!Object.keys(pr).length)return;
 const r={kws:v?v.split(/[,，\\s]+/).filter(Boolean):[]};
 Object.assign(r,pr);
 if(!targets.some(x=>sameRule(x,r))){targets.push(r);saveTargets()}
 document.getElementById('addKw').value='';
 renderChips();render();
}
/* ---- 微信推送（命中集合变化时上报，服务端去重） ---- */
let lastPushSig='';
function maybePush(hitItems){
 const sig=hitItems.map(({d})=>d.sku+'|'+d.price).sort().join(',');
 if(!hitItems.length||sig===lastPushSig)return;
 lastPushSig=sig;
 fetch('/api/push',{method:'POST',headers:{'Content-Type':'application/json','X-Dbk':'1'},
  body:JSON.stringify({hits:hitItems.map(({d})=>({city:d.city,store:d.store,title:d.title,quality:d.quality,price:d.price,retail:d.retail,sku:d.sku,dsm:d.dsm,model:d.model}))})})
 .then(r=>r.json()).then(j=>{if(j.pushed)console.log('已微信推送',j.pushed,'条')}).catch(()=>{});
}
/* ---- 命中弹窗（新命中集合只弹一次） ---- */
let lastHitSig=localStorage.getItem('dt_hit_sig')||'';
function showHitModal(hitItems){
 const sig=hitItems.map(({d})=>d.sku+'|'+d.price).sort().join(',');
 if(sig===lastHitSig)return;
 lastHitSig=sig;localStorage.setItem('dt_hit_sig',sig);
 if(!cfgModal)return;
 document.getElementById('hm-body').innerHTML=hitItems.map(({d,rule})=>`<div class="m-card"><div><img src="${imgP(d.image,400)}" loading="lazy" decoding="async" onclick="showLb('${d.image}')" style="cursor:zoom-in" onerror="this.onerror=null;this.src='${d.image}'"><br>${realBtn(d)}</div><div><div class="m-city">📍 ${d.city} · ${d.store||'查看门店'}</div><div style="font-size:12px;color:var(--sub);margin:2px 0">${d.title}</div><span class="badge q-${d.quality}">${d.quality}类</span> <span class="m-price">¥${d.price}</span> <span style="color:var(--sub);text-decoration:line-through;font-size:12px">¥${d.retail}</span><div style="font-size:12px;color:var(--sub);margin-top:4px">编码 ${d.sku} ｜ 命中规则: ${rule.kws.join(' + ')||'全部车型'}${priceTxt(rule)}</div>${mpBtn('<button class="mpbtn alt" onclick="copySearchTip(this)">📋 复制搜索口令</button>')}${itemBtn(d)}</div></div>`).join('');
 document.getElementById('hitmodal').style.display='flex';
}
function closeHitModal(){document.getElementById('hitmodal').style.display='none'}
/* ---- 大图查看 ---- */
/* ---- 图片代理助手（服务端缩放，原图不进浏览器防卡顿） ---- */
function imgP(u,w){return '/api/img?u='+encodeURIComponent(u||'')+'&w='+(w||900)}
function tgHelp(id){const el=document.getElementById(id);if(el)el.style.display=el.style.display==='none'?'block':'none'}
/* ---- 快捷跳转：展开并滚到折叠板块 ---- */
function jumpSec(i){const ds=document.querySelectorAll('details.sec');if(!ds[i])return;ds[i].open=true;ds[i].scrollIntoView({behavior:'smooth',block:'start'})}
/* ---- 灯箱（支持多图画廊） ---- */
let LB_LIST=null,LB_RAW=null,LB_IDX=0;
function showLb(src){
 const arr=Array.isArray(src)&&src.length;
 LB_RAW=arr?src.slice():[src];
 if(arr){LB_LIST=LB_RAW.map(u=>imgP(u,1400));LB_IDX=0;document.getElementById('lbimg').src=LB_LIST[0];document.getElementById('lbidx').textContent='1 / '+LB_LIST.length;document.getElementById('lbnav').style.display='flex'}
 else{LB_LIST=null;document.getElementById('lbimg').src=imgP(src,1400);document.getElementById('lbnav').style.display='none'}
 document.getElementById('lbori').href=LB_RAW[0]||src||'#';
 document.getElementById('lb').style.display='flex';
}
function lbGo(d){if(!LB_LIST||LB_LIST.length<2)return;LB_IDX=(LB_IDX+d+LB_LIST.length)%LB_LIST.length;document.getElementById('lbimg').src=LB_LIST[LB_IDX];document.getElementById('lbidx').textContent=(LB_IDX+1)+' / '+LB_LIST.length;document.getElementById('lbori').href=LB_RAW[LB_IDX]||'#'}
function closeLb(){document.getElementById('lb').style.display='none';LB_LIST=null}
document.addEventListener('keydown',e=>{if(document.getElementById('lb').style.display==='flex'){if(e.key==='ArrowLeft')lbGo(-1);else if(e.key==='ArrowRight')lbGo(1);else if(e.key==='Escape')closeLb()}});
/* ---- 成色实拍图 ---- */
async function showReal(btn,dsm,model,quality,city){
 const old=btn.textContent;btn.textContent='⏳';btn.disabled=true;
 try{
  const q='dsm='+encodeURIComponent(dsm)+'&model='+encodeURIComponent(model)+'&quality='+encodeURIComponent(quality||'')+'&city='+encodeURIComponent(city||'')+'&ts='+Date.now();
  const j=await(await fetch('/api/avatar?'+q,{cache:'no-store'})).json();
  const pics=(j.pics&&j.pics.length)?j.pics:(j.avatar?[j.avatar]:[]);
  if(pics.length){
   showLb(pics);
   const st=j.store||{};
   btn.textContent=pics.length+'张实拍'+(st.name?' · '+st.name:'');
   setTimeout(()=>{btn.textContent=old;btn.disabled=false},2500);
  }
  else{btn.textContent='无实拍';setTimeout(()=>{btn.textContent=old;btn.disabled=false},1500)}
 }catch(e){btn.textContent='失败';setTimeout(()=>{btn.textContent=old;btn.disabled=false},1500)}
}
function realBtn(d){return `<span style="display:inline-flex;gap:4px;margin-top:3px"><button class="ghost" style="font-size:11px;padding:1px 8px" onclick="showReal(this,'${d.dsm}','${d.model}','${d.quality}','${d.city}')">📷 实拍图</button><button class="ghost" style="font-size:11px;padding:1px 8px" onclick="showReport(this,'${d.dsm}','${d.model}','${d.quality}','${d.city}')">📋 车况</button></span>`}
async function showReport(btn,dsm,model,quality,city){
 const old=btn.textContent;btn.textContent='⏳';btn.disabled=true;
 try{
  const q='dsm='+encodeURIComponent(dsm)+'&model='+encodeURIComponent(model)+'&quality='+encodeURIComponent(quality||'')+'&city='+encodeURIComponent(city||'')+'&ts='+Date.now();
  const j=await(await fetch('/api/avatar?'+q,{cache:'no-store'})).json();
  const root=(j.deprecation&&j.deprecation.children)||[];
  const groups=root.map(g=>(g.children||[]).filter(x=>x.desc));
  const items=groups.flat();
  const isBad=x=>{const a=(x.additionalDesc||'').trim();return a&&a!=='否'&&!a.startsWith('无')};
  const gName=g=>{const s=g.map(x=>x.desc||'').join('|');
   if(/涉及到使用安全|缺少零配件/.test(s))return '🛡️ 车架与结构安全';
   if(/褪色|漆面/.test(s))return '🎨 外观漆面';
   if(/漏气|修补/.test(s))return '🛞 轮胎与轮组';
   if(/安全线|断齿|不润|间隙/.test(s))return '⚙️ 传动系统';
   if(/需要更换|清洁/.test(s))return '🧰 整备与更换';
   return '🔧 部件检查'};
  const bad=items.filter(isBad);
  const st=j.store||{};
  document.getElementById('rptbody').innerHTML=
   `<div class="rpt-head">${items.length} 项官方检测 ｜ <b style="color:${bad.length?'#d97706':'#16a34a'}">${bad.length?bad.length+' 项需注意':'全部通过 ✅'}</b>${st.name?` ｜ 🏬 ${st.name}`:''}${st.address?`<div style="font-size:12px;color:var(--sub);margin-top:2px">📍 ${st.address}</div>`:''}${st.workingHour?`<div style="font-size:12px;color:var(--sub)">🕐 ${st.workingHour}</div>`:''}</div>`+
   groups.map((g,i)=>g.length?`<div class="rpt-group"><div class="rpt-gt">${gName(g)}<span style="font-weight:400;color:var(--sub)">（${g.length} 项）</span></div>`+g.map(x=>{
     const badX=isBad(x);
     return `<div class="rpt-row${badX?' bad':''}"><span>${x.desc}</span><b>${x.additionalDesc||'—'}</b></div>`}).join('')+'</div>':'').join('')+
   `<div style="font-size:11px;color:var(--sub);margin-top:8px">数据来源：迪卡侬官方质检评估（v1 接口）</div>`;
  document.getElementById('rpt').style.display='flex';
  btn.textContent=old;btn.disabled=false;
 }catch(e){btn.textContent='失败';setTimeout(()=>{btn.textContent=old;btn.disabled=false},1500)}
}
function closeRpt(){document.getElementById('rpt').style.display='none'}
/* ---- 推送通道管理 ---- */
let CHANNELS=[];
const TYPE_META={serverchan:['🟢','Server酱(微信)'],pushplus:['🟣','PushPlus(微信)'],aliyun_sms:['🔵','阿里云短信'],tencent_sms:['🟠','腾讯云短信']};
function typeName(t){return (TYPE_META[t]||['',t])[1]||t}
function icon(t){return (TYPE_META[t]||['📲'])[0]}
const FIELDS={
 serverchan:[['name','通道名称，如 我的微信'],['key','SendKey（sct.ftqq.com，SCT 开头）']],
 pushplus:[['name','通道名称'],['key','PushPlus token（pushplus.plus）']],
 aliyun_sms:[['name','通道名称'],['access_key_id','AccessKeyId'],['access_key_secret','AccessKeySecret'],['phone','手机号'],['sign_name','短信签名（需备案）'],['template_code','模板Code（SMS_xxx）'],['params','变量映射(可选) 如 {"1":"{city} {title} ¥{price}"}']],
 tencent_sms:[['name','通道名称'],['secret_id','SecretId'],['secret_key','SecretKey'],['sdk_app_id','SmsSdkAppId'],['phone','手机号'],['sign_name','短信签名（需备案）'],['template_id','模板Id'],['params','变量映射(可选) 如 {"1":"{city} {title} ¥{price}"}']]
};
function fmtWx(j){
 const st=document.getElementById('wxchip');
 const chs=j.channels||[];
 if(!chs.length){st.textContent='📲 推送通道：未配置 ✎';st.style.color='var(--sub)';st.style.borderColor='var(--border)';return}
 const we=chs.filter(c=>c.type==='serverchan'||c.type==='pushplus').length, sm=chs.length-we;
 st.textContent='📲 推送：'+chs.length+' 通道（微信'+we+(sm?'·短信'+sm:'')+'）'+(j.last?' ｜ 上次 '+j.last:'');
 st.style.color='var(--green)';st.style.borderColor='var(--green)';
}
async function loadWxStatus(){try{fmtWx(await (await fetch('/api/push_status')).json())}catch(e){}}
async function openCfg(){
 try{const j=await(await fetch('/api/push_status')).json();CHANNELS=(j.channels||[]).map(c=>({...c}))}catch(e){CHANNELS=[]}
 renderChList();renderChFields();
 document.getElementById('cfgmodal').style.display='flex';
}
function closeCfg(){document.getElementById('cfgmodal').style.display='none';loadWxStatus()}
function renderChList(){
 document.getElementById('chlist').innerHTML=CHANNELS.length?
  CHANNELS.map((c,i)=>`<div class="chrow"><b>${icon(c.type)}</b> ${c.name||typeName(c.type)} <span class="chtype">${typeName(c.type)}</span> <button class="ghost mini" onclick="testCh(${i})">测试</button> <button class="ghost mini" onclick="delCh(${i})">删除</button></div>`).join('')
  :'<div style="color:var(--sub);font-size:12px;padding:6px 0">暂无通道，在下方添加（命中时广播到全部通道）</div>';
}
function renderChFields(){
 const t=document.getElementById('ctType').value;
 document.getElementById('chFields').innerHTML=FIELDS[t].map(([k,ph])=>`<input id="cf_${k}" placeholder="${ph}">`).join('');
}
function cf(k){const e=document.getElementById('cf_'+k);return e?e.value.trim():''}
async function saveChannels(){
 const j=await(await fetch('/api/push_channels',{method:'POST',headers:{'Content-Type':'application/json','X-Dbk':'1'},body:JSON.stringify({channels:CHANNELS})})).json();
 if(!j.saved)alert('保存失败：'+(j.info||''));
 return !!j.saved;
}
async function addChannel(){
 const t=document.getElementById('ctType').value;
 const ch={id:'c'+Date.now(),type:t};
 FIELDS[t].forEach(([k])=>{if(cf(k))ch[k]=cf(k)});
 if(t==='serverchan'&&!ch.key){alert('请填写 SendKey');return}
 if(t==='pushplus'&&!ch.key){alert('请填写 token');return}
 if(t==='aliyun_sms'&&(!ch.access_key_id||!ch.access_key_secret||!ch.phone||!ch.sign_name||!ch.template_code)){alert('阿里云短信需要填全：Key、Secret、手机号、签名、模板Code');return}
 if(t==='tencent_sms'&&(!ch.secret_id||!ch.secret_key||!ch.sdk_app_id||!ch.phone||!ch.sign_name||!ch.template_id)){alert('腾讯云短信需要填全：SecretId、SecretKey、SdkAppId、手机号、签名、模板Id');return}
 CHANNELS.push(ch);
 if(await saveChannels()){renderChList();renderChFields()}
}
function delCh(i){
 if(!confirm('删除通道「'+(CHANNELS[i].name||typeName(CHANNELS[i].type))+'」？'))return;
 CHANNELS.splice(i,1);saveChannels();renderChList();
}
async function testCh(i){
 const c=CHANNELS[i];
 const j=await(await fetch('/api/push_test_channel',{method:'POST',headers:{'Content-Type':'application/json','X-Dbk':'1'},body:JSON.stringify({id:c.id})})).json();
 alert((j.ok?'✅ 测试成功，请查收':'✗ 测试失败: '+(j.info||'').slice(0,120))+'\\n\\n通道: '+(c.name||typeName(c.type)));
}
async function testPush(btn){
 const old=btn.textContent;btn.disabled=true;btn.textContent='发送中…';
 try{
  const j=await (await fetch('/api/push_test',{method:'POST',headers:{'X-Dbk':'1'}})).json();
  btn.textContent=j.ok?'✅ 已发到微信':'✗ '+(j.info||'失败').slice(0,18);
 }catch(e){btn.textContent='✗ 网络错误'}
 setTimeout(()=>{btn.textContent=old;btn.disabled=false;loadWxStatus()},3000);
}
/* ---- 提示音与弹窗开关（localStorage 记忆） ---- */
let cfgModal=localStorage.getItem('dt_modal')!=='0';
let cfgSound=localStorage.getItem('dt_sound')!=='0';
function initToggles(){
 const tm=document.getElementById('tmodal'),ts=document.getElementById('tsound');
 tm.checked=cfgModal;ts.checked=cfgSound;
 tm.addEventListener('change',()=>{cfgModal=tm.checked;localStorage.setItem('dt_modal',cfgModal?'1':'0');
  if(cfgModal)render();});
 ts.addEventListener('change',()=>{cfgSound=ts.checked;localStorage.setItem('dt_sound',cfgSound?'1':'0');
  if(cfgSound)beep();});
}
/* ---- 提示音 ---- */
function beep(){try{const ctx=new (window.AudioContext||window.webkitAudioContext)();const o=ctx.createOscillator();const g=ctx.createGain();o.connect(g);g.connect(ctx.destination);o.type='sine';o.frequency.value=880;g.gain.value=.25;o.start();setTimeout(()=>o.stop(),400);}catch(e){}}
/* ---- 数据加载 ---- */
function schemaWarn(ok){let el=document.getElementById('schemawarn');
 if(ok!==false){if(el)el.style.display='none';return}
 if(!el)document.getElementById('status').insertAdjacentHTML('beforebegin','<span id="schemawarn" class="badge" style="border-color:var(--red);color:var(--red);margin-right:8px" title="扫到的车辆读不到价格，官方接口字段可能已改版——此时目标规则会永不命中，并非真的没车">⚠️ 数据异常：价格字段失效</span>');
 else el.style.display=''}
async function load(force){
 const s=document.getElementById('status');
 /* 手动查询才显示提示；后台自动刷新静默进行，杜绝状态栏闪变 */
 if(force)s.textContent='正在查询全国门店…（约3-6秒）';
 else if(!window._loadedOnce)s.textContent='加载中…';
 try{
  const ctl=new AbortController();setTimeout(()=>ctl.abort(),45000);
  const r=await fetch('/api/query'+(force?'?force=1':'')+(force?'&':'?')+'ts='+Date.now(),{signal:ctl.signal,cache:'no-store'});
  const j=await r.json();
  if(j.readonly&&!window._ro){window._ro=1;document.getElementById('status').insertAdjacentHTML('beforebegin','<span class="badge" style="border-color:var(--sub);color:var(--sub);margin-right:8px" title="只读模式：展示最近一次扫描快照，不访问官方接口、不推送">📖 只读模式</span>')}
  DATA=j.items;TS=j.time;FAILN=j.fail_streak||0;window._loadedOnce=true;
  schemaWarn(j.schema_ok);
  const dsig=DATA.length+'|'+DATA.map(d=>d.sku+':'+d.price).join(',');
  if(dsig!==window._dsig){window._dsig=dsig;buildFilters()}
  render();loadChanges();
  if(!window._histInit){window._histInit=1;setTimeout(loadHist,600)}
 }catch(e){
  s.innerHTML='⚠️ 数据加载失败：'+e+'　<button class="ghost" onclick="load(true)">🔄 重试</button>（3秒后自动重试）';
  setTimeout(()=>load(true),3000);
 }
}
/* ---- 库存变动日志 ---- */
async function loadChanges(){
 try{
  const j=await(await fetch('/api/changes?ts='+Date.now(),{cache:'no-store'})).json();
  const M={'上架':'t-add','下架':'t-del','改价':'t-pr'};
  document.getElementById('chg-sum').innerHTML=`<span class="badge t-add">今日上架 ${j.today_added}</span><span class="badge t-del">今日下架 ${j.today_removed}</span><span class="badge t-pr">今日改价 ${j.today_changed}</span>`;
  document.getElementById('chglist').innerHTML=j.events.length?j.events.map(e=>{const hit=ruleMatch(e);return `<div class="chg-row${hit?' chg-hit':''}"><span class="chg-time">${(e.time||'').slice(5,16)}</span><span class="chg-type ${M[e.type]||''}">${hit?'🎯 ':''}${e.type}</span><span class="chg-city">${e.city}</span><span class="chg-title">${e.title}</span>${e.type==='改价'?`<span class="chg-time">¥${e.old_price} →</span>`:''}<span class="chg-price">¥${e.price}</span></div>`}).join(''):'<div class="empty" style="padding:16px">暂无变动记录，从现在开始累计（每次刷新自动对比全国库存）</div>';
 }catch(e){}
}
function buildFilters(){ const sizes=new Set(),cities=new Set(),series=new Set(),colors=new Set();
 DATA.forEach(d=>{const t=d.title;
  const m=t.match(/(\\d{2})\\s*(寸|''|INCH)/i);if(m)sizes.add(m[1]);
  const se=t.match(/BIKE\\s*(\\d{3})/i);if(se)series.add(se[1]);
  const co=t.match(/[-—]\\s*([^\\s-]{2,6}色)/);if(co)colors.add(co[1]);
  cities.add(d.city)});
 const fs=document.getElementById('fsize'),fc=document.getElementById('fcity'),
       qs=document.getElementById('qaSize'),qe=document.getElementById('qaSeries'),qc=document.getElementById('qaColor'),qy=document.getElementById('qaCity');
 const ov=fs.value,oc=fc.value,vs=qs.value,ve=qe.value,vc=qc.value,vy=qy.value;
 fs.innerHTML='<option value="">全部尺寸</option>'+[...sizes].sort((a,b)=>a-b).map(x=>`<option ${x==ov?'selected':''}>${x}</option>`).join('');
 fc.innerHTML='<option value="">全部城市</option>'+[...cities].sort().map(x=>`<option ${x==oc?'selected':''}>${x}</option>`).join('');
 qs.innerHTML='<option value="">尺寸不限</option>'+[...sizes].sort((a,b)=>a-b).map(x=>`<option ${x==vs?'selected':''}>${x}</option>`).join('');
 qe.innerHTML='<option value="">系列不限</option>'+[...series].sort().map(x=>`<option ${x==ve?'selected':''}>${x}</option>`).join('');
 qc.innerHTML='<option value="">颜色不限</option>'+[...colors].sort().map(x=>`<option ${x==vc?'selected':''}>${x}</option>`).join('');
 qy.innerHTML='<option value="">城市不限</option>'+[...cities].sort().map(x=>`<option ${x==vy?'selected':''}>${x}</option>`).join('');
}
function render(){
 const kw=document.getElementById('kw').value.trim(),
  sz=document.getElementById('fsize').value,q=document.getElementById('fq').value,ct=document.getElementById('fcity').value;
 let rows=DATA.filter(d=>{
  if(kw&&!d.title.includes(kw))return false;
  if(q&&d.quality!==q)return false;
  if(ct&&d.city!==ct)return false;
  if(sz){const m=d.title.match(new RegExp(sz+"\\\\s*(寸|'')",'i'));if(!m)return false}
  return true});
 rows.sort((a,b)=>(isHit(b)-isHit(a))||((a.price||0)-(b.price||0)));
 const hitItems=DATA.map(d=>({d,rule:ruleMatch(d)})).filter(x=>x.rule);
 const hits=hitItems.length;
 /* 警报横幅 */
 const hb=document.getElementById('hitbox');
 if(hits){
  document.getElementById('hbtitle').textContent=`🚨 命中 ${hits} 辆目标车！手慢无！`;
  document.getElementById('hbcards').innerHTML=hitItems.map(({d,rule})=>`<div class="hb-card"><img src="${imgP(d.image,400)}" loading="lazy" decoding="async" onclick="showLb('${d.image}')" style="cursor:zoom-in" onerror="this.onerror=null;this.src='${d.image}'">${realBtn(d)}<div><div class="hb-city">${d.city} · ${d.store||'查看门店'}</div><div style="font-size:12px;color:var(--sub);margin:2px 0">${d.title}</div><span class="badge q-${d.quality}">${d.quality}类</span> <span class="hb-price">¥${d.price}</span> <span style="color:var(--sub);text-decoration:line-through;font-size:12px">¥${d.retail}</span><div style="font-size:12px;color:var(--sub)">规则: ${rule.kws.join(' + ')||'全部车型'}${priceTxt(rule)} ｜ 编码 ${d.sku} ｜ 小程序搜「二手童车」切${d.city}</div>${mpBtn()}${itemBtn(d)}</div></div>`).join('');
  hb.style.display='block';
 } else hb.style.display='none';
 if(hits>lastHits&&cfgSound)beep();
 if(hits>lastHits&&PG!==1){PG=1}/* 新命中：自动跳回第1页（命中车排序置顶），签名守卫会因 LAST_PG 变化强制重建 */
 lastHits=hits;
 /* 命中推送已由服务端守护线程接管（浏览器关闭也照推），此处仅负责展示 */
 if(hitItems.length)showHitModal(hitItems);
 document.title=hits?'🚨🚨 命中目标车！快抢！':'🚲 迪卡侬二手车全国库存查询';
 document.getElementById('st-total').textContent=DATA.length;
 document.getElementById('st-city').textContent=new Set(DATA.map(d=>d.city)).size;
 document.getElementById('st-hit').textContent=hits;
 try{buildHistOptions()}catch(e){}
 document.getElementById('status').textContent=`更新于 ${new Date(TS*1000).toLocaleString()} ｜ 筛选结果 ${rows.length} / ${DATA.length} 辆`+(FAILN>0?` ｜ ⚠️ 接口连续异常${FAILN}轮，数据可能不完整`:'');
 /* 分页：只渲染当前页切片，避免 1700 行 DOM 整体重建造成卡顿 */
 const pgEl=document.getElementById('pager');
 const pages=Math.max(1,Math.ceil(rows.length/PAGE));
 if(PG>pages)PG=pages; if(PG<1)PG=1;
 const start=(PG-1)*PAGE,slice=rows.slice(start,start+PAGE);
 /* 签名只算当前页可见行+总行数：不可见页的变动不触发重建，杜绝无谓闪烁 */
 const rsig=rows.length+'#'+slice.map(d=>d.sku+'|'+d.price+'|'+d.quality+'|'+d.city+'|'+(d.store||'')).join(';');
 if(rsig!==LAST_TB||LAST_PG!==PG){
  LAST_TB=rsig;LAST_PG=PG;
  document.getElementById('tb').innerHTML=slice.length?slice.map(d=>{
   const r=ruleMatch(d);const hit=!!r;
   const im=d.image?`<img src="${imgP(d.image,400)}" loading="lazy" decoding="async" onclick="showLb('${d.image}')" onerror="this.onerror=null;this.src='${d.image}'">`:`<div class="noimg"><span style="font-size:26px">🚲</span>只读模式无图</div>`;
   return `<tr class="${hit?'hit':''}"><td>${im}<br>${(d.dsm||d.model)?realBtn(d):''}</td>
   <td>${hit?'<span class="hitmark">🎯 </span>':''}${d.title}</td><td>${d.city}</td><td>${d.store||'—'}</td>
   <td><span class="badge q-${d.quality}">${d.quality}</span></td>
   <td><span class="price">¥${d.price}</span><span class="retail">¥${d.retail}</span></td><td>${d.sku}</td></tr>`}).join('')
   :'<tr><td colspan="7" class="empty">没有匹配的车辆</td></tr>';
 }
 if(pages>1){
  pgEl.style.display='flex';
  document.getElementById('pgInfo').textContent=`第 ${PG} / ${pages} 页 ｜ 共 ${rows.length} 辆（本页 ${slice.length} 辆）`;
  document.getElementById('pgPrev').disabled=PG<=1;
  document.getElementById('pgNext').disabled=PG>=pages;
 } else pgEl.style.display='none';
}
 let PG=1,PAGE=100,LAST_TB=null,LAST_PG=0;
function pgGo(d){PG=Math.max(1,PG+d);render();const t=document.getElementById('tb');const r0=t&&t.rows&&t.rows[0];if(r0&&r0.scrollIntoView)r0.scrollIntoView({block:'start'})}
function pgResize(){PAGE=+document.getElementById('pgSize').value;PG=1;render()}
function reset(){document.getElementById('kw').value='';document.getElementById('fsize').value='';document.getElementById('fq').value='';document.getElementById('fcity').value='';PG=1;render()}
/* ---- 趋势图（SVG，无外部依赖） ---- */
function buildHistOptions(){
 const sel=document.getElementById('histSel'),prev=sel.value;
 const opts=[];
 DATA.forEach(d=>{const r=ruleMatch(d);if(r&&d.sku){const v='t:'+d.sku+':'+(d.city||'');if(!opts.some(o=>o.v===v))opts.push({v,label:'🎯 '+(d.city||'')+' '+d.quality+'类 ¥'+d.price+' '+d.sku})}});
 [...new Set(DATA.map(d=>d.city))].sort().forEach(c=>opts.push({v:'c:'+c+':S',label:'📍 '+c+' S类库存'}));
 sel.innerHTML=opts.map(o=>`<option value="${o.v}">${o.label}</option>`).join('');
 if(opts.some(o=>o.v===prev))sel.value=prev;
}
function pickTrend(){return document.getElementById('histSel').value||''}
function parseTs(s){return new Date(s.replace('-','/').replace('-','/')).getTime()}
async function loadHist(){
 const sel=pickTrend();if(!sel)return;
 const box=document.getElementById('histBox'),meta=document.getElementById('histMeta');
 box.innerHTML='<span style="color:var(--sub);font-size:12px">加载中…</span>';
 try{
  const parts=sel.split(':');
  let url,j;
  if(parts[0]==='t'){url='/api/history?type=target&sku='+encodeURIComponent(parts[1])+'&city='+encodeURIComponent(parts[2]||'')+'&hours=48'}
  else{url='/api/history?type=city&city='+encodeURIComponent(parts[1])+'&quality='+encodeURIComponent(parts[2]||'S')+'&hours=48'}
  j=await(await fetch(url,{cache:'no-store'})).json();
  drawHist(j);
 }catch(e){box.innerHTML='<span style="color:var(--red);font-size:12px">加载失败</span>'}
}
function drawHist(j){
 const box=document.getElementById('histBox'),meta=document.getElementById('histMeta');
 const rows=j.rows||[];
 if(rows.length<2){box.innerHTML='<span style="color:var(--sub);font-size:12px">数据不足（历史刚开始累计，每2分钟一个点，约10分钟后可看趋势）</span>';meta.textContent='';return}
 const W=900,H=200,L=46,R=10,T=12,B=22;
 const t0=parseTs(rows[0].ts),t1=parseTs(rows[rows.length-1].ts),span=Math.max(t1-t0,1);
 const vals=rows.map(r=>j.mode==='target'?r.price:r.count);
 let vmin=Math.min(...vals),vmax=Math.max(...vals);
 if(vmax===vmin){vmax=vmin+1}
 const pad=(vmax-vmin)*0.08;vmin-=pad;vmax+=pad;
 const X=t=>L+(t-t0)/span*(W-L-R), Y=v=>T+(1-(v-vmin)/(vmax-vmin))*(H-T-B);
 const pts=rows.map(r=>X(parseTs(r.ts)).toFixed(1)+','+Y(j.mode==='target'?r.price:r.count).toFixed(1)).join(' ');
 let gaps=0,bands='';
 if(j.mode==='target'){
  for(let i=1;i<rows.length;i++){
   const dt=parseTs(rows[i].ts)-parseTs(rows[i-1].ts);
   if(dt>10*60000){gaps++;const x1=X(parseTs(rows[i-1].ts)),x2=X(parseTs(rows[i].ts));
    bands+=`<rect x="${x1.toFixed(1)}" y="${T}" width="${Math.max(x2-x1,2).toFixed(1)}" height="${H-T-B}" fill="rgba(255,107,107,.18)"/>`}
  }
 }
 const dotLast=rows[rows.length-1];
 const lastLabel=j.mode==='target'?('¥'+dotLast.price):('剩'+dotLast.count+'辆');
 const grid=[0,.5,1].map(f=>{const v=vmin+(vmax-vmin)*f,y=Y(v).toFixed(1);return `<line x1="${L}" y1="${y}" x2="${W-R}" y2="${y}" stroke="rgba(127,127,127,.18)" stroke-dasharray="3 4"/><text x="${L-5}" y="${(+y+4)}" fill="#9aa3b2" font-size="11" text-anchor="end">${j.mode==='target'?'¥'+v.toFixed(0):v}</text>`}).join('');
 const timeLab=[rows[0],rows[Math.floor(rows.length/2)],rows[rows.length-1]].map(r=>{const x=X(parseTs(r.ts)).toFixed(1);return `<text x="${x}" y="${H-6}" fill="#9aa3b2" font-size="11" text-anchor="middle">${r.ts.slice(5,16)}</text>`}).join('');
 box.innerHTML=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${grid}${bands}<polyline points="${pts}" fill="none" stroke="${j.mode==='target'?'#ffc53d':'#4f8cff'}" stroke-width="2"/>${rows.map((r,i)=>{if(i%Math.ceil(rows.length/40))return'';const x=X(parseTs(r.ts)),y=Y(j.mode==='target'?r.price:r.count);return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.2" fill="${j.mode==='target'?'#ffc53d':'#4f8cff'}"/>`}).join('')}<circle cx="${X(parseTs(dotLast.ts)).toFixed(1)}" cy="${Y(j.mode==='target'?dotLast.price:dotLast.count).toFixed(1)}" r="4" fill="#ff6b6b"/>${timeLab}</svg>`;
 meta.textContent=(j.mode==='target'?(j.sku+' ｜ '):((j.city||'')+' '+j.quality+'类 ｜ '))+'最新 '+lastLabel+' ｜ '+rows.length+' 个点'+(gaps?' ｜ ⚠️ 离架 '+gaps+' 次':'');
}
/* ---- 自动定时刷新 ---- */
let autoLeft=0;
function setAuto(){
 const v=parseInt(document.getElementById('fauto').value,10);
 clearInterval(window._autoTimer);autoLeft=0;window._autoTimer=null;
 if(v>0){autoLeft=v;window._autoTimer=setInterval(()=>{
   autoLeft--;
   if(autoLeft<=0){autoLeft=v;load(false);}
   document.getElementById('cd').textContent=`⏱ ${autoLeft}s 后刷新`;
 },1000);}
 else document.getElementById('cd').textContent='';
}
document.getElementById('kw').addEventListener('input',render);
document.getElementById('fauto').addEventListener('change',setAuto);
try{initToggles();renderChips();setAuto();initTargets();initMpCfg()}catch(e){console.error(e)}
loadWxStatus();load(false);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            b = HTML.encode("utf-8")
            self._send(200, b, "text/html; charset=utf-8")
        elif path == "/api/query":
            force = "force=1" in (urlparse(self.path).query or "")
            d = get_data(force)
            b = json.dumps({"time": _cache["time"], "cities": _cache["cities"], "items": d["items"],
                            "fail_streak": _guard["fail_streak"], "readonly": READONLY,
                            "schema_ok": bool(_guard["schema"].get("ok", True))},
                           ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/targets":
            b = json.dumps({"targets": load_targets()}, ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/mpconfig":
            b = json.dumps(load_mp_cfg(), ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/history":
            qs = parse_qs(urlparse(self.path).query)
            g = lambda k: (qs.get(k, [""])[0] or "")
            try:
                hours = min(int(g("hours") or 48), 168)
            except ValueError:
                hours = 48
            if g("type") == "city":
                rows = city_series(g("city"), (g("quality") or "S").upper(), hours)
                b = json.dumps({"mode": "city", "city": g("city"), "quality": (g("quality") or "S").upper(),
                                "rows": rows}, ensure_ascii=False).encode("utf-8")
            else:
                rows = target_series(g("sku"), hours, g("city") or None)
                b = json.dumps({"mode": "target", "sku": g("sku"), "city": g("city"), "rows": rows},
                               ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/img":
            qs = parse_qs(urlparse(self.path).query)
            g = lambda k: (qs.get(k, [""])[0] or "")
            try:
                w = min(max(int(g("w") or 900), 120), 1600)
            except ValueError:
                w = 900
            try:
                data = _img_proxy(g("u"), w)
            except Exception:
                data = None
            if data:
                self._send(200, data, "image/jpeg")
            else:
                self._send(404, b"img proxy failed", "text/plain")
        elif path == "/api/avatar":
            qs = parse_qs(urlparse(self.path).query)
            g = lambda k: (qs.get(k, [""])[0] or "")
            det = fetch_commodity(g("dsm"), g("model"), g("quality"), g("city"))
            b = json.dumps({"avatar": det.get("avatar", ""), "pics": det.get("pics", []),
                            "groups": det.get("groups", []), "store": det.get("store", {}),
                            "deprecation": det.get("deprecation", {}),
                            "commodityId": det.get("commodityId", "")},
                           ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/changes":
            try:
                log = json.loads(CHANGES_PATH.read_text(encoding="utf-8")) if CHANGES_PATH.exists() else []
            except Exception:
                log = []
            today = time.strftime("%Y-%m-%d")
            t_added = sum(1 for e in log if e.get("type") == "上架" and str(e.get("time", "")).startswith(today))
            t_removed = sum(1 for e in log if e.get("type") == "下架" and str(e.get("time", "")).startswith(today))
            t_changed = sum(1 for e in log if e.get("type") == "改价" and str(e.get("time", "")).startswith(today))
            b = json.dumps({"events": log[:30], "total": len(log),
                            "today_added": t_added, "today_removed": t_removed, "today_changed": t_changed},
                           ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        elif path == "/api/push_status":
            last = None
            st = BASE / "output" / "push_state.json"
            if st.exists():
                try:
                    last = json.loads(st.read_text(encoding="utf-8")).get("time")
                except Exception:
                    pass
            chs = [{"id": c.get("id"), "type": c.get("type"), "name": c.get("name")}
                   for c in load_channels()]
            b = json.dumps({"channels": chs, "last": last}, ensure_ascii=False).encode("utf-8")
            self._send(200, b, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.headers.get("X-Dbk") != "1":
            # CSRF 防护：恶意网页可向 localhost 发跨站 POST，但无法携带自定义头
            self._send(403, b"forbidden", "text/plain")
            return
        path = urlparse(self.path).path
        if path == "/api/push_channels":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                chs = body.get("channels") or []
                save_channels(chs)
                resp = {"saved": True, "count": len(chs)}
            except Exception as e:
                resp = {"saved": False, "info": str(e)}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/push_test_channel":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                ok, info = send_channel(body.get("id"), "🚲 迪卡侬监控推送测试",
                                        "该通道测试成功！命中目标车时，城市·门店·价格会直接推到这里。",
                                        {"city": "测试城市", "store": "测试门店", "title": "测试车 14寸 BIKE 900",
                                         "price": "999", "quality": "S", "sku": "TEST", "count": 1})
                resp = {"ok": ok, "info": info}
            except Exception as e:
                resp = {"ok": False, "info": str(e)}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/push_test":
            ok, info = wx_send("🚲 迪卡侬监控推送测试", "测试成功！命中目标车时，城市·门店·价格会直接推到这里。",
                               {"city": "测试城市", "store": "测试门店", "title": "测试车 14寸 BIKE 900",
                                "price": "999", "quality": "S", "sku": "TEST", "count": 1})
            resp = {"ok": ok, "info": info}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/push":
            # 浏览器命中上报（兼容保留）：统一走服务端推送（per-channel 去重），守护线程同 key 不重推
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                n = server_push_hits(body.get("hits") or [])
                resp = {"pushed": n, "ok": True}
            except Exception as e:
                resp = {"pushed": 0, "ok": False, "info": str(e)}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/mpconfig_save":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                p = str(body.get("path") or "").strip()
                q = str(body.get("query") or "").strip()
                if p:
                    save_mp_cfg({"path": p, "query": q})
                    resp = {"saved": True}
                else:
                    resp = {"saved": False, "info": "path 不能为空"}
            except Exception as e:
                resp = {"saved": False, "info": str(e)}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/targets_save":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                ts = body.get("targets") or []
                if isinstance(ts, list) and ts:
                    ts = [{"kws": [str(k) for k in (r.get("kws") if isinstance(r, dict) else r) or []],
                           **({"min": r["min"]} if isinstance(r, dict) and r.get("min") is not None else {}),
                           **({"max": r["max"]} if isinstance(r, dict) and r.get("max") is not None else {}),
                           **({"city": str(r["city"])} if isinstance(r, dict) and r.get("city") else {})}
                          for r in ts]
                    save_targets(ts)
                    # 规则一改就重扫存量车：把「改规则前就已命中、但从未推过」的车补推出来
                    # 匹配同步（毫秒级）、推送后台，避免点保存时页面卡住好几秒
                    resp = {"saved": True, "count": len(ts),
                            "rescan": rescan_existing_hits(ts, background=True)}
                else:
                    resp = {"saved": False, "info": "targets 不能为空"}
            except Exception as e:
                resp = {"saved": False, "info": str(e)}
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    if READONLY:
        # 只读模式：载入最近一次扫描快照，纯展示（不扫接口、不推送、不写变动）
        try:
            snap = json.loads(SNAP_PATH.read_text(encoding="utf-8"))
            items = [{"city": v.get("city", ""), "store": "", "title": v.get("title", ""),
                      "quality": v.get("quality", ""), "price": v.get("price"),
                      "retail": v.get("retail"), "sku": v.get("sku", ""),
                      "image": v.get("image", ""), "dsm": v.get("dsm", ""), "model": v.get("model", "")}
                     for v in snap.values()]
            items.sort(key=lambda x: (x["city"], x["price"] or 0))
            _cache.update({"time": SNAP_PATH.stat().st_mtime, "items": items,
                           "cities": len({i["city"] for i in items if i["city"]}),
                           "added": 0, "removed": 0, "price_changed": 0})
            print(f"📖 只读模式已启动: 快照 {len(items)} 辆/{_cache['cities']} 城，不扫描不推送")
        except Exception as e:
            print(f"📖 只读模式启动，但快照载入失败（页面将为空）: {e}")
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    else:
        print(f"🚲 迪卡侬二手车查询服务已启动(多线程): http://localhost:{PORT}")
        threading.Thread(target=guardian, daemon=True, name="guardian").start()
        print(f"🛡️ 守护线程已启动: 每{GUARD_INTERVAL}s巡检，命中/变动自动推送（无需开浏览器）")
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
