from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.request import urlopen

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .report import generate_html_report
from .storage import JobStore


app = typer.Typer(no_args_is_help=True, help="本地岗位数据分析、配置与求职复核工作台")
console = Console()


@app.command()
def doctor(config: str = "config.yaml") -> None:
    """检查配置、数据库目录和 CDP 地址。"""
    cfg = load_config(config)
    console.print(f"配置有效：{len(cfg.cities)} 个城市，{len(cfg.keywords)} 个关键词")
    console.print(f"浏览器模式：{cfg.browser.mode.value}")
    console.print(f"在线访问：{cfg.network_enabled}；参考线模式：{cfg.budget_mode}")
    console.print(f"自动开聊：enabled={cfg.auto_chat.enabled}, dry_run={cfg.auto_chat.dry_run}")
    if cfg.browser.mode.value == "cdp" and cfg.network_enabled:
        try:
            endpoint = cfg.browser.cdp_url.rstrip("/") + "/json/version"
            with urlopen(endpoint, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            console.print(f"CDP 可用：{payload.get('Browser', 'Chromium')}" )
        except Exception as exc:
            console.print(f"[yellow]CDP 暂不可用：{exc}。请先运行独立浏览器启动脚本。[/]")
    if cfg.auto_chat.enabled and not cfg.auto_chat.dry_run:
        console.print("[bold yellow]警告：配置请求真实自动开聊；collect 仍要求额外传入 --allow-live-chat。[/]")


@app.command()
def collect(
    config: str = "config.yaml",
    allow_live_chat: bool = typer.Option(False, "--allow-live-chat", help="允许执行真实开聊；不传时真实开聊配置会被拒绝"),
) -> None:
    """运行采集任务。"""
    cfg = load_config(config)
    from .runner import run_collection
    if cfg.auto_chat.enabled and not cfg.auto_chat.dry_run and not allow_live_chat:
        raise typer.BadParameter(
            "配置已启用真实自动开聊，但未传入 --allow-live-chat。请先使用 dry_run。"
        )
    stats = asyncio.run(run_collection(cfg))
    console.print(stats.model_dump_json(indent=2))
    if stats.stopped_reason:
        raise typer.Exit(2)


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
def risk_status(config: str = "config.local.yaml") -> None:
    """读取本地暂停状态，不向平台核验。"""
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    console.print_json(json.dumps(AccessGate(resolve_database(cfg, Path(config).resolve())).read_state().as_dict(), ensure_ascii=False))
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
def risk_pause(reason: str, config: str = "config.local.yaml", resume_after: str | None = None) -> None:
    """持久暂停采集；到期后仍需人工确认。"""
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    AccessGate(resolve_database(cfg, Path(config).resolve())).pause(reason, resume_after=resume_after)
    console.print("已保存本地暂停状态。")


@app.command(name="risk-release")
def risk_release(config: str = "config.local.yaml", confirm_account_recovered: bool = False) -> None:
    """人工确认账号恢复后解除本地暂停；不会自动开始采集。"""
    if not confirm_account_recovered:
        raise typer.BadParameter("请先人工确认平台账号已恢复，再传 --confirm-account-recovered")
    from .safety import AccessGate
    from .settings import resolve_database
    cfg = load_config(config)
    AccessGate(resolve_database(cfg, Path(config).resolve())).release("用户通过 CLI 确认账号已恢复")
    console.print("已解除本地暂停。采集仍受 network_enabled 配置与预算控制。")


@app.command()
def export(
    config: str = "config.yaml",
    csv_path: str = "output/jobs.csv",
    json_path: str = "output/jobs.json",
) -> None:
    """从 SQLite 导出 CSV 和 JSON。"""
    cfg = load_config(config)
    with JobStore(cfg.database_path, read_only=True) as store:
        console.print(f"CSV: {store.export_csv(csv_path)}")
        console.print(f"JSON: {store.export_json(json_path)}")


@app.command()
def report(config: str = "config.yaml", output: str = "output/report.html") -> None:
    """生成本地 HTML 市场报告。"""
    cfg = load_config(config)
    path = generate_html_report(cfg.database_path, output)
    console.print(f"报告已生成: {path}")


@app.command(name="summary")
def summary(config: str = "config.yaml") -> None:
    """显示城市汇总。"""
    cfg = load_config(config)
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
