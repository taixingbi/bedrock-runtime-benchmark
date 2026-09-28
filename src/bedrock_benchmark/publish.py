"""bedrock-benchmark publish -- copy a finished run from a laptop's
results/ to the team's shared, immutable location, with a manifest.

    <destination>/<YYYY>/<MM>/<run dir name>/
        manifest.yaml       who / why / what: run metadata, every file's sha256,
                            one line per capacity profile (validated against
                            the machine contract before anything is copied)
        <model>/...         the run directory, unchanged

destination is a local directory or s3://bucket/prefix. A run is
published once: an existing manifest at the target is never overwritten.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import List, Optional

import yaml

from .contract import validate_artifact

MANIFEST_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(value) -> Optional[str]:
    """measured_at as text (YAML may have parsed it into a datetime)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_manifest(run_dir: Path, *, owner: Optional[str] = None) -> dict:
    """Validates every capacity profile, then describes the run. Raises
    ValueError when there is nothing publishable or no owner."""
    files = sorted(p for p in run_dir.rglob("*") if p.is_file() and p.name != "manifest.yaml")
    profiles = [p for p in files if p.name.endswith("-capacity-profile.yaml")
                and not p.name.startswith("temporal-")]
    if not profiles:
        raise ValueError(f"{run_dir}: no *-capacity-profile.yaml -- nothing to publish")
    problems, entries, runs = [], [], []
    for p in profiles:
        schema, errors = validate_artifact(str(p))
        problems += [f"{p.relative_to(run_dir)}: {e}" for e in errors]
        doc = yaml.safe_load(p.read_text()) or {}
        run = doc.get("run") or {}
        runs.append(run)
        entries.append({
            "path": str(p.relative_to(run_dir)), "schema": schema, "model": (doc.get("model") or {}).get("name"),
            "experiment": doc.get("experiment"), "purpose": doc.get("purpose"),
            "measured_at": _iso((doc.get("environment") or {}).get("measured_at")),
            "evidence": (doc.get("validity") or {}).get("envelope"),
        })
    if problems:
        raise ValueError("profiles don't match the machine contract -- not publishing:\n  " + "\n  ".join(problems))
    owner = owner or next((r["owner"] for r in runs if r.get("owner")), None)
    if not owner:
        raise ValueError("no run owner -- profiles predate the `run:` block; pass --owner")
    meta = {k: next((r[k] for r in runs if r.get(k)), None) for k in ("run_id", "purpose", "ticket", "environment")}
    measured = sorted(e["measured_at"] for e in entries if e["measured_at"])
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_dir": run_dir.name,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "owner": owner,
        **meta,
        "measured_at": measured[0] if measured else None,
        # Every profile is a single-run operating envelope: publishing
        # shares evidence, it does not make anything production config.
        "evidence": "single_run_operating_envelope",
        "profiles": entries,
        "files": [{"path": str(p.relative_to(run_dir)), "bytes": p.stat().st_size, "sha256": _sha256(p)}
                  for p in files],
    }


def target_prefix(manifest: dict) -> str:
    stamp = manifest["measured_at"] or manifest["published_at"]
    return f"{stamp[:4]}/{stamp[5:7]}/{manifest['run_dir']}"


def publish(run_dir: str, destination: str, *, owner: Optional[str] = None, s3_client=None) -> (str, dict):
    """Returns (where it went, manifest)."""
    src = Path(run_dir)
    if not src.is_dir():
        raise ValueError(f"{run_dir}: not a run directory")
    manifest = build_manifest(src, owner=owner)
    prefix = target_prefix(manifest)
    body = yaml.safe_dump(manifest, sort_keys=False, width=120)
    files: List[str] = [f["path"] for f in manifest["files"]]

    if destination.startswith("s3://"):
        bucket, _, key_prefix = destination[5:].partition("/")
        base = "/".join(x for x in (key_prefix.strip("/"), prefix) if x)
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=f"{base}/manifest.yaml").get("KeyCount", 0)
        if existing:
            raise ValueError(f"s3://{bucket}/{base} is already published -- runs are immutable")
        for rel in files:
            s3_client.upload_file(str(src / rel), bucket, f"{base}/{rel}")
        # Manifest last: its presence means the upload completed.
        s3_client.put_object(Bucket=bucket, Key=f"{base}/manifest.yaml", Body=body.encode())
        return f"s3://{bucket}/{base}", manifest

    dest = Path(destination) / prefix
    if (dest / "manifest.yaml").exists():
        raise ValueError(f"{dest} is already published -- runs are immutable")
    for rel in files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest / rel)
    (dest / "manifest.yaml").write_text(body)
    return str(dest), manifest
