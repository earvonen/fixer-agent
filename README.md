# fixer-agent

Python service for **OpenShift** that watches **Tekton** `PipelineRun` objects, gathers failure context (status, TaskRuns, pod logs), clones the linked GitHub repository, and drives a **Llama Stack** model with **MCP tools** (Kubernetes, GitHub) plus local workspace file tools. The model is expected to fix the failure and open a **pull request** via the **GitHub MCP** (authentication lives on the MCP server, not in this pod by default).

## What it does

1. Polls the Kubernetes API for **failed** `PipelineRun`s (`tekton.dev/v1`, condition `Succeeded=False`) in a configured namespace **that reference a specific Tekton `Pipeline`** (`spec.pipelineRef.name` must equal `FIXER_PIPELINE_NAME`; optional namespace constraint via `FIXER_PIPELINE_NAMESPACE`).
2. Loads related **TaskRuns** and **pod logs** for failing work.
3. Resolves the Git clone URL from **`spec.params`** (`git-url`, `git-revision`) when present, else from [Pipelines-as-Code](https://pipelinesascode.com/) annotations or `github.com` URLs on the object (see below).
4. **Clones** the repo into a workspace (unauthenticated HTTPS when `GITHUB_TOKEN` is unset — suitable for **public** repos).
5. Calls **Llama Stack** (`chat.completions` + `tool_runtime`) with:
   - MCP tools from the tool groups you configure (`list_tools` / `invoke_tool`).
   - Built-in **local** tools: `workspace_list_files`, `workspace_read_file`, `workspace_write_file` (paths restricted to the clone).
6. Optionally uses the **GitHub REST API** from this process to commit, push, and open a PR **only if** `GITHUB_TOKEN` is set; otherwise PR creation is left to the **GitHub MCP**.

Processed runs are recorded in a **JSON state file** keyed by PipelineRun UID so the same failure is not handled twice.

## Requirements

- **Kubernetes / OpenShift** with Tekton (`PipelineRun` / `TaskRun` CRDs, typically `tekton.dev/v1`).
- A reachable **Llama Stack** HTTP endpoint and a **model** id (or rely on the first model returned by the stack).
- **MCP tool groups** registered with that stack (e.g. Kubernetes + GitHub), with IDs that match `FIXER_TOOL_GROUP_IDS`.
- **RBAC** for the pod’s `ServiceAccount`: `get/list/watch` on `pipelineruns`, `taskruns`, `pods`, and `pods/log` in the namespace you watch (see `deploy/openshift.yaml`).

## Install and run (local)

```bash
cd fixer-agent
pip install .
export FIXER_WATCH_NAMESPACE=agentic-demo
export FIXER_PIPELINE_NAME=redhat-screensaver-build
export LLAMA_STACK_BASE_URL=http://llamastack-service:8321
export FIXER_TOOL_GROUP_IDS=mcp-kubernetes,mcp-github
# Optional: .env file is also loaded (pydantic-settings)
fixer-agent
# or: python -m fixer_agent
```

Use a valid kubeconfig (or run inside the cluster with in-cluster config).

## Container image

Build with the included **Containerfile** (or **Dockerfile**):

```bash
podman build -f Containerfile -t fixer-agent:latest .
```

The image installs `git` (required for clone and local repo operations).

### Build on OpenShift with Tekton (ImageStream + Pipeline)

Same layout as the `tekton/` folder in **redhat-screensaver**: local **Tasks** (Alpine **git** clone + **Kaniko** build/push, non-privileged), one **Pipeline**, and examples. No Tekton Hub resolver required.

| File | Purpose |
|------|---------|
| [`deploy/imagestream.yaml`](deploy/imagestream.yaml) | `ImageStream` `fixer-agent` |
| [`deploy/tekton/task-git-clone.yaml`](deploy/tekton/task-git-clone.yaml) | Task `fixer-agent-git-clone` |
| [`deploy/tekton/task-build-push.yaml`](deploy/tekton/task-build-push.yaml) | Task `fixer-agent-build-push` (Kaniko + SA token auth) |
| [`deploy/tekton/pipeline.yaml`](deploy/tekton/pipeline.yaml) | Pipeline `fixer-agent-build` |
| [`deploy/tekton/pipelinerun.example.yaml`](deploy/tekton/pipelinerun.example.yaml) | Example `PipelineRun` (`emptyDir` workspace) |
| [`deploy/tekton/pvc.example.yaml`](deploy/tekton/pvc.example.yaml) | Optional PVC for a persistent workspace |

```bash
oc apply -f deploy/imagestream.yaml
oc apply -f deploy/tekton/task-git-clone.yaml
oc apply -f deploy/tekton/task-build-push.yaml
oc apply -f deploy/tekton/pipeline.yaml
oc create -f deploy/tekton/pipelinerun.example.yaml
```

Grant the **`pipeline`** ServiceAccount permission to push (same as screensaver), e.g.  
`oc policy add-role-to-user system:image-pusher system:serviceaccount:YOUR_NS:pipeline -n YOUR_NS`

Default Git URL is [https://github.com/earvonen/fixer-agent.git](https://github.com/earvonen/fixer-agent.git); override with Pipeline / PipelineRun params `git-url` and `git-revision`. Image push target uses `$(context.pipelineRun.namespace)` for the registry path segment (requires a recent Tekton version).

## OpenShift deployment

Example manifests: [`deploy/openshift.yaml`](deploy/openshift.yaml). The Deployment does **not** set `runAsUser` / `fsGroup` so **`restricted-v2`** can assign a UID from the namespace range (hardcoding UID `1000` is rejected on many clusters). The **Containerfile** uses `chgrp 0` + `chmod g=u` on `/app` so the process can run as that arbitrary UID with root group. **`HOME`** is set to the mounted data volume for git and caches.

Adjust:

- **Image** reference and **namespace** names.
- **`FIXER_WATCH_NAMESPACE`** if pipelines run in another namespace than the Deployment (you may need a **Role + RoleBinding in that namespace** binding the same `ServiceAccount`).
- **`LLAMA_STACK_BASE_URL`** to your Llama Stack **Service** URL.
- **`FIXER_TOOL_GROUP_IDS`** to the exact tool group IDs your distribution registers.
- **`FIXER_PIPELINE_NAME`** (and optionally **`FIXER_PIPELINE_NAMESPACE`**) so only runs started from that `Pipeline` are considered.

GitHub credentials are **not** required in the Deployment if the **GitHub MCP** is already configured on Llama Stack with a PAT.

## Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FIXER_WATCH_NAMESPACE` | **Yes** | — | Namespace to list/watch for Tekton `PipelineRun`s. |
| `FIXER_PIPELINE_NAME` | **Yes** | — | Only process runs whose `spec.pipelineRef.name` matches this Tekton `Pipeline` name. |
| `FIXER_PIPELINE_NAMESPACE` | No | — | If set, the resolved Pipeline namespace (`pipelineRef.namespace`, or the `PipelineRun` namespace if omitted) must match. |
| `LLAMA_STACK_BASE_URL` | **Yes** | — | Base URL of Llama Stack (e.g. `http://llama-stack:8321`). |
| `FIXER_TOOL_GROUP_IDS` | **Yes** | — | Comma-separated tool group IDs (e.g. `mcp::kubernetes,mcp::github`). |
| `FIXER_POLL_INTERVAL_SECONDS` | No | `120` | Sleep between poll loops. |
| `FIXER_MAX_COMPLETION_AGE_SECONDS` | No | — | If set, only failed runs with `status.completionTime` within this many seconds (UTC) are considered; older runs and runs without `completionTime` are skipped. |
| `FIXER_STATE_FILE` | No | `/tmp/fixer-agent-state.json` | JSON file of processed PipelineRun UIDs. |
| `FIXER_WORKSPACE_ROOT` | No | `/tmp/fixer-workspaces` | Parent directory for per-run clone directories. |
| `LLAMA_STACK_MODEL_ID` | No | — | Model id; if unset, the **first** model from `GET /models` is used. |
| `LLAMA_STACK_API_KEY` | No | — | Bearer token for Llama Stack (if your stack requires it). |
| `LLAMA_STACK_CLIENT_API_KEY` | No | — | Alternative env name supported by `llama-stack-client` if `api_key` is not passed explicitly. |
| `FIXER_MCP_REGISTRATIONS_JSON` | No | — | JSON array to register MCP SSE endpoints at startup (see below). |
| `GITHUB_TOKEN` | No | — | If set: clone private repos via HTTPS and/or open PR via GitHub REST API from this pod. If unset: public clone only; use **GitHub MCP** for PRs. |
| `FIXER_GIT_CLONE_DEPTH` | No | `50` | Shallow clone depth. |
| `FIXER_MAX_LLM_ITERATIONS` | No | `40` | Max chat completion rounds (tool loops). |
| `FIXER_LOG_TRUNCATE_BYTES` | No | `65536` | Approximate cap for collected pod logs. |
| `FIXER_PR_YAML_MAX_BYTES` | No | `131072` | Max size of embedded PipelineRun YAML in the prompt. |
| `FIXER_PR_BRANCH_PREFIX` | No | `fixer-agent` | Suggested branch prefix for the model / REST PR path. |
| `FIXER_DRY_RUN_NO_PR` | No | `false` | If `true`, skip all PR creation paths after the model run. |

Optional `.env` in the working directory is loaded automatically (`pydantic-settings`).

### Registering MCP servers at startup

If your stack does not persist tool group registration, you can pass:

```json
[{"toolgroup_id":"mcp::github","provider_id":"model-context-protocol","mcp_uri":"http://github-mcp:8080/sse"}]
```

as **`FIXER_MCP_REGISTRATIONS_JSON`** (single-line string in Kubernetes `ConfigMap`). Registration failures are logged (often harmless if the group already exists).

## PipelineRun → repository mapping

**Primary:** `spec.params` on the `PipelineRun` (same parameter names as **`fixer-agent-build`**):

| Param | Role |
|-------|------|
| `git-url` | Clone URL (`https://…`, `git@github.com:…`, etc.) |
| `git-revision` | Ref to check out **and** GitHub PR **merge base** (`base`) — same value, no extra Pipeline params. |

If `git-url` is set, it takes precedence. Owner/repo for GitHub REST PRs are parsed from the URL (GitHub https / `git@github.com`, or the last two path segments for other hosts). If `git-revision` is missing when using params, the in-app PR path falls back to the clone’s default branch (`origin/HEAD` / `main`).

**Fallback:** **Pipelines-as-Code** annotations (`url-org`, `url-repository`, `sha`, `source-branch`, `original-pr-url`) and any `https://github.com/org/repo` string in annotations.

If nothing resolves, the run is skipped and recorded in state with `reason: no_git_metadata`.

## GitHub: MCP vs `GITHUB_TOKEN`

- **Default:** No PAT in this app. **GitHub MCP** (configured on Llama Stack) carries the PAT for API operations the model performs. The app still **clones** with plain HTTPS; that only works for **public** repositories unless you set **`GITHUB_TOKEN`**.
- **With `GITHUB_TOKEN`:** You get authenticated **clone** (private repos) and an optional **in-process** path: commit, push, and open a PR via the GitHub REST API (see `git_repo.py`).

## State file

`FIXER_STATE_FILE` stores JSON like:

```json
{
  "processed_runs": {
    "<pipelinerun-uid>": {
      "pipelinerun": "name",
      "repository": "org/repo",
      "pull_request": "https://github.com/.../pull/1",
      "pr_via": "github_mcp | github_rest | dry_run"
    }
  }
}
```

Delete an entry or the file to re-process a run. On OpenShift, mount a **PersistentVolumeClaim** on `/var/lib/fixer-agent` if you need state across pod restarts (the sample manifest uses `emptyDir`).

## Project layout

| Path | Purpose |
|------|---------|
| `src/fixer_agent/main.py` | Poll loop, orchestration |
| `src/fixer_agent/k8s_tekton.py` | List failed runs, TaskRuns, logs |
| `src/fixer_agent/git_repo.py` | Repo discovery, clone, optional REST PR |
| `src/fixer_agent/llama_tools.py` | Llama Stack tool loop (MCP + workspace tools) |
| `src/fixer_agent/config.py` | Settings / env parsing |
| `src/fixer_agent/state_store.py` | Processed UID persistence |
| `deploy/openshift.yaml` | Example SA, Role, ConfigMap, Deployment |

## Limitations

- Only `PipelineRun`s with a **`spec.pipelineRef.name`** matching **`FIXER_PIPELINE_NAME`** are eligible. **Inline `pipelineSpec`** (no `pipelineRef.name`) and **remote resolver** refs without that field are ignored.
- Targets **`tekton.dev/v1`** `PipelineRun`s only; clusters still on `v1beta1` only may need API version adjustments.
- **Duplicate MCP tool names** across tool groups are not supported (the second definition is skipped).
- Heavy failures or huge logs are **truncated** to keep prompts bounded.

## License

See repository root for license terms if present.
