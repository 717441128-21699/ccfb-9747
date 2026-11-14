import unittest

from sbom_monitor.common.model import canonical_name, make_key, Package, PackageVersion


class TestCanonicalNames(unittest.TestCase):
    def test_npm_lowercase(self):
        self.assertEqual(canonical_name("npm", "@Scope/Pkg"), "@scope/pkg")

    def test_pypi_pep503(self):
        # PEP 503：小写 + 连续的 - _ . 归一为单个连字符
        self.assertEqual(canonical_name("pypi", "Some_Pkg.Name"), "some-pkg-name")
        self.assertEqual(canonical_name("pypi", "some__pkg"), "some-pkg")
        # 不同原始写法归并为同一规范名
        self.assertEqual(canonical_name("pypi", "Foo_Bar"),
                         canonical_name("pypi", "foo-bar"))

    def test_maven_coordinate(self):
        self.assertEqual(canonical_name("maven", "Com.Acme:Artifact"),
                         "com.acme:artifact")
        with self.assertRaises(ValueError):
            canonical_name("maven", "no-colon")

    def test_key_globally_unique_shape(self):
        self.assertTrue(make_key("npm", "x").startswith("npm:"))
        self.assertTrue(make_key("maven", "g:a").startswith("maven:"))

    def test_package_roundtrip(self):
        pkg = Package(ecosystem="npm", name="x",
                      versions=[PackageVersion(version="1.0.0", released_at="",
                                               licenses="MIT")])
        d = pkg.to_dict()
        again = Package.from_dict(d)
        self.assertEqual(again.key, pkg.key)
        self.assertEqual(again.versions[0].licenses, "MIT")
        self.assertEqual(again.versions[0].dependencies, [])


if __name__ == "__main__":
    unittest.main()
