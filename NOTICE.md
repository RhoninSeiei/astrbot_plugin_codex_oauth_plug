# 许可证与代码来源

从 v0.2.0 起，本仓库的组合版本按 GNU Affero General Public License v3.0 分发，完整文本见 [LICENSE](LICENSE)。

## 原插件代码

原插件由 RhoninSeiei 开发，版权声明为 `Copyright (c) 2026 RhoninSeiei`。原 MIT 许可全文保留在 [LICENSE-MIT](LICENSE-MIT)。该许可适用于原先以 MIT 发布的代码，不授予将下述 AGPL 改编代码重新作为 MIT 分发的权利。

## AstrBot 改编代码

上游项目：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)。上游作者与贡献者保留其版权，许可为 AGPL-3.0。

本次同步基于 AstrBot 4.27.5 维护版本的 OAuth 实现，以下文件包含直接改编的实现：

- `oauth_plug_openai_codex/provider.py`：源自 `astrbot/core/provider/sources/openai_oauth_source.py`。
- `oauth_plug_openai_codex/openai_oauth_audio.py`
- `oauth_plug_openai_codex/openai_oauth_audio_input.py`
- `oauth_plug_openai_codex/openai_oauth_realtime.py`
- `oauth_plug_openai_codex/openai_oauth_shared_state.py`
- `oauth_plug_openai_codex/openai_oauth_sse.py`
- `oauth_plug_openai_codex/openai_oauth_transcription.py`

上述辅助模块对应上游 `astrbot/core/provider/oauth/` 下的同名文件。`tests/test_provider_*_core.py` 改编自同一维护版本的 OAuth 回归测试。

本次改动日期为 2026-09-05。主要修改包括插件相对导入、插件级凭据服务与持久化、旧版核心接口兼容、单次图片超时、资源清理和独立插件测试适配。其他本轮改动见 [CHANGELOG](CHANGELOG.md)。

本仓库提供组合版本的对应源代码。分发或修改本版本时，请遵循 LICENSE 中的源码提供、许可及版权声明保留要求。
