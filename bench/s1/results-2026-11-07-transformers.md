# S1 false-absent rate (DT-SPIKE-01), index built 2026-11-07T03:33:07+00:00, public rows

**PASS**: sampled 0/250 = 0.0% (pass at <= 10%).

- 0 of the misses are names seen in other releases, so they would read as removed.
- Pre-registered rule (plain-module package imports in): 49/250 = 19.6%.
- init: 0/125 = 0.0%
- module: 0/125 = 0.0%

| package | releases | sampled | every runtime name |
|---|---|---|---|
| transformers | 5 | 0/250 = 0.0% | 15/58438 = 0.0% |

| package | version | python | modules (failed) | names | sampled misses |
|---|---|---|---|---|---|
| transformers | 4.26.0 | 3.10.22 | 612 (530) | 6880 | 0/50 |
| transformers | 4.37.0 | 3.11.18 | 820 (699) | 8881 | 0/50 |
| transformers | 4.47.1 | 3.12.16 | 982 (833) | 9513 | 0/50 |
| transformers | 4.57.3 | 3.13.18 | 1238 (956) | 16024 | 0/50 |
| transformers | 5.22.2 | 3.14.10 | 1566 (1218) | 17140 | 0/50 |

Every runtime name the index lacks, by shape (stratum, how listed, where defined; all shapes):

- module / dir / package: 8767
- init / __all__ / package: 10
- module / dir / own: 5

Sampled misses:

