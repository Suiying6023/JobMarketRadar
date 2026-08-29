"""平台采集器。

有 `__init__.py` 才会被 `setuptools.packages.find` 收进 wheel：
没有它时只有「编辑安装 + PEP 420 命名空间包」能 import 成功
（`.pth` 把 src/ 加进 sys.path），一旦真做非编辑式构建，
`collectors/` 整个目录会被静默漏掉，装完 import 就崩。
"""
