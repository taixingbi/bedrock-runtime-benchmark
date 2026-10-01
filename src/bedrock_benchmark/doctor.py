"""bedrock-benchmark doctor -- is this machine / account / model ready to
benchmark? Run it before anything that sends load.

    environment   Python in requires-python, boto3, git checkout clean
    config        slo.yaml, recommendation-policy.yaml, quota.yaml and every
                  experiment load and bind to the model
    AWS           identity (STS), region
    quota         quota.yaml entry for the LIVE account vs Service Quotas
    model access  one Converse call (maxTokens=1)
    streaming     one ConverseStream call, when an experiment streams
    tokens        which token-counting strategy padding will use

Cost: two 1-token inferences (plus one for token counting when the model
lacks CountTokens). Every AWS client is injectable, so tests use fakes.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

OK, WARN, FAIL = "ok", "warn", "FAIL"
_PROBE = "Reply with OK."


@dataclass
class Check:
    area: str
    status: str
    detail: str


def _error_code(exc: Exception) -> str:
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
    return f"{code}: {exc}" if code else f"{type(exc).__name__}: {exc}"


def check_environment(root: Path, *, git: Callable[[List[str]], str] = None) -> List[Check]:
    checks = []
    v = sys.version_info
    checks.append(Check("python", OK if (3, 11) <= v[:2] < (3, 13) else FAIL,
                        f"{v.major}.{v.minor}.{v.micro} (requires >=3.11,<3.13)"))
    try:
        import boto3
        checks.append(Check("boto3", OK, boto3.__version__))
    except ImportError:
        checks.append(Check("boto3", FAIL, "not installed -- pip install -e ."))
    git = git or (lambda args: subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                              text=True, check=True).stdout)
    try:
        commit = git(["rev-parse", "--short", "HEAD"]).strip()
        dirty = [line for line in git(["status", "--porcelain"]).splitlines()
                 if line[3:].split("/")[0] not in ("results", ".venv")]
        checks.append(Check("git", WARN if dirty else OK,
                            f"{commit}" + (f", {len(dirty)} uncommitted change(s) -- the profile won't match "
                                           f"a commit" if dirty else ", clean")))
    except Exception:  # noqa: BLE001 - not a git checkout (installed elsewhere)
        checks.append(Check("git", WARN, "not a git checkout -- results can't be tied to a commit"))
    return checks


def check_config(model_name: str, paths: List[str], *, account: Optional[str]) -> (List[Check], Any, list):
    """Returns (checks, model or None, loaded specs)."""
    from .constraints import load_policy, load_quotas, load_slo
    from .experiments.schema import NoMatchingWorkloads, load_experiment
    from .models import load_models
    checks, model, specs = [], None, []
    for label, loader in (("slo.yaml", load_slo), ("recommendation-policy.yaml", load_policy),
                          ("quota.yaml", load_quotas)):
        try:
            loader()
            checks.append(Check(label, OK, "valid"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(label, FAIL, str(exc)))
    try:
        model = load_models(names=[model_name], account=account)[0]
        checks.append(Check("model", OK, f"{model.name} = {model.model_id} ({model.region}), account {model.account}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("model", FAIL, str(exc)))
        return checks, None, specs
    bad = []
    for path in paths:
        try:
            specs.append(load_experiment(path, model))
        except NoMatchingWorkloads as exc:
            checks.append(Check(Path(path).stem, WARN, f"skipped: {exc}"))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{Path(path).stem}: {exc}")
    checks.append(Check("experiments", FAIL if bad else OK,
                        "; ".join(bad) if bad else f"{len(specs)} experiment(s) bind to {model.name}"))
    return checks, model, specs


def check_aws(model, *, sts_client=None, runtime_client=None, quota_fetch=None, specs=()) -> List[Check]:
    checks = []
    try:
        if sts_client is None:
            import boto3
            sts_client = boto3.client("sts", region_name=model.region)
        ident = sts_client.get_caller_identity()
        live = str(ident["Account"])
        checks.append(Check("identity", OK if live == model.account else FAIL,
                            f"{ident.get('Arn')} (account {live})" + (
                                "" if live == model.account else f" -- quotas resolved for {model.account}")))
    except Exception as exc:  # noqa: BLE001
        return [Check("identity", FAIL, f"no usable AWS credentials -- {_error_code(exc)}")]
    checks.append(Check("region", OK, model.region))

    # Quota: file (what the sweep is designed around) vs live account.
    if model.quota_rpm is None or model.quota_tpm is None:
        checks.append(Check("quota", FAIL, f"constraints/quota.yaml has no rpm/tpm for {model.account}/"
                                           f"{model.region}/{model.name} -- scripts/fetch_quota.py --model-id "
                                           f"{model.model_id}"))
    else:
        from .quota import fetch_quota_snapshot
        live_q = (quota_fetch or fetch_quota_snapshot)(model.model_id, region=model.region)
        filed = f"file rpm {model.quota_rpm:,.0f} tpm {model.quota_tpm:,.0f}"
        if live_q.source == "unknown":
            checks.append(Check("quota", WARN, f"{filed}; live value unknown (no Service Quotas mapping/permission)"))
        elif (live_q.rpm, live_q.tpm) == (model.quota_rpm, model.quota_tpm):
            checks.append(Check("quota", OK, f"{filed} = live"))
        else:
            checks.append(Check("quota", FAIL, f"{filed} but live rpm {live_q.rpm} tpm {live_q.tpm} -- update "
                                               f"constraints/quota.yaml (sweeps are designed around it)"))

    if runtime_client is None:
        import boto3
        runtime_client = boto3.client("bedrock-runtime", region_name=model.region)
    messages = [{"role": "user", "content": [{"text": _PROBE}]}]
    config = {"maxTokens": 1, "temperature": 0.0}
    try:
        runtime_client.converse(modelId=model.model_id, messages=messages, inferenceConfig=config)
        checks.append(Check("model access", OK, f"Converse {model.model_id}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("model access", FAIL, _error_code(exc)))
        return checks
    streams = any(s.stream for s in specs)
    try:
        resp = runtime_client.converse_stream(modelId=model.model_id, messages=messages, inferenceConfig=config)
        for _ in resp["stream"]:
            pass
        checks.append(Check("streaming", OK, "ConverseStream (TTFT is measured from it)"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("streaming", FAIL if streams else WARN,
                            _error_code(exc) + (" -- experiments use stream: true" if streams else "")))

    from .client import BedrockConverseTarget
    target = BedrockConverseTarget(model_id=model.model_id, region=model.region, client=runtime_client)
    try:
        strategy = model.token_counting
        if strategy in ("auto", "count_tokens"):
            try:
                target.count_tokens(_PROBE)
                checks.append(Check("token counting", OK, "count_tokens (CountTokens API, free)"))
                return checks
            except Exception as exc:  # noqa: BLE001
                if strategy == "count_tokens":
                    checks.append(Check("token counting", FAIL, f"token_counting: count_tokens but {_error_code(exc)}"))
                    return checks
        if strategy in ("auto", "converse_usage"):
            target.usage_input_tokens(_PROBE)
            checks.append(Check("token counting", OK, "converse_usage (one 1-token Converse per workload)"))
        else:
            checks.append(Check("token counting", WARN, "estimate (4 chars/token) -- workload sizes are approximate"))
    finally:
        target.close()
    return checks


def run_doctor(model_name: str, paths: List[str], root: Path, *, account: Optional[str] = None, git=None,
               sts_client=None, runtime_client=None, quota_fetch=None) -> List[Check]:
    checks = check_environment(root, git=git)
    config_checks, model, specs = check_config(model_name, paths, account=account)
    checks += config_checks
    if model is not None:
        checks += check_aws(model, sts_client=sts_client, runtime_client=runtime_client,
                            quota_fetch=quota_fetch, specs=specs)
    return checks


def format_doctor(model_name: str, checks: List[Check]) -> str:
    lines = [f"doctor: {model_name}"]
    lines += [f"  {c.status:<4} {c.area:<27} {c.detail}" for c in checks]
    failed = [c.area for c in checks if c.status == FAIL]
    warned = [c.area for c in checks if c.status == WARN]
    lines.append("")
    lines.append(f"NOT READY -- fix: {', '.join(failed)}" if failed else
                 "READY" + (f" (warnings: {', '.join(warned)})" if warned else ""))
    return "\n".join(lines)
