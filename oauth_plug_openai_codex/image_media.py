"""Preserve image formats and handle GIF references without implying animation edits."""

import base64
import io
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024


def image_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    raise ValueError("图片结果不是受支持的 PNG、JPEG、WebP 或 GIF 文件。")


def inspect_gif(data: bytes, *, first_frame: bool = False) -> tuple[int, bytes | None]:
    # Pillow is loaded only for GIF handling; it is already an AstrBot dependency.
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "GIF" or image.width * image.height > 25_000_000:
                raise ValueError("Invalid GIF")
            frames = 0
            for index in range(257):
                try:
                    image.seek(index)
                except EOFError:
                    break
                frames += 1
                if frames > 256 or frames * image.width * image.height > 100_000_000:
                    raise ValueError("GIF pixel or frame budget exceeded")
            image.load()
            if not first_frame:
                return frames, None
            image.seek(0)
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            return frames, output.getvalue()
    except (OSError, ValueError, EOFError, Image.DecompressionBombError):
        raise ValueError("GIF 文件无效或超过处理限制。") from None


def decode_result(value: str) -> tuple[bytes, str, str, int]:
    if value.startswith("data:"):
        value = value.partition(",")[2]
    if len(value) > MAX_RESULT_BYTES * 4 // 3 + 8:
        raise ValueError("图片结果超过大小限制。")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError("图片结果包含无效 Base64。") from None
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError("图片结果超过大小限制。")
    extension, mime = image_format(data)
    if extension == "gif":
        frames = inspect_gif(data)[0]
    else:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Image pixel budget exceeded")
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError("图片结果无效、截断或超过处理限制。") from None
        frames = 1
    return data, extension, mime, frames


def reference_image_url(value: str, *, gif_input_mode: str = "reject") -> str:
    if gif_input_mode not in {"reject", "first_frame"}:
        raise ValueError("gif_input_mode 必须为 reject 或 first_frame。")
    value = str(value or "").strip()
    if not value:
        raise ValueError("参考图不能为空。")
    lower = value.lower()
    if lower.startswith(("http://", "https://")):
        if urlsplit(value).path.lower().endswith(".gif"):
            raise ValueError("远程 GIF 请先保存为本地文件，以便检查动画帧。")
        return value
    if lower.startswith("data:image/"):
        header, separator, encoded = value.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise ValueError("参考图 data URL 必须使用 Base64。")
        if len(encoded) > MAX_REFERENCE_BYTES * 4 // 3 + 8:
            raise ValueError("参考图超过大小限制。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError("参考图包含无效 Base64。") from None
        mime = header[5:].split(";", 1)[0]
    else:
        path = Path(value[7:] if lower.startswith("file://") else value).expanduser()
        if not path.is_file():
            raise ValueError("参考图文件不存在。")
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise ValueError("参考图超过大小限制。")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError("参考图超过大小限制。")
    is_gif = data[:6] in (b"GIF87a", b"GIF89a")
    if mime.lower() == "image/gif" or is_gif:
        frames, _ = inspect_gif(data)
        if frames > 1 and gif_input_mode != "first_frame":
            raise ValueError("暂不支持动画 GIF 编辑；可显式使用 gif_input_mode=first_frame 编辑首帧。")
        _, png = inspect_gif(data, first_frame=True)
        data, mime = png, "image/png"
    elif lower.startswith("data:image/"):
        return value
    encoded = base64.b64encode(data).decode()
    return f"data:{mime};base64,{encoded}"
