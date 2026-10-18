"""Create the off-host backup bucket, its encryption-key secret and their permissions (V2-10).

OWNER-GATED. Nothing runs this automatically and the tests only use a fake gcloud. Run it from
Assaf's PC after he says go; `plan` (the default) prints every command and runs none of them.

  python provision_backup.py plan --project mia-assafweb
  python provision_backup.py generate-key --out D:\\offline\\mia-backup.key
  python provision_backup.py apply --project mia-assafweb --key-file D:\\offline\\mia-backup.key

`apply` is idempotent. It only touches Cloud Storage, Secret Manager and the two APIs they need:
  * a private bucket in me-west1 (uniform access, public access prevented), with the lifecycle
    rules from lifecycle.json: 15min/ kept 7 days, daily/ kept 90 days;
  * the VM's service account may add and read objects (objectCreator + objectViewer). It can
    not delete or overwrite one, so a compromised VM can not destroy the history;
  * a regional secret mia-backup-key readable by that service account, holding the key.
An existing bucket is left as it is only after its location, uniform access and public access
prevention are checked against those decisions; a mismatch stops `apply` before it changes anything.
The key goes to Secret Manager through stdin and is never printed. An existing key version is never
replaced: rotating the key would orphan every older backup, and `apply` prints the id of --key-file
for the owner to compare with a backup manifest's key_id. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

import backup_lib
from backup_lib import BackupError

LIFECYCLE_FILE = Path(__file__).resolve().with_name("lifecycle.json")
SERVICE_ACCOUNT = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com"
)
BUCKET_ROLES = ("roles/storage.objectCreator", "roles/storage.objectViewer")
SECRET_ROLE = "roles/secretmanager.secretAccessor"
APIS = ("storage.googleapis.com", "secretmanager.googleapis.com")


class Usage(Exception):
    """Bad input. Reported before any gcloud call."""


def default_bucket(project: str) -> str:
    return f"{project}-mia-backups"


def default_service_account(project: str) -> str:
    return f"mia-vm@{project}.iam.gserviceaccount.com"  # created by ops/gcp/provision.ps1


class Plan:
    """The commands apply would run, in order, with the guards that make each one idempotent."""

    def __init__(self, project: str, bucket: str, service_account: str, key: str | None):
        self.project, self.bucket, self.key = project, bucket, key
        self.member = f"serviceAccount:{service_account}"
        self.flag = f"--project={project}"
        self.url = f"gs://{bucket}"

    def services(self) -> list[str]:
        return ["gcloud", "services", "enable", *APIS, self.flag]

    def bucket_exists(self) -> list[str]:
        return ["gcloud", "storage", "buckets", "describe", self.url, "--format=json", self.flag]

    def bucket_create(self) -> list[str]:
        return [
            "gcloud",
            "storage",
            "buckets",
            "create",
            self.url,
            f"--location={backup_lib.REGION}",
            "--uniform-bucket-level-access",
            "--public-access-prevention",
            self.flag,
        ]

    def lifecycle(self) -> list[str]:
        return [
            "gcloud",
            "storage",
            "buckets",
            "update",
            self.url,
            f"--lifecycle-file={LIFECYCLE_FILE}",
            self.flag,
        ]

    def bucket_binding(self, role: str) -> list[str]:
        return [
            "gcloud",
            "storage",
            "buckets",
            "add-iam-policy-binding",
            self.url,
            f"--member={self.member}",
            f"--role={role}",
            self.flag,
        ]

    def secret_exists(self) -> list[str]:
        return ["gcloud", "secrets", "describe", backup_lib.SECRET_NAME, self.flag]

    def secret_create(self) -> list[str]:
        return [
            "gcloud",
            "secrets",
            "create",
            backup_lib.SECRET_NAME,
            "--replication-policy=user-managed",
            f"--locations={backup_lib.REGION}",
            self.flag,
        ]

    def secret_binding(self) -> list[str]:
        return [
            "gcloud",
            "secrets",
            "add-iam-policy-binding",
            backup_lib.SECRET_NAME,
            f"--member={self.member}",
            f"--role={SECRET_ROLE}",
            self.flag,
        ]

    def versions(self) -> list[str]:
        return [
            "gcloud",
            "secrets",
            "versions",
            "list",
            backup_lib.SECRET_NAME,
            "--limit=1",
            "--format=value(name)",
            self.flag,
        ]

    def key_add(self) -> list[str]:
        return [
            "gcloud",
            "secrets",
            "versions",
            "add",
            backup_lib.SECRET_NAME,
            "--data-file=-",
            self.flag,
        ]

    def printable(self) -> list[str]:
        steps = [
            ("1. enable the two APIs", self.services()),
            ("2. create the private bucket (skipped when it exists)", self.bucket_create()),
            ("3. retention: 15min/ 7 days, daily/ 90 days", self.lifecycle()),
            *[
                (
                    f"4. the VM account may {'add' if 'Creator' in role else 'read'} backups",
                    self.bucket_binding(role),
                )
                for role in BUCKET_ROLES
            ],
            ("5. create the key secret (skipped when it exists)", self.secret_create()),
            ("6. the VM account may read the key", self.secret_binding()),
            (
                "7. store the key from --key-file through stdin (skipped when a version exists)",
                self.key_add(),
            ),
        ]
        return [f"# {title}\n{shlex.join(argv)}" for title, argv in steps]


def parse_inputs(args: argparse.Namespace) -> tuple[str, str, str, str | None]:
    if not args.project or not backup_lib.PROJECT_ID.fullmatch(args.project):
        raise Usage("--project must be a Google Cloud project id")
    bucket = args.bucket or default_bucket(args.project)
    if not backup_lib.BUCKET_NAME.fullmatch(bucket):
        raise Usage("--bucket must be a plain lowercase bucket name, without gs://")
    account = args.service_account or default_service_account(args.project)
    if not SERVICE_ACCOUNT.fullmatch(account):
        raise Usage("--service-account must be a service account email")
    key = None
    if args.key_file is not None:
        try:
            key = backup_lib.validate_key(args.key_file.read_text(encoding="utf-8"))
        except OSError as error:
            raise Usage(f"cannot read the key file {args.key_file.name}") from error
        except BackupError as error:
            raise Usage(f"the key file is not valid: {error.detail}") from error
    return args.project, bucket, account, key


def _first(document: dict, *paths: tuple[str, ...]):
    """The first value found under any of the key paths, or None."""
    for path in paths:
        node = document
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if node is not None:
            return node
    return None


def bucket_posture(stdout: bytes) -> tuple[list[str], list[str]]:
    """What an EXISTING bucket gets wrong against the owner decisions, and what could not be read.

    `apply` leaves an existing bucket as it is, so it must first know the bucket is regional in
    me-west1, uniformly controlled and closed to public access. A fact gcloud's output does not
    state is reported as unverified rather than guessed: the owner then checks it by hand.
    """
    try:
        document = json.loads(stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        document = None
    document = document if isinstance(document, dict) else {}
    problems: list[str] = []
    unverified: list[str] = []

    location = _first(document, ("location",))
    if location is None:
        unverified.append("location")
    elif str(location).lower() != backup_lib.REGION:
        problems.append(f"location is {str(location).lower()}, expected {backup_lib.REGION}")

    uniform = _first(
        document,
        ("uniform_bucket_level_access",),
        ("uniformBucketLevelAccess",),
        ("iamConfiguration", "uniformBucketLevelAccess", "enabled"),
        ("iam_configuration", "uniform_bucket_level_access", "enabled"),
    )
    if uniform is None:
        unverified.append("uniform bucket-level access")
    elif uniform is not True:
        problems.append("uniform bucket-level access is off")

    prevention = _first(
        document,
        ("public_access_prevention",),
        ("publicAccessPrevention",),
        ("iamConfiguration", "publicAccessPrevention"),
        ("iam_configuration", "public_access_prevention"),
    )
    if prevention is None:
        unverified.append("public access prevention")
    elif str(prevention).lower() != "enforced":
        problems.append(f"public access prevention is {str(prevention).lower()}, not enforced")
    return problems, unverified


def print_plan(plan: Plan) -> None:
    print("PLAN ONLY: nothing below has been run. `apply` runs it, in this order.\n")
    print("\n\n".join(plan.printable()))
    print(
        "\nNext: python provision_backup.py apply --project "
        f"{plan.project} --key-file <your offline key file>"
    )


def apply(plan: Plan, runner) -> int:
    def run(argv: list[str], *, input_bytes: bytes | None = None, timeout: float = 180):
        return runner.run(argv, input_bytes=input_bytes, timeout=timeout)

    def fail(step: str, done) -> int:
        print(
            f"provision: {step} failed (gcloud exit {done.returncode}): "
            f"{backup_lib.tail(done.stderr)}",
            file=sys.stderr,
        )
        return 1

    done = run(plan.services())
    if done.returncode != 0:
        return fail("enabling the APIs", done)

    described = run(plan.bucket_exists())
    if described.returncode == 0:
        problems, unverified = bucket_posture(described.stdout)
        if problems:
            print(
                f"provision: {plan.url} exists but breaks the owner decisions: "
                f"{'; '.join(problems)}. Nothing was changed. Fix the bucket, or choose another "
                "with --bucket.",
                file=sys.stderr,
            )
            return 1
        print(f"bucket {plan.url} exists; left as it is")
        stated = {"location", "uniform bucket-level access", "public access prevention"}
        for fact in sorted(stated - set(unverified)):
            print(f"  its {fact} matches the owner decision")
        for fact in unverified:
            print(
                f"  warning: could not read the bucket's {fact} from gcloud's output; "
                "check it in the console before relying on this bucket"
            )
    else:
        done = run(plan.bucket_create())
        if done.returncode != 0:
            return fail("creating the bucket", done)
        print(f"created {plan.url} in {backup_lib.REGION}")
    steps = [
        ("setting the lifecycle rules", plan.lifecycle()),
        *[("granting the bucket permissions", plan.bucket_binding(role)) for role in BUCKET_ROLES],
    ]
    for label, argv in steps:
        done = run(argv)
        if done.returncode != 0:
            return fail(label, done)
    print("lifecycle and bucket permissions are in place")

    if run(plan.secret_exists()).returncode == 0:
        print(f"secret {backup_lib.SECRET_NAME} exists; left as it is")
    else:
        done = run(plan.secret_create())
        if done.returncode != 0:
            return fail("creating the secret", done)
        print(f"created secret {backup_lib.SECRET_NAME} in {backup_lib.REGION}")
    done = run(plan.secret_binding())
    if done.returncode != 0:
        return fail("granting the VM account access to the secret", done)

    existing = run(plan.versions())
    if existing.returncode != 0:
        return fail("listing the secret versions", existing)
    if existing.stdout.strip():
        print("a key version already exists; it is never replaced")
        if plan.key is not None:
            # apply cannot read the stored key. The id is a fingerprint, never the key: compare it
            # with the key_id of a backup manifest to know the offline copy is the stored key.
            print(
                f"the key id of --key-file is {backup_lib.key_id(plan.key)}; it must equal the "
                "key_id in a backup manifest, otherwise the offline copy is not the stored key"
            )
    elif plan.key is None:
        print(
            "no key stored yet. Next:\n"
            "  python provision_backup.py generate-key --out <a path on an offline device>\n"
            f"  python provision_backup.py apply --project {plan.project} --key-file <that file>"
        )
    else:
        done = run(plan.key_add(), input_bytes=(plan.key + "\n").encode("ascii"))
        if done.returncode != 0:
            return fail("storing the key", done)
        print(
            f"stored the key (id {backup_lib.key_id(plan.key)}). Keep the offline copy safe: "
            "without it a lost secret means unreadable backups."
        )
    return 0


def generate_key(out: Path) -> int:
    key = backup_lib.generate_key()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"provision: {out} already exists; it is never overwritten", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"provision: cannot create {out}: {error.strerror}", file=sys.stderr)
        return 2
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(key + "\n")
    print(
        f"wrote a new backup key to {out}\nkey id: {backup_lib.key_id(key)}\n"
        "Move that file to the offline place only Assaf holds, then run apply with --key-file."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "command", nargs="?", default="plan", choices=("plan", "apply", "generate-key")
    )
    parser.add_argument("--project", default="", help="Google Cloud project id")
    parser.add_argument("--bucket", default="", help="default: <project>-mia-backups")
    parser.add_argument(
        "--service-account", default="", help="default: mia-vm@<project>.iam.gserviceaccount.com"
    )
    parser.add_argument("--key-file", type=Path, help="the offline key file to store as the secret")
    parser.add_argument("--out", type=Path, help="where generate-key writes the new key")
    return parser


def main(argv=None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate-key":
        if args.out is None:
            parser.error("generate-key needs --out")
        return generate_key(args.out)
    try:
        project, bucket, account, key = parse_inputs(args)
    except Usage as error:
        print(f"provision: {error}", file=sys.stderr)
        return 2
    plan = Plan(project, bucket, account, key)
    if args.command == "plan":
        print_plan(plan)
        return 0
    return apply(plan, runner if runner is not None else backup_lib.Runner())


if __name__ == "__main__":
    sys.exit(main())
