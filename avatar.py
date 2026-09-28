# -*- coding: utf-8 -*-
"""成色实拍图 & 车辆详情 — 免凭证双接口
==========================================
1) v3 门店/车型: GET .../v3/open/customer/commodity/dsm/{dsmCode}?modelCode=X&city=Y&quality=Z
   → content.models[].commodities[].avatar / commodityId
2) v1 单车详情: GET .../v1/open/customer/commodity/{commodityId}（2026-09-15 由用户抓包发现）
   → picList（分组多图: OVERALL整体图/DETAIL细节图等）、pics（扁平列表）、
     store（门店名/地址/营业时间）、deprecation（折损评估明细树）
注意：图床 recycle.object.decathlon.com.cn 经系统代理可能 502/解析失败，默认直连（禁代理）。
"""
import json
import time
import urllib.request
from urllib.parse import quote, urlencode

V3_DSM_API = "https://buyback.decathlon.com.cn/recycle-gateway/recycle/v3/open/customer/commodity/dsm/{dsm}"
V1_COMMODITY_API = "https://buyback.decathlon.com.cn/recycle-gateway/recycle/v1/open/customer/commodity/{cid}"

_cache = {}    # dsm|model|quality|city -> (time, result_dict)
_TTL = 300

# 图床直连，不走系统代理
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with _opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _query_v3(dsm: str, model: str, quality: str, city: str):
    """调 v3 dsm 接口，返回 (avatar, commodityId)。"""
    params = {"modelCode": model or "", "quality": quality or "",
              "latitude": "34.193462", "longitude": "108.880916"}
    if city:
        params["city"] = city
    url = V3_DSM_API.format(dsm=quote(str(dsm))) + "?" + urlencode({k: v for k, v in params.items() if v})
    try:
        data = _get_json(url)
        for m in ((data.get("content") or {}).get("models") or []):
            if model and str(m.get("modelCode")) != str(model):
                continue
            for c in (m.get("commodities") or []):
                return c.get("avatar", ""), c.get("commodityId", "")
    except Exception:
        pass
    return "", ""


def _query_v1(cid: str):
    """调 v1 单车详情，返回 dict（失败返回 {}）。"""
    if not cid:
        return {}
    try:
        data = _get_json(V1_COMMODITY_API.format(cid=quote(str(cid))))
        if data.get("code") == "OK":
            return data.get("content") or {}
    except Exception:
        pass
    return {}


def fetch_commodity(dsm: str, model: str, quality: str, city: str) -> dict:
    """查一辆车的完整详情（多图+门店+折损）。返回：
    {commodityId, pics:[...], groups:[{type,desc,pics:[...]}], store:{name,address,workingHour},
     deprecation:{children:[...]}, avatar} —— 查不到时 pics 为空列表。
    两跳请求（v3取id → v1取详情），5分钟缓存；带quality失败自动去quality重试。"""
    if not dsm:
        return {"pics": [], "groups": [], "store": {}, "avatar": "", "commodityId": ""}
    key = f"{dsm}|{model}|{quality}|{city}"
    now = time.time()
    if key in _cache and now - _cache[key][0] < _TTL:
        return _cache[key][1]

    avatar, cid = _query_v3(dsm, model, quality, city)
    if not cid and quality:  # quality 不匹配时重试
        avatar, cid = _query_v3(dsm, model, "", city)

    result = {"commodityId": cid, "pics": [], "groups": [], "store": {},
              "deprecation": {}, "avatar": avatar or ""}
    detail = _query_v1(cid) if cid else {}
    if detail:
        result["pics"] = detail.get("pics") or []
        result["groups"] = [
            {"type": g.get("picType", ""), "desc": g.get("picTypeDesc", ""), "pics": g.get("pics") or []}
            for g in (detail.get("picList") or [])]
        st = detail.get("store") or {}
        result["store"] = {k: st.get(k, "") for k in ("name", "address", "workingHour", "city")}
        result["deprecation"] = detail.get("deprecation") or {}
        if not avatar:
            result["avatar"] = detail.get("avatar", "")
    # 命中结果缓存 5 分钟；查不到图的"空结果"只缓存 60 秒，尽快允许重试
    _cache[key] = (now if (result.get("pics") or result.get("avatar")) else now - _TTL + 60, result)
    return result


def fetch_avatar(dsm: str, model: str, quality: str, city: str) -> str:
    """兼容旧接口：只取一张主实拍图URL；查不到返回空字符串。"""
    if not dsm:
        return ""
    return fetch_commodity(dsm, model, quality, city).get("avatar", "")
