import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from kernel.mcp import MCPConfigurationError, _resolve_binding, main as mcp_main


class MCPBindingTests(unittest.TestCase):
    def test_missing_project_fails_closed_instead_of_using_current_directory(self) -> None:
        with self.assertRaisesRegex(MCPConfigurationError, "project must be fixed"):
            _resolve_binding(None, "claude", environment={})

    def test_uninitialized_bound_project_is_rejected_before_the_server_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "uninitialized"
            project.mkdir()
            error = io.StringIO()

            with contextlib.redirect_stderr(error):
                status = mcp_main(["--project", str(project), "--actor", "claude"])

        self.assertEqual(status, 2)
        self.assertIn("State tree is not a directory", error.getvalue())


if __name__ == "__main__":
    unittest.main()
