from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import urlopen

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .report import generate_html_report
from .storage import JobStore


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

app = typer.Typer(no_args_is_help=True, help="岗位数据服务：采集、存储、任务、配置与日志")
console = Console()
from .service_cli import tasks as task_commands, settings as config_commands, run_task, run_task_with_options
app.add_typer(task_commands, name="task")
app.add_typer(config_commands, name="config")
app.command(name="run")(run_task)


def _default_config() -> str:
    """默认读**本地**配置：`config.local.yaml` 是真实运行配置（已 gitignore），
    `config.yaml` 是提交在仓库里的模板（`network_enabled: false` 的安全默认）。

    以前 `doctor` / `collect` / `report` / `summary` 默认读模板，其余十三个命令读本地配置。
    结果是 `job-radar doctor` 会回答「在线访问：False」——而你实际在跑的配置是开着的。
    诊断命令给出与运行时相反的回答，比没有诊断更糟。
    新克隆只有模板，所以本地配置不存在时回退到模板。
    """
    local = Path("config.local.yaml")
    return str(local) if local.is_file() else "config.yaml"


DEFAULT_CONFIG = _default_config()


@app.command()
def login(config: str = "config.local.yaml", seconds: int = typer.Option(900, min=30, max=7200)) -> None:
    """打开可见浏览器供人工登录；结束时保存会话，不自动启动任务或解除访问暂停。"""
    import asyncio
    from .collectors.browser import BrowserSession
    cfg = load_config(config)
    browser_cfg = cfg.browser.model_copy(deep=True)
    browser_cfg.user_data_dir = str((Path(config).resolve().parent / browser_cfg.user_data_dir).resolve())
    browser_cfg.headless = False
    browser_cfg.window_position = "80,60"
    browser_cfg.block_heavy_resources = False

    async def open_login():
        session = BrowserSession(browser_cfg)
        try:
            context = await session.start()
            from urllib.parse import urlsplit
            page = next((p for p in context.pages if urlsplit(p.url).hostname == "www.zhipin.com"), None)
            if page is None:
                page = next((p for p in context.pages if p.url == "about:blank"), None) or await context.new_page()
            await page.goto("https://www.zhipin.com/web/geek/jobs", wait_until="domcontentloaded")
            await session.show(page)
            console.print("浏览器已打开。请手动登录并确认能搜索岗位。完成后关闭浏览器或按 Ctrl+C；会话保存在本机。")
            last_location = ""
            blank_seconds = 0
            for _ in range(seconds):
                if page.is_closed():
                    break
                # Observe navigation without reading credentials or attaching console listeners.
                location = urlsplit(page.url)
                safe_location = f"{location.scheme}://{location.hostname or ''}{location.path}"
                if safe_location != last_location:
                    console.print(f"当前页面：{safe_location}")
                    last_location = safe_location
                blank_seconds = blank_seconds + 1 if page.url == "about:blank" else 0
                if blank_seconds == 15:
                    console.print("页面已持续空白15秒，本次未自动导航重试；请检查窗口实际状态。")
                await asyncio.sleep(1)
        finally:
            await session.close()

    try:
        asyncio.run(open_login())
    except KeyboardInterrupt:
        console.print("登录窗口已结束，会话已保存。")


VERIFY_URL_HINTS = ("verify.html", "safe-validate", "passport/zp/verify")
VERIFY_TEXT_HINTS = ("安全验证", "完成验证", "点击按钮进行验证")


def _is_verification_page(url: str, body: str) -> bool:
    """判断当前页是不是"要求人工过验证"那一页。

    实测（2026-10-18）：`code=36`「您的账户存在异常行为.」出现后，岗位页会被 302 到
    `https://www.zhipin.com/web/passport/zp/verify.html?...&code=36`，正文是
    「您的账号可能存在异常访问行为，完成验证后即可正常使用」。
    这一页等 12 分钟不会自己消失，整页重载也清不掉，**只有人工过验证能解**。
    """
    lowered_url = (url or "").lower()
    if any(hint in lowered_url for hint in VERIFY_URL_HINTS):
        return True
    return any(hint in (body or "") for hint in VERIFY_TEXT_HINTS)


