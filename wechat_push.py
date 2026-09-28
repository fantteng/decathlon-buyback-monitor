# -*- coding: utf-8 -*-
"""
多通道推送模块 — 微信(Server酱/PushPlus) + 短信(阿里云/腾讯云)
================================================================
配置 push_config.json（多通道）:
{"channels": [
  {"id":"c1","type":"serverchan","name":"我的微信","key":"SCTxxx"},
  {"id":"c2","type":"pushplus","name":"备用微信","key":"tokenxxx"},
  {"id":"c3","type":"aliyun_sms","name":"阿里短信","access_key_id":"AK","access_key_secret":"SK",
   "phone":"138xxxx","sign_name":"签名","template_code":"SMS_xxx",
   "params":{"1":"{city} {title} ¥{price}"}},
  {"id":"c4","type":"tencent_sms","name":"腾讯短信","secret_id":"SID","secret_key":"SKEY",
   "sdk_app_id":"1400xxxx","phone":"138xxxx","sign_name":"签名","template_id":"1xxx",
   "params":{"1":"{city} {title} ¥{price}"}}
]}
兼容旧单通道格式 {"type":"serverchan","key":"..."}（自动迁移）

短信变量占位符可用: {city} {store} {title} {price} {quality} {sku} {count}
对外接口:
  send(title, content, fields=None)        -> (any_ok, 汇总info)  广播所有通道
  send_channel(id, title, content, fields) -> (ok, info)          单通道测试
  load_channels() / save_channels(list)
  load_pushed() / record_pushed(keys)
依赖: 仅 Python 标准库
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
import urllib.request
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "push_config.json"
PUSHED_PATH = BASE / "output" / "pushed.json"


def load_channels():
    if not CFG_PATH.exists():
        return []
    try:
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(cfg, dict) and isinstance(cfg.get("channels"), list):
        return cfg["channels"]
    if isinstance(cfg, dict) and (cfg.get("key") or cfg.get("token")):  # 旧单通道格式
        return [{"id": "legacy", "type": cfg.get("type"), "name": "微信通道",
                 "key": cfg.get("key") or cfg.get("token")}]
    return []


def save_channels(channels):
    CFG_PATH.write_text(json.dumps({"channels": channels}, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def _render(text, fields):
    for k, v in (fields or {}).items():
        text = text.replace("{" + k + "}", str(v))
    return text


# ---------- 基础 HTTP ----------

def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def _post_json(url, data, headers):
    req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------- 微信通道 ----------

def _serverchan(ch, title, content, fields):
    res = _post_form(f"https://sctapi.ftqq.com/{(ch.get('key') or '').strip()}.send",
                     {"title": title[:32], "desp": content[:3000]})
    try:
        return json.loads(res).get("code") == 0, res[:200]
    except Exception:
        return "success" in res.lower(), res[:200]


def _pushplus(ch, title, content, fields):
    res = _post_form("https://www.pushplus.plus/send",
                     {"token": (ch.get("key") or "").strip(), "title": title[:100],
                      "content": content[:3000], "template": "txt"})
    try:
        return json.loads(res).get("code") == 200, res[:200]
    except Exception:
        return "success" in res.lower(), res[:200]


# ---------- 阿里云短信（POP RPC 签名, HMAC-SHA1） ----------

def _pe(s):
    return urllib.parse.quote(str(s), safe="-_.~")


def _aliyun_sms(ch, fields):
    pm = ch.get("params") or {"1": "{city} {title} ¥{price}"}
    tp = {k: _render(str(v), fields) for k, v in pm.items()}
    params = {
        "AccessKeyId": ch.get("access_key_id"), "Action": "SendSms", "Format": "JSON",
        "PhoneNumbers": ch.get("phone"), "RegionId": "cn-hangzhou", "SignName": ch.get("sign_name"),
        "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0", "TemplateCode": ch.get("template_code"),
        "TemplateParam": json.dumps(tp, ensure_ascii=False),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    q = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    sts = "GET&%2F&" + _pe(q)
    key = (ch.get("access_key_secret") or "") + "&"
    sig = base64.b64encode(hmac.new(key.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    url = "https://dysmsapi.aliyuncs.com/?" + q + "&Signature=" + _pe(sig)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = r.read().decode("utf-8", errors="replace")
    d = json.loads(res)
    return d.get("Code") == "OK", (d.get("Message") or res)[:200]


# ---------- 腾讯云短信（TC3-HMAC-SHA256 签名） ----------

def _tencent_sms(ch, fields):
    pm = ch.get("params") or {"1": "{city} {title} ¥{price}"}
    pset = [_render(str(v), fields) for _, v in sorted(pm.items())]
    phone = str(ch.get("phone") or "")
    if phone.startswith("+86"):
        phone = phone[3:]
    phone = "+86" + phone
    ts = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    host = "sms.tencentcloudapi.com"
    payload = {"PhoneNumberSet": [phone], "SmsSdkAppId": ch.get("sdk_app_id"),
               "SignName": ch.get("sign_name"), "TemplateId": ch.get("template_id"),
               "TemplateParamSet": pset}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    hashed = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canon = ("POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:" + host +
             "\n\ncontent-type;host\n" + hashed)
    sts = ("TC3-HMAC-SHA256\n" + str(ts) + "\n" + date + "/sms/tc3_request\n" +
           hashlib.sha256(canon.encode("utf-8")).hexdigest())

    def _h(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    kdate = _h(("TC3" + (ch.get("secret_key") or "")).encode("utf-8"), date)
    kterm = _h(_h(_h(kdate, "sms"), "tc3_request"), sts)
    sig = hmac.new(kterm, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (f"TC3-HMAC-SHA256 Credential={ch.get('secret_id')}/{date}/sms/tc3_request, "
            f"SignedHeaders=content-type;host, Signature={sig}")
    headers = {"Authorization": auth, "Content-Type": "application/json; charset=utf-8",
               "Host": host, "X-TC-Action": "SendSms", "X-TC-Version": "2021-01-11",
               "X-TC-Timestamp": str(ts), "X-TC-Region": ch.get("region", "ap-guangzhou")}
    res = _post_json(f"https://{host}/", payload, headers)
    d = json.loads(res)
    err = (d.get("Response") or {}).get("Error")
    return (not err), ((err or {}).get("Message") or res)[:200]


# ---------- 统一发送 ----------

DISPATCH = {"serverchan": _serverchan, "pushplus": _pushplus,
            "aliyun_sms": _aliyun_sms, "tencent_sms": _tencent_sms}


def send_one(ch, title, content="", fields=None):
    t = (ch.get("type") or "").strip().lower()
    fn = DISPATCH.get(t)
    if not fn:
        return False, f"未知通道类型: {t}"
    try:
        return fn(ch, title, content, fields or {})
    except Exception as e:
        return False, str(e)


def send(title, content="", fields=None):
    """广播所有通道。返回 (any_ok, 汇总信息)。"""
    chs = load_channels()
    if not chs:
        return False, "未配置任何推送通道"
    results, any_ok = [], False
    for ch in chs:
        ok, info = send_one(ch, title, content, fields)
        name = ch.get("name") or ch.get("type")
        results.append(f"{name}: {'✅' if ok else '✗ ' + str(info)[:60]}")
        any_ok = any_ok or ok
    return any_ok, " ｜ ".join(results)


def send_channel(cid, title, content="", fields=None):
    for ch in load_channels():
        if ch.get("id") == cid:
            return send_one(ch, title, content, fields)
    return False, "通道不存在"


# ---------- 去重记录（per-channel） ----------
# pushed.json 新格式: {key: {"time": "...", "ok": [channel_id, ...]}}
# 旧格式（字符串列表）自动迁移，视为所有通道均已推送。
# 推送时只发给"尚未成功收到"的通道，失败通道下次同 key 再推时自动补发。

PUSHED_LIMIT = 2000  # 最多保留条数，超出按时间淘汰最旧


def _load_pushed_store() -> dict:
    if not PUSHED_PATH.exists():
        return {}
    try:
        d = json.loads(PUSHED_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
        if isinstance(d, list):  # 旧格式迁移
            return {k: {"time": "", "ok": ["*"]} for k in d}
    except Exception:
        pass
    return {}


def _save_pushed_store(store: dict):
    if len(store) > PUSHED_LIMIT:
        items = sorted(store.items(), key=lambda kv: kv[1].get("time") or "")
        store = dict(items[-PUSHED_LIMIT:])
    tmp = PUSHED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PUSHED_PATH)


def push_content(key: str, title: str, content: str, fields=None):
    """按 key 去重的服务端推送：只发给尚未成功收到的通道。
    返回 (any_new_ok, info, 本轮新推送成功的通道数)。"""
    chs = load_channels()
    if not chs:
        return False, "未配置推送通道", 0
    store = _load_pushed_store()
    done = set((store.get(key) or {}).get("ok") or [])
    pending = [c for c in chs if c.get("id") not in done and "*" not in done]
    if not pending:
        return False, "该事件已推送过", 0
    results, new_ok = [], []
    for ch in pending:
        ok, info = send_one(ch, title, content, fields or {})
        name = ch.get("name") or ch.get("type")
        results.append(f"{name}: {'✅' if ok else '✗ ' + str(info)[:60]}")
        if ok:
            new_ok.append(ch.get("id"))
    if new_ok:
        done.update(new_ok)
        store[key] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": sorted(done)}
        _save_pushed_store(store)
    return bool(new_ok), " ｜ ".join(results), len(new_ok)


def _dep_bad(x: dict) -> bool:
    """折损检测项是否需注意：'否' 或以'无'开头 = 正常，其他（如'简单清洁'）= 需注意。"""
    a = (x.get("additionalDesc") or "").strip()
    return bool(a) and a != "否" and not a.startswith("无")


def push_hits(hits: list, fmt, fields_fn=None, photo_fn=None, key_fn=None) -> int:
    """推送命中列表（按 key 去重 + per-channel 补发）。fmt(h)->str 单条文案；
    key_fn(h)->str 去重键（默认 fmt(h)，建议传 sku|city|price 与服务端统一）；
    fields_fn(h)->dict 可选，供短信变量渲染；
    photo_fn(h)->str|dict 可选：str=单张实拍图URL；dict=avatar.fetch_commodity 结果
    （含 pics 多图 + store 门店信息），微信推送自动附门店与前3张实拍图。返回实际推送条数。"""
    if not hits:
        return 0
    kf = key_fn or fmt
    chs = load_channels()
    if not chs:
        print("[推送] ✗ 未配置任何推送通道")
        return 0
    store = _load_pushed_store()
    new = []
    for h in hits:
        k = kf(h)
        done = set((store.get(k) or {}).get("ok") or [])
        if "*" in done or len(done) >= len(chs):
            continue
        new.append((h, k))
    if not new:
        return 0
    lines = [fmt(h) for h, _ in new]
    fields = fields_fn(new[0][0]) if fields_fn else {}
    fields = dict(fields, count=len(new))
    content = "\n\n—————\n\n".join(lines)
    if photo_fn:
        try:
            photo = photo_fn(new[0][0])
            if isinstance(photo, dict):  # 新格式：fetch_commodity 结果
                st = photo.get("store") or {}
                if st.get("name"):
                    content += f"\n\n🏬 {st['name']}"
                    if st.get("address"):
                        content += f"\n📍 {st['address']}"
                dep = photo.get("deprecation") or {}
                items = [x for g in (dep.get("children") or []) for x in (g.get("children") or []) if x.get("desc")]
                if items:
                    bad = [x for x in items if _dep_bad(x)]
                    summary = f"📋 官方车况：{len(items)} 项检测，" + ("全部通过 ✅" if not bad else f"{len(bad)} 项需注意 ⚠️")
                    content += f"\n\n{summary}" + ("".join(f"\n- {x.get('desc')}：{x.get('additionalDesc')}" for x in bad[:3]))
                pics = (photo.get("pics") or [])[:3] or ([photo["avatar"]] if photo.get("avatar") else [])
                if pics:
                    content += f"\n\n📸 车辆成色实拍（共{len(photo.get('pics') or pics)}张，附前{len(pics)}张）："
                    content += "\n\n".join(f"![实拍{i+1}]({p})" for i, p in enumerate(pics))
            elif photo:  # 旧格式：单张 URL
                content += f"\n\n📸 车辆成色实拍：\n\n![实拍图]({photo})"
        except Exception:
            pass
    batch_key = ",".join(sorted(k for _, k in new))
    ok, info, _n = push_content(batch_key, f"🚨 迪卡侬二手车命中 {len(new)} 辆!", content, fields)
    print(f"[推送] {'✅ 已推送 ' + str(len(new)) + ' 条 → ' + info if ok else '✗ 未推送: ' + info}")
    return len(new) if ok else 0
