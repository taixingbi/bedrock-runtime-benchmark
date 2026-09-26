"""ModelConfig -- the models a benchmark runs against, loaded from a
models file (scripts/models.yaml by default). Experiments are model-
agnostic workload/sweep definitions; every experiment runs against
every enabled model listed here, so adding a model means one entry in
this file, not a new experiment.

Each model carries its own quota snapshot because quotas differ by an
order of magnitude across models (50 RPM for nova-pro, 1000 for
qwen3-32b) -- rate sweeps are written as fractions of that quota, so
the same experiment brackets each model's own ceiling. Refresh the
numbers with `scripts/fetch_quota.py --all`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

DEFAULT_MODELS_FILE = "scripts/models.yaml"

# `name` is used as a results folder name -- keep it filesystem-safe.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass
class ModelConfig:
    name: str  # short, filesystem-safe; the results folder for this model
    model_id: str
    region: str = "us-east-1"
    quota_rpm: Optional[float] = None
    quota_tpm: Optional[float] = None
    # How many TPM-quota tokens one output token costs (some models bill
    # output at a multiple against TPM). See ceiling.py.
    output_burndown: float = 1.0
    # How prompt padding is sized (calibration.py): auto = CountTokens if
    # supported, else a Converse usage probe, else the 4-chars/token
    # estimate. Force one with count_tokens | converse_usage | estimate.
    token_counting: str = "auto"
    enabled: bool = True


def load_models(
    path: str = DEFAULT_MODELS_FILE, *, names: Optional[List[str]] = None, include_disabled: bool = False,
) -> List[ModelConfig]:
    """Enabled models in file order (every model with include_disabled);
    `names` selects specific ones, disabled or not, and must all exist."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    models = []
    for entry in raw.get("models") or []:
        quota = entry.pop("quota", None) or {}
        models.append(ModelConfig(**entry, quota_rpm=quota.get("rpm"), quota_tpm=quota.get("tpm")))

    seen = set()
    for m in models:
        if not _NAME_RE.match(m.name):
            raise ValueError(f"model name {m.name!r} must be lowercase letters/digits/._- (it's a folder name)")
        if m.name in seen:
            raise ValueError(f"duplicate model name {m.name!r} in {path}")
        seen.add(m.name)

    if names:
        unknown = [n for n in names if n not in seen]
        if unknown:
            raise ValueError(f"unknown model(s) {unknown}; defined in {path}: {sorted(seen)}")
        return [m for m in models if m.name in names]
    return [m for m in models if m.enabled or include_disabled]
