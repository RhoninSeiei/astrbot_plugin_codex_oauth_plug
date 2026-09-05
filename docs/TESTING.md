# 测试说明

## 执行

在可导入 AstrBot 的开发环境中，从插件仓库根目录执行：

```bash
python -m pytest -q tests
python -m ruff check main.py oauth_plug_openai_codex tests
git diff --check
```

测试需要 pytest 和 pytest-asyncio。Realtime 测试使用 websockets 15；这些是开发环境依赖，无需为了普通聊天安装 pytest。

`unittest discover` 可运行 unittest 用例，但不会执行全部 pytest 函数用例。完整回归应使用上面的 pytest 命令。

## 覆盖范围

- 授权解析、共享刷新、解除绑定、切换账号和卸载期间的并发保护。
- 提供商注册、中文配置模板、管理员命令和包路径加载。
- GPT-6 与旧模型参数、推理强度、搜索工具合并和引用。
- 文本 SSE、消费者取消、认证重试、错误事件及断流处理。
- 图片生成、参考图和单次超时。
- 音频大小限制、转录请求、Realtime 事件与资源清理。
- 真实 AstrBot 提供商连接测试专用的本地 HTTP 服务，验证聊天与流式完整调用链。

测试使用虚构凭据和隔离的网络替身。本地 HTTP 集成测试只连接回环地址，不请求 OpenAI，不发送机器人消息，不启用生产插件。

## v0.2.0 验证环境

验证分别使用 AstrBot 4.27.5 的维护版本核心，以及官方 `v4.24.1` 核心源码。两组均在同一 Linux 开发容器与 Python 3.12.13 环境中执行，依赖版本包括 httpx 0.28.1 和 websockets 15.0.1。

官方旧版核心验证证明接口兼容，不代表复现了旧版本发布时的全部依赖组合。测试不证明特定 OpenAI 账号具有搜索、图片、转录或实时音频权限，也不替代真实 WebRTC 音频设备验收。

Windows 本机的临时目录访问受限，完整测试以 Linux 开发容器结果为准。
