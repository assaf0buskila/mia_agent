from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import ipaddress
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_prepare_requires_exact_committed_inputs() -> None:
    module = load("business_stage_prepare", ROOT / "prepare.py")
    assert module.SHA_RE.fullmatch("a" * 40)
    assert not module.SHA_RE.fullmatch("a" * 39)
    assert module._service_url(module.SERVICE_URL) == module.SERVICE_URL
    assert module.WORKSPACE == "assafweb-staging"
    assert module.WORKBOOK_ID == "1T9Is3iJ_RlI8KndpTXYCfuTkVsMbncw3_0bAVze0RE4"
    with pytest.raises(SystemExit, match="candidate_test_service_not_approved"):
        module._service_url("https://script.google.com/macros/s/" + "x" * 24 + "/exec")
    with pytest.raises(SystemExit, match="candidate_invalid_test_service_url"):
        module._service_url("https://example.com/macros/s/" + "x" * 24 + "/exec")
    finalizer = load("business_stage_finalize", ROOT / "finalize.py")
    finalizer._image("assafweb-os-stage-api:abc123", DIGEST_A, DIGEST_B)
    with pytest.raises(SystemExit, match="candidate_invalid_image_lock"):
        finalizer._image("latest", DIGEST_A, DIGEST_B)


def _source_candidate(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "compose.yaml").write_text(
        "\n".join(["__API_IMAGE__"] * 3 + ["__BFF_IMAGE__"]), encoding="utf-8"
    )
    (root / "approved-images.lock.json").write_text(
        json.dumps({"images": {"caddy:fixed": DIGEST_A, "pgvector/fixed:tag": DIGEST_B}}),
        encoding="utf-8",
    )
    (root / "sources.lock.json").write_text(
        json.dumps({"backend": {"sha": "a" * 40}, "frontend": {"sha": "b" * 40}}),
        encoding="utf-8",
    )
    (root / "business-targets.lock.json").write_text(
        json.dumps({"mode": "TEST", "workspace_id": "assafweb-staging"}), encoding="utf-8"
    )
    hashes = {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in root.iterdir()
    }
    (root / "candidate-source.lock.json").write_text(
        json.dumps({"sha256": hashes}), encoding="utf-8"
    )
    return root


def test_finalize_rechecks_source_and_emits_locked_bundle(tmp_path: Path) -> None:
    finalizer = load("business_stage_finalize_success", ROOT / "finalize.py")
    root = _source_candidate(tmp_path)
    args = [
        "--candidate",
        str(root),
        "--api-image",
        "stage-api:abcdef",
        "--api-image-digest",
        DIGEST_A,
        "--api-config-id",
        DIGEST_B,
        "--bff-image",
        "stage-bff:abcdef",
        "--bff-image-digest",
        DIGEST_B,
        "--bff-config-id",
        DIGEST_A,
    ]
    assert finalizer.main(args) == 0
    assert root.with_suffix(".zip").is_file()
    assert "__API_IMAGE__" not in (root / "compose.yaml").read_text(encoding="utf-8")
    assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["sources"]


def test_finalize_rejects_source_drift(tmp_path: Path) -> None:
    finalizer = load("business_stage_finalize_drift", ROOT / "finalize.py")
    root = _source_candidate(tmp_path)
    (root / "compose.yaml").write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="candidate_source_integrity_failed"):
        finalizer.main(
            [
                "--candidate",
                str(root),
                "--api-image",
                "stage-api:abcdef",
                "--api-image-digest",
                DIGEST_A,
                "--api-config-id",
                DIGEST_B,
                "--bff-image",
                "stage-bff:abcdef",
                "--bff-image-digest",
                DIGEST_B,
                "--bff-config-id",
                DIGEST_A,
            ]
        )


def test_proxy_allows_only_official_hosts_and_public_dns() -> None:
    module = load("business_stage_proxy", ROOT / "runtime" / "egress_proxy.py")
    public = [(None, None, None, None, ("142.250.75.110", 443))]
    with patch("socket.getaddrinfo", return_value=public):
        assert asyncio.run(module.public_addresses("script.google.com")) == ("142.250.75.110",)
    with pytest.raises(ValueError, match="proxy_host_denied"):
        asyncio.run(module.public_addresses("metadata.google.internal"))
    private = [(None, None, None, None, ("169.254.169.254", 443))]
    with (
        patch("socket.getaddrinfo", return_value=private),
        pytest.raises(ValueError, match="proxy_address_denied"),
    ):
        asyncio.run(module.public_addresses("script.googleusercontent.com"))
    assert not ipaddress.ip_address("169.254.169.254").is_global


