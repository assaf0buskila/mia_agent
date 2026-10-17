"""Create a secret-free Business TEST candidate from the approved staging package."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SHA_RE = re.compile(r"[0-9a-f]{40}")
WORKSPACE = "assafweb-staging"
WORKBOOK_ID = "1T9Is3iJ_RlI8KndpTXYCfuTkVsMbncw3_0bAVze0RE4"
SERVICE_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzQQfv3xuyTTYiXHwgz75xnJ4oVskJTlp6k57mMOuSfFzngGPdPrPSh4N8ktAnmKMIikw/exec"
)
MOBILE = "prototypes/assafweb-os-mobile/"
MOBILE_FILES = (
    "index.html",
    "styles.css",
    "app.mjs",
    "contracts.mjs",
    "views.mjs",
    "client.mjs",
    "bff.mjs",
    "gateway.mjs",
    "server.mjs",
    "fonts/rubik-regular.ttf",
    "fonts/rubik-bold.ttf",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _clean_ci_head(repo: Path, sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise SystemExit("candidate_invalid_source_sha")
    if _git(repo, "rev-parse", "HEAD") != sha:
        raise SystemExit("candidate_source_not_head")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise SystemExit("candidate_source_not_clean")
    if _git(repo, "cat-file", "-t", sha) != "commit":
        raise SystemExit("candidate_source_not_commit")


def _archive_safe(repo: Path, sha: str, paths: list[str], prefix: str = "") -> None:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha, *paths],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as rows:
        for member in rows.getmembers():
            name = member.name
            relative_name = name[len(prefix):] if name.startswith(prefix) else name
            relative = PurePosixPath(relative_name)
            if (
                not member.isfile() and not member.isdir()
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in relative_name
                or ":" in relative_name
                or relative.name == ".env"
            ):
                raise SystemExit("candidate_source_archive_unsafe")
            if member.isdir():
                # Git includes safe ancestors of the requested frontend paths;
                # the reused extractor skips directories without writing them.
                if prefix and not (
                    name.startswith(prefix) or prefix.startswith(name.rstrip("/") + "/")
                ):
                    raise SystemExit("candidate_source_archive_unsafe")
                continue
            if not name.startswith(prefix):
                raise SystemExit("candidate_source_archive_unsafe")


def _service_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "script.google.com"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]{20,256}/exec", parsed.path)
    ):
        raise SystemExit("candidate_invalid_test_service_url")
    if value != SERVICE_URL:
        raise SystemExit("candidate_test_service_not_approved")
    return value


def _load_original(base: Path):
    path = base / "prepare.py"
    spec = importlib.util.spec_from_file_location("approved_owner_os_prepare", path)
    if spec is None or spec.loader is None:
        raise SystemExit("candidate_base_package_invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "extract", None)):
        raise SystemExit("candidate_base_package_invalid")
    return module


def _extend_bootstrap(path: Path, previous_sha: str, candidate_sha: str) -> None:
    source = path.read_text(encoding="utf-8")
    old_guard = """    if rows != {"database": DATABASE_NAME, "backend_sha": BACKEND_SHA}:
        raise StageConfigurationError("stage_bootstrap_marker_mismatch")
"""
    new_guard = f'''    if (
        set(rows) != {{"database", "backend_sha"}}
        or
        rows.get("database") != DATABASE_NAME
        or rows.get("backend_sha") not in {{"{previous_sha}", BACKEND_SHA}}
    ):
        raise StageConfigurationError("stage_bootstrap_marker_mismatch")
'''
    old_finish = """        _canonical_schema(engine)
        _harden_and_grant(engine)
        _seed(engine, config.owner_ids)
"""
    old_validation = """    _verify_runtime_roles(config)
