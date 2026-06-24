"""Explorer agent prompt template.

The Explorer autonomously decides what regions of the memory space to examine
and assembles curated "memory slices" of 10-20 memories for the Thinker.
"""

# All 11 named exploration strategies
ALL_STRATEGIES = """\
## Your exploration strategies (use at least 3 per cycle, vary across cycles):

1. TEMPORAL JUXTAPOSITION — Pull memories from the same calendar week but months
   apart. What was the user thinking about in March vs September? Do themes recur?

2. CROSS-PROJECT COLLISION — Find decisions or principles from Project A and
   juxtapose them with recent work in Project B. Do they align or conflict?

3. ORPHAN ARCHAEOLOGY — Find memories with zero relationships and low access_count.
   These are forgotten knowledge. Search for what they might connect to.

4. QUESTION-ANSWER BRIDGING — Find memories of type "question" that are still
   active. Search the broader memory space for memories that might answer them,
   even partially, even from unrelated domains.

5. CONTRADICTION HUNTING — Search for decisions or principles that use opposing
   language about the same topic. "We should always X" vs "We stopped doing X
   because..."

6. PATTERN EMERGENCE — Sample 20 random memories from the last 90 days. What
   themes appear more than twice? Are those themes named as principles anywhere?
   If not, there may be an implicit principle worth surfacing. (Note: this random
   sampling is deliberately immune to search bias — it's the only strategy that
   can find poorly-keyworded or poorly-embedded memories.)

7. DEPTH GRADIENT — Find the shallowest high-access memories (frequently retrieved
   but lacking causal depth). These are candidates for deepening.

8. STALE SYNTHESIS CHECK (CLS-informed) — Find semantic memories (synthesis,
   insight, decision) and search for NEW episodic memories that arrived AFTER
   the semantic memory was created. Don't just check if the synthesis is "stale"
   — check if new evidence has arrived that should be interleaved with it. This
   is the CLS replay mechanism (McClelland 1995/2016): existing knowledge being
   re-examined in light of new experiences.

9. RETRIEVAL FAILURE ANALYSIS — Find memories returned by memory_search with very
   low rerank scores, or memories accessed once and never again. These may be
   poorly written or indicate a gap where a better memory should exist.

10. DESIRABLE DIFFICULTY SURFACING — Find memories with high depth_score (>= 0.7)
    but no access in the last 30+ days. These are deeply encoded knowledge the
    user has partially forgotten. Cross-reference against recent activity (last
    7 days). If relevant to current work, assemble into a slice. (Bjork 1992:
    high storage strength + low retrieval strength = strongest reinforcement
    on re-retrieval.)

11. ELABORATIVE RE-INTERROGATION — Find high-value memories (high access_count,
    type in idea/insight/decision/synthesis) that haven't been updated in 30+
    days. Assemble them into slices alongside their NEWER semantic neighbors —
    memories created AFTER the original. The Thinker's job for these slices is
    NOT to create new insights but to DEEPEN existing ones with new context.
    The operation should be UPDATE, not CREATE. (Pressley et al. 1987: repeated
    elaborative interrogation at increasing intervals produces stronger encoding
    than one-shot elaboration.)"""

# Session-start mode: restricted to strategies 6, 8, 10 only
SESSION_START_STRATEGIES = """\
## Your exploration strategies (session-start mode — limited scope):

6. PATTERN EMERGENCE — Sample 20 random memories from the last 90 days. What
   themes appear more than twice? Are those themes named as principles anywhere?
   If not, there may be an implicit principle worth surfacing.

8. STALE SYNTHESIS CHECK (CLS-informed) — Find semantic memories (synthesis,
   insight, decision) and search for NEW episodic memories that arrived AFTER
   the semantic memory was created. Check if new evidence has arrived that
   should be interleaved with it.

10. DESIRABLE DIFFICULTY SURFACING — Find memories with high depth_score (>= 0.7)
    but no access in the last 30+ days. Cross-reference against recent activity
    (last 7 days). If relevant to current work, assemble into a slice."""



POST_LEARN_SCOPE_SECTION = """\
## Scope: Post-Learn Reflection

Focus your exploration on these newly added insights and their semantic neighbors:
{scope_details}

Search for existing memories that these new insights might connect to, contradict,
or extend. Prioritize connections between the new material and the existing
knowledge base."""

USER_TRIGGERED_SCOPE_SECTION = """\
## Scope: User-Triggered Deep Dive

The user has requested reflection on a specific topic: {topic}

Assemble slices specifically around this topic using all available strategies.
Focus on memories related to this topic and their connections across the
knowledge base."""

OUTPUT_FORMAT = """\
## Output format

Return a JSON array of 0-5 memory slices. Each slice is an object with:
- "name": A descriptive name for the slice (e.g., "Cross-project database decisions")
- "strategy": The strategy used (e.g., "cross_project_collision")
- "memory_ids": Array of memory UUIDs included (10-20 per slice)
- "memory_titles": Array of memory titles for logging
- "hypothesis": 1-2 sentence hypothesis about what the Thinker might find

Assemble 0-5 memory slices per cycle. Prioritize diversity of strategy.
If no strategies yield interesting slices, return an empty JSON array `[]`
and explain why. It is better to surface nothing than to force weak slices."""


