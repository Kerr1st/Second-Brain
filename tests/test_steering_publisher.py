"""Behavior tests for review-before-write AGENTS.md publication."""

from pathlib import Path

from src.db import create_memory


def test_agents_publication_requires_reviewed_digest_and_is_idempotent(
    test_db, clean_tables, tmp_path: Path
):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# Project guidance\n", encoding="utf-8")
    rule_id = create_memory(
        type="steering_rule",
        title="Vertical-slice delivery",
        content=(
            "Prove one integration through source evidence, processing, delivery, "
            "and outcome evaluation before generalizing."
        ),
        status="active",
        source_type="steering_governance",
        mem_class="procedural",
        metadata={
            "authority": "approved",
            "lifecycle": "active",
            "authority_scope": "project",
            "applicability": {"integrations": ["codex"]},
            "rule_version": 1,
            "candidate_id": "candidate-1",
        },
    )

    from src.steering_publisher import preview_agents_rule, publish_agents_rule

    preview = preview_agents_rule(rule_id, agents_path)
    assert preview.changed is True
    assert agents_path.read_text(encoding="utf-8") == "# Project guidance\n"
    assert "+Prove one integration" in preview.diff

    result = publish_agents_rule(
        rule_id,
        agents_path,
        expected_current_digest=preview.current_digest,
    )
    published = agents_path.read_text(encoding="utf-8")
    assert result.changed is True
    assert f"second-brain:steering-rule:{rule_id}:start" in published

    second = preview_agents_rule(rule_id, agents_path)
    assert second.changed is False
