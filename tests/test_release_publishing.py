"""Publication recovery must never overwrite an immutable published package."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publish_release", Path(__file__).resolve().parents[1] / ".github/scripts/publish_release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ReleasePublishingTests(unittest.TestCase):
    def test_published_draft_new_and_conflicting_releases(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            archive = output / "plugin.zip"
            archive.write_bytes(b"synthetic package")
            (output / "SHA256SUMS").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  plugin.zip\n")
            (output / "RELEASE_NOTES.md").write_text("notes")
            assets = [{"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()} for path in (archive, output / "SHA256SUMS")]
            published = {"tag_name": "v0.7.0", "draft": False, "assets": assets}
            for state in ("published", "draft", "new", "conflict", "historical"):
                with self.subTest(state=state):
                    calls = []
                    release = dict(published)
                    release["draft"] = state == "draft"
                    if state == "conflict":
                        release["assets"] = []
                    releases = [] if state == "new" else [release]
                    if state == "historical":
                        releases = [dict(published, tag_name="v0.8.0")]
                    def gh(*args):
                        calls.append(args)
                        return json.dumps([releases]) if args[0] == "api" else ""
                    with patch.dict(os.environ, {"GH_REPO": "owner/repo"}), patch.object(publisher, "gh", gh):
                        if state == "conflict":
                            with self.assertRaisesRegex(ValueError, "refusing replacement"):
                                publisher.publish("v0.7.0", output)
                        else:
                            publisher.publish("v0.7.0", output)
                    mutations = [call for call in calls if call[0] != "api"]
                    if state in ("published", "conflict"):
                        self.assertEqual(mutations, [])
                    elif state == "draft":
                        self.assertEqual(mutations[0][:2], ("release", "upload"))
                    else:
                        self.assertEqual(mutations[0][:2], ("release", "create"))
                    if state == "historical":
                        self.assertIn("--latest=false", mutations[-1])
