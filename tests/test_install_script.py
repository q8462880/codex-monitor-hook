from pathlib import Path
import unittest


INSTALL_SCRIPT = Path(__file__).parents[1] / "scripts" / "install.ps1"
POSIX_INSTALL_SCRIPT = Path(__file__).parents[1] / "scripts" / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def test_posix_installer_is_shipped(self) -> None:
        self.assertTrue(POSIX_INSTALL_SCRIPT.exists())
        text = POSIX_INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("python3", text)
        self.assertIn("--hook-profile", text)
        self.assertIn("posix", text)

    def test_posix_installer_exports_selected_codex_home_for_diagnostics(self) -> None:
        text = POSIX_INSTALL_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("export CODEX_HOME", text)

    def test_posix_installer_creates_private_quota_auth_home(self) -> None:
        text = POSIX_INSTALL_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("QUOTA_AUTH_DIR=${CODEX_SCREEN_QUOTA_AUTH_DIR", text)
        self.assertIn('mkdir -p "$CODEX_HOME" "$TARGET_DIR" "$QUOTA_AUTH_DIR"', text)
        self.assertIn('chmod 700 "$QUOTA_AUTH_DIR"', text)

    def test_posix_installer_uses_profile_specific_hook_count(self) -> None:
        text = POSIX_INSTALL_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("expected_hook_count()", text)
        self.assertIn('expected_hook_count "$HOOK_PROFILE"', text)

    def test_posix_installer_supports_python_39_with_tomli(self) -> None:
        text = POSIX_INSTALL_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("import tomli", text)
        self.assertNotIn("Python 3.11 or newer is required", text)

    def test_config_updater_falls_back_to_tomli(self) -> None:
        updater = (Path(__file__).parents[1] / "scripts" / "update_codex_config.py")
        text = updater.read_text(encoding="utf-8")

        self.assertIn("import tomli as tomllib", text)

    def test_bootstrap_validation_checks_user_site_import(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")

        self.assertIn("import codex_hook_bootstrap", text)
        self.assertIn("--hook-diagnostic-timeout", text)

    def test_install_script_has_failure_cleanup_guard(self) -> None:
        text = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")

        self.assertIn("finally", text)
        self.assertIn("Stop-InstalledRuntimeProcess", text)
        self.assertGreater(
            text.rfind("Stop-InstalledRuntimeProcess @($RelayTarget"),
            text.index("$BootstrapInstallerTarget $TargetDir"),
        )
