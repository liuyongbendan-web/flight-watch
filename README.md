# ✈️ 机票盯价（Trip.com）

告诉它你想坐哪条航线、哪几天、多少钱以下才买，它会**每 2 小时自动去 Trip.com 查一次价**：

- 价格跌到你的理想价以下 → **手机马上响**，告诉你是哪一班、几点起飞、怎么下单；
- **每晚 9 点**给你发一张图：过去 24 小时的价格曲线；
- 直飞、转机分开比价；可以只看某个时段起飞；可以要求「越快越好」，或者「中转时在那个城市停留一两天玩一玩」。

全部在 GitHub 的云端电脑上运行：**不用开着电脑，不用装软件，不花钱**。你只需要一部手机和一个 GitHub 账号。

> 它只提醒，**不会替你买票**。收到提醒后，自己点开链接去 Trip.com 下单。

---

## 第一次设置（约 30 分钟，一次就好）

### 第 1 步：注册 GitHub

1. 打开 <https://github.com/signup>，用邮箱注册（免费）。
2. 把你的 **GitHub 用户名** 发给分享这个项目的朋友，他会邀请你。
3. 邮箱里会收到一封 GitHub 邀请邮件，点 **Accept invitation**（接受邀请）。

### 第 2 步：复制一份成你自己的

1. 接受邀请后会进入这个项目的页面。点右上角的 **Fork**。
2. 下一页什么都不用改，直接点绿色的 **Create fork**。
3. 现在网址变成了 `github.com/你的用户名/flight-watch`——这是**你自己的一份**，后面所有设置都在这里做。

### 第 3 步：允许它自动运行

1. 在你自己的那份里，点上方的 **Actions** 标签。
2. 会看到一个绿色按钮 **I understand my workflows, go ahead and enable them**，点它。
3. 左边列表点 **flight-watch**，如果右边有黄色提示条说定时任务被关闭，点提示条里的 **Enable workflow**。

### 第 4 步：让它能推送到你的手机

我们用一个叫 **ntfy** 的免费 App 收通知。

1. 手机应用商店搜索 **ntfy** 安装（图标是白底黑色的对话框）。
2. 想一个**别人猜不到的主题名**，比如 `xiaomei-flight-8273649105`
   （名字 + 一串随机数字，只能用英文字母、数字、横线）。
   ⚠️ 这个主题名相当于密码：谁知道它，谁就能看到你的推送。不要用太简单的名字。
3. 打开 ntfy → 点 **+** → 输入刚才的主题名 → **Subscribe**（订阅）。
4. 回到电脑上你的 GitHub 仓库页面：
   **Settings**（齿轮，最右边）→ 左边 **Secrets and variables** → **Actions** → 绿色按钮 **New repository secret**
   - **Name** 填：`FW_WEBHOOK_URL`
   - **Secret** 填：`https://ntfy.sh/你的主题名`（例如 `https://ntfy.sh/xiaomei-flight-8273649105`）
   - 点 **Add secret**。

### 第 5 步：填你要盯的航线

1. 回到仓库首页，点 `flight-watch` 文件夹 → 点 `watchlist.json`。
2. 点右上角的**铅笔图标**（Edit this file）。
3. 按下面「清单怎么填」修改，**只改冒号后面的值，别删引号、逗号、括号**。
4. 改完点右上角绿色的 **Commit changes…** → 弹窗里再点一次 **Commit changes**。

保存后它会**马上查一次**（3–5 分钟）。到 **Actions** 标签能看到正在跑的任务：
黄色圆圈 = 正在查，绿色勾 = 成功，红色叉 = 出错了（见下面「常见问题」）。

第一次成功后，就再也不用管它了。

---

## 清单怎么填

`watchlist.json` 里，`"entries": [ ... ]` 中间每一段 `{ ... }` 是一条航线。

| 项目 | 意思 | 例子 |
|---|---|---|
| `note` | 备注，写给自己看 | `"春节回国"` |
| `enabled` | `true` 在盯，`false` 暂停 | `true` |
| `origin` | 出发机场三字码 | `"SYD"` |
| `destination` | 到达机场三字码 | `"PVG"` |
| `from` | 最早出发日期 | `"2027-01-20"` |
| `to` | 最晚出发日期（只看一天就和 `from` 一样）。最多相隔 40 天 | `"2027-01-25"` |
| `max_price` | 理想价（澳元，总价）。低于等于它才推送 | `800` |
| `max_stops` | 最多转几次：`0` 只要直飞，`1` 最多一转，`null` 不限 | `1` |
| `allow_self_transfer` | 接受「自行转机」吗（两张票拼起来，要自己取行李重新值机）。`true`/`false` | `false` |
| `need_bag` | 是否只要含托运行李的票 | `false` |
| `adults` | 几个成人 | `1` |
| `depart_slots` | 起飞时段，空 `[]` 表示都行。可选 `"morning"`（0–12 点）`"afternoon"`（12–18 点）`"evening"`（18–24 点） | `["morning", "afternoon"]` |
| `exclude` | 区间里不要的日期 | `["2027-01-22"]` |
| `transfer_pref` | 中转偏好，见下方：`"cheap"` / `"fast"` / `"stopover"` | `"cheap"` |
| `max_duration_h` | （`fast` 用）全程最多几小时，`null` 不限 | `14` |
| `max_layover_h` | （`fast` 用）每次中转最多等几小时，`null` 不限 | `3` |
| `stopover_city` | （`stopover` 用）想停留游玩的城市三字码 | `"BKK"` |
| `stopover_min_h` / `stopover_max_h` | （`stopover` 用）停留时长范围（小时） | `12` / `48` |