def test_async_dns_keeps_heartbeat_running_and_never_dns_is_bounded(monkeypatch):
    proxy = load("business_proxy_async_dns", ROOT / "runtime/egress_proxy.py")

    async def probe():
        loop = asyncio.get_running_loop()
        events = []

        async def slow_dns(*_args, **_kwargs):
            await asyncio.sleep(0.08)
            events.append("dns")
            return [(None, None, None, None, ("142.250.75.110", 443))]

        async def heartbeat():
            await asyncio.sleep(0.01)
            events.append("heartbeat")

        monkeypatch.setattr(loop, "getaddrinfo", slow_dns)
        await asyncio.gather(proxy.public_addresses("script.google.com"), heartbeat())
        assert events == ["heartbeat", "dns"]

        async def never_dns(*_args, **_kwargs):
            await asyncio.sleep(10)

        monkeypatch.setattr(loop, "getaddrinfo", never_dns)
        monkeypatch.setattr(proxy, "CONNECT_TIMEOUT", 0.02)
        with pytest.raises(TimeoutError):
            await proxy.public_addresses("script.google.com")

    asyncio.run(probe())


def test_global_proxy_deadline_bounds_all_slow_connects(monkeypatch, capsys):
    proxy = load("business_proxy_deadline", ROOT / "runtime/egress_proxy.py")

    class Writer:
        closed = False

        def write(self, _data):
            pass

        async def drain(self):
            pass

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

    async def probe():
        attempts = []

        async def dns(_host):
            await asyncio.sleep(0.015)
            return tuple(f"142.250.75.{number}" for number in range(1, 21))

        async def slow_connect(address, _port):
            attempts.append(address)
            await asyncio.sleep(0.04)
            raise OSError("synthetic-private-connect-detail")

        monkeypatch.setattr(proxy, "public_addresses", dns)
        monkeypatch.setattr(proxy.asyncio, "open_connection", slow_connect)
        monkeypatch.setattr(proxy, "TOTAL_TIMEOUT", 0.09)
        reader = asyncio.StreamReader()
        reader.feed_data(b"CONNECT script.google.com:443 HTTP/1.1\r\n\r\n")
        writer = Writer()
        await proxy.handle(reader, writer)
        assert writer.closed and 1 <= len(attempts) < 20

    asyncio.run(probe())
    output = capsys.readouterr().err
    assert "proxy_deadline" in output
    assert "synthetic-private-connect-detail" not in output


def test_existing_bundle_rejected_before_any_finalizer_mutation(tmp_path):
    finalizer = load("business_finalize_existing", ROOT / "finalize.py")
    root = _source_candidate(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    root.with_suffix(".zip").write_bytes(b"existing")
    with pytest.raises(SystemExit, match="candidate_bundle_already_exists"):
        finalizer.main([
            "--candidate", str(root), "--api-image", "stage-api:abcdef",
            "--api-image-digest", DIGEST_A, "--api-config-id", DIGEST_B,
            "--bff-image", "stage-bff:abcdef", "--bff-image-digest", DIGEST_B,
            "--bff-config-id", DIGEST_A,
        ])
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize("unsafe", [False, True])
def test_frontend_archive_ancestors_and_unsafe_directories(monkeypatch, tmp_path, unsafe):
    prepare = load("business_archive_ancestors", ROOT / "prepare.py")
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name in [
            "prototypes/", "prototypes/assafweb-os-mobile/", "prototypes/assafweb-os-mobile/fonts/"
        ]:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        if unsafe:
            member = tarfile.TarInfo("prototypes/../../escape/")
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        member = tarfile.TarInfo("prototypes/assafweb-os-mobile/index.html")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"ok"))
    monkeypatch.setattr(
        prepare.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=data.getvalue())
    )
    if unsafe:
        with pytest.raises(SystemExit, match="candidate_source_archive_unsafe"):
            prepare._archive_safe(tmp_path, "a" * 40, ["index.html"], prepare.MOBILE)
    else:
        prepare._archive_safe(tmp_path, "a" * 40, ["index.html"], prepare.MOBILE)


def test_static_contract_keeps_bff_db_isolated_and_secrets_server_side() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    launch = (ROOT / "launch.mjs").read_text(encoding="utf-8")
    settings = (ROOT / "runtime" / "settings.py").read_text(encoding="utf-8")
    assert "business_ledger_hmac_secret: {file:" in compose
    assert "business_ledger_hmac_secret" not in launch
    assert "networks: [frontend]" in compose
    assert "networks: [database]" in compose
    assert "ledger_proxy: {ipv4_address: 172.29.22.2}" in compose
    assert "script.google.com" not in caddy
    assert '== "true"' in settings
    assert 'BUSINESS_SCOPES = "business-ledger:read,business-ledger:write"' in settings
    assert '"owner_os_actions_enabled": False' in settings
    assert '"reminder_delivery_enabled": False' in settings
    proxy = (ROOT / "runtime" / "egress_proxy.py").read_text(encoding="utf-8")
    assert "print(code" in proxy
    assert "print(host" not in proxy
    assert "print(header" not in proxy
    for path in ("/v1/owner/os/business", "/v1/owner/os/business/receipts/*"):
        assert path in caddy
