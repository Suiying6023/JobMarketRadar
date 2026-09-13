"""离线安全闸门：持久化的访问策略 + 强制在线前置检查。

为什么单独成模块（而不是塞进 collector）：

- 采集链路（带账号 cookie + 家宽）与刷新链路（免登录 + 机房出口）是两个风控主体，
  但「现在能不能联网」对两者是同一个答案，必须由**同一个文件**回答，
  不能各写一份判断 —— 两份判断迟早会漂移，而漂移的代价是冻结期里哪一个入口没拦住。
- 冻结必须是**状态**而不是异常。进程会被重启、脚本会被人手工再跑一次，
  而「账号被禁止使用 3 天」这件事活在进程之外。事故复盘里最贵的一课：
  熔断写在异常里，重启就归零；写在文件里才拦得住下一个人。

三条不可退让的语义（`tests/test_safety_gate.py` 逐条钉住）：

1. **默认拒绝**：状态文件缺失或损坏 = offline。绝不因为「读不出来」而放行。
2. **过期不等于放行**：`resume_after` 只是「最早可以人工解除的时刻」，
   到点不会自动变 ready —— 必须有人显式 `release(confirmation)`。
3. **不读密钥、不联网**：本模块只碰一个 JSON 文件，不导入采集 / 浏览器 / 网络依赖。

状态文件固定为 `<数据库父目录>/access-policy.json`：与主库同目录，
库被备份 / 迁移 / 换成临时库时策略跟着走，不会出现「库换了地方而策略还留在旧机器」。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .local_files import atomic_write, file_mutex

#: 策略文件名。位置 = 数据库文件所在目录。
STATE_FILENAME = "access-policy.json"

MODE_READY = "ready"
MODE_PAUSED = "paused"
MODE_OFFLINE = "offline"

_MODES = frozenset({MODE_READY, MODE_PAUSED, MODE_OFFLINE})


class AccessBlocked(RuntimeError):
    """当前策略不允许联网（未启用 / 冻结中 / 策略文件不可用）。

    ⚠️ 与 `RiskControlStop` 的区别：那个是「刚刚撞到了风控」，
    这个是「出门之前就被拦住」。两者都会停止本轮，但前者要写策略、
    后者只是读策略 —— 所以不要把两者合并成一个异常。
    """


class AccessPolicyLocked(AccessBlocked):
    """另一个进程正持有策略锁：**拒绝**这次读改写，不要无锁写。

    为什么必须拒绝而不是"照写下去"：策略写是「读-判断-写」。
    无锁写会与持锁的 `release()` 交错 —— pause 刚写下 paused，
    持锁方随后用它在锁内读到的旧状态覆盖成 ready，冻结**静默丢失**。
    冻结丢失比一次写失败严重得多，所以这里选择拒绝，并由调用方停止本进程。
    """


@dataclass(frozen=True)
class AccessState:
    """策略快照。字段名即契约：mode / reason / resume_after / manual_release_required。

    `corrupt=True` 表示文件存在但读不懂；此时 `mode` 被强制为 `offline`，
    且必须由人重新 `release()`（或重新 `pause()`）才能写回一个可用文件。
    """

    mode: str = MODE_OFFLINE
    reason: str = ""
    resume_after: str = ""
    manual_release_required: bool = True
    updated_at: str = ""
    source: str = ""
    corrupt: bool = False

    #: 允许 `state["mode"]` 与 `state.mode` 两种读法：
    #: 调用方有 CLI（习惯字典）也有采集代码（习惯属性），契约不该逼人选一种。
    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError as exc:  # pragma: no cover - 防御性
            raise KeyError(key) from exc

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "resume_after": self.resume_after,
            "manual_release_required": self.manual_release_required,
            "updated_at": self.updated_at,
            "source": self.source,
            "corrupt": self.corrupt,
        }

    @property
    def ready(self) -> bool:
        return self.mode == MODE_READY

    def release_allowed(self, now: datetime | None = None) -> bool:
        """现在能不能人工解除（只判 `resume_after`，不判 mode）。"""
        return not release_blocked_reason(self.resume_after, now=now)


def now_local() -> datetime:
    """带本地时区的当前时刻（全模块统一用它取时间，便于测试注入）。"""
    return datetime.now().astimezone()


def _parse_dt(value) -> datetime | None:
    """把策略里的时间解析成**带时区**的 datetime；解析不了返回 None。

    支持：ISO8601（含 `Z`、含偏移）、`YYYY-MM-DD HH:MM:SS`（无时区则按本地时区解释）、
    epoch 秒 / 毫秒（int/float）。

    不可解析、NaN/±inf、溢出（`1e309`、`10**30`、超出 `datetime` 范围）一律返回 None ——
    调用方据此把策略判为**损坏**并拒绝访问。绝不能把这类值当成"没有期限"：
    那等于把一条明确的冻结改写成"随时可解除"。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(ts):        # NaN / inf（JSON 里可以是裸 NaN/Infinity）
            return None
        if abs(ts) > 1e11:               # 毫秒（平台侧给的 expireTime 就是毫秒）
            ts /= 1000.0
        if not math.isfinite(ts):
            return None
        try:
            return datetime.fromtimestamp(ts).astimezone()
        except (OverflowError, OSError, ValueError):   # 1e20、10**30 之类
            return None
    text = str(value).strip()
    if not text:
        return None
    # 纯数字字符串 = epoch（平台把 expireTime 给成毫秒数字，人手抄进来可能带引号）。
    # 裸数字绝不是合法 ISO 日期，所以这条判断没有歧义。
    if text.lstrip("+-").isdigit():
        try:
            return _parse_dt(int(text))
        except (TypeError, ValueError, OverflowError):
            return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def normalise_resume_after(value) -> str:
    """把 `resume_after` 规范成 ISO8601 字符串；不可解析 → ValueError。

    ⚠️ 不可解析必须报错而不是当空值：把「2026-09-15 23:10:00 之前不许解除」
    读成「没有期限」等于凭空放开三天冻结。
    """
    if value is None or value == "":
        return ""
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError(f"resume_after 不是可解析的时间：{value!r}")
    return parsed.isoformat(timespec="seconds")


