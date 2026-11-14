import unittest

from sbom_monitor.versions import semver, pep440
from sbom_monitor.versions import maven as maven_ver
from sbom_monitor.versions import resolver


class TestSemVer(unittest.TestCase):
    def test_caret(self):
        self.assertTrue(semver.satisfies("1.2.3", "^1.0.0"))
        self.assertFalse(semver.satisfies("2.0.0", "^1.0.0"))
        self.assertTrue(semver.satisfies("0.2.5", "^0.2.3"))
        self.assertFalse(semver.satisfies("0.3.0", "^0.2.3"))
        self.assertTrue(semver.satisfies("0.0.3", "^0.0.3"))
        self.assertFalse(semver.satisfies("0.0.4", "^0.0.3"))

    def test_tilde_and_ranges(self):
        self.assertTrue(semver.satisfies("1.2.9", "~1.2.3"))
        self.assertFalse(semver.satisfies("1.3.0", "~1.2.3"))
        self.assertTrue(semver.satisfies("1.5.0", ">=1.0.0 <2.0.0"))
        self.assertTrue(semver.satisfies("1.2.3", "1.2.x"))
        self.assertTrue(semver.satisfies("2.1.0", "1.x || 2.x"))

    def test_prerelease_gate(self):
        self.assertTrue(semver.satisfies("1.0.0-beta.2", ">=1.0.0-beta.1"))
        self.assertFalse(semver.satisfies("1.0.0-beta.2", ">=1.0.0"))

    def test_sort(self):
        self.assertEqual(
            semver.sort_versions(["1.10.0", "1.9.0", "1.9.0-beta.1"]),
            ["1.9.0-beta.1", "1.9.0", "1.10.0"])


class TestPEP440(unittest.TestCase):
    def test_specifiers(self):
        self.assertTrue(pep440.satisfies("1.2.3", ">=1.0,<2"))
        self.assertFalse(pep440.satisfies("2.0.0", ">=1.0,<2"))
        self.assertTrue(pep440.satisfies("1.2.0", "==1.2.*"))
        self.assertTrue(pep440.satisfies("1.5", "==1.5"))

    def test_compatible_release(self):
        self.assertTrue(pep440.satisfies("1.3.0", "~=1.2"))
        self.assertFalse(pep440.satisfies("2.0.0", "~=1.2"))
        self.assertFalse(pep440.satisfies("1.3.0", "~=1.2.0"))

    def test_prerelease_exclusion(self):
        self.assertFalse(pep440.satisfies("1.0a1", ">=1.0"))
        self.assertTrue(pep440.satisfies("1.0a1", ">=1.0a1"))  # 说明符带预发则隐式放行

    def test_sort(self):
        self.assertEqual(
            pep440.sort_versions(["1.0.0", "1.0.0.dev1", "1.0.0a1", "1.0.0.post1"]),
            ["1.0.0.dev1", "1.0.0a1", "1.0.0", "1.0.0.post1"])


class TestMaven(unittest.TestCase):
    def test_ranges(self):
        self.assertTrue(maven_ver.satisfies("1.5.0", "[1.0,2.0)"))
        self.assertFalse(maven_ver.satisfies("2.0.0", "[1.0,2.0)"))
        self.assertTrue(maven_ver.satisfies("1.0", "[1.0]"))
        self.assertTrue(maven_ver.satisfies("2.0", "[1.5,)"))
        self.assertTrue(maven_ver.satisfies("0.9", "(,1.0]"))

    def test_soft_requirement(self):
        self.assertTrue(maven_ver.soft_match("1.5", "1.0"))
        self.assertFalse(maven_ver.soft_match("0.9", "1.0"))

    def test_ordering(self):
        ordered = ["1.0-alpha1", "1.0-alpha2", "1.0-beta1", "1.0", "1.0-sp1"]
        self.assertEqual(
            maven_ver.sort_versions(list(reversed(ordered))), ordered)
        self.assertEqual(
            maven_ver.sort_versions(["1.10", "1.9"]), ["1.9", "1.10"])

    def test_numeric_padding(self):
        self.assertEqual(maven_ver.MavenVersion.parse("2.0"),
                         maven_ver.MavenVersion.parse("2.0.0"))


class TestResolverDispatch(unittest.TestCase):
    def test_dispatch(self):
        self.assertTrue(resolver.satisfies("npm", "1.2.0", "^1.0.0"))
        self.assertTrue(resolver.satisfies("pypi", "1.2.0", "~=1.0"))
        self.assertTrue(resolver.satisfies("maven", "1.2.0", "[1.0,2.0)"))
        self.assertTrue(resolver.satisfies("npm", "9.9.9", "*"))


if __name__ == "__main__":
    unittest.main()
