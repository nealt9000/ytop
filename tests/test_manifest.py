import shutil
import subprocess

import pytest

from ytop.manifest import (
    for_platforms,
    hash_tree,
    read_manifest,
    sha256_file,
    write_manifest,
)

H1 = "a" * 64
H2 = "b" * 64


def test_round_trip_is_sha256sum_format(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/a.py").write_text("print(1)\n")
    (tmp_path / "boot.py").write_text("x = 1\n")
    entries = hash_tree(tmp_path, ["boot.py", "app/a.py"])
    assert entries["boot.py"] == sha256_file(tmp_path / "boot.py")
    manifest = tmp_path / "manifest.sha256"
    write_manifest(manifest, entries)
    assert manifest.read_text() == (
        f"{entries['app/a.py']}  app/a.py\n{entries['boot.py']}  boot.py\n"
    )
    assert read_manifest(manifest) == entries
    if shutil.which("sha256sum"):
        done = subprocess.run(
            ["sha256sum", "-c", "--quiet", "manifest.sha256"], cwd=tmp_path
        )
        assert done.returncode == 0


def test_write_sorts_lines(tmp_path):
    write_manifest(tmp_path / "m", {"boot.py": H2, "app/a.py": H1})
    assert (tmp_path / "m").read_text() == f"{H1}  app/a.py\n{H2}  boot.py\n"


@pytest.mark.parametrize(
    "line",
    [
        f"{'g' * 64}  app/a.py\n",
        f"{H1} app/a.py\n",
        f"{H1}  /abs/a.py\n",
    ],
)
def test_read_rejects_malformed_lines(tmp_path, line):
    path = tmp_path / "m"
    path.write_text(f"{H2}  boot.py\n{line}")
    assert read_manifest(path) is None


def test_read_missing_file_is_none(tmp_path):
    assert read_manifest(tmp_path / "nope") is None


def test_for_platforms_keeps_shared_and_chosen():
    entries = {
        "boot.py": H1,
        "app/a.py": H1,
        "lib/common/c.py": H1,
        "lib/linux-x86_64/l.so": H1,
        "lib/macos-arm64/l.so": H1,
        "runtime/linux-x86_64/python/bin/python3": H1,
        "runtime/macos-arm64/python/bin/python3": H1,
    }
    got = for_platforms(entries, ["linux-x86_64"])
    assert set(got) == {
        "boot.py",
        "app/a.py",
        "lib/common/c.py",
        "lib/linux-x86_64/l.so",
        "runtime/linux-x86_64/python/bin/python3",
    }
