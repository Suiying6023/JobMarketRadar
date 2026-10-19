# 推荐页与新的独立岗位采样

读取网页自己触发的推荐请求，使用既有登录Profile与访问闸门；岗位写到新建独立数据库。
当前实测入口为`GET /wapi/zpgeek/pc/recommend/job/list.json`，网页原生滚动分页每页15条。
本次捕获实例位于`local/runs/20261001/recommendation-fresh/`，最新阶段看progress.json和capture-report.json。

## 请求与来源

| 内容 | 当前观察 |
|---|---|
| 页码 | page、pageSize；滚动下移后网页请求下一页 |
| 求职期望 | encryptExpectId、expectInfo、mixExpectType；参数值有账户上下文，报告只保存字段名和上下文hash |
| 筛选 | city、degree、experience、salary、industry、scale、jobType |
| 临时参数 | `_`等由网页产生，不在分析中作为岗位身份 |
| 返回信封 | code、zpData；内部jobList、hasMore、lid、type |
| 稳定岗位身份 | jobList[].encryptJobId；对应详情jobInfo.encryptId |
| 详情 | /wapi/zpgeek/job/detail.json；岗位凭据来自该次列表或网页卡片 |

新模块`collectors/recommendation.py`核对实际分页页码、求职期望/筛选上下文hash，
来源为原生业务响应。切换现有期望页签用于分流采样，不修改求职期望资料。

## 保存、去重与正文

推荐分页会重复返回岗位，网页可同时保留多个同ID卡片。整页列表先落入独立库，再获取详情。
主键去重与观察次数分开；每页最多取两个尚未获得正文、且能按ID唯一定位的岗位。
重复卡片不按序号猜测，改取其他唯一候选。正文核验沿用页面/服务端登录证据、详情身份与可见正文校验。

脚本`collect-fresh.py`分别记录混合推荐、已有Python/算法求职期望、原六关键词和扩展FDE/AI Coding/AI评测搜索。
一个新库保存不同来源批次；实际查询、观察时间、页码、返回字段和来源hash保留。
Jev由`tag-fresh.py`独立读取本次核验正文，结构化判断存为JSONL，不导入历史正文。

## 时间与数量解释

目前观察到的推荐列表和jobInfo没有可靠的岗位发布日期；招聘者活跃时间另有语义。
当前页面未出现“最新发布”排序控件，当前请求参数也未观察到发布时间排序。
这些事实描述本次页面和请求，不宣称穷尽平台所有接口和产品版本。

推荐每页15与hasMore只能描述当前个性化结果序列。独立库中的first_seen_at是本次获取时间；
跨历史快照首见是采样增量，均不能直接换算为最近发布数或“某技术造成的新增岗位数”。
日期缺失时保留未知，不用访问时间、请求时间戳或招聘者活跃度补成发布日期。
本轮列表图标的公开图片已核对：iconFlagList中的8对应“猎头”、2对应“代招”、17对应“国央企”；
这些图标表达招聘来源或企业属性。图标静态资源URL中的年份与岗位发布时间没有对应关系。

## 响应等待与批间节奏

原生列表监听的超时从真正准备导航/滚动之后开始，配置休息先完成。
`_native_list`先调用pacer，再进入expect_response，导航用already_paced避免重复休息。
访问闸门、请求记账与原有节奏继续生效；较长批间等待不会耗尽尚未发出请求的响应等待窗口。

## 执行

确认8765没有活动浏览器任务后，先运行inspect-recommendation.py查看当前推荐和登录状态。
collect-fresh.py使用独立库，初次从空库开始；中断后可继续读取新页面并复用本次已核验正文。
原数据池只用作访问闸门和请求记账，原jobs表不导入这轮结果。
平台需要登录/验证时仍停止并持久暂停，保留已获取的数据。

## 手机版内部分析

实际APK14.170静态调用链定位GET `/api/zpgeek/app/geek/recommend/joblist`；extra_map的
latest=sortType2、recommend=1、nearby=6、active=7，pageSize15。
手机详情候选`/api/zpgeek/jobapp/geek/job/querydetail`的jobBaseInfo有jobPubTimeDesc，
非空直接显示、空隐藏。搜索另有sort参数，枚举尚未核实，不能套用推荐的2。
这些属于APK调用链实证，实际主机与业务JSON未取得。

本次正常只读运行时attach超时，临时server/转发已清理，未改App或鉴权。
手机只取得5张唯一可见卡片，平台ID和发布文案值未取得，分别保留在
`local/runs/20261001/mobile-recommendation/mobile-report.md`及mobile-ui-samples.json。
手机卡片不与网页主键直接合并，也不把“最新”筛选量当近期首次发布量。
