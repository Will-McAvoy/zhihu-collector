import re
import subprocess
import sys
import tomllib
from pathlib import Path

from zhihu_collector import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_matches_package_version():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_installer_matches_package_version():
    text = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    match = re.search(r'#define AppVersion "([^"]+)"', text)
    assert match and match.group(1) == __version__


def test_changelog_has_entry_for_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text


def test_cli_prints_version():
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "zhihu_collector", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert result.stdout.strip() == f"知乎内容搜集器 {__version__}"
