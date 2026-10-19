---
name: boss-scrape-layer
description: src/boss_scrape 独立抓取层：stoken 就地滚动零成本、详情页是 SSR（detail.json 不会自己发）、三种短页面必须分开、每 IP 请求墙与风控阶梯、恢复要开新页面
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 抓取层（`src/boss_scrape`）

脱离数据库与前端的独立抓取层，`D:\Code\Boss\src\boss_scrape`，入口 `python -m boss_scrape probe|search|verify|logs`。
`job_radar` 采集链路与刷新链路都复用它；分析层 `src/job_analysis` 与本层无关。

## stoken：就地滚动，零请求

平台限流是**按请求次数**的窗口，一个 `__zp_stoken__` 撑约 5 条，之后 `code=37`「您的环境存在异常」。
等待无效（实测干等 90 秒仍是 37），只有换 token 能续。

关键实测：**每个 `code=0` 的响应都下发 `__zp_sseed__` / `__zp_sname__` / `__zp_sts__`**，
`__zp_stoken__ = new ABC().z(seed, sts + (480+tzOffset)*60000)`，ABC 从 `/web/common/security-js/<sname>.js` 取。
所以取完一条就地算新 token，成本 **0 个请求**；旧的「整页导航重铸」实测 92–993 个请求。

两个必踩的坑：

- **patchright 的 `page.evaluate` 在隔离世界**，注入 `<script>` 跑在主世界，`window.ABC` 两边看不见。
  必须用 DOM 属性桥（`data-zp-*`）传值。
- **cookie 值必须 URL 编码**（`quote(token, safe="")`），塞原始 base64 会一直 `code=37`。

## 三种短页面必须分开

| 页面 | 长度 | 标志 | 处置 |
|---|---:|---|---|
| 岗位已下架 | 约 11KB | 404 / 「页面不存在」 | 判关闭，**别重试** |
| JS 挑战页 | 约 41KB | `security-check` / 请稍候 | 停机，等自解 |
| 验证页 | 约 44.5KB | 「安全验证」/ `passport/zp/verify` | 开新页面自愈 |

混成一类会让脚本对着已下架的岗位反复重试——重试一万次也变不出内容，看起来却完全像风控。
正常岗位页 138–147KB，60KB 是保守下界。

## 请求墙与风控阶梯（实测）

正常 → 约 **86 个请求/IP**（2–4 秒节奏）→ JS 挑战（自愈 10–16 秒）→ 滑块（要人）→ `403.html?code=31`（24 小时，页面写明解封时刻）。
出口已被降级时，额度掉到 23–25 个。**机房 IP 会被直接挑战，家庭 IP 健康**。

⚠️ **这两个数字都有适用条件，2026-10-19 更正**：

- **86 请求/IP** 是**刷新链路**走代理出口、2–4 秒节奏下测的。采集链路是
  **直连本机家宽**，不适用这个阈值。
- **10–16 秒自愈**出自 `boss_scrape/refresh.py:292`，原文是
  「**屏内窗口** 10–16 秒自愈，**屏外窗口一直不跳转**」，样本 n=10~18。
  采集链路配置 `window_position: 2400,1400` 是**屏外**，按原文根本不该期待自愈。

采集链路的实测（2026-10-19）：会话健康，正常岗位页 157,819 / 161,707 字节；
偶发的 `security.html` 是**单请求分流**，重开一次就恢复 —— 既不是会话级封禁，
也不是岗位属性。详见 `D:\Code\Boss\docs\PLAYBOOK.md`。

撞墙后**不许继续请求**：实测每一级都不可逆地推高下一级。

## 恢复必须开新页面

复用那个已经跑了几十次 fetch 的页面做 `goto`，恢复不了；**新开一个页面** `goto` 同一 URL，
约 12 秒后站点自己跳到 `job_detail/...?_security_check=1_<ts>`。
原因是 `fetch()` 只拿到验证页 HTML、不执行它的 JS，而校验逻辑就在那段 JS 里。

Chrome 对**被遮挡/后台**页面会节流定时器（`setTimeout` 压到约每分钟一次），挑战跑不完，
所以启动参数必须有 `--disable-background-timer-throttling` /
`--disable-backgrounding-occluded-windows` / `--disable-renderer-backgrounding`。
不要靠把窗口开到屏内解决——无人值守时会抢用户焦点。

## 详情页是 SSR：正文在文档里，`detail.json` 不会自己发

2026-10-19 实测，直接 `goto /job_detail/{id}.html` 后**页面一次都不请求 `job/detail.json`**
（等 40 秒超时，`detail_requests: []`）。它发的是 `jobdetail/popupguides/query.json` 和
`zpitem/web/competitive/jobDetail.json`，都不是正文。

正文在 SSR 的 `.job-sec-text` 里，**和 detail.json 的 `postDescription` 逐字相等**：

```
2187e8053a1d033b03V93d-4GFBV  库里 743 字 → 渲染 DOM 740 字 → 归一化后 661/661，差 0
efbf18628af27d300nJ_29-6GFBW  库里 7181 字 → 渲染 DOM 7130 字 → 归一化后 6111/6111，差 0
```

只放行 document、其余请求全 `abort()` 时，146–157KB 的 HTML 里 `.job-sec-text` 就是全文
（720/7066 字，首尾各 200 字都命中）。所以**最省的详情路径是 1 次文档请求、0 次接口调用**，
用 `htmlpage.py` 解析即可，不需要 JS 引擎、不需要 `securityId`、不需要 stoken。

页面里还内联着 `var _jobInfo = {job_id, user_id, job_name, job_salary, company, securityId, position}`
和 `var _userInfo = {isLogin, ...}`。两个推论：

- **`securityId` 可以就地重铸**——剪枝删掉的那批不是死路，打开详情页就能取到新的。
- **登录态不用额外请求**：`_userInfo.isLogin` 就在 HTML 里，比 `getUserInfo.json` 少一次接口。

这条路径已在 `collectors/boss.py::_collect_ssr_detail` 落地，配置 `detail_transport: ssr`。
实测两条已知长度的岗位：743→720、7181→7066 字，**归一化后与 detail.json 逐字相等**，
每岗位 1 个请求。招聘者姓名/头衔/ID、地址、技能、岗位状态、HR 活跃文案全部能拿到；
岗位状态写「关闭」时置 `job_invalid=True`，没写就保留原标记（只降不升，与刷新链路同一判据）。

⚠️ 不 abort 子资源时，一次详情页导航会发 **24 个 `/wapi/` 引导请求**，比直接 fetch 还贵。
⚠️ 之前记的「页面 HTML 只有 362 字摘要」是**错的**：那是页面内 `fetch()` 拿到的 HTML，
不是导航返回的文档。两者不是同一个东西。

## 解析在 Python 侧

`fetch` 只取字节，按容器取值走 `htmlpage.py`（标准库 `html.parser`，无新依赖），
判定是纯函数。这样整条链能用真实 HTML 脱网测试。代价是每条多传约 140KB 过 CDP。

相关：[[boss-refresh-chain]] [[boss-risk-control]] [[boss-job-radar]]
