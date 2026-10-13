# 开发测试

本页仅供修改插件代码时使用，安装和配置插件见 [README](../README.md)。

在可导入 AstrBot 的开发环境中，安装 `pytest`、`pytest-asyncio` 和 `ruff` 后，从仓库根目录执行：

```bash
python -m pytest -q tests
python -m ruff check main.py oauth_plug_openai_codex tests
git diff --check
```

Realtime 测试需要 `websockets>=15`。完整回归使用 pytest；`unittest discover` 不会收集全部用例。

测试覆盖授权与并发刷新、提供商注册、聊天流式输出、图片与音频接口、额度查询和用量统计。使用虚构凭据及模拟服务，本地 HTTP 集成测试仅连接回环地址，无需真实账号。新核心专用的用量记录器测试会在旧核心上跳过。
