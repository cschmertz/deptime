import json

import pytest

from deptime.bench import baseline_doc, load_cases, regressions, run, summarize
from tests.test_tracebacks import HTTPX_TB


def _case(cid: str, **expected) -> dict:
    exp = {"package": "httpx", "symbol_suffix": "Client.__init__(proxies)", "issue": "param_removed"}
    return {
        "id": cid,
        "source": "constructed",
        "traceback": HTTPX_TB,
        "installed": {"httpx": "0.28.1"},
        "expected": exp | expected,
        "status": "expected_pass",
    }


def test_scores_root_cause_boundary_and_replacement(dt):
    [r] = run(dt, [_case("ok", last_version_with="0.27", replacement_contains="proxy")])
    assert (r.root_cause, r.boundary, r.replacement, r.passed, r.tier) == (True, True, True, True, "B")


def test_wrong_boundary_fails_the_case_but_keeps_the_root_cause(dt):
    [r] = run(dt, [_case("early", last_version_with="0.26")])
    assert (r.root_cause, r.boundary, r.replacement, r.passed) == (True, False, None, False)


def test_first_version_with_is_scored_on_the_first_indexed_release_with_it(dt):
    # Code written for a newer release: httpx.NewThing first appears in 0.27.2 in the synthetic index.
    tb = "ImportError: cannot import name 'NewThing' from 'httpx' (/app/x.py)"
    case = _case("new", symbol_suffix="httpx.NewThing", issue="symbol_not_yet_added") | {"traceback": tb}
    case["installed"] = {"httpx": "0.26.0"}
    hit, miss = (
        case | {"id": cid, "expected": case["expected"] | {"first_version_with": v}}
        for cid, v in (("hit", "0.27"), ("miss", "0.28"))
    )
    assert [(r.root_cause, r.boundary) for r in run(dt, [hit, miss])] == [(True, True), (True, False)]


def test_boundary_and_replacement_require_the_root_cause(dt):
    case = _case("wrong", last_version_with="0.27", replacement_contains="proxy")
    case["expected"]["issue"] = "symbol_removed"
    [r] = run(dt, [case])
    assert (r.root_cause, r.boundary, r.replacement) == (False, False, False)


def test_no_finding_is_a_miss(dt):
    case = _case("nothing") | {"traceback": "not a traceback"}
    [r] = run(dt, [case])
    assert not r.root_cause
    assert r.got.startswith("no finding")


def test_summary_reports_tiers_and_known_gap_share(dt):
    gap = _case("gap", last_version_with="0.26") | {"status": "known_gap"}
    s = summarize(run(dt, [_case("ok", last_version_with="0.27"), gap]))
    assert s["cases"] == 2
    assert s["known_gap_share"] == 50.0
    assert s["overall"] == {
        "root_cause": 100.0,
        "root_cause_and_boundary": 50.0,
        "boundary": 50.0,
        "replacement": None,
        "replacement_uncurated": None,
    }
    assert list(s["by_tier"]) == ["B"]
    assert list(s["by_origin"]) == ["constructed"]


def test_regression_is_measured_on_shared_cases_only(dt):
    baseline = baseline_doc(run(dt, [_case("a", last_version_with="0.27")]), None)
    # A new failing case is not a regression: it isn't in the baseline.
    assert (
        regressions(run(dt, [_case("a", last_version_with="0.27"), _case("new", issue="x")]), baseline) == []
    )
    # The same case now failing is.
    failed = regressions(run(dt, [_case("a", issue="symbol_removed")]), baseline)
    assert any(f.startswith("root_cause: 100.0 -> 0.0") for f in failed)


def test_load_cases_rejects_duplicate_ids(tmp_path):
    (tmp_path / "a.jsonl").write_text(json.dumps(_case("same")) + "\n\n")
    (tmp_path / "b.jsonl").write_text(json.dumps(_case("same")) + "\n")
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases([tmp_path])


def test_replacement_is_also_scored_without_curated_hints():
    # DT-DATA-08: curated hints come from migration guides that benchmark cases may share, so the replacement
    # DepTime finds by itself is reported beside the headline one.
    from deptime.config import Coverage
    from deptime.query import DepTime
    from tests.test_query import POLICY, curated_store

    store, spec = curated_store()
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    case = {
        "id": "p8-client",
        "traceback": "ImportError: cannot import name 'Client' from 'p8' (/x.py)",
        "installed": {"p8": "2.0"},
        "expected": {
            "package": "p8",
            "symbol_suffix": "p8.Client",
            "issue": "symbol_removed",
            "replacement_contains": "Session",
        },
    }
    [r] = run(dt, [case])
    assert (r.replacement, r.replacement_uncurated) == (True, False)
    assert summarize([r])["overall"]["replacement_uncurated"] == 0.0
    # Older baselines have no such field; the regression check skips it.
    assert (
        regressions(
            [r], {"cases": {"p8-client": {"root_cause": True, "boundary": None, "replacement": True}}}
        )
        == []
    )