@app.command()
def verify(config: str = "config.local.yaml",
           seconds: int = typer.Option(600, min=60, max=3600)) -> None:
    """检查是否被要求人工验证；需要就弹出可见窗口，等人过完再确认。

    为什么单独做成一条命令：`code=36` 和 `code=37` 完全不是一回事。
    37 是一个 token 的额度用完（采集器现在会自己滚动续上，成本 0 个请求）；
    36 是平台要求**人工过验证**，等待和重载都无效，只能人来点。
    采集器碰到 36 会立刻停机并冻结策略 —— 停机之后，用这条命令把验证过掉，
    再手动解除冻结，就能继续。
    """
    import asyncio
    from .collectors.browser import BrowserSession

    cfg = load_config(config)
    browser_cfg = cfg.browser.model_copy(deep=True)
    browser_cfg.user_data_dir = str((Path(config).resolve().parent / browser_cfg.user_data_dir).resolve())
    browser_cfg.headless = False
    browser_cfg.window_position = "80,60"
    browser_cfg.block_heavy_resources = False
    search_url = "https://www.zhipin.com/web/geek/jobs"

    async def run_verify():
        session = BrowserSession(browser_cfg)
        try:
            context = await session.start()
            from urllib.parse import urlsplit
            page = next((p for p in context.pages
                         if urlsplit(p.url).hostname == "www.zhipin.com"), None)
            if page is None:
                page = next((p for p in context.pages if p.url == "about:blank"), None) \
                    or await context.new_page()
            try:
                await page.goto(search_url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 - 打不开也要把原因说清楚
                console.print(f"打开岗位页失败：{type(exc).__name__}: {exc}")
                return
            await page.wait_for_timeout(3000)

            body = ""
            try:
                body = await page.content()
            except Exception:  # noqa: BLE001
                pass
            needs = _is_verification_page(page.url, body)

            if not needs:
                console.print("当前没有要求人工验证，岗位页可以直接访问。")
                return

            await session.show(page)
            console.print("需要人工验证：请在刚打开的窗口里完成验证（通常是点按钮后拖滑块）。")
            console.print("验证通过后页面会自己跳回岗位页，这里会自动确认。")
            for _ in range(seconds):
                if page.is_closed():
                    console.print("窗口被关闭，未确认验证结果。")
                    return
                try:
                    current = page.url
                    content = await page.content()
                except Exception:  # noqa: BLE001 - 导航中读不到属正常
                    await asyncio.sleep(1)
                    continue
                if not _is_verification_page(current, content):
                    console.print("验证已通过，页面已回到岗位页。可以重新运行采集。")
                    return
                await asyncio.sleep(1)
            console.print("等待超时，验证仍未完成。窗口保持打开，可以继续手动操作。")
        finally:
            await session.close()

    try:
        asyncio.run(run_verify())
    except KeyboardInterrupt:
        console.print("验证窗口已结束，会话已保存。")


@app.command(name="rounds")
def show_rounds(config: str = "config.local.yaml") -> None:
    """列出批次、岗位数与完成查询数。"""
    from .rounds import list_rounds
    from .settings import resolve_database
    from .service_cli import output
    output(list_rounds(resolve_database(load_config(config), Path(config).resolve())))


@app.command(name="export-round")
def export_collection_round(round_id: str, output: Path, config: str = "config.local.yaml") -> None:
    """将一个批次的岗位快照导出为独立 SQLite 和来源清单。"""
    from .rounds import export_round
    from .settings import resolve_database
    from .service_cli import output as print_json
    try:
        print_json(export_round(resolve_database(load_config(config), Path(config).resolve()), round_id, output))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command(name="import-capture")
def import_capture_file(source: Path, output: Path, round_id: str = "imported-capture",
                        preserve_verified: bool = False) -> None:
    """导入已取得的岗位响应到新数据库，不联网；HAR不自动认定全文核验。"""
    from .capture_import import import_capture
    from .task_store import TaskOptions
    from .service_cli import output as print_json
    try:
        TaskOptions(round_id=round_id)
        if not source.is_file():
            raise ValueError("响应源文件不存在")
        if not round_id:
            raise ValueError("导入需要非空round_id")
        print_json(import_capture(source.resolve(), output.resolve(), round_id, preserve_verified))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def doctor(config: str = DEFAULT_CONFIG) -> None:
    """检查配置、数据库目录和 CDP 地址。"""
    cfg = load_config(config)
    console.print(f"配置有效：{len(cfg.cities)} 个城市，{len(cfg.keywords)} 个关键词")
    console.print(f"浏览器模式：{cfg.browser.mode.value}")
    console.print(f"在线访问：{cfg.network_enabled}")
    console.print(f"自动开聊：enabled={cfg.auto_chat.enabled}, dry_run={cfg.auto_chat.dry_run}")
    if cfg.browser.mode.value == "cdp" and cfg.network_enabled:
        try:
            endpoint = cfg.browser.cdp_url.rstrip("/") + "/json/version"
            with urlopen(endpoint, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            console.print(f"CDP 可用：{payload.get('Browser', 'Chromium')}" )
        except Exception as exc:
            console.print(f"[yellow]CDP 暂不可用：{exc}。请先运行独立浏览器启动脚本。[/]")


@app.command()
def collect(
    config: str = DEFAULT_CONFIG,
    allow_live_chat: bool = typer.Option(False, "--allow-live-chat", hidden=True),
    limit: int = 100,
    dry_run: bool = False,
) -> None:
    """独立运行采集（与 run collect 共用任务核心）。"""
    if allow_live_chat:
        raise typer.BadParameter("数据服务任务处理岗位数据；联系动作使用独立流程")
    run_task("collect", config, limit, dry_run)


@app.command()
def session(
    config: str = DEFAULT_CONFIG,
    phase: str = typer.Option("calibrate", "--phase",
                              help="calibrate=推荐流校准（含抓正文）/ sweep=关键词扫描 / backfill=存量补正文"),
    pages: int | None = typer.Option(None, "--pages", min=1, max=30,
                                     help="翻几页；不填按阶段默认（校准 5 页 / 扫描取配置 pages_per_query）"),
    detail_limit: int | None = typer.Option(None, "--detail-limit", min=0, max=500,
                                            help="抓多少条正文；不填按阶段默认（校准 40 / 扫描 8 / 补正文取 --limit）；扫描填 0 表示只扫不抓"),
    keywords: str = typer.Option("", "--keywords",
                                 help="逗号分隔；留空用配置里的，并按「最久没采过」轮转（仅 sweep 生效）"),
    cities: str = typer.Option("", "--cities", help="逗号分隔；留空用配置里的（仅 sweep 生效）"),
    output: str = typer.Option("", "--output",
                               help="校准记录 JSONL 路径；留空 output/calibration-<任务ID>.jsonl"),
    limit: int = typer.Option(100, "--limit", min=1, max=100000, help="数量上限（校准的候选预算 / 补正文的条数上限）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出执行计划，不联网、不起浏览器"),
) -> None:
    """采集会话：推荐流校准 → 关键词扫描 → 存量补正文（与网页共用任务核心）。

    三个阶段可以单独跑，也可以按顺序跑完一条链：

        job-radar session --phase calibrate --pages 5 --detail-limit 40
        job-radar session --phase sweep --keywords "AI Agent,Python" --detail-limit 8
        job-radar session --phase backfill --detail-limit 60 --limit 60

    `--dry-run` 不花任何请求：先把关键词、城市、页数的解析结果打出来核对。
    任务运行中改配置（`request_delay_seconds` / `detail_delay_seconds` /
    `detail_batch` / `burst_threshold` 等）会即时生效，见 `worker.HOT_FIELDS`。
    """
    if phase not in ("calibrate", "sweep", "backfill"):
        raise typer.BadParameter("阶段只能是 calibrate / sweep / backfill")
    options: dict = {"phase": phase}
    # 只在**真的传了**的时候写进选项：`detail_limit=0` 对扫描阶段是有效取值
    # （只扫不抓正文），不能用 `or` 之类的写法把 0 当成「没填」。
    if pages is not None:
        options["pages"] = pages
    if detail_limit is not None:
        options["detail_limit"] = detail_limit
    if keywords.strip():
        options["keywords"] = keywords
    if cities.strip():
        options["cities"] = cities
    if output.strip():
        options["output"] = output
    run_task_with_options("session", config, limit, dry_run, "", options)


@app.command(name="init")
def init_workspace(config: str = "config.local.yaml", database: str = "data/jobs.sqlite3") -> None:
    """创建可移植的离线配置，保留现有文件。"""
    from .settings import init_config
    try:
        console.print(f"已创建离线配置：{init_config(config, database)}")
    except (FileExistsError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def serve(config: str = "config.local.yaml", port: int = 8765) -> None:
    """在本机打开配置、关键词和岗位复核工作台（不发起采集）。"""
    from .workbench import serve as run_server
    try:
        run_server(config, port)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command(name="maintenance")
def maintenance(plan: Path = typer.Option(..., "--plan"), inspect_only: bool = False,
                review_task: str = "", review_reason: str = "") -> None:
    """执行一次有界维护；周期由外部调度器管理。"""
    from .maintenance import cycle
    from .service_cli import output
    try:
        output(cycle(plan, inspect_only=inspect_only, review_task=review_task, review_reason=review_reason))
    except (ValueError, OSError, KeyError) as exc:
        output({"action": "maintenance_error", "error": f"{type(exc).__name__}: {exc}"})
        raise typer.Exit(1) from exc


@app.command()
def analyze(config: str = "config.local.yaml", output: str = "output/market-analysis.json") -> None:
    """只读分析全国整库，再按配置画像生成复核队列。"""
    from .analysis.market import MarketSnapshot, write_analysis
    from .candidate import CandidateProfile
    from .settings import resolve_database
    cfg = load_config(config)
    snapshot = MarketSnapshot(resolve_database(cfg, Path(config).resolve()))
    path = write_analysis(snapshot, cfg.candidate or CandidateProfile(), output)
    console.print(f"分析已保存：{path.resolve()}")
    console.print(snapshot.overview()["totals"])


@app.command(name="risk-status")
def risk_status(config: str = "config.local.yaml", scope: str = "") -> None:
    """读取本地暂停状态，不向平台核验。"""
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    console.print_json(json.dumps(AccessGate(resolve_database(cfg, Path(config).resolve()), scope=scope).read_state().as_dict(), ensure_ascii=False))
    console.print(f"配置允许在线访问：{cfg.network_enabled}")


@app.command()
def demo(directory: str = "demo") -> None:
    """创建六条合成示例，离线体验界面，不含真实招聘数据。"""
    from .data_tools import create_demo
    try:
        config_path = create_demo(directory)
    except (FileExistsError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"合成示例已创建。启动：job-radar serve --config {config_path}")


@app.command(name="db-backup")
def db_backup(destination: str, config: str = "config.local.yaml") -> None:
    """一致性备份并生成校验清单，不覆盖既有文件。"""
    from .data_tools import backup_database
    from .settings import resolve_database
    cfg = load_config(config)
    manifest = backup_database(resolve_database(cfg, Path(config).resolve()), destination)
    console.print_json(json.dumps(manifest, ensure_ascii=False))


@app.command(name="risk-pause")
def risk_pause(reason: str, config: str = "config.local.yaml", resume_after: str | None = None, scope: str = "") -> None:
    """持久暂停采集；到期后仍需人工确认。"""
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    AccessGate(resolve_database(cfg, Path(config).resolve()), scope=scope).pause(reason, resume_after=resume_after)
    console.print("已保存本地暂停状态。")


@app.command(name="risk-release")
def risk_release(config: str = "config.local.yaml", confirm_account_recovered: bool = False, scope: str = "") -> None:
    """人工确认账号恢复后解除本地暂停；不会自动开始采集。"""
    if not confirm_account_recovered:
        raise typer.BadParameter("请先人工确认平台账号已恢复，再传 --confirm-account-recovered")
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    AccessGate(resolve_database(cfg, Path(config).resolve()), scope=scope).release("用户通过 CLI 确认访问状态已恢复")
    console.print("已解除本地暂停。采集仍受 network_enabled 配置与预算控制。")


@app.command()
def export(
    config: str = DEFAULT_CONFIG,
    csv_path: str = "output/jobs.csv",
    json_path: str = "output/jobs.json",
) -> None:
    """从 SQLite 导出 CSV 和 JSON。"""
    cfg = load_config(config)
    from .settings import resolve_database
    cfg.database_path = str(resolve_database(cfg, Path(config).resolve()))
    with JobStore(cfg.database_path, read_only=True) as store:
        console.print(f"CSV: {store.export_csv(csv_path)}")
        console.print(f"JSON: {store.export_json(json_path)}")


@app.command()
def report(config: str = DEFAULT_CONFIG, output: str = "output/report.html") -> None:
    """生成本地 HTML 市场报告。"""
    cfg = load_config(config)
    from .settings import resolve_database
    cfg.database_path = str(resolve_database(cfg, Path(config).resolve()))
    path = generate_html_report(cfg.database_path, output)
    console.print(f"报告已生成: {path}")


@app.command(name="summary")
def summary(config: str = DEFAULT_CONFIG) -> None:
    """显示城市汇总。"""
    cfg = load_config(config)
    from .settings import resolve_database
    cfg.database_path = str(resolve_database(cfg, Path(config).resolve()))
    with JobStore(cfg.database_path, read_only=True) as store:
        rows = store.city_summary()
    table = Table("城市", "岗位数", "公司数", "平均薪资中点(K)", "初级岗位", "本科可投")
    for row in rows:
        table.add_row(
            str(row["city"]), str(row["jobs"]), str(row["companies"]),
            str(row["avg_mid_salary_k"] or ""), str(row["junior_jobs"]), str(row["bachelor_accessible"]),
        )
    console.print(table)


if __name__ == "__main__":
    app()
