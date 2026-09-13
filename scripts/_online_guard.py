"""Keep legacy network entrypoints behind the same persistent local pause."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def guard(*, allow_dry_run: bool = False) -> None:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        return
    if allow_dry_run and "--dry-run" in sys.argv[1:]:
        return
    from job_radar.safety import AccessBlocked, AccessGate
    try:
        AccessGate(ROOT / "data" / "hangzhou_agent.sqlite3").require_online()
    except AccessBlocked as exc:
        print(f"[ACCESS-BLOCKED] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
