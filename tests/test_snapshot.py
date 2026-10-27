"""griffe extraction against a tiny on-disk package (no network)."""

from pathlib import Path

from deptime.ingest.snapshot import ApiSnapshot, build_snapshot

FILES = {
    "fakepkg/__init__.py": 'from fakepkg._impl import Client, helper\n__all__ = ["Client", "helper", "sub"]\n',
    "fakepkg/_impl.py": (
        "import warnings\n\n"
        "class Client:\n"
        "    def __init__(self, *, proxy=None, proxies=None, timeout=5):\n"
        "        if proxies:\n"
        "            message = (\n"
        "                \"The 'proxies' argument is now deprecated.\"\n"
        "                \" Use 'proxy' or 'mounts' instead.\"\n"
        "            )\n"
        "            warnings.warn(message, DeprecationWarning)\n\n"
        "    def get(self, url):\n"
        "        return url\n\n"
        "    def _private(self):\n"
        "        pass\n\n"
        "def helper(x, y=1):\n"
        "    return x\n"
    ),
    "fakepkg/sub/__init__.py": "def thing():\n    pass\n",
}


def _write(root: Path) -> Path:
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_public_paths_signatures_and_deprecations(tmp_path):
    snap = build_snapshot("fakepkg", "1.0.0", ["fakepkg"], _write(tmp_path))
    paths = set(snap.symbols)
    assert {
        "fakepkg",
        "fakepkg.Client",
        "fakepkg.Client.__init__",
        "fakepkg.Client.get",
        "fakepkg.helper",
        "fakepkg.sub",
        "fakepkg.sub.thing",
    } <= paths
    assert "fakepkg.Client._private" not in paths
    assert not any("._impl" in p for p in paths)
    init = snap.symbols["fakepkg.Client.__init__"]
    assert [p["name"] for p in init.params] == ["proxy", "proxies", "timeout"]
    assert init.canonical == "fakepkg._impl.Client.__init__"
    assert "'proxies' argument is now deprecated" in init.deprecated


def test_snapshot_roundtrip(tmp_path):
    snap = build_snapshot("fakepkg", "1.0.0", ["fakepkg"], _write(tmp_path / "src"))
    out = tmp_path / "s.json.gz"
    snap.save(out)
    again = ApiSnapshot.load(out)
    assert again.symbols == snap.symbols and again.version == "1.0.0"
