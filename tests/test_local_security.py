import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_gitignore_protects_local_secrets_and_keeps_template_and_state(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    project = Path(__file__).resolve().parents[1]
    (tmp_path / ".gitignore").write_bytes((project / ".gitignore").read_bytes())
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    # Only a temporary repository containing ignore rules; never stage the real .env.
    subprocess.run([git, "init", "--quiet", "--template=", str(tmp_path)], check=True, capture_output=True, env=env)
    protected = [".env", "local.env", ".env.local", ".env.bak", "local.env.backup",
                 "jobradar/__pycache__/config.cpython-312.pyc", ".venv/config.txt",
                 "secrets/local.json", ".secrets/local.json", "credentials.json", "token.json",
                 "service.key", "service.pem", "service.pfx", ".env~", "local.log"]
    visible = [".env.example", "data/state.json", "jobradar/config.py", "README.md"]
    result = subprocess.run([git, "check-ignore", "--no-index", "--stdin", "-z"],
                            input=("\0".join(protected + visible) + "\0").encode(), cwd=tmp_path,
                            check=True, capture_output=True, env=env)
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(protected)
