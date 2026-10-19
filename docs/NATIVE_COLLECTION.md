# 网页原生采集通道

2026-10-01。原项目持久 Chrome 会话可用；以下结论以真实页面请求和独立 SQLite 验收为依据。标准 Playwright MCP 的空白/关窗问题不用于推断此通道不可用。

## 连接与身份

| 业务 | 本机实际请求 | 来源核对 |
|---|---|---|
| 搜索、滚动翻页 | `POST /wapi/zpgeek/search/joblist.json`，表单编码 | `city`、`query`、`page`、`pageSize`、`scene`；额外筛选必须与计划一致 |
| 初始自动选中或点击卡片 | `GET /wapi/zpgeek/job/detail.json` | 参数名为 `securityId`、`lid`、`_`；岗位身份回到响应 `jobInfo.encryptId` 核对 |
| 列表身份 | `jobList[].encryptJobId` | 保存为 `boss:<ID>`，不使用标题、公司或列表序号作为主键 |
| 卡片定位 | 链接 `/job_detail/<ID>.html` | 当前卡片没有 `data-jobid`；按链接定位唯一卡片，不按序号回退 |

原生列表每页15条；原有GET通道每页30条。`page_size`只控制原有接口，网页方式由网站决定。主轮`20260930-main`保留原有GET采样；原生通道更换采样语义，须建立新批次。批次指纹会阻止两种计划续进同一批次。

请求来源仅保存必要业务参数和参数名，不导出Cookie、请求认证头或临时凭据。列表`hasMore=false`结束；重复页没有新岗位则报错并保持查询未完成。`totalCount`保留为平台响应字段，不能当作市场完整覆盖的分母。

`anonymous`不能用于判定未登录或正文截断：已核验81份正文中，69份为0、5份为20、7份为101；非零值与匿名公司岗位相关。这是本轮实证关联，不是官方字段契约。正文核验依赖页面登录状态、成功业务信封、对应岗位ID及实际显示，不将匿名公司误删。

## 实现与使用

`collectors/boss.py`的`_native_list`监听网页自身响应，并核对实际请求参数。`_collect_native_detail`按链接ID定位卡片，接收同次详情并复核返回ID与页面正文。首次导航自动选中的详情在当前查询内短暂缓存，不为首个卡片重复发送手工接口请求。缓存不跨查询，最多30个响应句柄；结束时移除监听。

配置：`list_source: native`。设置`collect_details: true`时，采集同时取卡片详情；设置为false时独立读列表。已有`details`补全任务继续使用原有同源详情接口，适合数据库存量岗位。默认保留`api`，保证旧配置和旧批次兼容；控制台配置页提供选择。

网页调用仍通过已有访问策略和节奏控制。捕获原生响应不等于浏览器所有子请求都已记录；验收中的18条是采集器受控导航、列表和详情操作的计数，不用它估算浏览器总网络成本。

暂停或验证异常仍停止，不计算/伪造平台校验凭据，不自动重试业务限制。详情失败保留已获得的列表，不能把列表字段当作完整正文。同步模型抽取不抢浏览器；模型仍独立5并发。

## 实际验收

`local/runs/20261001/native-connections.json`：上海LLM、深圳AI Agent两组查询，各两页，共60个列表记录；4个手动选中岗位的详情ID匹配，响应正文出现在页面。

`local/runs/20261001/native-pipeline-acceptance.json`及`native-pipeline-v2.sqlite3`：生产采集器完成杭州、上海、深圳AI Agent各两页，每城30个唯一ID；合计90个列表记录、9份核验详情、0详情失败，SQLite `quick_check=ok`。每城包含首次自动选中的详情；查询完成记录真实落库。主数据池没有写入这些验收岗位，只有正常请求记账。

`local/runs/20261001/native-filters-acceptance.json`：杭州LLM的本科/1–3年/全职实际表单参数为degree=203、experience=104、jobType=1901，返回15条；上海AI Agent应届生/全职为experience=102、jobType=1901，本次返回0条。两组实际城市、关键词、页码和条件均匹配计划。零岗位成功响应保留最后一次安全查询来源，不误归类为传输失败。

第一次生产验收暴露了首卡没有新请求、`anonymous`误判两项问题；修正后重新运行上述三城验收。该成果不声称验证了任意筛选组合、搜索全量覆盖或长期账号可用性。

本轮未编写或运行单元测试。源码编译、真实响应、页面显示、数据库和批次回执组成验收证据。

## 控制台加载

控制台已加载`list_source`字段。此前管理员权限未解决执行策略拒绝；用户更新命令放行规则后，原样重启命令成功（启动PID16468）。真实界面显示两种采集方式，选择native有效，未保存配置变更，页面无脚本错误；原先误显示过期的标注现已正确显示为当前版本。回执`local/runs/20261001/service-restart-acceptance.json`。

日常启动命令：

```powershell
cd D:\Code\Boss
uv run job-radar serve --config config.local.yaml --port 8765
```

配置页对旧服务缺少字段时隐藏该选择，兼容既有页面。当前主轮继续API采样，native另建批次；重启后主访问ready、独立跳板刷新paused，未修改两者状态。

## Camoufox与其他工具

本地`D:\Code\Camoufox`已有实际可执行文件、`camoufox`和Playwright，`camoufox_runtime.py`通过Firefox控制API创建新context，`inspect_network.py`已有response观测示例。它可作为独立浏览器实验环境，但现有实现不保持BOSS会话，也不能直接使用本项目Chrome登录；其`browser-use`直接Agent接入限制以本地README与当前源码为准。

本轮原项目已完成列表/详情实时观测，没有安装TLS中间人代理、系统证书或更换采集浏览器。需要分析此通道无法呈现的连接层现象时，再选择相应工具，并把工具影响与网站自身行为分开记录。
