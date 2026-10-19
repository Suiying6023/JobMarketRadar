"""Tests use explicit fixture preferences, never the workstation's private .env."""
# 2026-10-19：原先这里设 `JOB_RADAR_CITIES=杭州,上海,深圳` 给旧脚本测试用。
# `profile.CITY_WHITELIST` / `city_allowed()` 已删除（城市的唯一真源改为
# `config.local.yaml` 的 `candidate.cities`），这个环境变量不再有任何读取方，
# 所以一并去掉 —— 留着会让人以为还有一套城市白名单在生效。

