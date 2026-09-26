import asyncio
import os
import subprocess
import sys
import textwrap
import unittest

from oauth_plug_openai_codex import openai_oauth_audio_input as audio_input


class AudioInputResourceLimitTests(unittest.TestCase):
    def test_converter_preserves_bounded_failure_diagnostic(self):
        resolver = audio_input.BoundedOAuthAudioResolver("unused.wav", max_bytes=1024, timeout=5)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(resolver._run_converter(
                sys.executable, "-c", "import sys;sys.stderr.write('diagnostic-'+'x'*700);sys.exit(7)"
            ))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(str(caught.exception.__cause__), "converter exit code 7: " + ("diagnostic-" + "x"*700)[:512])

    def test_converter_success_returns_normally(self):
        resolver = audio_input.BoundedOAuthAudioResolver("unused.wav", max_bytes=1024, timeout=5)
        asyncio.run(resolver._run_converter(sys.executable, "-c", "pass"))

    def test_macos_limit_failure_reports_current_limits_and_vsize(self):
        harness = textwrap.dedent(
            """
            import sys
            import types

            resource = types.ModuleType("resource")
            resource.RLIMIT_FSIZE = 1
            resource.RLIMIT_AS = 2
            resource.RLIM_INFINITY = -1
            resource.getrlimit = lambda _kind: (4096, 8192)

            def reject_limit(_kind, _limits):
                raise ValueError("simulated macOS limit failure")

            resource.setrlimit = reject_limit
            sys.modules["resource"] = resource

            subprocess = types.ModuleType("subprocess")
            subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(
                stdout="12345\\n"
            )
            sys.modules["subprocess"] = subprocess
            sys.platform = "darwin"
            sys.argv = ["limit-wrapper", "1024", "2048", "ignored"]
            exec(sys.argv_source)
            """
        ).replace("sys.argv_source", "sys.modules['__main__']._limit_script")
        runner = (
            "import sys;"
            "sys.modules['__main__']._limit_script=sys.argv[1];"
            + harness
        )
        result = subprocess.run(
            [sys.executable, "-c", runner, audio_input._LIMITED_EXEC_SCRIPT],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RLIMIT_FSIZE soft=4096 hard=8192", result.stderr)
        self.assertIn("requested=1024 target=1024", result.stderr)
        self.assertIn("vsize_kib=12345", result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX resource limits")
    def test_converter_respects_existing_resource_limits(self):
        cases = (
            (1024 * 1024, 1024 * 1024),
            (512 * 1024, 1024 * 1024),
        )
        for soft_limit, hard_limit in cases:
            with self.subTest(soft_limit=soft_limit, hard_limit=hard_limit):
                outer_script = (
                    "import resource,sys;"
                    "resource.setrlimit(resource.RLIMIT_FSIZE,"
                    f"({soft_limit},{hard_limit}));"
                    "sys.argv=sys.argv[1:];exec(sys.argv[0])"
                )
                probe_script = (
                    "import resource;"
                    "print(resource.getrlimit(resource.RLIMIT_FSIZE))"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        outer_script,
                        audio_input._LIMITED_EXEC_SCRIPT,
                        str(2 * 1024 * 1024),
                        str(512 * 1024 * 1024),
                        sys.executable,
                        "-c",
                        probe_script,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    f"({soft_limit}, {soft_limit})",
                )


if __name__ == "__main__":
    unittest.main()
