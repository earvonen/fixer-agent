from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class McpRegistration:
    """Optional MCP registration applied at startup (Llama Stack toolgroups.register)."""

    toolgroup_id: str
    provider_id: str
    mcp_uri: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kubernetes_namespace: str = Field(
        ...,
        description="Namespace to watch for Tekton PipelineRuns",
        validation_alias="FIXER_WATCH_NAMESPACE",
    )
    pipeline_name: str = Field(
        ...,
        description="Only failed PipelineRuns that reference this Tekton Pipeline (spec.pipelineRef.name)",
        validation_alias="FIXER_PIPELINE_NAME",
    )
    pipeline_namespace: str | None = Field(
        None,
        description="If set, Pipeline must resolve in this namespace "
        "(spec.pipelineRef.namespace, else PipelineRun namespace)",
        validation_alias="FIXER_PIPELINE_NAMESPACE",
    )
    poll_interval_seconds: int = Field(120, validation_alias="FIXER_POLL_INTERVAL_SECONDS")
    state_file_path: str = Field("/tmp/fixer-agent-state.json", validation_alias="FIXER_STATE_FILE")

    llama_stack_base_url: str = Field(..., validation_alias="LLAMA_STACK_BASE_URL")
    llama_stack_api_key: str | None = Field(None, validation_alias="LLAMA_STACK_API_KEY")
    llama_stack_model_id: str | None = Field(None, validation_alias="LLAMA_STACK_MODEL_ID")

    tool_group_ids: str = Field(
        ...,
        description="Comma-separated Llama Stack tool group IDs (e.g. mcp::k8s,mcp::github)",
        validation_alias="FIXER_TOOL_GROUP_IDS",
    )

    mcp_registrations_json: str | None = Field(
        None,
        validation_alias="FIXER_MCP_REGISTRATIONS_JSON",
        description='Optional JSON list: [{"toolgroup_id":"mcp::x","provider_id":"model-context-protocol","mcp_uri":"http://host/sse"}]',
    )

    github_token: str | None = Field(
        None,
        validation_alias="GITHUB_TOKEN",
        description="Optional: HTTPS clone of private repos + REST API PR fallback. "
        "If unset, use public clone only and rely on GitHub MCP (its own PAT) for PRs.",
    )
    git_clone_depth: int = Field(50, validation_alias="FIXER_GIT_CLONE_DEPTH")
    workspace_root: str = Field("/tmp/fixer-workspaces", validation_alias="FIXER_WORKSPACE_ROOT")

    max_llm_iterations: int = Field(40, validation_alias="FIXER_MAX_LLM_ITERATIONS")
    log_truncate_bytes: int = Field(65536, validation_alias="FIXER_LOG_TRUNCATE_BYTES")
    pipelinerun_yaml_max_bytes: int = Field(131072, validation_alias="FIXER_PR_YAML_MAX_BYTES")

    pr_branch_prefix: str = Field("fixer-agent", validation_alias="FIXER_PR_BRANCH_PREFIX")
    dry_run_no_pr: bool = Field(False, validation_alias="FIXER_DRY_RUN_NO_PR")

    @property
    def tool_group_id_list(self) -> list[str]:
        return [x.strip() for x in self.tool_group_ids.split(",") if x.strip()]

    def parsed_mcp_registrations(self) -> list[McpRegistration]:
        if not self.mcp_registrations_json:
            return []
        raw: list[Any] = json.loads(self.mcp_registrations_json)
        out: list[McpRegistration] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("FIXER_MCP_REGISTRATIONS_JSON must be a JSON list of objects")
            out.append(
                McpRegistration(
                    toolgroup_id=str(item["toolgroup_id"]),
                    provider_id=str(item.get("provider_id") or "model-context-protocol"),
                    mcp_uri=str(item["mcp_uri"]),
                )
            )
        return out

    @field_validator("poll_interval_seconds", "git_clone_depth", "max_llm_iterations")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v
