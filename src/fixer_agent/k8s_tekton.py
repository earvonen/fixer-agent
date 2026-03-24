from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

TEKTON_GROUP = "tekton.dev"
PIPELINERUN_VERSION = "v1"
PIPELINERUN_PLURAL = "pipelineruns"
TASKRUN_PLURAL = "taskruns"


@dataclass
class IncidentContext:
    namespace: str
    pipelinerun_name: str
    pipelinerun_uid: str
    pipelinerun_yaml: str
    failure_summary: str
    taskrun_summaries: list[str]
    pod_logs: list[str] = field(default_factory=list)


def load_kube_config() -> None:
    try:
        config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes configuration")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Using local kubeconfig")


def _is_pipelinerun_failed(obj: dict[str, Any]) -> bool:
    status = obj.get("status") or {}
    conditions = status.get("conditions") or []
    for c in conditions:
        if c.get("type") != "Succeeded":
            continue
        if c.get("status") == "False":
            return True
        if str(c.get("reason", "")).lower() == "failed":
            return True
    return False


def _pipeline_ref_name(pr: dict[str, Any]) -> str | None:
    spec = pr.get("spec") or {}
    ref = spec.get("pipelineRef") or {}
    n = ref.get("name")
    return str(n).strip() if n else None


def _resolved_pipeline_namespace(pr: dict[str, Any]) -> str:
    """Namespace where the referenced Pipeline CR lives (Tekton default: same as PipelineRun)."""
    spec = pr.get("spec") or {}
    ref = spec.get("pipelineRef") or {}
    ref_ns = ref.get("namespace")
    if ref_ns:
        return str(ref_ns)
    return str((pr.get("metadata") or {}).get("namespace") or "")


def pipelinerun_matches_configured_pipeline(
    pr: dict[str, Any],
    pipeline_name: str,
    pipeline_namespace: str | None,
) -> bool:
    """
    True if this run was started from the given Tekton Pipeline (pipelineRef.name).
    Inline-only spec.pipelineSpec (no pipelineRef.name) does not match.
    """
    ref_name = _pipeline_ref_name(pr)
    if not ref_name or ref_name != pipeline_name:
        return False
    if pipeline_namespace is not None:
        if _resolved_pipeline_namespace(pr) != pipeline_namespace:
            return False
    return True


def _parse_k8s_timestamp(iso: str) -> datetime | None:
    """Parse Kubernetes API RFC3339 timestamps to timezone-aware UTC."""
    s = iso.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        logger.warning("Unparseable timestamp: %r", iso)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def pipelinerun_completion_within_max_age(
    pr: dict[str, Any],
    max_age_seconds: int | None,
) -> bool:
    """
    If max_age_seconds is set, require status.completionTime within the last max_age_seconds (UTC).
    Runs without completionTime are excluded when the filter is active.
    """
    if max_age_seconds is None:
        return True
    raw = (pr.get("status") or {}).get("completionTime")
    if not raw:
        return False
    completed = _parse_k8s_timestamp(str(raw))
    if completed is None:
        return False
    now = datetime.now(timezone.utc)
    age_sec = (now - completed).total_seconds()
    if age_sec < 0:
        return True
    return age_sec <= max_age_seconds


def _failure_message(obj: dict[str, Any]) -> str:
    status = obj.get("status") or {}
    conditions = status.get("conditions") or []
    parts: list[str] = []
    for c in conditions:
        parts.append(
            yaml.safe_dump(
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "message": c.get("message"),
                },
                default_flow_style=False,
            ).strip()
        )
    return "\n".join(parts) if parts else "(no conditions)"


