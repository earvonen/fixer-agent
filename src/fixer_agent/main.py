from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

from llama_stack_client import LlamaStackClient

from fixer_agent.config import Settings
from fixer_agent.git_repo import (
    clone_repository,
    create_branch_commit_push_pr,
    discover_git_source,
    git_repo_summary,
)
from fixer_agent.k8s_tekton import build_incident_context, list_failed_pipelineruns, load_kube_config
from fixer_agent.llama_tools import run_tool_assisted_fix
from fixer_agent.state_store import StateStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert software engineer running inside an OpenShift CI repair agent.

You are given a failed Tekton PipelineRun, TaskRun details, pod logs, and a local Git clone of the repository
that produced the pipeline. Your goals:
1. Identify the root cause of the failure using logs and the codebase.
2. Apply minimal, correct fixes using the workspace_* tools (read/list/write files under the repo).
3. Use the Kubernetes MCP tools when they help (extra cluster context).
4. Use the GitHub MCP tools to publish your fix: create a branch, push commits, and open a pull request.
   GitHub authentication is handled by the GitHub MCP server, not by this application.

Constraints:
- Prefer small, reviewable changes; do not refactor unrelated code.
- Do not commit secrets or credentials.
- After you believe the fix is complete, summarize what you changed and why in plain language.
"""


def _register_mcp_endpoints(client: LlamaStackClient, settings: Settings) -> None:
    for reg in settings.parsed_mcp_registrations():
        try:
            client.toolgroups.register(
                toolgroup_id=reg.toolgroup_id,
                provider_id=reg.provider_id,
                mcp_endpoint={"uri": reg.mcp_uri},
            )
            logger.info("Registered MCP toolgroup %s", reg.toolgroup_id)
        except Exception as e:
            logger.warning(
                "Could not register MCP toolgroup %s (may already exist): %s",
                reg.toolgroup_id,
                e,
            )


def _resolve_model_id(client: LlamaStackClient, configured: str | None) -> str:
    if configured:
        return configured
    models = client.models.list()
    if not models:
        raise RuntimeError("LLAMA_STACK_MODEL_ID is unset and Llama Stack returned no models")
    mid = models[0].id
    logger.info("Using first available Llama Stack model: %s", mid)
    return mid


def _build_user_prompt(
    ctx,
    git_summary: str,
    repo_path: Path,
    branch_hint: str,
    pr_merge_base_branch: str | None,
) -> str:
    pr_base = pr_merge_base_branch or "(PipelineRun spec.params git-revision)"
    logs_blob = "\n\n".join(ctx.pod_logs) if ctx.pod_logs else "(no pod logs collected)"
    tasks_blob = "\n\n".join(ctx.taskrun_summaries) if ctx.taskrun_summaries else "(no taskruns)"
    return f"""## PipelineRun
Namespace: {ctx.namespace}
Name: {ctx.pipelinerun_name}
UID: {ctx.pipelinerun_uid}

### Status / conditions
{ctx.failure_summary}

### TaskRuns
{tasks_blob}

### Pod logs
{logs_blob}

### PipelineRun object (YAML)
```yaml
{ctx.pipelinerun_yaml}
```

## Local repository
Path on disk: {repo_path}
Recent commits:
```
{git_summary}
```

