# -*- coding: utf-8 -*-
"""
迪卡侬小程序「官方二手车」全国库存监控
====================================
用法:
  python decathlon_monitor.py            # 全国扫描（cities.json 存在时）或按 config.json 单城市
  python decathlon_monitor.py --test     # 只测连通性

逻辑:
  1. 读取 cities.json（全国有门店的城市+门店映射，由 build_cities.py 生成）
  2. 并发请求各城市二手车列表接口（无需登录凭证）
  3. 按目标筛选: 14寸 + 900系列 + 青玉色
  4. 命中 -> output/HIT.json + 报告；未命中 -> 记录 latest.json
依赖: 仅 Python 标准库
"""
import json
import re
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
CITIES_PATH = BASE / "cities.json"
OUT_DIR = BASE / "output"
OUT_DIR.mkdir(exist_ok=True)

# 免疫抓包工具（Reqable）退出后的系统代理残留：所有外呼一律直连
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

sys.path.insert(0, str(BASE))
from avatar import fetch_commodity
from wechat_push import push_hits

TARGETS = {"size": "14", "model_keywords": ["900"], "color_keywords": ["青玉"]}


def hit_text(h: dict) -> str:
    """推送文案（wechat_push 用它做去重 key 的一部分，保持稳定）"""
    return (f"🚨 {h['城市']}·{h.get('门店名') or '门店编码' + str(h['门店编码'])}\n"
            f"{h['名称']}\n成色 {h['成色']}类 ｜ ¥{h['价格']}（原价 ¥{h['原价']}）\n"
            f"编码 {h['车辆编码']}\n"
            f"👉 微信小程序搜「二手童车」切{h['城市']}下单（单件售罄即无）")


def hit_fields(h: dict) -> dict:
    """短信通道变量字段"""
    return {"city": h.get("城市", ""), "store": h.get("门店名", ""), "title": h.get("名称", ""),
            "price": h.get("价格", ""), "quality": h.get("成色", ""), "sku": h.get("车辆编码", "")}

# ---------- HTTP ----------

