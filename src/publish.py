from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from utils import load_config, parse_target_date, public_dir, repo_root, stage_dir


def publish_site(config: dict, root: Path | None = None) -> Path:
    root = root or repo_root()
    mode = config.get("publish_mode", "github_pages")
    if mode != "github_pages":
        raise ValueError(f"Unsupported publish_mode: {mode}")

    source_dir = stage_dir(config, root)
    if not source_dir.exists():
        raise FileNotFoundError(f"render output not found: {source_dir}")

    target_dir = public_dir(config, root)
    tmp_dir = target_dir.with_name(f"{target_dir.name}__tmp")
    backup_dir = target_dir.with_name(f"{target_dir.name}__backup")

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    shutil.copytree(source_dir, tmp_dir)
    (tmp_dir / ".nojekyll").write_text("", encoding="utf-8")

    try:
        if target_dir.exists():
            target_dir.rename(backup_dir)
        tmp_dir.rename(target_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        if backup_dir.exists():
            backup_dir.rename(target_dir)
        raise
    return target_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    _ = parser.parse_args()
    config = load_config()
    publish_site(config)


if __name__ == "__main__":
    main()
