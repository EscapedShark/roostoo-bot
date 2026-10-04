#!/usr/bin/env python3
"""Build a source-only archive; never include .env, state, data or logs."""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    fixed = ["README.md", "STRATEGY.md", "GITHUB_PUBLISH.md", "DEPLOYMENT_GUIDE.md",
             "VALIDATION.md", "TEST_RUN_REPORT.md", "STRATEGY_MANIFEST.json",
             "FEATURE_PARITY.json", ".env.example", ".gitignore", "requirements.txt", "run.sh",
             "roostoo-bot.service.example"]
    files = [ROOT / name for name in fixed]
    files += list(ROOT.glob("*.py"))
    files += list((ROOT / "roostoo_bot").glob("*.py"))
    files += list((ROOT / "tests").glob("*.py"))
    output = ROOT / "dist" / "roostoo-deploy.tar.gz"
    output.parent.mkdir(exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(set(files)):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"invalid package source: {path.name}")
            archive.add(path, arcname="roostoo-bot/" + path.relative_to(ROOT).as_posix(), recursive=False)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n")
    print(f"Built {output.name}: {output.stat().st_size} bytes, {len(set(files))} files")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    main()
