from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from llama_stack_client import LlamaStackClient
from llama_stack_client.types.chat.completion_create_response import (
    ChoiceMessageOpenAIAssistantMessageParamOutput,
)

logger = logging.getLogger(__name__)

LOCAL_TOOL_NAMES = frozenset(
    {
        "workspace_read_file",
        "workspace_write_file",
        "workspace_list_files",
    }
)


def _tool_result_to_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), default=str)
    return str(result)


def _safe_rel_path(repo_root: Path, rel: str) -> Path:
    rel = rel.strip().lstrip("/").replace("\\", "/")
    if ".." in rel.split("/"):
        raise ValueError("invalid path")
    p = (repo_root / rel).resolve()
    root_r = repo_root.resolve()
    try:
        p.relative_to(root_r)
    except ValueError as e:
        raise ValueError("path outside workspace") from e
    return p


def _local_workspace_read(repo_root: Path, rel_path: str) -> str:
    p = _safe_rel_path(repo_root, rel_path)
    if not p.is_file():
        return f"(not a file: {rel_path})"
    return p.read_text(encoding="utf-8", errors="replace")


def _local_workspace_write(repo_root: Path, rel_path: str, content: str) -> str:
    p = _safe_rel_path(repo_root, rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"ok: wrote {rel_path} ({len(content)} chars)"


def _local_workspace_list(repo_root: Path, max_entries: int) -> str:
    files: list[str] = []
    for path in sorted(repo_root.rglob("*")):
        if path.is_file():
            try:
                rel = path.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            if rel.startswith(".git/"):
                continue
            files.append(rel)
            if len(files) >= max_entries:
                files.append("... truncated ...")
                break
    return "\n".join(files)


def build_openai_tools_from_defs(tool_defs: list[Any]) -> list[dict[str, Any]]:
    openai_tools: list[dict[str, Any]] = []
    for td in tool_defs:
        name = getattr(td, "name", None) or td.get("name")
        desc = getattr(td, "description", None) or td.get("description") or ""
        schema = getattr(td, "input_schema", None) or td.get("input_schema") or {
            "type": "object",
            "properties": {},
        }
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": schema,
                },
            }
        )
    return openai_tools


def local_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "workspace_list_files",
                "description": "List files under the cloned repository workspace (excluding .git).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_entries": {
                            "type": "integer",
                            "description": "Maximum file paths to return",
                            "default": 400,
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workspace_read_file",
                "description": "Read a UTF-8 text file from the workspace, relative to repo root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string", "description": "Path relative to repository root"},
                    },
                    "required": ["rel_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workspace_write_file",
                "description": "Write or overwrite a UTF-8 text file in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["rel_path", "content"],
                },
            },
        },
    ]


def collect_mcp_tool_definitions(
    client: LlamaStackClient,
    tool_group_ids: list[str],
) -> tuple[list[Any], dict[str, str]]:
    """
    Returns tool defs for chat and a map tool_name -> toolgroup_id for invoke_tool routing.
    """
    all_defs: list[Any] = []
    name_to_group: dict[str, str] = {}
    for gid in tool_group_ids:
        defs = client.tool_runtime.list_tools(tool_group_id=gid)
        for d in defs:
            n = d.name
            if n in name_to_group:
                logger.warning(
                    "Skipping duplicate MCP tool name %r (already from group %s, also in %s)",
                    n,
                    name_to_group[n],
                    gid,
                )
                continue
            name_to_group[n] = gid
            all_defs.append(d)
    return all_defs, name_to_group


def _assistant_to_message_dict(
    msg: ChoiceMessageOpenAIAssistantMessageParamOutput | Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant"}
    if msg.content is not None:
        if isinstance(msg.content, str):
            out["content"] = msg.content
        else:
            parts = []
            for block in msg.content:
                if getattr(block, "text", None):
                    parts.append(block.text)
            out["content"] = "\n".join(parts) if parts else None
    else:
        out["content"] = None

    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name if tc.function else None,
                    "arguments": tc.function.arguments if tc.function else "{}",
                },
            }
            for tc in msg.tool_calls
        ]
    return out


def run_tool_assisted_fix(
    client: LlamaStackClient,
    model_id: str,
    tool_group_ids: list[str],
    repo_root: Path,
    system_prompt: str,
    user_prompt: str,
    max_iterations: int,
) -> str:
    mcp_defs, name_to_group = collect_mcp_tool_definitions(client, tool_group_ids)
    openai_tools = build_openai_tools_from_defs(mcp_defs) + local_tool_definitions()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    dispatch_local: dict[str, Callable[..., str]] = {
        "workspace_read_file": lambda **kw: _local_workspace_read(repo_root, kw["rel_path"]),
        "workspace_write_file": lambda **kw: _local_workspace_write(
            repo_root, kw["rel_path"], kw["content"]
        ),
        "workspace_list_files": lambda **kw: _local_workspace_list(
            repo_root, int(kw.get("max_entries") or 400)
        ),
    }

    last_text = ""
    for i in range(max_iterations):
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            temperature=0.2,
        )
        choice = resp.choices[0]
        msg = choice.message
        tcalls = getattr(msg, "tool_calls", None) or []

        if not tcalls:
            c = getattr(msg, "content", None)
            if isinstance(c, str):
                last_text = c
            elif c is None:
                last_text = ""
            else:
                parts = [getattr(b, "text", "") for b in c if getattr(b, "text", None)]
                last_text = "\n".join(parts)
            break

        messages.append(_assistant_to_message_dict(msg))

        for tc in tcalls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            fname = fn.name or ""
            raw_args = fn.arguments or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}

            if fname in LOCAL_TOOL_NAMES:
                try:
                    fn = dispatch_local[fname]
                    result_text = fn(**args)
                except Exception as e:
                    result_text = f"error: {e}"
            else:
                tg = name_to_group.get(fname)
                if not tg:
                    result_text = f"unknown tool {fname!r} (not in MCP tool groups)"
                else:
                    try:
                        inv = client.tool_runtime.invoke_tool(tool_name=fname, kwargs=args)
                        if inv.error_message:
                            result_text = f"error: {inv.error_message}"
                        else:
                            result_text = _tool_result_to_text(inv.content)
                    except Exception as e:
                        result_text = f"invoke error: {e}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id or "",
                    "content": result_text,
                }
            )

        logger.debug("LLM iteration %s completed", i + 1)
    else:
        last_text = last_text or "(max iterations reached)"

    return last_text
