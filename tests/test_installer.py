import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"


def _read(path):
    return path.read_text(encoding="utf-8")


class InstallerTest(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue((INSTALLER / "qgis-xtopo.iss").exists())
        self.assertTrue((INSTALLER / "scripts" / "install-qgis.ps1").exists())
        self.assertTrue((INSTALLER / "build-installer.sh").exists())
        self.assertTrue((INSTALLER / "build-installer.ps1").exists())

    def test_version_matches_metadata(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        match = re.search(r'#define MyAppVersion "([^"]+)"', text)
        self.assertIsNotNone(match)
        metadata = _read(ROOT / "metadata.txt")
        version = re.search(r"^version=(.+)$", metadata, re.MULTILINE).group(1).strip()
        self.assertEqual(match.group(1), version)

    def test_plugin_goes_to_the_qgis_profile(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("QGIS\\QGIS3\\profiles\\default", text)
        self.assertIn("python\\plugins\\qgis_xtopo", text)

    def test_plugin_is_enabled_in_ini(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("[PythonPlugins]", text)
        self.assertIn("qgis_xtopo=true", text)

    def test_shortcut_is_created(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("{autodesktop}\\QGIS-xtopo", text)
        self.assertIn("{group}\\QGIS-xtopo", text)
        self.assertIn("QGIS_XTOPO_OPEN=1", text)

    def test_qgis_is_searched_and_installed(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("qgis-bin.exe", text)
        self.assertIn("SOFTWARE\\QGIS\\QGIS3", text)
        self.assertIn("OSGeo4W", text)
        self.assertIn("install-qgis.ps1", text)

    def test_helper_downloads_qgis_installer(self):
        text = _read(INSTALLER / "scripts" / "install-qgis.ps1")
        self.assertIn("https://qgis.org/download/", text)
        self.assertIn("download.qgis.org/downloads", text)
        self.assertIn("msiexec.exe", text)
        self.assertIn("qgis-bin.exe", text)

    def test_qgis_is_installed_through_osgeo4w(self):
        # Сетевой установщик OSGeo4W с фиксированным списком пакетов:
        # качает только QGIS LTR вместо полного standalone-MSI.
        text = _read(INSTALLER / "scripts" / "install-qgis.ps1")
        self.assertIn("osgeo4w-setup.exe", text)
        self.assertIn("download.osgeo.org/osgeo4w/v2/", text)
        self.assertIn("qgis-ltr", text)
        self.assertIn("-P", text)
        self.assertIn("LOCALAPPDATA", text)

    def test_msi_stays_as_a_fallback(self):
        text = _read(INSTALLER / "scripts" / "install-qgis.ps1")
        self.assertIn("Install-Osgeo4W", text)
        self.assertIn("Install-Msi", text)
        # Сначала пробуем OSGeo4W, и только потом MSI.
        self.assertLess(text.index("Install-Osgeo4W)"), text.index("Install-Msi)"))

    def test_both_qgis_layouts_are_supported(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("qgis-bin.exe", text)
        self.assertIn("qgis-ltr-bin.exe", text)
        self.assertIn("qgis-ltr.bat", text)

    def test_installer_works_without_admin_rights(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("PrivilegesRequired=lowest", text)

    def test_qgis_install_is_verified_not_trusted(self):
        # Код возврата скрипта не признак успеха: заглушка powershell.exe
        # (в wine) выходит с 0, ничего не сделав. Проверяем результат.
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("Installed := QgisExe;", text)
        self.assertIn("FileExists(LogFile)", text)

    def test_qgis_question_is_asked_on_a_visible_page(self):
        # Вопрос задаётся при нажатии «Далее» на странице выбора каталога:
        # из CurPageChanged мастер после ответа сразу завершается.
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("AskInstallQgis", text)
        self.assertIn("wpSelectDir", text)
        self.assertIn("NextButtonClick", text)

    def test_helper_writes_log_at_start(self):
        text = _read(INSTALLER / "scripts" / "install-qgis.ps1")
        self.assertIn("Скрипт установки QGIS запущен", text)

    def test_install_check_script_exists(self):
        script = INSTALLER / "scripts" / "check-qgis-install.py"
        self.assertTrue(script.exists())
        text = _read(script)
        self.assertIn("qgis_xtopo", text)
        self.assertIn("GetDriverByName", text)

    def test_ci_can_verify_the_installation(self):
        workflow = ROOT / ".github" / "workflows" / "verify-qgis-install.yml"
        self.assertTrue(workflow.exists())
        text = _read(workflow)
        self.assertIn("install-qgis.ps1", text)
        self.assertIn("check-qgis-install.py", text)


if __name__ == "__main__":
    unittest.main()
