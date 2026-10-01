"""Check release archives for required assets, licensing, and accidental local data."""

from pathlib import Path, PurePosixPath
from tarfile import open as open_tar
from zipfile import ZipFile


def check_names(names: set[str]) -> None:
    forbidden_directories = {
        ".agents",
        ".aws",
        ".codex",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "data",
    }
    for name in names:
        path = PurePosixPath(name)
        assert not forbidden_directories.intersection(path.parts), f"Local data in archive: {name}"
        assert not any(part.startswith(".venv") for part in path.parts), name
        assert not (path.name.startswith(".env") and path.name != ".env.example"), (
            f"Environment file in archive: {name}"
        )
        assert not path.name.endswith((".pyc", ".pyo", ".pem", ".key")), name
        assert (
            ".sqlite" not in path.name and not path.name.endswith(".db") and ".db-" not in path.name
        ), name
        assert not (path.name.startswith("alpaca-") and path.name.endswith(".csv")), (
            f"Account export in archive: {name}"
        )
    for asset in ("dashboard.css", "dashboard.js"):
        assert any(name.endswith(f"alpaca_dashboard/assets/{asset}") for name in names), (
            f"Missing packaged asset: {asset}"
        )
    assert any(PurePosixPath(name).name == "LICENSE" for name in names), "Missing LICENSE"


def main() -> None:
    distributions = Path("dist")
    wheels = list(distributions.glob("*.whl"))
    sdists = list(distributions.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected exactly one wheel and one source distribution"

    with ZipFile(wheels[0]) as archive:
        check_names(set(archive.namelist()))
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(metadata) == 1, "Missing wheel metadata"
        text = archive.read(metadata[0]).decode("utf-8")
        assert "License-File: LICENSE" in text, "LICENSE is missing from package metadata"
        assert "License-Expression: MIT" in text, "MIT license is missing from package metadata"
    with open_tar(sdists[0], "r:gz") as archive:
        check_names(set(archive.getnames()))
    print("Wheel and source distribution contain assets and LICENSE, with no local data.")


if __name__ == "__main__":
    main()
