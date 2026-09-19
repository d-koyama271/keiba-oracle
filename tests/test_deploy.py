from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deploy import deploy_site
from publish import publish_site


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "dev"
        self.remote = self.base / "remote.git"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Deploy Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Deploy Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "-b", "main")
        (self.root / "public").mkdir()
        (self.root / "public" / "old.html").write_text("old")
        (self.root / "code.py").write_text("original")
        (self.root / ".gitignore").write_text("public/\n")
        self.git(self.root, "add", "code.py", ".gitignore")
        self.git(self.root, "commit", "-m", "initial")
        self.git(self.root, "remote", "add", "pages", str(self.remote))
        self.git(self.root, "push", "pages", "main")
        self.main_head = self.git(self.remote, "rev-parse", "main")
        self.git(self.root, "checkout", "-b", "deploy-pages")
        self.git(self.root, "add", "-f", "public")
        self.git(self.root, "commit", "-m", "initial public")
        self.git(self.root, "push", "pages", "deploy-pages")
        self.git(self.root, "checkout", "main")
        (self.root / "public").mkdir(exist_ok=True)
        (self.root / "public" / "old.html").write_text("old")
        self.config = {"data_dir": str(self.base / "runtime"), "public_dir": "public",
                       "publish_mode": "github_pages",
                       "deployment": {"github_pages": {"remote": "pages", "branch": "deploy-pages"}}}
        self.clone = self.base / "runtime" / "deploy" / "github_pages"

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    def test_sync_only_public_noop_and_remote_refresh(self):
        (self.root / "code.py").write_text("developer staged work")
        self.git(self.root, "add", "code.py")
        (self.root / "public" / "old.html").unlink()
        (self.root / "public" / "index.html").write_text("new")
        before = (self.git(self.root, "status", "--porcelain"), self.git(self.root, "rev-parse", "HEAD"),
                  self.git(self.root, "diff", "--cached"))
        with patch("deploy.subprocess.run", wraps=subprocess.run) as run:
            deploy_site(self.config, self.root)
            self.assertTrue(run.call_args_list)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertTrue((self.clone / ".git").is_dir())
        self.assertEqual(self.git(self.clone, "branch", "--show-current"), "deploy-pages")
        self.assertEqual(self.git(self.clone, "check-ignore", "--no-index", "public/index.html"), "public/index.html")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.main_head)
        self.assertEqual(self.git(self.clone, "show", "HEAD:code.py"), "original")
        self.assertFalse((self.clone / "public" / "old.html").exists())
        self.assertEqual((self.clone / "public" / "index.html").read_text(), "new")
        names = self.git(self.clone, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
        self.assertTrue(names and all(n.startswith("public/") for n in names))
        self.assertEqual(before, (self.git(self.root, "status", "--porcelain"), self.git(self.root, "rev-parse", "HEAD"),
                                  self.git(self.root, "diff", "--cached")))
        head = self.git(self.clone, "rev-parse", "HEAD")
        # Reject pushes: a no-op must still succeed without attempting push.
        self.git(self.remote, "config", "receive.denyNonFastForwards", "true")
        with patch("deploy.subprocess.run", wraps=subprocess.run) as run:
            deploy_site(self.config, self.root)
            self.assertFalse(any(c.args[0][1] in ("fetch", "commit", "push") for c in run.call_args_list))
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), head)
        other = self.base / "other"
        self.git(self.base, "clone", "-b", "deploy-pages", str(self.remote), str(other))
        (other / "code.py").write_text("remote update")
        self.git(other, "add", "code.py")
        self.git(other, "commit", "-m", "remote change")
        self.git(other, "push", "origin", "deploy-pages")
        (self.root / "public" / "index.html").write_text("updated public")
        deploy_site(self.config, self.root)
        self.assertEqual((self.clone / "code.py").read_text(), "remote update")

    def test_push_failure_then_retry_discards_local_commit(self):
        deploy_site(self.config, self.root)
        original = self.git(self.clone, "rev-parse", "HEAD")
        (self.root / "public" / "index.html").write_text("first attempt")
        run = subprocess.run
        def fail_push(args, **kwargs):
            if args[1] == "push":
                raise subprocess.CalledProcessError(1, args, stderr="push rejected")
            return run(args, **kwargs)
        with patch("deploy.subprocess.run", side_effect=fail_push):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy_site(self.config, self.root)
        self.assertNotEqual(self.git(self.clone, "rev-parse", "HEAD"), original)
        self.assertEqual(self.git(self.remote, "rev-parse", "deploy-pages"), original)
        deploy_site(self.config, self.root)
        self.assertEqual(self.git(self.remote, "show", "deploy-pages:public/index.html"), "first attempt")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.main_head)
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD^"), original)

    def test_publish_independent_and_unsupported_deploy(self):
        self.config["publish_mode"] = "other"
        stage = Path(self.config["data_dir"]) / "_site_stage"
        stage.mkdir(parents=True)
        (stage / "index.html").write_text("rendered")
        self.assertEqual(publish_site(self.config, self.root), self.root / "public")
        self.assertEqual((self.root / "public" / "index.html").read_text(), "rendered")
        with self.assertRaisesRegex(ValueError, "Unsupported publish_mode"):
            deploy_site(self.config, self.root)


if __name__ == "__main__":
    unittest.main()
