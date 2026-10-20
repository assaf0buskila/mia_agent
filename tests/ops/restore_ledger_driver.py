"""Run the DEPLOYED ops/assaf-os/provision_ledger.py inside the restore drill. TEST SUPPORT ONLY.

    python restore_ledger_driver.py <path to provision_ledger.py> <the script's own arguments>

The script pins the production VM: PostgreSQL on 127.0.0.1:5432 and the VM-owned
/etc/mia/runtime.env. The drill's disposable server listens on 127.0.0.1:56218 and its runtime.env
is a scratch file. This driver loads the real script, refuses to run when its `mia-vm` pin is no
longer the one `mia restore` relies on, swaps only the port and the env file path, and calls the
script's own main(). Every statement, validator and refusal after that is the script's own.

Environment: V210_LEDGER_PORT, V210_RUNTIME_ENV, and optionally
  V210_ETC_MIA_READONLY=1  the compose shim sets this from `-v /etc/mia:/etc/mia:ro`. Any write the
                           script then tries into the env file's directory fails with EROFS, as a
                           read-only bind mount does, and is recorded in V210_WRITE_LOG.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# host, port, database, migration role, main role, runtime env file: what `mia restore` assumes.
PRODUCTION_PIN = ("127.0.0.1", 5432, "mia", "mia", "mia_app", "/etc/mia/runtime.env")


def read_only(directory: Path) -> None:
    """Make `directory` behave like a read-only mount for the two calls the script writes with."""
    log = Path(os.environ["V210_WRITE_LOG"])
    where = directory.resolve()
    real_mkstemp, real_replace = tempfile.mkstemp, os.replace

    def refuse(path) -> None:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"write attempted under {directory.name}\n")
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    def mkstemp(*args, **kwargs):
        if kwargs.get("dir") and Path(kwargs["dir"]).resolve() == where:
            refuse(kwargs["dir"])
        return real_mkstemp(*args, **kwargs)

    def replace(source, target, *args, **kwargs):
        if Path(target).resolve().parent == where:
            refuse(target)
        return real_replace(source, target, *args, **kwargs)

    tempfile.mkstemp = mkstemp
    os.replace = replace


def main() -> None:
    script = Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location("provision_ledger", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pin = module._TARGETS["mia-vm"]
    if tuple(pin) != PRODUCTION_PIN:
        raise SystemExit("restore_ledger_driver: the mia-vm pin is not the one mia restore uses")
    env_file = os.environ["V210_RUNTIME_ENV"]
    module._TARGETS["mia-vm"] = pin._replace(
        port=int(os.environ["V210_LEDGER_PORT"]), runtime_env=env_file
    )
    if os.environ.get("V210_ETC_MIA_READONLY") == "1":
        read_only(Path(env_file).parent)
    module.main(sys.argv[2:])


if __name__ == "__main__":
    main()