def do_request(req_cfg: dict) -> str:
    method = (req_cfg.get("method") or "GET").upper()
    headers = dict(req_cfg.get("headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    body = req_cfg.get("body")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")
    request = urllib.request.Request(req_cfg["url"], data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")

# ---------- 名称识别 ----------

NAME_PRIORITY = ("dsmtitle", "skuname", "productname", "goodsname", "title", "spuname", "itemname", "name")

def get_name(item: dict):
    for token in NAME_PRIORITY:
        for k, v in item.items():
            if token == k.lower() and isinstance(v, str) and v.strip():
                return v
    return None

# ---------- 匹配 ----------

def match_item(item: dict, targets: dict):
    title = get_name(item) or ""
    text = title or " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))
    if not text:
        return None
    size = str(targets.get("size", "14"))
    size_ok = bool(re.search(rf"{size}\s*(?:寸|''|inch|″|in)", text, re.I))
    model_ok = any(kw in text for kw in targets.get("model_keywords", []))
    color_ok = any(kw in text for kw in targets.get("color_keywords", []))
    if size_ok and model_ok and color_ok:
        return {
            "名称": title[:80],
            "价格": item.get("sellingPrice"),
            "原价": item.get("retailPrice"),
            "成色": item.get("quality"),
            "门店编码": item.get("dsmCode"),
            "车辆编码": item.get("randomItemCode"),
            "图片": item.get("mainImage"),
            "原始数据": item,
        }
    return None

# ---------- 主流程 ----------

def scan_one(city: str, store_map: dict, template: dict, targets: dict):
    """扫描单个城市，返回 (city, items_count, hits, error, inventory)"""
    body = json.loads(json.dumps(template["body"], ensure_ascii=False))
    body["city"] = city
    url = template["url"]
    if "?" not in url:
        url += "?pageIndex=0&pageSize=200"
    req = {"name": city, "method": "POST", "url": url,
           "headers": template.get("headers", {}), "body": body}
    try:
        raw = do_request(req)
        data = json.loads(raw)
        if data.get("code") != "OK":
            return city, 0, [], f"{city}: code={data.get('code')}", []
        results = (data.get("content") or {}).get("results") or []
        hits = []
        inventory = []
        for it in results:
            inventory.append({"sku": it.get("randomItemCode", ""), "city": city,
                              "title": (get_name(it) or "").strip('"'),
                              "quality": it.get("quality", ""),
                              "price": it.get("sellingPrice"), "retail": it.get("retailPrice")})
            h = match_item(it, targets)
            if h:
                h["城市"] = city
                h["门店名"] = store_map.get(str(h["门店编码"]), "") or "门店详见小程序"
                hits.append(h)
        return city, len(results), hits, None, inventory
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return city, 0, [], f"{city}: {e}", []

def run(test_mode=False):
    targets = TARGETS
    template = None
    if CFG_PATH.exists():
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        if cfg.get("requests"):
            template = cfg["requests"][0]
    if template is None:
        print("[X] config.json 缺失且无模板")
        sys.exit(2)

    # 城市列表: cities.json 优先（只保留有门店的城市）
    cities = {}
    if CITIES_PATH.exists():
        all_cities = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
        cities = {c: {str(k): v for k, v in stores} for c, stores in all_cities.items() if stores}
    print(f"[i] 目标城市: {len(cities)} 个" if cities else "[i] 单城市模式")

    all_hits, total_items, errors, inventory = [], 0, [], []
    if cities:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(scan_one, c, m, template, targets) for c, m in cities.items()]
            for f in as_completed(futs):
                city, n, hits, err, inv = f.result()
                total_items += n
                all_hits += hits
                inventory += inv
                flag = "🎯" if hits else "✓"
                print(f"  [{flag}] {city}: {n} 辆" + (f"，命中 {len(hits)}!" if hits else ""))
                if err:
                    errors.append(err)
    else:
        city, n, hits, err, inv = scan_one(cfg.get("city", "西安市"), {}, template, targets)
        total_items, all_hits, errors, inventory = n, hits, [err] if err else [], inv
        print(f"  [✓] {city}: {n} 辆，命中 {len(all_hits)}")

    # 变动日志已统一由 webapp 守护线程记录（约2分钟一轮，先于本脚本发现变动）；
    # 本脚本只做兜底推送，不再写 changes.json（避免双扫描源重复事件）。
    # 推送去重键与看板统一为 sku|city|price，双方互不重推。

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (OUT_DIR / "latest.json").write_text(
        json.dumps({"time": ts, "scanned_cities": len(cities) or 1, "total_items": total_items,
                    "hits": all_hits, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 🚲 迪卡侬二手车监控报告（全国）", f"⏰ {ts} ｜ 城市 {len(cities) or 1} ｜ 车辆 {total_items} ｜ 命中 {len(all_hits)}", ""]
    if all_hits:
        lines.append("## 🎯 命中目标（14寸 + 900系列 + 青玉色）")
        for i, h in enumerate(all_hits, 1):
            lines.append(f"{i}. **{h['城市']}·{h['门店名']}** ｜ **{h['名称']}**\n   - 成色: {h['成色']} ｜ 二手价: ¥{h['价格']}（原价 ¥{h['原价']}） ｜ 车辆编码: {h['车辆编码']}\n   - 图片: {h['图片']}")
            det = fetch_commodity((h.get("原始数据") or {}).get("dsmCode", ""),
                                  (h.get("原始数据") or {}).get("modelCode", ""),
                                  h.get("成色", ""), h.get("城市", ""))
            st = det.get("store") or {}
            if st.get("name"):
                lines.append(f"   - 🏬 {st['name']}" + (f"（{st['address']}）" if st.get("address") else ""))
            pics = det.get("pics") or []
            if pics:
                lines.append(f"   - 📸 成色实拍（共{len(pics)}张）: " + " ｜ ".join(pics[:3]))
        (OUT_DIR / "HIT.json").write_text(
            json.dumps({"time": ts, "hits": all_hits}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            push_hits(all_hits, hit_text, hit_fields,
                      photo_fn=lambda h: fetch_commodity(
                          (h.get("原始数据") or {}).get("dsmCode", ""),
                          (h.get("原始数据") or {}).get("modelCode", ""),
                          h.get("成色", ""), h.get("城市", "")),
                      key_fn=lambda h: f"{h.get('车辆编码')}|{h.get('城市')}|{h.get('价格')}")  # 命中 -> 全通道推送（去重键与看板守护线程统一）
        except Exception as e:
            print(f"[微信推送] ✗ 异常: {e}")
    else:
        lines.append("## 暂无命中，继续等待上新…")
        hit_file = OUT_DIR / "HIT.json"
        if hit_file.exists():
            hit_file.unlink()
    if errors:
        lines.append(f"\n## ⚠️ 异常 {len(errors)} 条")
        lines += [f"- {e}" for e in errors[:10]]
    (OUT_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md").write_text(
        "\n".join(lines), encoding="utf-8")

    print(f"\n===== 结果: 城市 {len(cities) or 1} ｜ 车辆 {total_items} ｜ 命中 {len(all_hits)} ｜ 异常 {len(errors)} =====")
    return len(all_hits)

if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
