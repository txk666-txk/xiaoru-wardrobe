import os
import sys
import unittest

from faymcp.mcp_client import McpClient


class McpPythonResolutionTests(unittest.TestCase):
    def test_plain_python_uses_fay_interpreter_in_source_mode(self):
        client = McpClient(
            transport="stdio",
            stdio_config={
                "command": "python",
                "args": ["test/mcp_stdio_example.py"],
                "cwd": os.getcwd(),
            },
        )

        resolved = client._resolve_stdio_launch_config()

        self.assertEqual(
            os.path.normcase(os.path.abspath(resolved["command"])),
            os.path.normcase(os.path.abspath(sys.executable)),
        )


if __name__ == "__main__":
    unittest.main()
