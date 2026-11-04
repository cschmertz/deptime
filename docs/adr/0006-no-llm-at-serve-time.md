# ADR 0006 — No LLM at serve time; deterministic answers with evidence

**Status:** Accepted, 2026-10-17; amended 2026-11-04 (what a curated hint must pass)

## Context

The callers are LLMs. What they lack is *facts* (which release removed what), not more reasoning. An LLM in the serving path would add cost per call, latency, nondeterminism, and a new hallucination source inside a tool whose whole job is to stop hallucinations.

## Decision

Every answer is computed deterministically from the index:
- version boundaries from spans;
- replacements mined offline from code, with their evidence text;
- confidence derived from how exactly the installed version matched.

Language models may be used **offline** only, for example to collect curated migration hints. Any such hint is stored with `source="curated"`, carries the quote and URL it came from as its evidence, and ships only if it passes mechanical checks:
- the quote appears verbatim on the page its URL names (`uv run pytest -m network tests/test_curated.py`);
- the quote names both the old name and its replacement;
- the index has the old name (or the function took the argument) and the replacement (`bench/curated_check.py`, run by `bench.yml` against the published index).

A hint that fails is fixed from the page's real text or dropped. These checks take the place of reading each hint by hand. They catch an invented or paraphrased quote and a name the library never had; what they can't catch, a quote read the wrong way, is in the evidence for anyone who follows the URL.

## Consequences

- Answering needs no model API key, network call or per-call spend.
- Answers are reproducible and testable, so the benchmark can run in CI.
- Explanations are templated, not eloquent. That's fine, because the consumer is an agent.
