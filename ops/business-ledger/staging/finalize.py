"""Lock built images and emit an immutable candidate without building or deploying."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9_.-]*")


def _image(tag: str, digest: str, config: str) -> None:
    if (
        not IMAGE_RE.fullmatch(tag)
        or not DIGEST_RE.fullmatch(digest)
        or not DIGEST_RE.fullmatch(config)
    ):
        raise SystemExit("candidate_invalid_image_lock")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--api-image-digest", required=True)
    parser.add_argument("--api-config-id", required=True)
    parser.add_argument("--bff-image", required=True)
    parser.add_argument("--bff-image-digest", required=True)
    parser.add_argument("--bff-config-id", required=True)
    args = parser.parse_args(argv)
    root = args.candidate.resolve()
    bundle = root.with_suffix(".zip")
    if bundle.exists():
        raise SystemExit("candidate_bundle_already_exists")
    _image(args.api_image, args.api_image_digest, args.api_config_id)
    _image(args.bff_image, args.bff_image_digest, args.bff_config_id)

    source_lock_path = root / "candidate-source.lock.json"
    source_lock = json.loads(source_lock_path.read_text(encoding="utf-8"))["sha256"]
    actual = {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(root.rglob("*"))
        if file.is_file() and file != source_lock_path
    }
    if actual != source_lock:
        raise SystemExit("candidate_source_integrity_failed")

    compose_path = root / "compose.yaml"
    compose = compose_path.read_text(encoding="utf-8")
    if compose.count("__API_IMAGE__") != 3 or compose.count("__BFF_IMAGE__") != 1:
        raise SystemExit("candidate_compose_placeholder_invalid")
    compose_path.write_text(
        compose.replace("__API_IMAGE__", args.api_image).replace("__BFF_IMAGE__", args.bff_image),
        encoding="utf-8",
    )

    approved = json.loads((root / "approved-images.lock.json").read_text(encoding="utf-8"))
    vendor = {
        name: digest
        for name, digest in approved["images"].items()
        if name.startswith("pgvector/") or name.startswith("caddy:")
    }
    image_lock = {
        "platform": "linux/amd64",
        "images": {
            args.api_image: args.api_image_digest,
            args.bff_image: args.bff_image_digest,
            **vendor,
        },
        "exported_config_ids": {
            args.api_image: args.api_config_id,
            args.bff_image: args.bff_config_id,
        },
    }
    (root / "images.lock.json").write_text(
        json.dumps(image_lock, indent=2) + "\n", encoding="utf-8"
    )
    sources = json.loads((root / "sources.lock.json").read_text(encoding="utf-8"))
    targets = json.loads((root / "business-targets.lock.json").read_text(encoding="utf-8"))
    hashes = {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(root.rglob("*"))
        if file.is_file() and file.name != "manifest.json"
    }
    (root / "manifest.json").write_text(
        json.dumps({"sources": sources, "business_targets": targets, "sha256": hashes}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(root))
    print(
        json.dumps(
            {"candidate": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
