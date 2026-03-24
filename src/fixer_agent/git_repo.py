from __future__ import annotations

import logging
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from git import Repo

logger = logging.getLogger(__name__)

_GITHUB_HTTPS = re.compile(
    r"https?://(?:[^@]+@)?github\.com/([^/]+)/([^/.]+)(?:\.git)?",
    re.I,
)


@dataclass(frozen=True)
class GitSource:
    owner: str
    repo: str
    clone_url_https: str
    revision: str | None
    default_branch_hint: str | None


def _annotation(pr: dict[str, Any], key: str) -> str | None:
    md = pr.get("metadata") or {}
    ann = md.get("annotations") or {}
    v = ann.get(key)
    return str(v) if v else None


def _first_github_url(text: str | None) -> str | None:
    if not text:
        return None
    m = _GITHUB_HTTPS.search(text)
    return m.group(0).rstrip("/") if m else None


def discover_git_source(pipelinerun: dict[str, Any]) -> GitSource | None:
    """
    Resolve GitHub coordinates from PipelineRun metadata.

    Primary: Pipelines-as-Code annotations.
    Fallback: any https://github.com/org/repo URL in annotations.
    """
    org = _annotation(pipelinerun, "pipelinesascode.tekton.dev/url-org")
    repo = _annotation(pipelinerun, "pipelinesascode.tekton.dev/url-repository")
    sha = _annotation(pipelinerun, "pipelinesascode.tekton.dev/sha")
    branch = _annotation(pipelinerun, "pipelinesascode.tekton.dev/source-branch")
    pr_url = _annotation(pipelinerun, "pipelinesascode.tekton.dev/original-pr-url")

    if org and repo:
        clone_url = f"https://github.com/{org}/{repo}.git"
        return GitSource(
            owner=org,
            repo=repo,
            clone_url_https=clone_url,
            revision=sha or branch,
            default_branch_hint=branch,
        )

    md = pipelinerun.get("metadata") or {}
    ann = md.get("annotations") or {}
    for val in ann.values():
        if not isinstance(val, str):
            continue
        url = _first_github_url(val)
        if url:
            m = _GITHUB_HTTPS.search(url)
            if m:
                o, r = m.group(1), m.group(2)
                return GitSource(
                    owner=o,
                    repo=r,
                    clone_url_https=f"https://github.com/{o}/{r}.git",
                    revision=sha,
                    default_branch_hint=branch,
                )

    src = _first_github_url(pr_url)
    if src:
        m = _GITHUB_HTTPS.search(src)
        if m:
            o, r = m.group(1), m.group(2)
            return GitSource(
                owner=o,
                repo=r,
                clone_url_https=f"https://github.com/{o}/{r}.git",
                revision=sha,
                default_branch_hint=branch,
            )

    return None


def _authenticated_clone_url(clone_url: str, token: str | None) -> str:
    if not token:
        return clone_url
    if not clone_url.startswith("https://github.com/"):
        return clone_url
    rest = clone_url.removeprefix("https://")
    user = urllib.parse.quote("x-access-token", safe="")
    tok = urllib.parse.quote(token, safe="")
    return f"https://{user}:{tok}@{rest}"


def clone_repository(
    source: GitSource,
    dest: Path,
    token: str | None,
    depth: int,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"Workspace not empty: {dest}")

    url = _authenticated_clone_url(source.clone_url_https, token)
    logger.info("Cloning %s into %s", source.clone_url_https, dest)
    repo = Repo.clone_from(url, dest, depth=depth, multi_options=[f"--depth={depth}"])

    if source.revision:
        try:
            repo.git.fetch("origin", source.revision, depth=depth)
        except Exception as e:
            logger.warning("Fetch revision %s failed (%s); continuing with default checkout", source.revision, e)
        try:
            repo.git.checkout(source.revision)
        except Exception:
            try:
                repo.git.checkout("FETCH_HEAD")
            except Exception as e2:
                logger.warning("Checkout %s failed: %s", source.revision, e2)

    return dest


def git_repo_summary(repo_path: Path, max_lines: int = 200) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_path), "log", "-n", "20", "--oneline"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        return f"(git log failed: {e})"
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["..."]
    return "\n".join(lines)


def create_branch_commit_push_pr(
    repo_path: Path,
    branch_name: str,
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    base_branch: str | None,
) -> str:
    """
    Commit all changes, push branch, open a pull request via GitHub REST API.
    """
    r = Repo(repo_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "fixer-agent")
        cw.set_value("user", "email", "fixer-agent@users.noreply.openshift.local")
    if r.is_dirty(untracked_files=True):
        r.git.checkout("-b", branch_name)
        r.git.add(all=True)
        r.git.commit("-m", "fix: address Tekton pipeline failure (fixer-agent)")
    else:
        return "(no local changes; skipping PR)"

    auth_url = _authenticated_clone_url(f"https://github.com/{owner}/{repo}.git", token)
    r.git.remote("set-url", "origin", auth_url)
    r.git.push("--set-upstream", "origin", branch_name)

    import json
    import urllib.error
    import urllib.request

    base = base_branch
    if not base:
        try:
            base = r.git.rev_parse("--abbrev-ref", "origin/HEAD").replace("origin/", "")
        except Exception:
            base = "main"

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    payload = {"title": title, "body": body, "head": branch_name, "base": base}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("html_url", data))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub PR API error {e.code}: {err_body}") from e
