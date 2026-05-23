import subprocess
import sys
import unittest

from tests.dns_test.dns_query_command import build_python_script_argv


class DNSQueryCommandTest(unittest.TestCase):
    def test_multiline_python_script_runs_with_arguments(self):
        script = """
import sys
print("ARG1=%s" % sys.argv[1])
print("ARG2=%s" % sys.argv[2])
"""

        argv = build_python_script_argv(
            script,
            ["web.cs305.local", "192.168.1.3"],
            python_executable=sys.executable,
        )
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARG1=web.cs305.local", result.stdout)
        self.assertIn("ARG2=192.168.1.3", result.stdout)
        self.assertNotIn("SyntaxError", result.stderr)


if __name__ == "__main__":
    unittest.main()