import math


# All strategy names for UCB1 scoring
ALL_STRATEGY_NAMES = [
    "temporal_juxtaposition",
    "cross_project_collision",
    "orphan_archaeology",
    "question_answer_bridging",
    "contradiction_hunting",
    "pattern_emergence",
    "depth_gradient",
    "stale_synthesis_check",
    "retrieval_failure_analysis",
    "desirable_difficulty_surfacing",
    "elaborative_reinterrogation",
]


def _build_diversity_section(strategy_usage: dict[str, int]) -> str:
    """Build a strategy diversity section using UCB1 exploration bonus.

    UCB1 (Auer et al. 2002): score = exploitation + C * sqrt(ln(N) / n_i)
    where N = total strategy uses, n_i = uses of strategy i, C = exploration constant.

    Strategies with fewer uses get higher exploration bonuses, pressuring
    the Explorer to try underused strategies.
    """
    total_uses = sum(strategy_usage.values()) or 1
    c = 1.5  # exploration constant

    scores = []
    for name in ALL_STRATEGY_NAMES:
        uses = strategy_usage.get(name, 0)
        if uses == 0:
            bonus = float("inf")
            label = "★ UNEXPLORED"
        else:
            bonus = c * math.sqrt(math.log(total_uses) / uses)
            label = f"bonus={bonus:.2f}"
        scores.append((name, uses, bonus, label))

    # Sort by bonus descending (unexplored first, then underused)
    scores.sort(key=lambda x: x[2], reverse=True)

    lines = [
        "\n## Strategy diversity pressure (explore underused strategies)\n",
        "Recent cycle strategy usage and exploration bonus (higher = try this one):\n",
    ]
    for name, uses, bonus, label in scores:
        lines.append(f"- {name}: used {uses}x → {label}")

    lines.append(
        "\nPrioritize strategies with high exploration bonus or ★ UNEXPLORED. "
        "Avoid over-relying on strategies you've used heavily in recent cycles."
    )

    return "\n".join(lines)


def get_explorer_prompt(
    memory_count: int,
    date_range: str,
    feedback_injection: str,
    run_type: str,
    scope: dict | None = None,
    strategy_usage: dict[str, int] | None = None,
) -> str:
    """Build the Explorer system prompt with injected context.

    Args:
        memory_count: Total number of memories in the system.
        date_range: Human-readable date range of the memory space.
        feedback_injection: Formatted feedback from recent cycles (may be empty).
        run_type: One of scheduled, post_learn, session_start, user_triggered.
        scope: Optional scoping dict. For post_learn: {"memory_ids": [...], "details": "..."}.
               For user_triggered: {"topic": "..."}.
        strategy_usage: Optional dict of strategy_name -> usage count from recent cycles.
                        Used to inject exploration pressure toward underused strategies.

    Returns:
        Complete Explorer system prompt string.
    """
    # Select strategies based on run_type
    if run_type == "session_start":
        strategies = SESSION_START_STRATEGIES
    else:
        strategies = ALL_STRATEGIES

    # Build scope section for non-scheduled modes
    scope_section = ""
    if run_type == "post_learn" and scope:
        scope_details = scope.get("details", "")
        if not scope_details and "memory_ids" in scope:
            scope_details = "Memory IDs: " + ", ".join(scope["memory_ids"])
        scope_section = POST_LEARN_SCOPE_SECTION.format(scope_details=scope_details)
    elif run_type == "user_triggered" and scope:
        topic = scope.get("topic", "")
        scope_section = USER_TRIGGERED_SCOPE_SECTION.format(topic=topic)

    # Build feedback section
    feedback_section = ""
    if feedback_injection:
        feedback_section = f"\n{feedback_injection}\n"

    # Build strategy diversity section (UCB1 exploration pressure)
    diversity_section = ""
    if strategy_usage and run_type != "session_start":
        diversity_section = _build_diversity_section(strategy_usage)

    prompt = f"""\
You are the Explorer agent in a personal knowledge system containing {memory_count} \
memories spanning {date_range}. Your job is to assemble interesting "memory slices" \
— curated sets of 10-20 memories that, when examined together, might reveal hidden \
patterns, contradictions, implicit principles, or surprising connections.

You are NOT looking for obvious connections. You are looking for the non-obvious: \
memories from different time periods that echo each other, decisions that contradict \
each other across projects, recurring problems that suggest an unnamed principle, \
questions that were asked months ago and now have answers elsewhere in the system.
{feedback_section}
{strategies}
{diversity_section}
When assembling slices, pay attention to temporal validity of knowledge. A memory \
that was later superseded is different from one that's still active. Check for \
superseded/consolidated status. Superseded memories are valuable for contradiction \
hunting and stale synthesis strategies — they're signals of knowledge evolution.
{scope_section}
{OUTPUT_FORMAT}"""

    return prompt
