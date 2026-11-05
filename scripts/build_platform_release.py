"""Build a fresh wheel in an isolated build directory using the current Python.

No pip install, network, virtualenv creation, database access or source cleanup.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from infra.platform_migrations import PLATFORM_SCHEMA_REVISION
from infra.release_checks import REQUIRED_ASSETS
from scripts.repository_policy import private_distribution_path

ROOT = Path(__file__).resolve().parents[1]


def verify_wheel(path):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for package, paths in REQUIRED_ASSETS.items():
            for asset in paths:
                if package.replace(".", "/") + "/" + asset not in names:
                    raise ValueError("Wheel is missing required platform assets")
        if any(
            private_distribution_path(name)
            or any(part.lower() == "config" for part in name.split("/"))
            for name in names
        ):
            raise ValueError("Wheel contains private or development files")
        entries = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        if "enterprise-ai = interfaces.cli.platform:main" not in archive.read(entries).decode():
            raise ValueError("Wheel has the wrong application entry point")
    return {"wheel": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "schema_revision": PLATFORM_SCHEMA_REVISION, "pi_dependencies": "install separately with the packaged lockfile"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, help="New, non-existing release directory")
    args = parser.parse_args(argv)
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="platform-build-", dir=output) as build:
        with (output / "build.log").open("w") as log:
            subprocess.run([sys.executable, "-c", "from setuptools import setup; setup()", "build", "--build-base", build + "/lib",
                "bdist_wheel", "--bdist-dir", build + "/wheel", "--dist-dir", str(output)], cwd=ROOT, check=True,
                stdout=log, stderr=subprocess.STDOUT)
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one new release wheel")
    result = verify_wheel(wheels[0])
    (output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
