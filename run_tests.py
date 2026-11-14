"""一键测试入口：py -I run_tests.py

兼容 Python 隔离模式（-I）：先把仓库根目录注入 sys.path，
再用 unittest 的 TestLoader 按模块名加载 tests 包下全部用例。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

MODULES = [
    "tests.test_versions",
    "tests.test_licenses",
    "tests.test_model",
    "tests.test_sim_features",
    "tests.test_pipeline",
    "tests.test_graph",
    "tests.test_crash_recovery",
]


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod in MODULES:
        suite.addTests(loader.loadTestsFromName(mod))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
