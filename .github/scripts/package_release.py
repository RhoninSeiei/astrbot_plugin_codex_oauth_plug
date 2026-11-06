"""Build a version-checked, tracked-file-only AstrBot installation archive."""

import hashlib
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def main(tag: str) -> None:
    root = Path(__file__).resolve().parents[2]
    version = re.search(r"^version:\s*(v\d+\.\d+\.\d+)\s*$", (root / "metadata.yaml").read_text(encoding="utf-8"), re.MULTILINE)
    if not version or tag != version[1]:
        raise ValueError("Release tag must match metadata.yaml version")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    required = {"main.py", "metadata.yaml", "_conf_schema.json", "README.md", "LICENSE", "LICENSE-MIT", "NOTICE.md", "CHANGELOG.md", "logo.png"}
    files = sorted(name for name in tracked if name in required or (name.startswith("oauth_plug_openai_codex/") and name.endswith(".py")) or name.startswith("docs/"))
    if not required.issubset(files):
        raise ValueError("Required installation or license files are missing")
    output = root / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"astrbot_plugin_codex_oauth_plug-{tag}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in files:
            info = zipfile.ZipInfo(f"astrbot_plugin_codex_oauth_plug/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, (root / name).read_bytes())
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None or len(package.namelist()) != len(files):
            raise ValueError("Installation archive verification failed")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(rf"^## {re.escape(tag)}[^\n]*\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    if not section:
        raise ValueError("Release notes are missing from CHANGELOG.md")
    notes = f"{section[1].strip()}\n\n安装包：`{archive.name}`。下载后可用 `sha256sum -c SHA256SUMS` 校验。\n\n基于官方 AstrBot 4.29.4 的自动测试通过后发布；宿主前置压缩及旧版本统计能力边界见 README 与 docs/API.md。\n"
    (output / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    print(f"{archive.name}: {len(files)} files; SHA256 {checksum}")


if __name__ == "__main__":
    main(sys.argv[1])
