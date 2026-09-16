"""Chunk C9 (auto-sync): tests for scripts/lambda_sync_db_password.py.

No AWS calls anywhere here -- `FakeSecretsManager`/`FakeEcs` are plain,
stateful in-memory doubles that satisfy the module's own `Protocol`s.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine.url import make_url

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "lambda_sync_db_password", ROOT / "scripts" / "lambda_sync_db_password.py"
)
assert SPEC and SPEC.loader
sync_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_mod)

RDS_ARN = sync_mod.RDS_SECRET_ARN
MIA_PROD = sync_mod.MIA_PROD_SECRET_ID


class FakeSecretsManager:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)
        self.put_calls: list[tuple[str, str]] = []

    def get_secret_value(self, *, SecretId: str) -> dict:
        return {"SecretString": self._secrets[SecretId]}

    def put_secret_value(self, *, SecretId: str, SecretString: str) -> dict:
        self.put_calls.append((SecretId, SecretString))
        self._secrets[SecretId] = SecretString
        return {}


class FakeEcs:
    def __init__(self) -> None:
        self.update_service_calls: list[dict[str, Any]] = []

    def update_service(self, *, cluster: str, service: str, forceNewDeployment: bool) -> dict:
        self.update_service_calls.append(
            {"cluster": cluster, "service": service, "forceNewDeployment": forceNewDeployment}
        )
        return {}

    def describe_services(self, *, cluster: str, services: list[str]) -> dict:
        return {"services": [{"status": "ACTIVE"}]}


def _mia_prod_payload(url: str) -> dict:
    """Other keys deliberately named after real ones in
    deploy/mia-prod.secret.example.json -- the point is that whichever other
    keys are present, they survive untouched."""
    return {
        "MIA_DATABASE_URL": url,
        "MIA_OPENAI_API_KEY": "sk-existing-unrelated",
        "MIA_TELEGRAM_BOT_TOKEN": "tg-existing-unrelated",
        "MIA_GEMINI_API_KEY": "",
    }


def _secretsmanager(
    *, rds_password: str, mia_prod_url: str, bom: bool = False
) -> FakeSecretsManager:
    mia_prod_json = json.dumps(_mia_prod_payload(mia_prod_url))
    return FakeSecretsManager(
        {
            RDS_ARN: json.dumps({"password": rds_password}),
            MIA_PROD: ("﻿" + mia_prod_json) if bom else mia_prod_json,
        }
    )


@pytest.mark.parametrize(
    "password",
    [
        "plain",
        "has#hash",
        "has?question",
        "has/slash",
        "has@at",
        "has%percent",
        "has:colon",
        "has space",
        "mix#?/@%: space",
    ],
)
def test_sync_substitutes_password_and_round_trips_through_sqlalchemy(password: str) -> None:
    """The real proof of correct encoding: SQLAlchemy's own DSN parser must
    recover the exact original password, not just a string that looks right."""
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia?sslmode=verify-full"
    secretsmanager = _secretsmanager(rds_password=password, mia_prod_url=original_url)
    ecs = FakeEcs()

    result = sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    written = json.loads(secretsmanager._secrets[MIA_PROD])
    parsed = make_url(written["MIA_DATABASE_URL"])
    assert parsed.password == password
    assert parsed.username == "appuser"
    assert parsed.host == "db.example.internal"
    assert parsed.port == 5432
    assert parsed.database == "mia"
    assert parsed.query.get("sslmode") == "verify-full"
    assert result["password_changed"] is True


def test_sync_preserves_every_other_key_byte_for_byte() -> None:
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia"
    payload = _mia_prod_payload(original_url)
    secretsmanager = _secretsmanager(rds_password="rotated", mia_prod_url=original_url)
    ecs = FakeEcs()

    sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    written = json.loads(secretsmanager._secrets[MIA_PROD])
    assert written["MIA_OPENAI_API_KEY"] == payload["MIA_OPENAI_API_KEY"]
    assert written["MIA_TELEGRAM_BOT_TOKEN"] == payload["MIA_TELEGRAM_BOT_TOKEN"]
    assert written["MIA_GEMINI_API_KEY"] == payload["MIA_GEMINI_API_KEY"]
    assert set(written) == set(payload)


def test_sync_never_writes_a_bom() -> None:
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia"
    secretsmanager = _secretsmanager(rds_password="rotated", mia_prod_url=original_url)
    ecs = FakeEcs()

    sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    written_raw = secretsmanager._secrets[MIA_PROD]
    assert not written_raw.startswith("﻿")
    json.loads(written_raw)  # must parse as JSON on its own, no stray prefix


def test_sync_recovers_from_a_bom_on_the_secrets_it_reads() -> None:
    """A UTF-8 BOM already made mia/prod invalid JSON once (a hand-edit
    during the C8 incident, most likely via a Windows editor). A leading BOM
    on read must not crash the handler, and must never be carried forward."""
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia"
    secretsmanager = _secretsmanager(rds_password="rotated", mia_prod_url=original_url, bom=True)
    ecs = FakeEcs()

    sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    written_raw = secretsmanager._secrets[MIA_PROD]
    assert not written_raw.startswith("﻿")
    assert json.loads(written_raw)["MIA_OPENAI_API_KEY"] == "sk-existing-unrelated"


def test_sync_forces_an_ecs_deployment() -> None:
    """Not optional: without this, mia/prod holds the correct password but
    the already-running task keeps the old one until something else restarts
    it -- C8's failure mode, just moved one layer down."""
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia"
    secretsmanager = _secretsmanager(rds_password="rotated", mia_prod_url=original_url)
    ecs = FakeEcs()

    sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    assert ecs.update_service_calls == [
        {"cluster": "mia", "service": "mia", "forceNewDeployment": True}
    ]


