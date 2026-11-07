"""Resume draft publication and verify already-published assets without replacing them."""

import hashlib
import json
import os
from pathlib import Path
import subprocess


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8")


def publish(tag, output=Path("dist")):
    repo = os.environ["GH_REPO"]
    assets = sorted(output.glob("*.zip")) + [output / "SHA256SUMS"]
    if len(assets) != 2 or not all(path.is_file() for path in assets):
        raise ValueError("Expected exactly one installation ZIP and SHA256SUMS")
    checksum, filename = assets[-1].read_text(encoding="utf-8").split()
    if filename != assets[0].name or checksum != hashlib.sha256(assets[0].read_bytes()).hexdigest():
        raise ValueError("Installation checksum does not match")
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    releases = [release for page in releases for release in page]
    existing = next((release for release in releases if release["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        remote = {asset["name"]: asset.get("digest") for asset in existing["assets"]}
        for asset in assets:
            expected = "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest()
            if remote.get(asset.name) != expected:
                raise ValueError(f"Published asset differs or is missing: {asset.name}; refusing replacement")
        print(f"{tag}: published assets verified; no changes required")
        return
    if existing:
        gh("release", "upload", tag, *map(str, assets), "--clobber")
        gh("release", "edit", tag, "--notes-file", str(output / "RELEASE_NOTES.md"))
    else:
        gh("release", "create", tag, *map(str, assets), "--verify-tag", "--draft", "--title", tag, "--notes-file", str(output / "RELEASE_NOTES.md"))
    # Do not promote a historical version over a newer published release.
    def version(value):
        return tuple(map(int, value.removeprefix("v").split(".")))
    newer = any(not release["draft"] and release["tag_name"].startswith("v") and _newer(release["tag_name"], tag, version) for release in releases)
    gh("release", "edit", tag, "--draft=false", "--latest=false" if newer else "--latest")


def _newer(candidate, tag, parse):
    try:
        return parse(candidate) > parse(tag)
    except ValueError:
        return False


if __name__ == "__main__":
    publish(os.environ["RELEASE_TAG"])
