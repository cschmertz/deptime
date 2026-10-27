from datetime import UTC, date, datetime

from packaging.version import Version

from deptime.config import VersionPolicy
from deptime.ingest.pypi import Release, ReleaseFile, parse_simple_index, pick_file, select_versions


def _rel(v: str, day: str, *, yanked: bool = False, files: tuple[ReleaseFile, ...] | None = None) -> Release:
    f = ReleaseFile(
        f"pkg-{v}-py3-none-any.whl",
        "https://x",
        None,
        datetime.fromisoformat(day).replace(tzinfo=UTC),
        yanked,
        "wheel",
        True,
    )
    return Release(Version(v), files or (f,))


POLICY = VersionPolicy(
    since=date(2023, 1, 1), recent_months=12, include_prereleases=False, include_yanked=False
)


def test_selects_series_endpoints_and_recent_releases():
    releases = [
        _rel("1.0.0", "2022-01-01"),
        _rel("1.0.5", "2022-06-01"),  # series ended before `since` -> skipped
        _rel("1.1.0", "2022-12-01"),
        _rel("1.1.1", "2023-02-01"),
        _rel("1.1.2", "2023-03-01"),  # 1.1 series active after since -> first (1.1.0) + last (1.1.2)
        _rel("2.0.0", "2025-11-01"),
        _rel("2.0.1", "2026-02-01"),  # recent -> included
        _rel("2.0.2", "2026-05-01"),
        _rel("2.1.0rc1", "2026-06-01"),  # pre-release -> excluded
        _rel("2.0.3", "2026-07-01", yanked=True),  # yanked -> excluded
    ]
    got = [str(r.version) for r in select_versions(releases, POLICY, today=date(2026, 10, 17))]
    assert got == ["1.1.0", "1.1.2", "2.0.0", "2.0.1", "2.0.2"]


def test_pick_file_prefers_pure_wheel_then_any_wheel_then_sdist():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    sdist = ReleaseFile("pkg-1.0.tar.gz", "u", None, t, False, "sdist")
    plat = ReleaseFile("pkg-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", "u", None, t, False, "wheel")
    pure = ReleaseFile("pkg-1.0-py3-none-any.whl", "u", None, t, False, "wheel", True)
    assert pick_file(Release(Version("1.0"), (sdist, plat, pure))) is pure
    assert pick_file(Release(Version("1.0"), (sdist, plat))) is plat
    assert pick_file(Release(Version("1.0"), (sdist,))) is sdist


def test_parse_simple_index_groups_files_by_version():
    payload = {
        "name": "pkg",
        "files": [
            {
                "filename": "pkg-1.0-py3-none-any.whl",
                "url": "u1",
                "hashes": {"sha256": "a"},
                "upload-time": "2024-01-01T00:00:00Z",
                "yanked": False,
            },
            {
                "filename": "pkg-1.0.tar.gz",
                "url": "u2",
                "hashes": {"sha256": "b"},
                "upload-time": "2024-01-01T00:01:00Z",
                "yanked": False,
            },
            {
                "filename": "pkg-1.1.tar.gz",
                "url": "u3",
                "hashes": {},
                "upload-time": "2024-02-01T00:00:00Z",
                "yanked": "broken",
            },
            {"filename": "other-9.9.tar.gz", "url": "u4", "hashes": {}, "upload-time": None, "yanked": False},
        ],
    }
    releases = parse_simple_index(payload)
    assert [str(r.version) for r in releases] == ["1.0", "1.1"]
    assert releases[0].files[0].pure_python is True
    assert releases[1].yanked is True
