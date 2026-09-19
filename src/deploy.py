from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from utils import atomic_write_json, data_dir, now_jst, public_dir, repo_root


def deploy_site(config: dict, root: Path | None = None) -> None:
    mode = config["publish_mode"]
    if mode != "github_pages":
        raise ValueError(f"Unsupported publish_mode: {mode}")
    root = (root or repo_root()).resolve()
    settings = config["deployment"]["github_pages"]
    remote, branch = settings["remote"], settings["branch"]
    source = public_dir(config, root).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"public output not found: {source}")
    clone = data_dir(config, root) / "deploy" / "github_pages"
    if clone.is_symlink() or clone.resolve() == root or root.is_relative_to(clone.resolve()):
        raise ValueError("deploy clone must be separate from the source repository")
    target = clone / "public"
    if source.is_relative_to(clone.resolve()) or clone.resolve().is_relative_to(source):
        raise ValueError("source public must be outside the deploy clone")

    def git(*args: str, cwd: Path = clone) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True, encoding="utf-8",
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout.strip()

    url = git("remote", "get-url", remote, cwd=root)
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file():
            digest.update(json.dumps([path.relative_to(source).as_posix(),
                                      hashlib.sha256(path.read_bytes()).hexdigest()]).encode())
    state_path = clone.parent / "github_pages_state.json"
    published = {"url": url, "branch": branch, "digest": digest.hexdigest()}
    try:
        if (clone / ".git").is_dir() and json.loads(state_path.read_text(encoding="utf-8")) == published:
            return
    except (FileNotFoundError, ValueError):
        pass
    # Only a confirmed remote publication may permit a later local-only skip.
    state_path.unlink(missing_ok=True)
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--branch", branch, "--single-branch", "--", url, str(clone), cwd=root)
    if not (clone / ".git").is_dir() or (clone / ".git").is_symlink():
        raise ValueError("deploy directory must be a dedicated clone")
    git("remote", "set-url", "origin", url)
    git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    git("checkout", "-B", branch, f"refs/remotes/origin/{branch}")
    git("reset", "--hard", f"refs/remotes/origin/{branch}")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        if target.resolve().parent != clone.resolve():
            raise ValueError("deploy public path escapes the dedicated clone")
        shutil.rmtree(target)
    shutil.copytree(source, target)
    git("add", "-f", "-A", "--", "public")
    if not git("diff", "--cached", "--name-only", "--", "public"):
        atomic_write_json(state_path, published)
        return
    git("commit", "-m", f"Publish site {now_jst():%Y-%m-%d %H:%M} JST")
    git("push", "origin", f"HEAD:refs/heads/{branch}")
    atomic_write_json(state_path, published)
