"""Test driver: run the REAL ops/gcp/vm/app_role_demote.py main() against the disposable server.

`mia restore` calls `python3 .../app_role_demote.py --reapply`. The helper insists on euid 0 and
reaches PostgreSQL with `docker compose -p mia ... exec -T postgres psql ...`. The drill has
neither a root user (Windows has no geteuid) nor the compose project, so this driver stands in for
exactly those two things and then executes the helper file unchanged, through runpy:

  usage: restore_demote_driver.py <path to app_role_demote.py> [helper args ...]
  env:   V210_PG_CONTAINER   the disposable container; `docker compose ... exec -T postgres X`
                             becomes `docker exec -i <container> X`.
"""

import os
import runpy
import subprocess
import sys

script, *arguments = sys.argv[1:]
container = os.environ["V210_PG_CONTAINER"]
os.geteuid = lambda: 0  # the VM runs `mia restore` as root
real_run = subprocess.run


def reroute(command, *args, **kwargs):
    if list(command[:2]) == ["docker", "compose"]:
        tail = list(command)
        index = tail.index("exec")
        assert tail[index + 1 : index + 3] == ["-T", "postgres"], tail
        command = ["docker", "exec", "-i", container, *tail[index + 3 :]]
    return real_run(command, *args, **kwargs)


subprocess.run = reroute
sys.argv = [script, *arguments]
runpy.run_path(script, run_name="__main__")
