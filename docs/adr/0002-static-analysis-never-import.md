# ADR 0002 — Static analysis only; never import or execute indexed packages

**Status:** Accepted, 2026-10-17. Revisit after spike S1.

## Context

Importing thousands of package versions would mean:
- executing arbitrary third-party code (a supply-chain risk);
- resolving each version's dependency tree;
- dealing with compiled extensions per platform.

griffe extracts signatures, aliases and decorators from source without importing anything (`allow_inspection=False`).

## Decision

Index from static analysis of wheel and sdist contents only.
- Pure wheels are preferred, then platform wheels (their `.py`/`.pyi` files), then the sdist.
- Compiled-only APIs without stubs are reported as not covered rather than guessed.

## Consequences

- **Safe:** no code execution at index time.
- **Fast:** about 0.1 s per httpx release.
- **Reproducible.**
- **Blind spots:** lazy `__getattr__` re-exports (langchain, transformers), runtime-generated APIs, and C extensions without stubs. Spike S1 measures the false-absent rate; above 10%, we add a *sandboxed* dynamic fallback for packages flagged `lazy`, in a disposable, network-less container. The fallback is never part of the default path.
