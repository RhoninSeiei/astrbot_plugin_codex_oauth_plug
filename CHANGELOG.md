# 更新日志

## 未发布

- 公共 `generate_image()` 增加可选的单次 `timeout` 参数，供 GroupChat、ImgFlow 等插件为图片请求单独设置超时，并保持 provider 默认超时值。
- 默认模型组更新为 GPT-5.6 Sol、Terra 和 Luna，并保留旧模型及自定义模型配置兼容。
- 为 AstrBot 4.24 补充核心请求重试模块缺失时的兼容分支。
- 同步 `reasoning_effort` 规范化与覆盖优先级，拒绝单次 Provider 请求使用 `ultra`，旧模型的 `max` 转换为 `xhigh`。
- 统一 Provider 与连接测试的 Codex 请求头，携带 `version=0.144.0`、`User-Agent=codex_cli_rs/0.144.0` 和 JWT residency。
- 保持文生图、参考图编辑和 SSE 增量解析兼容，并补充相关回归测试。

- 补充 OAuth 生图扩展回归测试，覆盖 SSE 输出回填、重复输出去重、401/403 后刷新重试和其他插件带参考图调用。
- 同步 AstrBot 本体整合版 OAuth 生图专用 SSE 请求分支，供 `generate_image()` 增量读取图片生成事件。
- 同步 AstrBot 本体整合版 OAuth 图像能力，支持 `generate_image()` 传入参考图并自动使用图片编辑请求。
- README 补充其他插件调用 `generate_image()` 的方法、参考图输入类型和生成文件保存位置。

## v0.1.3

- 注册 provider 模板到 AstrBot 模型服务提供商页面，使新增提供商菜单可显示 `OAuth_plug OpenAI Codex OAuth`。
- 修正默认 provider source ID，避免模型显示为重复的 `source/model/model` 形式。
- 简化 OAuth 配置页显示，隐藏临时授权字段与未稳定返回的账号邮箱字段。
- 修复 `codex_oauth_test` 的 Codex backend 测试请求结构，补充必需的 `instructions` 字段。
- 支持 AstrBot 以包路径方式加载 WebUI 安装的插件。
- 新增 MIT License。

## v0.1.0

- 初始版本，提供 OpenAI Codex OAuth PKCE 绑定、刷新、测试命令和 AstrBot provider 适配器。
