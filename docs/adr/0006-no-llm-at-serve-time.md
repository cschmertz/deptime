# ADR 0006 — No LLM at serve time; deterministic answers with evidence

**Status:** Accepted, 2026-10-17

## Context

The callers are LLMs. What they lack is *facts* (which release removed what), not more reasoning. An LLM in the serving path would add cost per call, latency, nondeterminism, and a new hallucination source inside a tool whose whole job is to stop hallucinations.

## Decision

Every answer is computed deterministically from the index:
- version boundaries from spans;
- replacements mined offline from code, with their evidence text;
- confidence derived from how exactly the installed version matched.

Language models may be used **offline** only, for example to propose curated migration hints. Any such hint is stored with `source="curated"` and needs human review before it ships.

## Consequences

- The marginal cost per call is about $0, and answering needs no model API key or network call.
- Answers are reproducible and testable, so the benchmark can run in CI.
- Explanations are templated, not eloquent. That's fine, because the consumer is an agent.
