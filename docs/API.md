# 开发接口

日常安装与授权见 [README](../README.md)。本文面向调用本插件的 AstrBot 插件开发者。

## 获取提供商

```python
provider = context.get_provider_by_id(provider_id)
if provider is None:
    raise RuntimeError("未找到指定模型提供商")
```

`provider_id` 使用 AstrBot 模型页面保存的实际 ID。类型为 `oauth_plug_openai_codex_chat_completion`，与内建 OAuth 类型独立。所有模型共享插件的账号授权；不要由调用方读取或更新令牌。

## 聊天与流式输出

```python
response = await provider.text_chat(
    prompt="分析这段内容",
    reasoning_effort="high",
)
print(response.completion_text)

async for response in provider.text_chat_stream(prompt="介绍这个项目"):
    # 流中包含增量片段和最终结果，按 AstrBot 当前版本的 is_chunk 语义处理。
    if getattr(response, "is_chunk", False):
        print(response.completion_text, end="")
```

保留 AstrBot 的图片输入、工具调用和上下文参数。流式请求发生中断后不会把已经输出的文本当作完整回复。

推理强度从高到低依次取：

1. 单次 `reasoning.effort`。
2. 单次 `reasoning_effort`。
3. 提供商 `custom_extra_body.reasoning.effort`。
4. 提供商 `custom_extra_body.reasoning_effort`。

最终请求统一为 `reasoning.effort`。`off` 归一化为 `none`，旧模型不支持 `max` 时转换为 `xhigh`；`ultra` 不属于单次请求支持值。模型能力表中的默认强度仅为元数据，不会自动写入请求。

## 联网搜索

```python
response = await provider.text_chat(
    prompt="查找相关官方文档",
    oauth_web_search="live",
)
```

单次 `oauth_web_search` 可取 `disabled`、`cached`、`live` 或 `inherit`。省略或使用 `inherit` 时继承提供商设置。提供商设置未指定时使用插件高级设置，默认关闭。

提供商配置可包含：

```json
{
  "oauth_web_search": "live",
  "oauth_web_search_domains": ["example.com"]
}
```

搜索作为 Codex Responses 工具发送，支持与函数工具合并；冲突的工具定义会报错。返回结果保留服务端 URL 引用。是否搜索及搜索结果由服务端决定。

## 图片生成与参考图编辑

```python
capabilities = getattr(provider, "capabilities", {})
if not capabilities.get("image_generate"):
    raise RuntimeError("当前提供商缺少生图能力")

reference_images = ["/tmp/reference.png"]
if reference_images and not capabilities.get("image_edit"):
    raise RuntimeError("当前提供商缺少参考图编辑能力")

images = await provider.generate_image(
    prompt="根据参考图重绘背景",
    model="gpt-5.6-sol",
    size="1024x1024",
    n=1,
    reference_images=reference_images,
    timeout=180.0,
)
for image in images:
    print(image.path, image.mime_type)
```

- `reference_images` 支持本地路径、`file://`、HTTP 图片 URL 和 `data:image/...`。
- 有参考图时默认 `action=edit`，无参考图时默认 `action=generate`，也可显式传入 `action`。
- `timeout` 为本次图片请求的超时秒数；省略或为 `None` 时使用提供商默认值，不修改其他调用的超时。
- 结果对象包含 `path`、`mime_type`、`revised_prompt` 和 `raw`。
- 文件保存到插件配置的 `generated_image_dir`；留空时为 AstrBot data 下的 `generated/oauth_plug_openai_codex_images`。调用方负责发送、转存和后续文件管理。

## 音频转录

```python
text = await provider.transcribe_audio(
    "/tmp/voice.wav",
    model="gpt-4o-transcribe",
    language="zh",
)
```

该接口需要账号额外具备 OpenAI 音频转录权限，聊天授权成功不代表具有转录权限。插件限制输入大小和执行时间，清理临时文件，并在提供商关闭时取消未完成请求。

如需在聊天的 `audio_urls` 中自动转录，设置 `oauth_audio_transcription=true`，可通过 `oauth_transcription_model` 指定转录模型。默认关闭，关闭时音频输入给出明确提示。

## 实时音频

`create_realtime_session(sdp_offer, model="gpt-live-1-codex", voice="cove", instructions="")` 接受调用方 WebRTC 的 SDP offer，返回插件管理的实时会话。调用方负责音频传输和界面，插件负责 OAuth 会话建立与控制通道。

```python
session = await provider.create_realtime_session(sdp_offer)
try:
    # 将 session.answer_sdp 交给调用方的 WebRTC 客户端。
    await session.send_text("请用中文回答")
    async for event in session.events():
        # 按事件类型处理文本、工具委托和状态；不要记录凭据。
        handle_event(event)
finally:
    await session.close()
```

Realtime 需要 `websockets>=15` 的 asyncio 客户端支持，且账号必须具有对应服务权限。该依赖仅在调用实时音频时加载；普通聊天与生图不依赖实时音频权限。非 WAV 音频转换还需要环境中的 FFmpeg。实际支持的模型和声音由服务端决定。

## 插件 Web API

以下均为 `POST`，使用 AstrBot Dashboard 的认证机制。Dashboard JWT 与开发者 OpenAPI Key 不可互换。

| 路径 | JSON 请求体 | 用途 |
| --- | --- | --- |
| `/api/plug/oauth-plug-openai-codex/start` | `{}` | 创建授权链接。 |
| `/api/plug/oauth-plug-openai-codex/complete` | `{"input":"完整回调地址或 JSON 凭据"}` | 完成授权。 |
| `/api/plug/oauth-plug-openai-codex/refresh` | `{}` | 手动刷新。 |
| `/api/plug/oauth-plug-openai-codex/test` | `{"model":"gpt-6-astra"}` | 测试模型，可省略 model。 |
| `/api/plug/oauth-plug-openai-codex/disconnect` | `{}` | 清除插件授权。 |

检查返回体的 `status` 和 `message`，不能仅凭 HTTP 200 判断业务成功。授权链接、回调、JSON 凭据和响应中的账号信息不得写入公开日志。