"""
    new_validation = f'''    _verify_runtime_roles(config)
    marker_engine = create_engine(_admin_url(config))
    try:
        with marker_engine.begin() as connection:
            updated = connection.execute(
                text("UPDATE owner_os_staging_marker SET marker_value=:candidate "
                     "WHERE marker_key='backend_sha' AND marker_value IN (:previous,:candidate)"),
                {{"candidate": BACKEND_SHA, "previous": "{previous_sha}"}},
            )
            if updated.rowcount != 1:
                raise StageConfigurationError("stage_bootstrap_marker_mismatch")
    finally:
        marker_engine.dispose()
'''
    if (
        old_guard not in source
        or old_finish not in source
        or source.count(old_validation) != 1
        or previous_sha == candidate_sha
    ):
        raise SystemExit("candidate_bootstrap_adapter_invalid")
    path.write_text(
        source.replace(old_guard, new_guard).replace(old_validation, new_validation),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-package", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--backend-sha", required=True)
    parser.add_argument("--frontend-sha", required=True)
    parser.add_argument("--test-service-url", default=SERVICE_URL)
    parser.add_argument("--test-workbook-id", default=WORKBOOK_ID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    base, output = args.approved_package.resolve(), args.output.resolve()
    backend = args.backend.resolve()
    if not output.is_relative_to(backend):
        raise SystemExit("candidate_output_outside_workspace")
    if output.exists():
        raise SystemExit("candidate_output_must_be_new")
    if not all(
        (base / name).is_file()
        for name in (
            "prepare.py",
            "sources.lock.json",
            "images.lock.json",
            "verify_bundle.py",
            "verify_images.py",
            "runtime/settings.py",
            "runtime/api.py",
        )
    ):
        raise SystemExit("candidate_base_package_invalid")
    _clean_ci_head(backend, args.backend_sha)
    _clean_ci_head(args.frontend.resolve(), args.frontend_sha)
    service_url = _service_url(args.test_service_url)
    if args.test_workbook_id != WORKBOOK_ID:
        raise SystemExit("candidate_test_workbook_not_approved")
    original = _load_original(base)
    base_lock = json.loads((base / "sources.lock.json").read_text(encoding="utf-8"))
    old_backend_sha = base_lock["backend"]["sha"]
    migration_delta = subprocess.run(
        ["git", "-C", str(backend), "diff", "--quiet", old_backend_sha, args.backend_sha,
         "--", "migrations"],
        capture_output=True,
    )
    if migration_delta.returncode != 0:
        raise SystemExit("candidate_migration_delta_denied")
    _archive_safe(backend, args.backend_sha, ["app", "migrations", "pyproject.toml", "uv.lock"])
    _archive_safe(
        args.frontend.resolve(), args.frontend_sha,
        [MOBILE + name for name in MOBILE_FILES], MOBILE,
    )
    output.mkdir(parents=True)
    original.extract(
        args.backend.resolve(),
        args.backend_sha,
        ["app", "migrations", "pyproject.toml", "uv.lock"],
        output / "build/api",
    )
    original.extract(
        args.frontend.resolve(),
        args.frontend_sha,
        [MOBILE + name for name in MOBILE_FILES],
        output / "build/mobile",
        MOBILE,
    )

    for name in (
        "generate_secrets.py",
        "smoke.py",
        "verify_bundle.py",
        "verify_images.py",
        "iphone-checklist.md",
        "gcp-costs.json",
        ".gitignore",
    ):
        shutil.copy2(base / name, output / name)
    shutil.copytree(
        base / "runtime",
        output / "build/api/staging_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(
        output / "build/api/staging_runtime/settings.py",
        output / "build/api/staging_runtime/base_settings.py",
    )
    shutil.copy2(
        output / "build/api/staging_runtime/api.py",
        output / "build/api/staging_runtime/base_api.py",
    )
    base_api = output / "build/api/staging_runtime/base_api.py"
    base_api_source = base_api.read_text(encoding="utf-8")
    base_api_tail = "\n\nstage_config = load_stage_config()\napp = create_app(stage_config)\n"
    if base_api_tail not in base_api_source:
        raise SystemExit("candidate_base_api_adapter_invalid")
    base_api.write_text(base_api_source.replace(base_api_tail, "\n"), encoding="utf-8")
    base_settings = output / "build/api/staging_runtime/base_settings.py"
    base_settings.write_text(
        base_settings.read_text(encoding="utf-8").replace(old_backend_sha, args.backend_sha),
        encoding="utf-8",
    )
    _extend_bootstrap(
        output / "build/api/staging_runtime/bootstrap.py", old_backend_sha, args.backend_sha
    )
    for name in ("settings.py", "api.py", "egress_proxy.py"):
        shutil.copy2(ROOT / "runtime" / name, output / "build/api/staging_runtime" / name)
    shutil.copy2(ROOT / "launch.mjs", output / "build/mobile/launch.mjs")
    for name in ("compose.yaml", "Caddyfile", "staging.vars.example"):
        shutil.copy2(ROOT / name, output / name)
    shutil.copy2(ROOT / "README.md", output / "BUSINESS_STAGING_README.md")

    targets = {
        "mode": "TEST",
        "workspace_id": WORKSPACE,
        "workbook_id": args.test_workbook_id,
        "service_url": service_url,
    }
    (output / "build/api/staging_runtime/business_targets.json").write_text(
        json.dumps(targets, indent=2) + "\n", encoding="utf-8"
    )
    (output / "business-targets.lock.json").write_text(
        json.dumps(targets, indent=2) + "\n", encoding="utf-8"
    )
    sources = {"backend": {"sha": args.backend_sha}, "frontend": {"sha": args.frontend_sha}}
    (output / "sources.lock.json").write_text(
        json.dumps(sources, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copy2(base / "images.lock.json", output / "approved-images.lock.json")
    python_image = base_lock["images"]["python"]
    uv_image = base_lock["images"]["uv"]
    node_image = base_lock["images"]["node"]
    api_command = json.dumps(
        [
            "python",
            "-m",
            "uvicorn",
            "staging_runtime.api:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--proxy-headers",
            "--forwarded-allow-ips",
            "172.29.21.2",
            "--no-access-log",
            "--log-level",
            "critical",
        ]
    )
    (output / "Dockerfile.api").write_text(
        f"""FROM {uv_image} AS uv
FROM {python_image}
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
WORKDIR /opt/mia
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache \\
    && useradd --uid 10001 --no-create-home stage
COPY app ./app
COPY migrations ./migrations
COPY staging_runtime ./staging_runtime
ENV PATH=\"/opt/mia/.venv/bin:$PATH\" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV OS_STAGE_BACKEND_SHA={args.backend_sha}
LABEL org.opencontainers.image.revision={args.backend_sha}
USER 10001:10001
CMD {api_command}
""",
        encoding="utf-8",
    )
    (output / "Dockerfile.bff").write_text(
        f"""FROM {node_image}
WORKDIR /opt/mobile
COPY . ./
ENV NODE_ENV=production
LABEL org.opencontainers.image.revision={args.frontend_sha}
USER node
CMD [\"node\", \"launch.mjs\"]
""",
        encoding="utf-8",
    )

    hashes = {
        file.relative_to(output).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(output.rglob("*"))
        if file.is_file()
    }
    (output / "candidate-source.lock.json").write_text(
        json.dumps({"sha256": hashes}, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"candidate_source": str(output), "source_files": len(hashes)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