def test_sync_is_idempotent_and_never_double_encodes_on_repeated_invocation() -> None:
    original_url = "postgresql://appuser:oldpass@db.example.internal:5432/mia"
    password = "R%o#t@8:9 pw"  # deliberately hits every char class quote() encodes
    secretsmanager = _secretsmanager(rds_password=password, mia_prod_url=original_url)
    ecs = FakeEcs()

    first = sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)
    second = sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    assert first["password_changed"] is True
    assert second["password_changed"] is False  # already applied; no-op on the value
    # Still forces a deployment every run -- cheap insurance, never skipped.
    assert len(ecs.update_service_calls) == 2

    written = json.loads(secretsmanager._secrets[MIA_PROD])
    parsed = make_url(written["MIA_DATABASE_URL"])
    assert parsed.password == password
    # A re-applied, already-encoded '%' would compound into '%2525'.
    assert "%2525" not in written["MIA_DATABASE_URL"]
    assert secretsmanager._secrets[MIA_PROD] == secretsmanager.put_calls[-1][1]


def test_sync_raises_when_mia_prod_url_has_no_credentials_section() -> None:
    """A silent no-op here would leave mia/prod's password stale forever with
    no signal -- fail loudly instead, and touch nothing on the way out."""
    secretsmanager = _secretsmanager(
        rds_password="rotated", mia_prod_url="postgresql://db.example.internal:5432/mia"
    )
    ecs = FakeEcs()

    with pytest.raises(ValueError, match="credentials section"):
        sync_mod.sync_database_password(secretsmanager=secretsmanager, ecs=ecs)

    assert secretsmanager.put_calls == []
    assert ecs.update_service_calls == []


def test_iac_examples_are_least_privilege_and_linked() -> None:
    """Not a claim that the EventBridge pattern is verified against real AWS
    (it isn't -- flagged in the rule's own Description) -- just that the IAM
    shape stays least-privilege and every example file agrees on the same
    names, so they can't silently drift from each other."""
    function = json.loads(
        (ROOT / "deploy/lambda-db-password-sync.example.json").read_text(encoding="utf-8")
    )
    trust = json.loads(
        (ROOT / "deploy/iam-lambda-db-password-sync-trust.example.json").read_text(
            encoding="utf-8"
        )
    )
    policy = json.loads(
        (ROOT / "deploy/iam-lambda-db-password-sync.example.json").read_text(encoding="utf-8")
    )
    rule = json.loads(
        (ROOT / "deploy/eventbridge-db-password-rotation-rule.example.json").read_text(
            encoding="utf-8"
        )
    )
    targets = json.loads(
        (ROOT / "deploy/eventbridge-db-password-rotation-targets.example.json").read_text(
            encoding="utf-8"
        )
    )
    invoke_permission = json.loads(
        (ROOT / "deploy/iam-lambda-invoke-permission.example.json").read_text(encoding="utf-8")
    )

    assert function["Handler"] == "lambda_sync_db_password.lambda_handler"
    assert trust["Statement"][0]["Principal"]["Service"] == "lambda.amazonaws.com"

    for statement in policy["Statement"]:
        resource = statement["Resource"]
        resources = resource if isinstance(resource, list) else [resource]
        assert all(r != "*" for r in resources)
    actions = {
        action
        for statement in policy["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "ecs:UpdateService",
        "ecs:DescribeServices",
    }
    rds_statement = next(s for s in policy["Statement"] if s["Sid"] == "ReadRdsManagedPassword")
    assert rds_statement["Action"] == "secretsmanager:GetSecretValue"
    assert "rds!db-d7c051e7" in rds_statement["Resource"]
    mia_prod_statement = next(
        s for s in policy["Statement"] if s["Sid"] == "ReadAndRewriteMiaProdBoxOnly"
    )
    assert "mia/prod" in mia_prod_statement["Resource"]
    assert "rds!db" not in mia_prod_statement["Resource"]

    assert rule["Name"] == "mia-db-password-rotated"
    assert "rds!db-d7c051e7" in json.dumps(rule["EventPattern"])
    assert targets["Rule"] == rule["Name"]
    assert function["FunctionName"] in targets["Targets"][0]["Arn"]
    assert invoke_permission["FunctionName"] == function["FunctionName"]
    assert invoke_permission["SourceArn"].endswith(f"rule/{rule['Name']}")
    assert invoke_permission["Principal"] == "events.amazonaws.com"