其他几项（`id`、`promo_code`、`auto_prepare`、`snipe_at`）保持原样即可。`id` 每条要不一样，随便写 6 个字母数字。

最下面 `settings` 里：
- `passenger_name`：你护照上的英文名，推送里提醒你下单时勾这个乘机人；
- `report_hour`：每天几点发价格曲线日报（0–23），`-1` 不发。

### 三种中转偏好

- **`"cheap"` 便宜优先**：只看价格，找最便宜的。
- **`"fast"` 越快越好**：配合 `max_duration_h`、`max_layover_h`，超出的航班不算，在剩下的里找最便宜。
- **`"stopover"` 中转玩一玩**：在 `stopover_city` 停留 `stopover_min_h`–`stopover_max_h` 小时。会同时找两种：
  一张票里本来就在那儿长时间中转的（有保障），和「出发地→停留城市」「停留城市→目的地」两张单程票拼的（常常更便宜，但**两张票互不保障**：前一段延误误了后一段要自己负责；行李要重新托运；入境签证自己确认）。日期 `from`/`to` 指第一段。

### 想盯多条航线？

复制一整段 `{ ... }`，两段之间加一个英文逗号，再改内容和 `id`：

```json
"entries": [
  { "id": "a1", "origin": "SYD", "destination": "PVG", ... },
  { "id": "b2", "origin": "SYD", "destination": "TYO", ... }
],
```

### 常用三字码

悉尼 SYD · 墨尔本 MEL · 布里斯班 BNE · 上海 SHA（含浦东/虹桥）· 北京 BJS · 广州 CAN · 成都 CTU · 香港 HKG · 台北 TPE · 东京 TYO · 大阪 OSA · 首尔 SEL · 新加坡 SIN · 吉隆坡 KUL · 曼谷 BKK · 清迈 CNX · 胡志明市 SGN · 巴厘岛 DPS · 奥克兰 AKL
其他城市搜索「城市名 IATA code」。

---

## 收到推送后

推送里会写好航班号、起飞时间、价格和下单步骤。点开通知会直接打开 Trip.com 的搜索结果页：

1. 点 **Cheapest**（最便宜排序）；
2. 找推送里写的那一班 → **View Details** → **Select**；
3. 填乘机人 → 不要的保险、附加项都选 No → 到付款页；
4. 付款前核对总价。价格随时会变，以页面为准。

---

## 常见问题

**Actions 里出现红叉？**
点进去看红字提示。最常见是第 4 步的 `FW_WEBHOOK_URL` 没设或填错。修好后，到 Actions → flight-watch → **Run workflow** 手动再跑一次。出错时 GitHub 也会发邮件给你。

**一直没收到推送？**
说明价格还没低于你的理想价，正常。每晚 9 点的日报能看到它确实在查。想测试推送，可以临时把 `max_price` 改高一点。

**想立刻查一次？**
Actions → 左边 flight-watch → 右边 **Run workflow** → 绿色按钮。

**想暂停？**
把那条航线的 `enabled` 改成 `false`。全部不想要了：Actions → flight-watch → 右上角 `…` → **Disable workflow**。

**要花钱吗？**
不用。GitHub 免费账号每月有 2000 分钟云端时间，每 2 小时查一次大约用 1000–1500 分钟。航线很多时每次会跑得更久；在 GitHub 头像 → **Settings** → **Billing and licensing** 能看到用了多少。超了也不会扣钱，只是当月停止运行。

**提示有更新？**
分享给你的朋友修好问题后，你仓库首页会出现 **Sync fork** 按钮，点它 → **Update branch** 就更新了。你的航线清单不会被覆盖。

**日期过去了？**
那条航线会自动不再查。改成新日期或者删掉即可。

---

## 须知

- 仅供个人使用。数据来自 Trip.com 澳洲站（价格为澳元），Trip.com 改版时可能暂时失效。
- 推送的价格是查价那一刻的价格，下单时以 Trip.com 页面为准。
- 你的航线和查价记录保存在你自己的私有仓库里，别人看不到。
- 如果分享者把你从原项目移除，你复制的这份也会被 GitHub 删除。
