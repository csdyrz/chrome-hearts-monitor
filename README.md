# Chrome Hearts 官网服装上新监控

实时轮询 `chromehearts.com` 的服装分类页,一旦发现新上架商品(按商品 SKU 判新),
立刻推送到你的微信(通过 Server酱)。

## 它是怎么工作的

- 官网用的是 Salesforce Commerce Cloud (Demandware),分类页**服务端渲染**,
  每个商品都带一个 `<span class="product-metadata" data-pid data-name data-price ...>`。
- 所以脚本用普通 HTTP 请求就能解析出商品,**不需要开浏览器**,又快又省资源。
- 每个商品的 SKU(`data-pid`)唯一,脚本记住已见过的 SKU,**出现新 SKU = 上新**,立刻通知。
- 跨分类自动去重:同一件衣服同时挂在 `/hoodie` 和 `/mens`,只通知一次。

## ⚠️ 必读:能力边界

- Chrome Hearts 是**限量秒罄**模式:衣服上架后可能几分钟甚至几十秒就售罄。
  脚本默认每 90 秒扫一轮,**理论上可能错过"上架→秒罄→下架"全过程极快的款**。
  好消息是:实测官网**售罄商品通常仍会以 `OUT OF STOCK` 留在列表里一段时间**,
  所以即使没抢到,也大概率能收到"上新(已售罄)"通知。想更激进可把间隔调到 30~60 秒。
- 微信无法被个人脚本直接调用,这里走 **Server酱** 中转(它把消息发到你关注的服务号)。

## 一、配置 Server酱(拿 SendKey)

1. 手机/电脑打开 https://sct.ftqq.com
2. 用**微信扫码登录**。
3. 登录后按提示**关注它的服务号**(这样才能收到推送)。
4. 在「SendKey」页面复制你的 SendKey(形如 `SCTxxxxxxxxxxxxxxxx`)。
5. 打开本目录的 `config.json`,把它填进 `serverchan_sendkey`:

   ```json
   "serverchan_sendkey": "SCT你的key",
   ```

6. 验证配置是否成功:

   ```powershell
   python chrome_hearts_monitor.py --test-push
   ```

   微信收到测试消息即配置成功。

## 二、运行

```powershell
# 持续监控(主用法,一直开着)
python chrome_hearts_monitor.py

# 只跑一轮就退出(调试看效果)
python chrome_hearts_monitor.py --once

# 自检:用香水/袜子分类验证抓取逻辑(无需 sendkey)
python chrome_hearts_monitor.py --selftest
```

- 首次运行会**静默建立基线**(把当前在架的都记为"已知",不轰炸你),之后才推送真正的新增。
- 运行日志写在 `monitor.log`,已见过的 SKU 存在 `state.json`(删掉它会重新建立基线)。

## 三、让它长期挂着 / 开机自启

最省心的方式是用 **Windows 任务计划程序**:

1. 开始菜单搜「任务计划程序」打开。
2. 右侧「创建基本任务」→ 取名 `ChromeHeartsMonitor`。
3. 触发器选「计算机启动时」(或「登录时」)。
4. 操作选「启动程序」:
   - 程序/脚本:`python`(或填 Python 完整路径,可用 `where python` 查)
   - 添加参数:`chrome_hearts_monitor.py`
   - 起始于:`C:\Users\chens\chrome-hearts`
5. 完成后右键该任务→属性→可勾选「不管用户是否登录都要运行」让它后台常驻。

> 想临时后台跑也可以:`Start-Process python -ArgumentList 'chrome_hearts_monitor.py' -WindowStyle Hidden`

## 四、自定义(改 `config.json`)

| 字段 | 含义 |
|---|---|
| `serverchan_sendkey` | Server酱 SendKey |
| `poll_interval_seconds` | 轮询间隔秒数(默认 90;想更快设 30~60) |
| `categories` | 监控的分类 slug 列表(默认覆盖卫衣/T恤/外套/裤子等) |
| `notify_out_of_stock` | 售罄的新品是否也通知(默认 true) |
| `desktop_notification` | 是否弹 Windows 桌面通知(默认 true) |
| `first_run_silent` | 首次运行是否静默建基线(默认 true) |

可监控的服装 slug(已确认官网存在):
`hoodie hoodies sweatshirt sweatshirts t-shirt shirt shirts jacket sweater sweaters pants shorts denim mens womens shop`
另外饰品类还有:`hats hat beanie bags eyewear shoes boots`(需要可自行加进 `categories`)。

## 五、注意

- 别把间隔设得过低(如几秒),频繁请求可能触发官网风控(它用了 Forter 风控)。30 秒以上较稳妥。
- 仅供个人监控自用,请遵守官网条款,不要用于抢购脚本/批量下单等用途。
