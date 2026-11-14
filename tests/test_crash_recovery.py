"""进程韧性测试：引擎子进程被强杀后重启，断点续跑、不重不漏。

该用例会 spawn 独立 Python 子进程跑 ``run.py engine``，
在爬取中途强制结束（Windows: taskkill /F，POSIX: kill -9 语义），
再启动第二个进程续跑，最终与仿真真值对账要求零差异、零重复行。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sbom_monitor.collector.storage import Database
from sbom_monitor.reconcile.checker import Reconciler
from sbom_monitor.sim.cluster import SimCluster


class TestCrashRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="sbom-crash-")
        # 让 run.py 使用的默认 data 目录指向临时位置：通过覆盖 CWD 不行，
        # 因此直接用环境无关的独立 workdir，手动构造与 CLI 相同的目录布局
        cls.workdir = cls.tmpdir
        cls.sim_dir = os.path.join(cls.workdir, "sim")
        cls.db_path = os.path.join(cls.workdir, "sbom.db")
        cls.repo_cfg = os.path.join(cls.workdir, "repos.json")
        cls.cluster = SimCluster(
            cls.sim_dir, counts={"npm": 12, "pypi": 10, "maven": 8},
            ports={"npm": 19701, "pypi": 19702, "maven": 19703},
            mutate_interval=1000.0, rate_per_sec=500.0, burst=1000)
        cls.cluster.start(populate=True)
        cls.cluster.wait_healthy()
        cls.cluster.write_repo_config(cls.repo_cfg)
        cls.truth_pkgs = {h.ecosystem: len(h.state.packages) for h in cls.cluster.handles}
        cls.truth_vers = {
            h.ecosystem: sum(len(p["versions"]) for p in h.state.packages.values())
            for h in cls.cluster.handles}

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _spawn_engine(self, db_env_dir):
        # 子进程通过专用小脚本指向临时 workdir，避免污染默认 data/
        runner = os.path.join(self.workdir, "_engine_proc.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {ROOT!r})\n"
                "from sbom_monitor.collector.engine import Engine\n"
                "from sbom_monitor.collector.storage import Database\n"
                "import sbom_monitor.cli as cli\n"
                f"cli.DEFAULT_WORKDIR = {self.workdir!r}\n"
                f"cli.DB_PATH = {self.db_path!r}\n"
                f"cli.SIM_DIR = {self.sim_dir!r}\n"
                f"cli.REPO_CFG = {self.repo_cfg!r}\n"
                "db = Database(cli.DB_PATH)\n"
                "cli.init_db(db)\n"
                "eng = Engine(db, feed_interval=0.2, verify_interval=9999, page_size=5)\n"
                "eng.start(); eng.wait()\n")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        return subprocess.Popen([sys.executable, "-I", runner], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=flags)

    def _kill(self, proc):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.kill()
        proc.wait(timeout=10)

    def _wait_packages(self, minimum, timeout=60):
        deadline = time.time() + timeout
        last = 0
        while time.time() < deadline:
            if os.path.exists(self.db_path):
                db = Database(self.db_path)
                last = db.count_packages()
                db.close()
                if last >= minimum:
                    return last
            time.sleep(0.3)
        return last

    def test_kill_and_resume(self):
        # 第一次启动，爬到一部分后强杀
        eng1 = self._spawn_engine(self.workdir)
        got = self._wait_packages(6)
        self.assertGreaterEqual(got, 6)
        time.sleep(0.4)
        self._kill(eng1)

        # 被杀瞬间库必须完好且无重复
        db = Database(self.db_path)
        self.assertEqual(db.conn.execute(
            "SELECT COUNT(*) n FROM (SELECT name, ecosystem FROM packages "
            "GROUP BY name, ecosystem HAVING COUNT(*)>1)").fetchone()["n"], 0)
        self.assertEqual(db.conn.execute(
            "SELECT COUNT(*) n FROM (SELECT key, version FROM package_versions "
            "GROUP BY key, version HAVING COUNT(*)>1)").fetchone()["n"], 0)
        db.close()

        # 重启续跑至全量完成
        eng2 = self._spawn_engine(self.workdir)
        deadline = time.time() + 120
        done = False
        while time.time() < deadline:
            db = Database(self.db_path)
            rows = db.conn.execute("SELECT done FROM crawl_cursors").fetchall()
            done = len(rows) == 3 and all(r["done"] for r in rows)
            db.close()
            if done:
                break
            time.sleep(0.5)
        self.assertTrue(done, "重启后未在时限内完成全量爬取")
        self._kill(eng2)

        # 数量与真值一致、无重复、对账零差异
        db = Database(self.db_path)
        for eco in ("npm", "pypi", "maven"):
            self.assertEqual(db.count_packages(eco), self.truth_pkgs[eco])
            self.assertEqual(db.count_versions(eco), self.truth_vers[eco])
        report = Reconciler(db, self.cluster.base_urls()).run()
        db.close()
        self.assertTrue(report["ok"], f"强杀恢复后存在差异: {report}")


if __name__ == "__main__":
    unittest.main()
