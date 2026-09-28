# -*- coding: utf-8 -*-
"""Python 侧永久回归 — 覆盖 2026-09-16 逮住的三类 bug
====================================================
用法（改 tracker.py / rules.py / webapp.py 的匹配与生命周期逻辑后必跑）：
  python py_test.py
全过输出 ALL PASS 并 exit 0；任何断言失败 exit 1。

覆盖：
1. tracker._item_key：城市|sku 复合键（sku 全国不唯一坑）
2. tracker.diff_and_record：跨城出现/消失各自报事件、同城往返防抖抵消、
   跨城反向不抵消、改价城市正确
3. tracker._identity：跨城同 sku 不被错误配对
4. rules.match_rules：城市过滤、价格缺失不命中、关键词且关系
5. webapp.check_target_changes：双城同款生命周期独立（复合键）
6. history.target_series：按 sku+city 过滤趋势点位
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tracker  # noqa: E402
import rules  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="dbk_test_"))
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def car(sku, city, price="709.90", quality="S", title="14寸LEARNING BIKE 900 二合一-青玉色"):
    return {"sku": sku, "city": city, "title": title, "quality": quality,
            "price": price, "retail": "999.90"}


GZ = car("4311500@S", "广州市", "709.90")
BJ = car("4311500@S", "北京市", "569.90")

# ---------- 1. _item_key 复合键 ----------
check("1.1 同sku跨城 key 不同",
      tracker._item_key(GZ) != tracker._item_key(BJ),
      f"{tracker._item_key(GZ)} vs {tracker._item_key(BJ)}")
check("1.2 key 含城市前缀", tracker._item_key(GZ).startswith("广州市|"))
check("1.3 sku 缺失退化 key 仍含城市",
      tracker._item_key({"city": "深圳市", "title": "X", "quality": "S", "price": "1"})
      .startswith("深圳市|"))

# ---------- 2/3. diff_and_record + _identity ----------
snap1, chg1 = TMP / "s1.json", TMP / "c1.json"
items = [GZ]
tracker.diff_and_record(items, snap1, chg1, log_human=False)  # 建基线

# 2.1 北京出现同款第二辆 → 只报北京上架
a, r, c = tracker.diff_and_record(items + [BJ], snap1, chg1, log_human=False)
evs = json.loads(chg1.read_text(encoding="utf-8"))
bj = [e for e in evs if e.get("city") == "北京市"]
check("2.1 北京出现: 上架1条且城市正确", a == 1 and len(bj) == 1 and bj[0]["type"] == "上架",
      f"a={a} bj={bj}")

# 2.2 广州下架 + 北京在架（同sku跨城）→ 只报广州下架
a, r, c = tracker.diff_and_record([BJ], snap1, chg1, log_human=False)
evs = json.loads(chg1.read_text(encoding="utf-8"))
gz = [e for e in evs if e.get("city") == "广州市" and e["type"] == "下架"]
check("2.2 广州下架: 只报广州", r == 1 and len(gz) == 1, f"r={r} gz={gz}")

# 2.3 跨城反向（全新日志：基线只有广州，一次 diff 换成北京）→ 上架+下架都保留，不得跨城抵消
snap3, chg3 = TMP / "s3.json", TMP / "c3.json"
tracker.diff_and_record([GZ], snap3, chg3, log_human=False)  # 基线
a, r, c = tracker.diff_and_record([BJ], snap3, chg3, log_human=False)
check("2.3 跨城反向不抵消: 上架+下架各1条", a == 1 and r == 1, f"a={a} r={r}")

# 2.4 同城快速往返 → 按设计抵消；他城真实事件不受牵连
#     接 2.2 的状态（snapshot=[BJ]，log=[上架北京,下架广州]）：清空列表 → 北京下架与2.1上架
#     同城配对抵消（a+r=0）；广州下架(2.2)不受北京翻转牵连，仍留在日志
a, r, c = tracker.diff_and_record([], snap1, chg1, log_human=False)
evs = json.loads(chg1.read_text(encoding="utf-8"))
bj_kept = [e for e in evs if e.get("city") == "北京市" and e["sku"] == "4311500@S"]
gz_kept = [e for e in evs if e.get("city") == "广州市" and e["type"] == "下架"]
check("2.4 同城往返抵消且不牵连他城事件", a + r == 0 and not bj_kept and len(gz_kept) == 1,
      f"a={a} r={r} bj={bj_kept} gz={gz_kept}")

# 2.5 改价：城市+老价格正确
snap2, chg2 = TMP / "s2.json", TMP / "c2.json"
base = [GZ, car("2540275@S", "石家庄市", "639.90", "S", "20寸EXPLORE 500")]
tracker.diff_and_record(base, snap2, chg2, log_human=False)
changed = [dict(x, price="599.90") if x["city"] == "石家庄市" else x for x in base]
a, r, c = tracker.diff_and_record(changed, snap2, chg2, log_human=False)
evs = json.loads(chg2.read_text(encoding="utf-8"))
cg = [e for e in evs if e["type"] == "改价"]
check("2.5 改价1条且城市/老价正确",
      c == 1 and len(cg) == 1 and cg[0]["city"] == "石家庄市" and cg[0]["old_price"] == "639.90",
      f"c={c} cg={cg}")

# ---------- 4. rules.match_rules ----------
R_CITY = [{"kws": ["900", "青玉"], "max": 650, "city": "北京市"}]
R_ANY = [{"kws": ["900", "青玉"], "max": 650}]
check("4.1 城市相符命中", rules.match_rules(BJ, R_CITY) is not None)
check("4.2 城市不符不命中", rules.match_rules(GZ, R_CITY) is None)
check("4.3 无城市规则全国命中", rules.match_rules(BJ, R_ANY) is not None)
check("4.4 价格缺失不命中（宁可漏报不推假警报）",
      rules.match_rules(dict(BJ, price=None), R_ANY) is None)
check("4.5 关键词且关系（缺一不可）",
      rules.match_rules(dict(BJ, title="14寸LEARNING BIKE 900"), R_ANY) is None)
check("4.6 关键词 | 或语法",
      rules.match_rules(dict(BJ, title="14''LEARNING BIKE 900 青玉色"), R_ANY) is not None)

# ---------- 5. check_target_changes 复合键（import webapp 不启动服务） ----------
import webapp  # noqa: E402

webapp._guard["tstate"] = {}
al = webapp.check_target_changes([GZ, BJ])
check("5.1 双城同款播种: 零警报+两个独立状态",
      al == [] and set(webapp._guard["tstate"]) == {"广州市|4311500@S", "北京市|4311500@S"},
      f"keys={list(webapp._guard['tstate'])}")
al = webapp.check_target_changes([GZ])
check("5.2 北京消失: 只报北京下架",
      len(al) == 1 and al[0][0] == "下架" and al[0][1]["city"] == "北京市", f"al={al}")
al = webapp.check_target_changes([GZ, BJ])
check("5.3 北京重现: 只报北京重新上架",
      len(al) == 1 and al[0][0] == "重新上架" and al[0][1]["city"] == "北京市", f"al={al}")
al = webapp.check_target_changes([dict(GZ, price="649.90"), BJ])
check("5.4 广州改价: 只报广州且带old_price",
      len(al) == 1 and al[0][0] == "改价" and al[0][1]["old_price"] == "709.90", f"al={al}")

# ---------- 6. target_series 城市过滤 ----------
import history  # noqa: E402

test_db = history.DB_PATH
history.DB_PATH = TMP / "h.db"
try:
    import sqlite3
    conn = sqlite3.connect(TMP / "h.db")
    conn.execute("CREATE TABLE target_hist(ts TEXT, sku TEXT, city TEXT, title TEXT,"
                 " quality TEXT, price REAL)")
    now = time.strftime(history._TS)
    for h in (GZ, BJ):
        conn.execute("INSERT INTO target_hist VALUES(?,?,?,?,?,?)",
                     (now, h["sku"], h["city"], h["title"], h["quality"], float(h["price"])))
    conn.commit()
    conn.close()
    all_rows = history.target_series("4311500@S", 48)
    gz_rows = history.target_series("4311500@S", 48, city="广州市")
    check("6.1 不带城市: 两城点位都返回", len(all_rows) == 2)
    check("6.2 带城市: 只返回该城点位",
          len(gz_rows) == 1 and gz_rows[0]["city"] == "广州市" and gz_rows[0]["price"] == 709.9)
finally:
    history.DB_PATH = test_db

# ---------- 结果 ----------
print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("ALL PASS")
