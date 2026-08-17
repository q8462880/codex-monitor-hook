import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from install_python_hook_bootstrap import PTH_FILE_NAME, install_bootstrap


class InstallPythonHookBootstrapTest(unittest.TestCase):
    def test_installs_runtime_path_and_bootstrap_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_dir = root / "runtime"
            user_site = root / "user-site"

            with patch(
                "install_python_hook_bootstrap.site.getusersitepackages",
                return_value=str(user_site),
            ):
                pth_path = install_bootstrap(runtime_dir)

            self.assertEqual(user_site / PTH_FILE_NAME, pth_path)
            self.assertEqual(
                f"{runtime_dir}\nimport codex_hook_bootstrap\n",
                pth_path.read_text(encoding="utf-8"),
            )
