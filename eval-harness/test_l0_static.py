"""L0: static checks on the runtime code. No LLM calls, no AWS calls.

Imports managed-agentcore/src directly (see conftest.py), so these run in
seconds on every commit.
"""
import os
import re
import string
from pathlib import Path

import pytest
import yaml

import cost

AGENTCORE = Path(__file__).resolve().parents[1] / "managed-agentcore"
PROMPTS = AGENTCORE / "src" / "prompts"

# Context keys each prompt receives at its call site (src/graph/nodes.py,
# src/tools/*_tool.py). A placeholder outside this set raises KeyError at
# runtime, mid-workflow.
CALL_SITE_KEYS = {
    "coordinator": set(),
    "planner": {"USER_REQUEST"},
    "planner_revise": {"USER_REQUEST", "PREVIOUS_PLAN", "USER_FEEDBACK", "REVISION_COUNT", "MAX_REVISIONS"},
    "supervisor": {"FULL_PLAN"},
    "coder": {"USER_REQUEST", "FULL_PLAN"},
    "validator": {"USER_REQUEST", "FULL_PLAN", "EXECUTION_ENVIRONMENT"},
    "reporter": {"USER_REQUEST", "FULL_PLAN", "EXECUTION_ENVIRONMENT"},
    "auditor": {"USER_REQUEST", "FULL_PLAN", "EXECUTION_ENVIRONMENT"},
    "tracker": {"USER_REQUEST", "FULL_PLAN"},
    "summarization": set(),
}


@pytest.mark.parametrize("name", sorted(CALL_SITE_KEYS))
def test_prompt_placeholders_match_call_site(name):
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    unknown = fields - CALL_SITE_KEYS[name] - {"CURRENT_TIME"}
    assert not unknown, f"{name}.md uses placeholders its call site does not pass: {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(CALL_SITE_KEYS))
def test_prompt_renders(name):
    from src.prompts.template import apply_prompt_template
    ctx = {k: f"<{k}>" for k in CALL_SITE_KEYS[name]}
    rendered = apply_prompt_template(prompt_name=name, prompt_context=ctx)
    assert len(rendered) > 100


# --- model request fields ----------------------------------------------------

MODELS = [
    "global.anthropic.claude-opus-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-sonnet-5",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
]
# Families that reject temperature/top_p/top_k with a 400.
NO_SAMPLING = ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8", "claude-fable")


def _model(model_id, reasoning):
    os.environ.setdefault("AWS_REGION", "us-west-2")
    from src.utils.strands_sdk_utils import strands_utils
    return strands_utils.get_model(llm_type=model_id, enable_reasoning=reasoning, tool_cache=False).config


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("reasoning", [True, False])
def test_request_fields(model_id, reasoning):
    cfg = _model(model_id, reasoning)
    extra = cfg.get("additional_request_fields", {})
    if reasoning:
        assert extra.get("thinking") == {"type": "adaptive"}
        assert "budget_tokens" not in str(extra), "legacy fixed thinking budget"
        assert "temperature" not in cfg
    else:
        assert extra.get("thinking") == {"type": "disabled"}
        # thinking off + effort above high is a 400 (Claude Code v2.1.251)
        effort = (extra.get("output_config") or {}).get("effort")
        assert effort in (None, "low", "medium", "high")
    if any(f in model_id for f in NO_SAMPLING):
        assert "temperature" not in cfg and "top_p" not in cfg


@pytest.mark.xfail(strict=True, reason="known issue: legacy '\\n\\nHuman' stop sequence truncates reports "
                   "that contain e.g. '\\n\\nHuman Resources'; see tmp/issue-opus55.md item 4")
def test_no_legacy_human_stop_sequence():
    cfg = _model(MODELS[0], False)
    assert not any("Human" in s for s in cfg.get("stop_sequences") or [])


# --- config consistency ------------------------------------------------------

def _env_example_model_ids():
    text = (AGENTCORE / ".env.example").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z_]+_MODEL_ID)=(\S+)", text, re.M))


def test_every_configured_model_has_a_pricing_entry():
    pricing = cost.load_pricing()
    missing = {k: v for k, v in _env_example_model_ids().items() if cost.price_for(v, pricing) is None}
    assert not missing, f"add these model families to pricing.yaml: {missing}"


def test_scenarios_point_to_existing_files():
    here = Path(__file__).resolve().parent
    scenarios = yaml.safe_load((here / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]
    for name, s in scenarios.items():
        assert (here / s["csv"]).is_file(), name
        assert (here / s["answer_key"]).is_file(), name
        local = AGENTCORE / s["data_directory"]
        assert local.is_dir(), f"{name}: {s['data_directory']} not in managed-agentcore/"
