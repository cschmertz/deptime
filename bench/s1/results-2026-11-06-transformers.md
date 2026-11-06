# S1 false-absent rate (DT-SPIKE-01), index built 2026-11-06T19:18:37+00:00, public rows

**PASS**: sampled 1/250 = 0.4% (pass at <= 10%).

- 1 of the misses are names seen in other releases, so they would read as removed.
- Pre-registered rule (plain-module package imports in): 50/250 = 20.0%.
- init: 1/125 = 0.8%
- module: 0/125 = 0.0%

| package | releases | sampled | every runtime name |
|---|---|---|---|
| transformers | 5 | 1/250 = 0.4% | 257/58438 = 0.4% |

| package | version | python | modules (failed) | names | sampled misses |
|---|---|---|---|---|---|
| transformers | 4.26.0 | 3.10.22 | 612 (530) | 6880 | 0/50 |
| transformers | 4.37.0 | 3.11.18 | 820 (699) | 8881 | 0/50 |
| transformers | 4.47.1 | 3.12.16 | 982 (833) | 9513 | 0/50 |
| transformers | 4.57.3 | 3.13.18 | 1238 (956) | 16024 | 1/50 |
| transformers | 5.22.2 | 3.14.10 | 1566 (1218) | 17140 | 0/50 |

Every runtime name the index lacks, by shape (stratum, how listed, where defined; all shapes):

- module / dir / package: 8767
- init / __all__ / package: 252
- module / dir / own: 5

Sampled misses:

- transformers 4.57.3: `transformers.models.xlstm.xLSTMModel` (init, __all__, class, package; seen in other releases)
