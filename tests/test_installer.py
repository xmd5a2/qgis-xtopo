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

    def test_installer_works_without_admin_rights(self):
        text = _read(INSTALLER / "qgis-xtopo.iss")
        self.assertIn("PrivilegesRequired=lowest", text)


if __name__ == "__main__":
    unittest.main()