def later_deadline(*values) -> str:
    """取最晚的一个期限（空值/不可解析的值忽略），返回规范化 ISO 字符串。

    `pause()` 用它**保留已知的更晚解封时刻**：第二条风险事件常常只带一个 reason，
    若直接把期限写成空，上一条记下的「封禁到 09-15 23:10」就被抹掉了，
    而 `release()` 的期限校验正是靠它 —— 抹掉等于把提前解除的口子重新打开。
    """
    best: datetime | None = None
    for value in values:
        parsed = _parse_dt(value)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return "" if best is None else best.isoformat(timespec="seconds")


def release_blocked_reason(resume_after, now: datetime | None = None) -> str:
    """返回「现在还不能 release」的原因；可以 release 时返回空串。

    这是**唯一**一处判断「期限到没到」的地方：CLI 想提前告诉人「还没到点」
    也应该调它，别在 CLI 里再写一遍比较。
    """
    deadline = _parse_dt(resume_after)
    if deadline is None:
        return ""
    current = now or now_local()
    if current < deadline:
        return (f"策略要求不早于 {deadline.isoformat(timespec='seconds')} 解除，"
                f"当前 {current.isoformat(timespec='seconds')}")
    return ""


def _write_atomic(path: Path, payload: dict) -> None:
    """原子落盘（同目录临时文件 + fsync + os.replace）。

    实现复用 `local_files.atomic_write`（单一实现，别在两边各写一份）：
    必须是原子的 —— 读到半个 JSON 的策略文件会被判为损坏 → 默认拒绝，
    一次断电就足以让"冻结"变成"打不开"。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write(path, text)


#: 策略锁文件后缀。`local_files.file_mutex` 用 **非阻塞** 系统锁
#: （Windows `LK_NBLCK` / POSIX `LOCK_NB`），所以这里用短暂重试代替等待。
#: 预算 5s：锁的持有时间是一次 JSON 落盘（微秒级），真等不到就说明有别的写入方
#: 卡住了 —— 此时必须**拒绝**而不是无锁写。
LOCK_SUFFIX = ".lock"
_LOCK_TRIES = 50
_LOCK_SLEEP = 0.1


class _LockAttempt:
    """严格获取策略锁；拿不到返回 False，由调用方**拒绝操作**。

    绝不降级为"无锁也写"：读改写一旦无锁，就可能与持锁的 `release()` 交错，
    把刚落地的 paused 覆盖回 ready —— 冻结静默丢失比一次写失败严重得多。
    （测试可以用较短的 `_LOCK_TRIES/_LOCK_SLEEP` monkeypatch 掉这段等待。）

    锁只覆盖一次读改写，持有时间是一次 JSON 落盘（微秒级）。
    """

    def __init__(self, state_path: Path):
        self._path = state_path.with_name(state_path.name + LOCK_SUFFIX)
        self._cm = None

    def __enter__(self) -> bool:
        for _ in range(_LOCK_TRIES):
            cm = file_mutex(self._path)
            try:
                cm.__enter__()
            except OSError:
                time.sleep(_LOCK_SLEEP)
                continue
            self._cm = cm
            return True
        return False

    def __exit__(self, *exc) -> bool:
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False


class AccessGate:
    """访问策略的唯一读写入口。

    用法（采集侧）：`AccessGate(config.database_path).require_online(enabled)`；
    用法（人工侧）：`gate.release("2026-09-16 账号已自然解封，用户确认")`。
    """

    def __init__(self, database_path: str | Path):
        path = Path(database_path) if not isinstance(database_path, Path) else database_path
        # 允许直接传目录（临时库/测试用 tmp_path 时更自然）。
        base = path if path.is_dir() else path.parent
        self.state_path = base / STATE_FILENAME

    # ---- 读 ----

    def read_state(self) -> AccessState:
        """读策略。**任何读不出来的情况都返回 offline**，不抛异常。

        不抛异常是为了让 CLI / 巡检能打印「为什么被拦」；拒绝访问的责任在
        `require_online()`，两者分工明确。
        """
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return AccessState(
                mode=MODE_OFFLINE,
                reason=f"没有访问策略文件 {self.state_path}（默认离线，需人工 release）",
                resume_after="",
                manual_release_required=True,
                source="default",
            )
        except OSError as exc:
            return AccessState(
                mode=MODE_OFFLINE,
                reason=f"访问策略文件读不出来（{exc.__class__.__name__}: {exc}），按离线处理",
                resume_after="",
                manual_release_required=True,
                source="unreadable",
                corrupt=True,
            )

        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            return self._corrupt(f"不是合法 JSON（{exc.msg}）")
        if not isinstance(data, dict):
            return self._corrupt(f"顶层应是对象，实际是 {type(data).__name__}")

        mode = data.get("mode")
        if mode not in _MODES:
            return self._corrupt(f"mode 取值非法：{mode!r}")
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            return self._corrupt("reason 不是字符串")
        raw_resume = data.get("resume_after", "")
        deadline = _parse_dt(raw_resume)
        if raw_resume not in (None, "") and deadline is None:
            return self._corrupt(f"resume_after 不可解析：{raw_resume!r}")
        # ⚠️ 读入即规范化成 ISO：数值(秒/毫秒)/裸数字字符串都必须在这里统一，
        # 否则出口是个"数字字符串"，`release()` 二次解析时会把"有期限"读成
        # "没有期限" —— 那正是提前解除的口子（主代理独立复现过）。
        resume_after = "" if deadline is None else deadline.isoformat(timespec="seconds")
        manual = data.get("manual_release_required", mode != MODE_READY)
        if not isinstance(manual, bool):
            return self._corrupt("manual_release_required 不是布尔值")
        updated_at = data.get("updated_at", "")
        source = data.get("source", "")
        if not isinstance(updated_at, str) or not isinstance(source, str):
            return self._corrupt("updated_at / source 不是字符串")

        # 一致性校验：矛盾的 ready 绝不能被当成放行依据（require_online 只认 mode）。
        if mode == MODE_READY:
            if manual:
                return self._corrupt(
                    "mode=ready 却同时要求人工解除（自相矛盾）—— 不得据此放行")
            if resume_after:
                return self._corrupt(
                    f"mode=ready 却带着未到期的 resume_after={resume_after!r}"
                    f"（自相矛盾）—— 不得据此放行")
        elif mode == MODE_PAUSED and not manual:
            return self._corrupt("mode=paused 却标 manual_release_required=false（自相矛盾）")

        return AccessState(
            mode=mode,
            reason=reason,
            resume_after=resume_after,
            manual_release_required=manual,
            updated_at=updated_at,
            source=source,
        )

    def _corrupt(self, why: str) -> AccessState:
        return AccessState(
            mode=MODE_OFFLINE,
            reason=f"访问策略文件损坏（{why}），按离线处理，需人工复核后 release",
            resume_after="",
            manual_release_required=True,
            source="corrupt",
            corrupt=True,
        )

    # ---- 写 ----

    def pause(self, reason: str, resume_after=None, source: str = "manual") -> AccessState:
        """持久熔断：写 `paused` 并落盘。返回新状态。

        `reason` 为空不报错但会被记成「(未记录原因)」（已有 paused 原因时沿用旧的）——
        停机原因是要给人看的，一个空的 reason 会让下一个人无从判断能不能解除。

        `resume_after` 是「最早可解除时刻」，**取新旧两者中最晚的**：
        第二条风险只写 reason 时不得把上一条记下的解封期限抹掉（否则 release
        的期限校验就形同虚设）。它仍然不会自动解除（见模块 docstring 第 2 条）。

        **严格同锁串行**：读-判断-写全在策略锁内完成；抢不到锁 → 抛
        `AccessPolicyLocked`（拒绝写），由调用方停止本进程。绝不无锁写。
        """
        text = (reason or "").strip()
        candidate = normalise_resume_after(resume_after)   # 锁外先校验参数
        with _LockAttempt(self.state_path) as locked:
            if not locked:
                raise AccessPolicyLocked(
                    f"策略锁被占用（{self.state_path.name}{LOCK_SUFFIX}），"
                    f"拒绝无锁写入；本进程应停止并由人工确认（不得当作已熔断）")
            current = self.read_state()                    # 锁内读，判断才是原子的
            if (current.mode == MODE_PAUSED and source.startswith("managed-task:")
                    and current.source != source):
                return current
            previous = current if current.mode == MODE_PAUSED else None
            state = AccessState(
                mode=MODE_PAUSED,
                reason=text or (previous.reason if previous else "") or "(未记录原因)",
                resume_after=later_deadline(
                    candidate, previous.resume_after if previous else ""),
                manual_release_required=True,
                updated_at=now_local().isoformat(timespec="seconds"),
                source=(source or "manual")[:80],
            )
            _write_atomic(self.state_path, state.as_dict())
        return state

    def release(self, confirmation: str, *, expected_source: str | None = None) -> AccessState:
        """人工解除冻结 → 写 `ready`。

        `confirmation` 必须非空：它是「谁 / 依据什么判断可以恢复」的审计痕迹，
        空确认等于把冻结悄悄取消掉。若策略带非空 `resume_after` 且现在还没到点，
        **拒绝**（抛 AccessBlocked）—— 提前解除正是要防的那件事。
        读-判断-写同样全在锁内；抢不到锁 → 拒绝（可重试的人工动作）。
        """
        text = (confirmation or "").strip()
        if not text:
            raise ValueError("release 需要非空的 confirmation（记录依据什么判断可以恢复）")
        with _LockAttempt(self.state_path) as locked:
            if not locked:
                raise AccessPolicyLocked(
                    f"策略锁被占用（{self.state_path.name}{LOCK_SUFFIX}），"
                    f"请稍后重试 release")
            # 拿锁之后**重新读**：用过期状态判断"期限到没到"会放走一次提前解除。
            current = self.read_state()
            if expected_source is not None and current.source != expected_source:
                raise AccessBlocked("当前暂停来自另一项操作，请在访问控制中确认后恢复")
            blocked = release_blocked_reason(current.resume_after)
            if blocked:
                raise AccessBlocked(f"拒绝解除：{blocked}")
            state = AccessState(
                mode=MODE_READY,
                reason=f"人工解除：{text[:200]}",
                resume_after="",
                manual_release_required=False,
                updated_at=now_local().isoformat(timespec="seconds"),
                source="manual-release",
            )
            _write_atomic(self.state_path, state.as_dict())
        return state

    # ---- 执行点 ----

    def require_online(self, enabled: bool = True) -> AccessState:
        """联网前置检查：`enabled` 为真且 mode=ready 才放行，否则抛 AccessBlocked。

        ⚠️ 调用点必须在**请求之前**（起浏览器之前更好）：这个方法存在的意义
        就是让"冻结期一次请求都没发出去"，而不是发完再判。
        ⚠️ 它**不检查** `resume_after` 是否到期：到期不等于放行（模块 docstring 第 2 条）。
        """
        if not enabled:
            raise AccessBlocked(
                "网络访问未启用（network_enabled=False）。本轮不启动浏览器、不发任何请求。"
                "确认账号可恢复后再显式打开。"
            )
        state = self.read_state()
        if state.mode != MODE_READY:
            raise AccessBlocked(self.explain(state))
        return state

    def explain(self, state: AccessState | None = None) -> str:
        """给人和日志看的完整说明（含解除办法）。不读密钥、不含任何凭据。"""
        state = state or self.read_state()
        parts = [f"当前访问策略 mode={state.mode}",
                 f"原因：{state.reason or '(未记录)'}"]
        if state.resume_after:
            deadline = _parse_dt(state.resume_after)
            current = now_local()
            if deadline and current < deadline:
                parts.append(f"最早可解除时间 {state.resume_after}（当前 {current.isoformat(timespec='seconds')}，"
                             f"到点也不会自动放行）")
            else:
                parts.append(f"期限 {state.resume_after} 已过，可人工复核后解除")
        if state.manual_release_required:
            parts.append(f"需人工解除：release(confirmation=\"...\")，状态文件 {self.state_path}")
        return "；".join(parts)