def list_failed_pipelineruns(
    namespace: str,
    pipeline_name: str,
    pipeline_namespace: str | None = None,
    max_completion_age_seconds: int | None = None,
) -> list[dict[str, Any]]:
    load_kube_config()
    api = client.CustomObjectsApi()
    resp = api.list_namespaced_custom_object(
        TEKTON_GROUP,
        PIPELINERUN_VERSION,
        namespace,
        PIPELINERUN_PLURAL,
    )
    items: list[dict[str, Any]] = resp.get("items") or []
    failed = [item for item in items if _is_pipelinerun_failed(item)]
    matched = [
        item
        for item in failed
        if pipelinerun_matches_configured_pipeline(item, pipeline_name, pipeline_namespace)
    ]
    if len(matched) < len(failed):
        logger.debug(
            "Filtered failed PipelineRuns: %s match pipeline %r (namespace filter: %s), %s ignored",
            len(matched),
            pipeline_name,
            pipeline_namespace or "any",
            len(failed) - len(matched),
        )
    windowed = [
        item for item in matched if pipelinerun_completion_within_max_age(item, max_completion_age_seconds)
    ]
    if max_completion_age_seconds is not None and len(windowed) < len(matched):
        logger.debug(
            "Completion-time window (%ss): %s of %s failed runs still eligible",
            max_completion_age_seconds,
            len(windowed),
            len(matched),
        )
    return windowed


def _list_taskruns_for_pipelinerun(namespace: str, pr_name: str) -> list[dict[str, Any]]:
    api = client.CustomObjectsApi()
    try:
        resp = api.list_namespaced_custom_object(
            TEKTON_GROUP,
            PIPELINERUN_VERSION,
            namespace,
            TASKRUN_PLURAL,
            label_selector=f"tekton.dev/pipelineRun={pr_name}",
        )
    except ApiException as e:
        logger.warning("Could not list TaskRuns for %s/%s: %s", namespace, pr_name, e)
        return []
    return list(resp.get("items") or [])


def _taskrun_brief(tr: dict[str, Any]) -> str:
    md = tr.get("metadata") or {}
    name = md.get("name", "?")
    status = tr.get("status") or {}
    conditions = status.get("conditions") or []
    cond_txt = yaml.safe_dump(conditions, default_flow_style=False).strip()
    pod = status.get("podName")
    return f"### TaskRun {name}\npodName: {pod}\nconditions:\n{cond_txt}"


def _read_pod_logs(namespace: str, pod_name: str, max_bytes: int) -> str:
    load_kube_config()
    v1 = client.CoreV1Api()
    try:
        raw = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=5000,
        )
    except ApiException as e:
        return f"(failed to read logs for pod {pod_name}: {e})"
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > max_bytes:
        raw = raw.encode("utf-8")[-max_bytes:].decode("utf-8", errors="replace")
        raw = "--- truncated ---\n" + raw
    return raw


def build_incident_context(
    namespace: str,
    pipelinerun: dict[str, Any],
    log_truncate_bytes: int,
    yaml_max_bytes: int,
) -> IncidentContext:
    md = pipelinerun.get("metadata") or {}
    name = md.get("name", "unknown")
    uid = md.get("uid", name)
    pr_yaml = yaml.safe_dump(pipelinerun, default_flow_style=False)
    if len(pr_yaml.encode("utf-8")) > yaml_max_bytes:
        pr_yaml = pr_yaml.encode("utf-8")[:yaml_max_bytes].decode("utf-8", errors="replace") + "\n--- yaml truncated ---\n"

    taskruns = _list_taskruns_for_pipelinerun(namespace, name)
    summaries = [_taskrun_brief(tr) for tr in taskruns]

    logs: list[str] = []
    per_pod_budget = max(log_truncate_bytes // max(len(taskruns), 1), 8192)
    for tr in taskruns:
        st = tr.get("status") or {}
        pod_name = st.get("podName")
        if not pod_name:
            continue
        tr_md = tr.get("metadata") or {}
        tr_name = tr_md.get("name", pod_name)
        body = _read_pod_logs(namespace, pod_name, per_pod_budget)
        logs.append(f"### Logs for TaskRun {tr_name} (pod {pod_name})\n{body}")

    return IncidentContext(
        namespace=namespace,
        pipelinerun_name=name,
        pipelinerun_uid=uid,
        pipelinerun_yaml=pr_yaml,
        failure_summary=_failure_message(pipelinerun),
        taskrun_summaries=summaries,
        pod_logs=logs,
    )
