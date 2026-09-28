"""The machine-readable artifact contract: JSON Schemas shipped with the
package (bedrock_benchmark/schemas/) for both artifact types --

    capacity-profile-v<N>.json            a single-run capacity profile
    temporal-capacity-profile-v<N>.json   repeated runs combined over time

A consumer validates a profile against these (any JSON Schema validator,
or `bedrock-benchmark validate-profile`) instead of relying on prose.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import List, Tuple

import yaml


def schema_for(doc: dict) -> Tuple[str, dict]:
    """(schema file name, schema) for a loaded artifact, by its version field."""
    if doc.get("artifact") == "temporal_capacity_profile":
        name = f"temporal-capacity-profile-v{doc.get('temporal_profile_schema_version')}.json"
    else:
        name = f"capacity-profile-v{doc.get('schema_version')}.json"
    path = resources.files("bedrock_benchmark") / "schemas" / name
    if not path.is_file():
        raise ValueError(f"no schema {name} -- unsupported artifact version (this tool ships "
                         f"{', '.join(sorted(p.name for p in (resources.files('bedrock_benchmark') / 'schemas').iterdir()))})")
    return name, json.loads(path.read_text())


def validate_artifact(path: str) -> Tuple[str, List[str]]:
    """(schema name, errors) -- errors empty when the artifact conforms."""
    import jsonschema
    doc = yaml.safe_load(Path(path).read_text()) or {}
    name, schema = schema_for(doc)
    validator = jsonschema.Draft202012Validator(schema)
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
              for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))]
    return name, errors
