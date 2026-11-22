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
            f"2 {i18n.tr('ГБ')}",
        )
        self.assertEqual(
            disk_space.human_size(5 * disk_space.MIB),
            f"5 {i18n.tr('МБ')}",
        )
        self.assertEqual(
            disk_space.human_size(900),
            f"1 {i18n.tr('КБ')}",
        )

    def test_relief_estimate_counts_real_file_types(self):
        """Оценка считает настоящие размеры файлов.

        DEM и уклон — float32 (четыре байта на пиксель), оттенок и его
        итоговая копия — байт на пиксель.  Раньше итоговая копия
        оттенка считалась байтовой, а записывалась как float32: оценка
        была вчетверо ниже реальности.
        """
        pixels = 1000 * 1000
        estimate = disk_space.relief_estimate(1000, 1000)
        # DEM с запасом на копию, уклон, оттенок и промежуточные растры.
        self.assertEqual(
            estimate,
            pixels * (5 * disk_space.FLOAT_PIXEL + 2 * disk_space.BYTE_PIXEL),
        )

    def test_relief_estimate_covers_measured_files(self):
        """Оценка должна покрывать реальные файлы с запасом.

        Замер на DEM 16346×6674 (109 млн пикселей) после отказа от
        укрупнения: все растры нативного размера, пик подготовки —
        около 2,2 ГБ.
        """
        estimate = disk_space.relief_estimate(16346, 6674)
        self.assertGreater(estimate, 2 * disk_space.GIB)
        self.assertLess(estimate, 3 * disk_space.GIB)

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
        self.assertIn(f"1 {i18n.tr('ГБ')}", warning)
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
            error = disk_space.out_of_space("slope.tif")
        self.assertIsInstance(error, disk_space.InsufficientSpace)
        self.assertIn("slope.tif", str(error))
