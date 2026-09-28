# 🚲 迪卡侬官方二手车全国捡漏监控

> 监控迪卡侬官方回收平台（buyback.decathlon.com.cn）全国 66 城的在售二手自行车/童车，关键词+价格规则命中后 **30 秒内推送微信**，自带本地网页看板。
>
> *Decathlon official buyback (second-hand) nationwide monitor with a local web dashboard and WeChat push alerts.*

![看板首屏](screenshots/layout_quicknav.png)

## ✨ 功能

- **全国 66 城并发扫描**：10 线程并发 + 自动翻页，单轮 1700~2200 辆在售车
- **规则命中警报**：关键词（且关系）+ 价格区间 + 城市 三要素规则，命中即金色高亮 + 推送
- **服务端推送**：守护线程每 30 秒巡检，关掉浏览器照样推；Server酱 / PushPlus / 阿里云短信 / 腾讯云短信 多通道广播，失败通道自动补发
- **库存变动追踪**：上架🟢 / 下架🔴 / 改价💲 自动 diff；防抖去除跨城/短时间抖动的假事件
- **目标车生命周期**：被抢/改价/重新上架单独告警，多城市同款独立跟踪（`城市|sku` 复合键）
- **网页看板**：统计卡 / 多维筛选 / 分页大表 / 命中横幅 / 实拍图画廊 / 官方车况报告 / 48 小时价格趋势图 / 快捷跳转导航
- **图片代理**：服务端 Pillow 缩放 + 磁盘缓存，列表小图秒开，灯箱看大图不卡
- **只读模式**：`readonly.flag` 开关 —— 只展示快照不访问官方接口
- **看门狗自愈**：Windows 计划任务每 5 分钟探活拉起

## 📦 安装

```bash
git clone https://github.com/<YOU>/decathlon-buyback-monitor.git
cd decathlon-buyback-monitor
pip install -r requirements.txt
```

要求：Python 3.10+（图片代理需要 Pillow）。

## 🚀 快速开始

1. **配置接口**（默认可用）：`config.json` 已内置官方公开接口参数，可参考 `config.example.json` 按自己抓包结果调整城市/坐标
2. **配置推送**：`cp push_config.example.json push_config.json`，填入你的 Server酱 SendKey（[sct.ftqq.com](https://sct.ftqq.com) 免费申请）等
3. **配置目标规则**：`cp targets.example.json targets.json`，改自己的关键词和预算
4. **启动看板**：

```bash
python webapp.py
# 浏览器打开 http://localhost:8787
```

5. **（可选）看门狗自愈**：Windows 任务计划程序创建任务，每 5 分钟运行 `watchdog.bat`；服务挂掉自动拉起，`readonly.flag` 存在时以只读模式启动

## 🖐️ 接口怎么来的

官方回收小程序的接口无需登录凭证，公开可调。想自己重新抓包（换城市/品类参数）看 [docs/抓包教程.md](docs/抓包教程.md) 和 [零基础手把手版](docs/抓包教程-手把手版.md)（使用 Reqable，全程点鼠标）。

## 📁 目录结构

```
├── webapp.py             # 网页看板 + 服务端守护推送（核心，单文件）
├── decathlon_monitor.py  # 命令行扫描器（config.json 驱动）
├── wechat_push.py        # 多通道推送（Server酱/PushPlus/阿里云短信/腾讯云短信）
├── tracker.py            # 库存变动 diff + 防抖（城市|sku 复合键）
├── rules.py              # 目标规则匹配
├── history.py            # 价格/库存历史（SQLite，趋势图数据源）
├── daily_report.py       # 每日日报生成+推送
├── avatar.py             # 实拍图/官方车况详情（免凭证双接口）
├── build_cities.py       # 城市门店坐标表构建
├── cities.json           # 全国城市/门店坐标
├── watchdog.bat          # 看门狗（计划任务用，支持只读模式开关）
├── py_test.py            # Python 回归测试（20 断言）
├── sim_pg.js / js_test.js / js_check.js  # 前端模拟/检查
└── docs/                 # 抓包教程
```

## 🧪 测试

```bash
python py_test.py      # tracker/rules/生命周期 20 断言
node js_test.js        # 前端逻辑测试（需服务运行）
```

## ⚠️ 免责声明

- 本项目仅供个人学习与研究，请勿用于任何商业用途
- 数据来自迪卡侬官方回收平台公开接口，版权归迪卡侬所有；请控制扫描频率，尊重平台服务
- "迪卡侬 / Decathlon" 商标归其权利人所有，本项目与其无任何关联
- 请合理设置扫描间隔（内置了 25 秒强刷节流与 2 分钟数据缓存），勿对官方接口造成压力

## 📄 License

[MIT](LICENSE)
