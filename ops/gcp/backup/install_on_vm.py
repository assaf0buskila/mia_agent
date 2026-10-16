"""Install the off-host backup on the Mia VM (V2-10).

OWNER-GATED: run as root, by hand, after Assaf's go.

  sudo python3 install_on_vm.py --bucket mia-assafweb-mia-backups --project mia-assafweb
  sudo python3 install_on_vm.py --bucket ... --project ... --enable-timer

It copies the backup scripts to /opt/mia/backup, the systemd units to /etc/systemd/system and the
settings to /etc/mia/backup-offsite.env, creates the private work directory and runs
`systemctl daemon-reload`. It does not enable the timer unless --enable-timer is given, never starts
the backup service itself, and never touches `mia`, compose.yaml, the app, the database or the jobs.
It does not install the restore drill: that runs on Assaf's PC, never on the VM.

Safe to repeat. It writes nothing unless every precondition holds. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import backup_lib

SOURCE = Path(__file__).resolve().parent
SCRIPTS = ("backup_lib.py", "backup_offsite.py", "ledger_posture.py")
UNITS = ("mia-backup-offsite.service", "mia-backup-offsite.timer")
TOOLS = ("python3", "docker", "gcloud", "openssl")


def write_file(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def make_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def default_is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def main(
    argv=None, *, runner=None, root: Path = Path("/"), which=shutil.which, is_root=default_is_root
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", required=True, help="the private backup bucket")
    parser.add_argument("--project", default="", help="Google Cloud project id (optional)")
    parser.add_argument(
        "--enable-timer", action="store_true", help="also enable and start the 15 minute timer"
    )
    args = parser.parse_args(argv)
    runner = runner if runner is not None else backup_lib.Runner()

    if not backup_lib.BUCKET_NAME.fullmatch(args.bucket):
        print(
            "install: --bucket must be a plain lowercase bucket name, without gs://",
            file=sys.stderr,
        )
        return 2
    if args.project and not backup_lib.PROJECT_ID.fullmatch(args.project):
        print("install: --project is not a project id", file=sys.stderr)
        return 2
    if not is_root():
        print("install: run as root (sudo)", file=sys.stderr)
        return 1
    missing = [tool for tool in TOOLS if not which(tool)]
    if missing:
        print(f"install: missing on this VM: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not (root / "opt/mia/compose.yaml").is_file():
        print("install: Mia is not deployed here yet (no /opt/mia/compose.yaml)", file=sys.stderr)
        return 1
    try:
        payload = {name: (SOURCE / name).read_bytes() for name in (*SCRIPTS, *UNITS)}
    except OSError as error:
        print(f"install: cannot read {Path(error.filename or '?').name}", file=sys.stderr)
        return 1

    settings = [f"MIA_BACKUP_BUCKET={args.bucket}"]
    if args.project:
        settings.append(f"MIA_BACKUP_PROJECT={args.project}")

    make_dir(root / "opt/mia/backup", 0o755)
    for name in SCRIPTS:
        write_file(root / "opt/mia/backup" / name, payload[name], 0o644)
    for name in UNITS:
        write_file(root / "etc/systemd/system" / name, payload[name], 0o644)
    make_dir(root / "etc/mia", 0o700)
    write_file(
        root / "etc/mia/backup-offsite.env", ("\n".join(settings) + "\n").encode("utf-8"), 0o600
    )
    make_dir(root / "var/lib/mia/backup-offsite", 0o700)
    make_dir(root / "var/lib/mia/backup-offsite/gcloud", 0o700)

    done = runner.run(["systemctl", "daemon-reload"], timeout=60)
    if done.returncode != 0:
        print(
            f"install: systemctl daemon-reload failed: {backup_lib.tail(done.stderr)}",
            file=sys.stderr,
        )
        return 1
    if args.enable_timer:
        done = runner.run(["systemctl", "enable", "--now", "mia-backup-offsite.timer"], timeout=60)
        if done.returncode != 0:
            print(
                f"install: enabling the timer failed: {backup_lib.tail(done.stderr)}",
                file=sys.stderr,
            )
            return 1
        print("timer enabled: the first backup runs within 15 minutes")
    else:
        print(
            "installed; the timer is NOT enabled. Do one supervised run first:\n"
            "  sudo systemctl start mia-backup-offsite.service && journalctl -u "
            "mia-backup-offsite.service -n 20 --no-pager\n"
            "then re-run with --enable-timer"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
