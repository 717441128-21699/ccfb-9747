"""根入口：把仓库根目录加入 sys.path，使 `py run.py <command>` 在任何 CWD 下可用。

用法见 README，常用：
  py run.py sim --populate          启动三个仿真仓库并生成数据
  py run.py engine                  启动采集引擎（全量/变更流/校验）
  py run.py web                     启动 Web 控制台
  py run.py reconcile               与仿真仓库真值对账
  py run.py demo                    一条命令拉起整套演示
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sbom_monitor.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
