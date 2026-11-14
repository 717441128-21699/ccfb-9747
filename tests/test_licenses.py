import unittest

from sbom_monitor.common.licenses import normalize_license, normalize_one, is_unknown


class TestLicenseNormalization(unittest.TestCase):
    def test_aliases(self):
        cases = {
            "MIT": "MIT", "mit": "MIT", "MIT License": "MIT",
            "Apache 2.0": "Apache-2.0", "Apache-2": "Apache-2.0",
            "ASL 2.0": "Apache-2.0", "BSD 3-Clause": "BSD-3-Clause",
            "GPLv3": "GPL-3.0-only", "GPLv2+": "GPL-2.0-or-later",
            "LGPL 2.1": "LGPL-2.1-only", "MPL 2.0": "MPL-2.0",
            "The Unlicense": "Unlicense", "ISC License": "ISC",
            "": "NOASSERTION", "unknown": "NOASSERTION",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_one(raw), expected, raw)

    def test_spdx_case_insensitive(self):
        self.assertEqual(normalize_one("apache-2.0"), "Apache-2.0")
        self.assertEqual(normalize_one("bsd-2-clause"), "BSD-2-Clause")

    def test_expression(self):
        self.assertEqual(normalize_one("(mit OR apache-2.0) AND bsd-2-clause"),
                         "(MIT OR Apache-2.0) AND BSD-2-Clause")

    def test_multi_license_list(self):
        self.assertEqual(normalize_license(["MIT", "Apache 2.0"]),
                         "(MIT OR Apache-2.0)")
        self.assertEqual(normalize_license([None, "", "MIT"]), "MIT")

    def test_unknown_preserved(self):
        spdx = normalize_one("WeirdCustomLicense")
        self.assertTrue(is_unknown(spdx))
        self.assertIn("WeirdCustomLicense", spdx)


if __name__ == "__main__":
    unittest.main()
