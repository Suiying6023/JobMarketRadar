"""让存档的历史用例仍然可被显式跑起来。

这些文件原先在 `tests/` 根目录，靠 pytest 的 rootdir 把 `scripts/` 间接带上 sys.path，
所以 `import backfill_details` 这类语句能过。搬进子目录后路径变了，这里补回来。

它们**不进入默认门禁**（见 pyproject 的 `testpaths`），只在需要查证历史行为时
用 `python -m pytest tests/_legacy` 显式调出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent

for extra in (ROOT / "scripts", HERE):
    if str(extra) not in sys.path:
        # 用 append 不用 insert(0)：`scripts/` 里有和标准库/依赖同名的文件，
        # 插到最前面会遮蔽真正的模块，表现是一批毫不相干的用例莫名其妙地红。
        sys.path.append(str(extra))