Use workspace_list_files then workspace_read_file / workspace_write_file to inspect and fix the project.
When your changes are ready, use the GitHub MCP tools to open a pull request (suggested head branch name:
`{branch_hint}`). The pull request **must** target merge base branch **`{pr_base}`** (the branch this
PipelineRun built from; do not assume `main`).
Then write a short summary of the root cause and the fix (include the PR link if you have it).
"""


def process_failed_run(
    settings: Settings,
    state: StateStore,
    client: LlamaStackClient,
    model_id: str,
    pr_obj: dict,
) -> None:
    ctx = build_incident_context(
        settings.kubernetes_namespace,
        pr_obj,
        log_truncate_bytes=settings.log_truncate_bytes,
        yaml_max_bytes=settings.pipelinerun_yaml_max_bytes,
    )
    if state.is_processed(ctx.pipelinerun_uid):
        return

    src = discover_git_source(pr_obj)
    if not src:
        logger.warning(
            "PipelineRun %s/%s: could not resolve GitHub repo from annotations; skipping",
            ctx.namespace,
            ctx.pipelinerun_name,
        )
        state.mark_processed(
            ctx.pipelinerun_uid,
            {"reason": "no_git_metadata", "pipelinerun": ctx.pipelinerun_name},
        )
        return

    ws = Path(settings.workspace_root) / ctx.pipelinerun_uid
    if ws.exists():
        shutil.rmtree(ws)

    try:
        clone_repository(src, ws, settings.github_token, settings.git_clone_depth)
    except Exception as e:
        logger.exception(
            "Clone failed for %s/%s: %s. For private repos set GITHUB_TOKEN; otherwise ensure the repo is public.",
            src.owner,
            src.repo,
            e,
        )
        return

    summary = git_repo_summary(ws)
    branch_hint = f"{settings.pr_branch_prefix}/{ctx.pipelinerun_name}"[:250]
    user_prompt = _build_user_prompt(ctx, summary, ws, branch_hint, src.default_branch_hint)

    logger.info(
        "Invoking Llama Stack (model=%s) for PipelineRun %s/%s",
        model_id,
        ctx.namespace,
        ctx.pipelinerun_name,
    )
    try:
        llm_summary = run_tool_assisted_fix(
            client=client,
            model_id=model_id,
            tool_group_ids=settings.tool_group_id_list,
            repo_root=ws,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_iterations=settings.max_llm_iterations,
        )
    except Exception:
        logger.exception("Llama Stack run failed for %s/%s", ctx.namespace, ctx.pipelinerun_name)
        return

    logger.info("Model finished with summary (excerpt): %s", llm_summary[:2000])

    pr_url: str | None = None
    pr_via = "github_mcp"
    if settings.dry_run_no_pr:
        logger.info("FIXER_DRY_RUN_NO_PR set; skipping PR creation")
        pr_via = "dry_run"
    elif settings.github_token:
        branch = branch_hint
        pr_title = f"fix(ci): Tekton failure for {ctx.pipelinerun_name}"
        pr_body = (
            f"Automated fix proposal from **fixer-agent**.\n\n"
            f"PipelineRun: `{ctx.namespace}/{ctx.pipelinerun_name}` (UID `{ctx.pipelinerun_uid}`)\n\n"
            f"### Model summary\n\n{llm_summary}\n"
        )
        try:
            pr_url = create_branch_commit_push_pr(
                ws,
                branch_name=branch,
                token=settings.github_token,
                owner=src.owner,
                repo=src.repo,
                title=pr_title,
                body=pr_body,
                base_branch=src.default_branch_hint,
            )
        except Exception:
            logger.exception("Failed to create pull request for %s/%s", src.owner, src.repo)
            return
        pr_via = "github_rest"
    else:
        logger.info(
            "GITHUB_TOKEN unset: skipping in-app PR/push; expecting GitHub MCP to have opened a PR if needed"
        )

    logger.info("Pull request result: %s", pr_url or "(none from app; see GitHub MCP / model summary)")
    state.mark_processed(
        ctx.pipelinerun_uid,
        {
            "pipelinerun": ctx.pipelinerun_name,
            "repository": f"{src.owner}/{src.repo}",
            "pull_request": pr_url,
            "pr_via": pr_via,
        },
    )


def run_forever(settings: Settings, state: StateStore) -> None:
    load_kube_config()
    client = LlamaStackClient(
        base_url=settings.llama_stack_base_url,
        api_key=settings.llama_stack_api_key,
        timeout=600.0,
    )
    _register_mcp_endpoints(client, settings)
    model_id = _resolve_model_id(client, settings.llama_stack_model_id)

    while True:
        try:
            failed = list_failed_pipelineruns(
                settings.kubernetes_namespace,
                settings.pipeline_name,
                settings.pipeline_namespace,
                max_completion_age_seconds=settings.max_completion_age_seconds,
            )
            if failed:
                logger.info("Found %s failed PipelineRun(s) in %s", len(failed), settings.kubernetes_namespace)
            for pr_obj in failed:
                process_failed_run(settings, state, client, model_id, pr_obj)
        except Exception:
            logger.exception("Poll iteration failed")

        time.sleep(settings.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings()
    state = StateStore(settings.state_file_path)
    run_forever(settings, state)


if __name__ == "__main__":
    main()
