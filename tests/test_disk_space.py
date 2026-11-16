import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qgis_xtopo.core import disk_space, i18n


class DiskSpaceTest(unittest.TestCase):
    """Оценка свободного места и предупреждения о нехватке."""

    def test_free_bytes_of_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertGreater(disk_space.free_bytes(directory), 0)

    def test_free_bytes_of_missing_directory_uses_parent(self):
        """Каталога может ещё не быть — том ищется по родителю."""
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "нет" / "такого"
            self.assertGreater(disk_space.free_bytes(missing), 0)

    def test_human_size(self):
        self.assertEqual(
            disk_space.human_size(2 * disk_space.GIB),
            f"2.0 {i18n.tr('ГБ')}",
        )
        self.assertEqual(
            disk_space.human_size(5 * disk_space.MIB),
            f"5 {i18n.tr('МБ')}",
        )
        self.assertEqual(
            disk_space.human_size(900),
            f"1 {i18n.tr('КБ')}",
        )

    def test_relief_estimate_grows_with_upscale(self):
        """Укрупнённый рельеф в разы тяжелее нативного."""
        plain = disk_space.relief_estimate(1000, 1000, False)
        upscaled = disk_space.relief_estimate(1000, 1000, True)
        self.assertGreater(upscaled, plain * 10)

    def test_relief_estimate_covers_measured_files(self):
        """Оценка должна покрывать реальные файлы с запасом.

        Замер на проекте Камчатки: DEM 2357×1198 (2.8 млн пикселей), на
        диске укрупнённый DEM 108 МБ, уклон 113 МБ, оттенок 38 МБ — и
        около 100 МБ промежуточных файлов.
        """
        estimate = disk_space.relief_estimate(2357, 1198, True)
        self.assertGreater(estimate, 250 * 1024 * 1024)
        self.assertLess(estimate, 1024 * 1024 * 1024)

    def test_warning_is_absent_when_space_is_enough(self):
        template = "Недостаточно места для рельефа: нужно ≈{}, свободно {}."
        with mock.patch.object(
            disk_space, "free_bytes", return_value=10 * disk_space.GIB
        ):
            self.assertIsNone(
                disk_space.space_warning("/tmp", disk_space.GIB, template)
            )

    def test_warning_reports_needed_and_free_space(self):
        template = "Недостаточно места для рельефа: нужно ≈{}, свободно {}."
        with mock.patch.object(
            disk_space, "free_bytes", return_value=disk_space.MIB
        ):
            warning = disk_space.space_warning("/tmp", disk_space.GIB, template)
        self.assertIn(f"1.0 {i18n.tr('ГБ')}", warning)
        self.assertIn(f"1 {i18n.tr('МБ')}", warning)

    def test_require_space_raises_when_not_enough(self):
        template = "Недостаточно места для растра {}: нужно ≈{}, свободно {}."
        with mock.patch.object(disk_space, "free_bytes", return_value=1024):
            with self.assertRaises(disk_space.InsufficientSpace) as context:
                disk_space.require_space("/tmp", disk_space.GIB, template, "slope")
        self.assertIn("slope", str(context.exception))

    def test_require_space_passes_when_enough(self):
        template = "Недостаточно места для растра {}: нужно ≈{}, свободно {}."
        with mock.patch.object(
            disk_space, "free_bytes", return_value=10 * disk_space.GIB
        ):
            disk_space.require_space("/tmp", disk_space.GIB, template, "slope")

    def test_out_of_space_names_the_file(self):
        with mock.patch.object(disk_space, "free_bytes", return_value=0):
            error = disk_space.out_of_space("slope_upscaled.tif")
        self.assertIsInstance(error, disk_space.InsufficientSpace)
        self.assertIn("slope_upscaled.tif", str(error))
