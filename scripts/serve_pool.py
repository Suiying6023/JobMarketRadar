"""Compatibility launcher for the maintained offline workbench.

The previous unauthenticated write API is retired. Existing job evidence is
read-only; bookmarks and notes are kept in a separate local review database.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))


def main() -> None:
    parser = argparse.ArgumentParser(description='打开本地离线岗位工作台')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--config', default=str(ROOT / 'config.local.yaml'))
    args = parser.parse_args()
    from job_radar.workbench import serve
    serve(args.config, args.port)


if __name__ == '__main__':
    main()
