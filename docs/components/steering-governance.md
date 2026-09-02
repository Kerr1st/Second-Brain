# Steering Governance Module

> **Status: Codex-first vertical slice implemented and live-proofed.** Last reviewed: 2026-08-29.

Steering Governance converts explicit durable direction or Correction Episode evidence into a
reviewed recommendation, preserves Dream Cycle consensus, and requires a separate user approval
before guidance can become active or be published.

## Interfaces

```python
evaluate_steering_proposal(SteeringProposal) -> SteeringReviewResult
approve_steering_candidate(candidate_id, wording, authority_scope, applicability)
    -> ApprovedSteeringRule
preview_agents_rule(rule_id, path) -> PublicationPreview
publish_agents_rule(rule_id, path, expected_current_digest) -> PublicationResult
```

Four independent model roles evaluate a Steering Candidate. The existing three-of-four quorum
retains an accepted candidate as `type='steering_candidate'`, but it remains inactive. User approval
creates `type='steering_rule'` with final wording, Authority Scope, Applicability, approval evidence,
and a version. A later accepted supersession creates a new version, marks the previous rule
superseded, and preserves the relationship.

The AGENTS publisher accepts only an active approved rule. Preview returns the exact unified diff
and current-file digest without writing. Publication requires that reviewed digest, rejects
symlinks and non-`AGENTS.md` targets, writes atomically, keeps a local rollback copy, and is
idempotent. A superseding rule removes the prior managed block.

## Runtime flow

```text
explicit durable decision or Correction Episodes
  → duplicate suppression
  → four independent Steering Candidate reviews
  → at least three ACCEPT votes
  → inactive Steering Candidate
  → explicit user approval of wording, scope, and applicability
  → versioned active Steering Rule
  → reviewed AGENTS.md diff
  → digest-guarded publication
```

## Entry points and data

| Purpose | Entry point |
|---|---|
| Candidate evaluation and approval | `src/steering.py` |
| AGENTS publication adapter | `src/steering_publisher.py` |
| Review/approve/publish command | `scripts/steering.py` |
| Candidate and rule storage | `memories` plus permanent relationships |
| Panel audit | `dream_cycle_runs` and `dream_cycle_candidates` |
| Behavior tests | `tests/test_steering.py`, `tests/test_steering_publisher.py` |

## Activation and proof

The first live panel accepted candidate `b7b1d9e8-aac2-4def-bf1e-84c199e91045` from explicit user
direction. Separate approval created Steering Rule v1
`ae410076-3f7a-48d5-80c5-ec1e535f66a2`. Its reviewed managed block was published to root
`AGENTS.md`, and a second preview was unchanged.

No candidate can edit steering files, install a hook, or affect future behavior without the
separate approval and publication calls.

## Related

- [Architecture Component Index](index.md)
- [ADR 0007](../adr/0007-capture-correction-episodes-before-steering.md)
- [ADR 0010](../adr/0010-prove-capabilities-vertically-before-generalizing.md)
- [Memory Context Broker](context-broker.md)
