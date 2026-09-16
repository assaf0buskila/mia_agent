"""Lambda: keep mia/prod's MIA_DATABASE_URL in sync with RDS password rotation.

Chunk C9 (auto-sync), the approach chosen after C8's in-app override design was
dropped: `miaTaskExecutionRole`'s inline policy `ReadMiaProdBoxOnly` is scoped
to `secret:mia/prod*` only, so the running container cannot read the
RDS-managed secret directly, and Assaf declined to widen that policy. Instead,
this Lambda is triggered by an EventBridge rule on the RDS-managed secret's
rotation-succeeded event (see `deploy/eventbridge-db-password-rotation.example.json`)
and pushes the new password to where the container can already read it.

Deployment (this Lambda, its IAM role, and the EventBridge rule) is a separate
approval. This module is code only, exercised by `tests/unit/test_lambda_sync_db_password.py`
against fake AWS clients -- it makes no AWS calls of its own at import time.

Why forcing a new ECS deployment (step 3) is not optional: ECS injects a
container secret only at task start. Rewriting mia/prod alone would leave the
already-running task on the password it started with until something else
restarts it -- that is C8's nine-hour outage, just moved one layer down.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import quote

# The RDS-managed secret Secrets Manager rotates automatically. Its `password`
# key always holds the current master password (see HANDOFF.md C8 root cause).
RDS_SECRET_ARN = (
    "arn:aws:secretsmanager:eu-north-1:535252061205:secret:"
    "rds!db-d7c051e7-2f6a-4711-826d-2bf7d243a2f8-gjs5XD"
)
MIA_PROD_SECRET_ID = "mia/prod"
DATABASE_URL_KEY = "MIA_DATABASE_URL"
ECS_CLUSTER = "mia"
ECS_SERVICE = "mia"


class SecretsManagerClient(Protocol):
    def get_secret_value(self, *, SecretId: str) -> dict: ...
    def put_secret_value(self, *, SecretId: str, SecretString: str) -> dict: ...


class EcsClient(Protocol):
    def update_service(
        self, *, cluster: str, service: str, forceNewDeployment: bool
    ) -> dict: ...
    def describe_services(self, *, cluster: str, services: list[str]) -> dict: ...


def _strip_bom(text: str) -> str:
    """A UTF-8 BOM already made this exact secret invalid JSON once (a
    hand-edit during the C8 incident, most likely from a Windows editor).
    Strip one on read so a pre-existing BOM never propagates forward; a BOM
    is never written by this module (see `sync_database_password`)."""
    return text[1:] if text.startswith("﻿") else text


def _dsn_has_credentials_section(url: str) -> bool:
    """True when `url` has a `scheme://user[:pass]@host` authority -- i.e. a
    slot `_with_overridden_dsn_password` can actually inject a password into.
    Mirrors that function's own authority-splitting exactly so the two can
    never disagree about what counts as "has a slot to fill"."""
    marker = "://"
    scheme_end = url.find(marker)
    if scheme_end == -1:
        return False
    rest = url[scheme_end + len(marker) :]
    slash_idx = rest.find("/")
    authority = rest if slash_idx == -1 else rest[:slash_idx]
    return "@" in authority


def _with_overridden_dsn_password(url: str, password: str) -> str:
    """Return `url` with its credentials-section password replaced by `password`.

    Carried over verbatim from the dropped C8 in-app override (chunk C8's
    `app/core/config.py::_with_overridden_dsn_password`, reverted in commit
    `8a953d2`) -- review verified this exact logic against 23 adversarial
    passwords (including `%40`, `%2F`, 512 chars, Hebrew, emoji) with no
    double-encoding and correct last-`@` anchoring. Not rewritten from
    scratch here on purpose.

    Preserves scheme, driver suffix, username, host, port, path and query
    byte-for-byte -- only the password changes, and it is percent-encoded so
    punctuation RDS generates (`#? / @ % :` and spaces) can never corrupt URL
    parsing downstream. The split point when locating the existing userinfo
    is the LAST `@` before the first `/` after the scheme, so a stray,
    unencoded `@` already inside an old password is never mistaken for the
    userinfo/host boundary. Caller (`sync_database_password`) is responsible
    for refusing to call this when `_dsn_has_credentials_section` is False.
    """
    marker = "://"
    scheme_end = url.find(marker)
    scheme = url[:scheme_end]
    rest = url[scheme_end + len(marker) :]

    slash_idx = rest.find("/")
    if slash_idx == -1:
        authority, tail = rest, ""
    else:
        authority, tail = rest[:slash_idx], rest[slash_idx:]

    userinfo, _, hostport = authority.rpartition("@")
    username = userinfo.split(":", 1)[0]
    encoded_password = quote(password, safe="")
    return f"{scheme}{marker}{username}:{encoded_password}@{hostport}{tail}"


def sync_database_password(
    *,
    secretsmanager: SecretsManagerClient,
    ecs: EcsClient,
) -> dict[str, Any]:
    """Core, side-effecting logic. Takes already-constructed clients so tests
    exercise this against fakes/fixtures with zero AWS calls; `lambda_handler`
    below is the only place that constructs real boto3 clients.

    Idempotent: re-running against a secret that already carries the current
    rotated password is a no-op on the secret's value (still forces a
    deployment -- cheap insurance if a prior invocation wrote the secret but
    was killed before that step). Never logs the password, the URL, or any
    fragment of either; the return value is booleans/status only.
    """
    rds_secret = secretsmanager.get_secret_value(SecretId=RDS_SECRET_ARN)
    rds_payload = json.loads(_strip_bom(rds_secret["SecretString"]))
    password = rds_payload["password"]

    mia_secret = secretsmanager.get_secret_value(SecretId=MIA_PROD_SECRET_ID)
    mia_payload = json.loads(_strip_bom(mia_secret["SecretString"]))

    current_url = str(mia_payload.get(DATABASE_URL_KEY, ""))
    if not _dsn_has_credentials_section(current_url):
        raise ValueError(
            f"{MIA_PROD_SECRET_ID}'s {DATABASE_URL_KEY} has no user@host "
            "credentials section to inject the rotated password into; "
            "refusing to write a URL that still could not authenticate."
        )
    new_url = _with_overridden_dsn_password(current_url, password)

    updated_payload = dict(mia_payload)
    updated_payload[DATABASE_URL_KEY] = new_url
    new_secret_string = json.dumps(updated_payload)
    assert not new_secret_string.startswith("﻿")  # never write a BOM

    secretsmanager.put_secret_value(SecretId=MIA_PROD_SECRET_ID, SecretString=new_secret_string)

    # Not optional -- see module docstring. Called every run, including the
    # idempotent no-op case, so a task that never picked up a previous
    # invocation's write still gets one.
    ecs.update_service(cluster=ECS_CLUSTER, service=ECS_SERVICE, forceNewDeployment=True)
    described = ecs.describe_services(cluster=ECS_CLUSTER, services=[ECS_SERVICE])
    services = described.get("services") or [{}]

    return {
        "password_changed": new_url != current_url,
        "deployment_forced": True,
        "service_status": str(services[0].get("status", "")),
    }


def lambda_handler(event: dict, context: object) -> dict:  # pragma: no cover - thin AWS glue
    """Real entry point. Not exercised by tests (would require live AWS
    credentials/network); `sync_database_password` above carries all the
    logic and is what tests call directly against fakes."""
    import boto3

    return sync_database_password(
        secretsmanager=boto3.client("secretsmanager"),
        ecs=boto3.client("ecs"),
    )
