from __future__ import annotations

import logging
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from git import Repo

logger = logging.getLogger(__name__)

_GITHUB_HTTPS = re.compile(
    r"https?://(?:[^@]+@)?github\.com/([^/]+)/([^/.]+)(?:\.git)?",
    re.I,
)
_GITHUB_SSH = re.compile(
    r"git@github\.com:([^/]+)/([^/.]+)(?:\.git)?\s*$",
    re.I,
)


@dataclass(frozen=True)
class GitSource:
    owner: str
    repo: str
    clone_url: str
    revision: str | None
    default_branch_hint: str | None


def _spec_param(pr: dict[str, Any], key: str) -> str | None:
    """Read a string value from PipelineRun spec.params by name."""
    spec = pr.get("spec") or {}
    for p in spec.get("params") or []:
        if not isinstance(p, dict) or p.get("name") != key:
            continue
        v = p.get("value")
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return str(v).strip() or None
    return None


def _owner_repo_from_clone_url(clone_url: str) -> tuple[str, str] | None:
    """Best-effort owner/repo for GitHub PRs and logging; supports https and git@ GitHub, else URL path tail."""
    u = clone_url.strip()
    m = _GITHUB_HTTPS.search(u)
    if m:
        return m.group(1), m.group(2)
    m = _GITHUB_SSH.match(u)
    if m:
        return m.group(1), m.group(2)
    if u.startswith("git@"):
        tail = u.split(":", 1)[-1]
        parts = tail.replace(".git", "").strip("/").split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None
    path = urlparse(u).path.strip("/")
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 2:
        return parts[-2], parts[-1].removesuffix(".git")
    return None


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
    Resolve clone URL and revision from the PipelineRun.

    Primary: ``spec.params`` entries ``git-url`` and ``git-revision`` (same names as the
    fixer-agent-build Pipeline).

    Fallback: Pipelines-as-Code annotations and GitHub URLs in annotations.
    """
    git_url = _spec_param(pipelinerun, "git-url")
    git_revision = _spec_param(pipelinerun, "git-revision")
    if git_url:
        parsed = _owner_repo_from_clone_url(git_url)
        if not parsed:
            logger.warning("Could not parse owner/repo from git-url param: %s", git_url)
            return None
        owner, repo = parsed
        return GitSource(
            owner=owner,
            repo=repo,
            clone_url=git_url,
            revision=git_revision,
            default_branch_hint=git_revision,
        )

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
            clone_url=clone_url,
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
                    clone_url=f"https://github.com/{o}/{r}.git",
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
                clone_url=f"https://github.com/{o}/{r}.git",
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

    url = _authenticated_clone_url(source.clone_url, token)
    logger.info("Cloning %s into %s", source.clone_url, dest)
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
