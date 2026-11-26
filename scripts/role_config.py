#!/usr/bin/env python3
"""统一角色配置加载、参数翻译、无头调用适配与 herdr/config.yaml 同步工具。

唯一真相源：~/.hwf/roles.yaml（支持环境变量 HWF_ROLES_CONFIG 覆盖）。
用法：
    python3 ~/.hwf/scripts/role_config.py show    # 展示全部角色的工具、模型、思考强度与展开参数
    python3 ~/.hwf/scripts/role_config.py sync    # 按 roles.yaml 重新生成 herdr/config.yaml（及托管 droid 配置）
    python3 ~/.hwf/scripts/role_config.py check   # 校验 roles.yaml 合法性以及与 herdr/config.yaml 是否同步一致
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

HWF_HOME = Path(os.environ.get("HWF_HOME", Path.home() / ".hwf"))
ROLES_FILENAME = "roles.yaml"
TEAM_TOOLS = frozenset({"codex", "agy", "omp", "claude", "droid", "pi"})
HEADLESS_TOOLS = frozenset({"omp", "droid", "codex", "agy", "claude"})
SERVICE_TOOLS = HEADLESS_TOOLS | frozenset({"typesafe"})
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
DROID_FALLBACK_PATHS = (Path.home() / ".local" / "bin" / "droid",)

# 按 (绝对路径) -> (mtime_ns, size, parsed_data) 做进程内缓存，修改 roles.yaml 后免重启自动刷新。
_ROLES_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}


class RoleConfigError(RuntimeError):
    """角色配置文件结构错误、工具不支持或同步校验失败。"""


def clear_cache() -> None:
    """清空进程内缓存（供单测在极短时间改写同一文件时使用）。"""
    _ROLES_CACHE.clear()


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _expand_hwf_path(raw: str, hwf_home: Path) -> str:
    """展开路径；以 ~/.hwf/ 开头时绑定到传入的 hwf_home，保证跨机器与临时目录单测一致。"""
    text = raw.strip()
    if text == "~/.hwf":
        return str(hwf_home)
    if text.startswith("~/.hwf/"):
        return str(hwf_home / text[len("~/.hwf/") :])
    return os.path.expanduser(text)


def _format_hwf_path(raw: str, hwf_home: Path, *, portable: bool) -> str:
    """格式化写入配置的路径。

    portable=True 时保留 ~/.hwf/... 形式（不写入当前用户的绝对 HOME 路径），
    确保入库的 herdr/config.yaml 在不同电脑与 GitLab CI 容器路径下完全一致；
    portable=False 时展开为当前 hwf_home 下的绝对路径供运行时直接传给子进程。
    """
    text = raw.strip()
    if not portable:
        return _expand_hwf_path(text, hwf_home)
    if text == "~/.hwf" or text.startswith("~/.hwf/"):
        return text
    try:
        rel = Path(text).resolve().relative_to(hwf_home.resolve())
        return f"~/.hwf/{rel.as_posix()}" if rel.as_posix() != "." else "~/.hwf"
    except ValueError:
        return text


def roles_path(hwf_home: Optional[Path] = None, *, path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    if hwf_home is not None:
        return Path(hwf_home) / ROLES_FILENAME
    env_override = os.environ.get("HWF_ROLES_CONFIG", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    return HWF_HOME / ROLES_FILENAME


def find_executable(name: str, fallbacks: Sequence[Path] = ()) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return name


def validate_roles_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RoleConfigError("roles.yaml 顶层必须是 YAML 映射对象")
    team = data.get("team")
    services = data.get("services")
    if team is not None and not isinstance(team, Mapping):
        raise RoleConfigError("roles.yaml 的 team 必须是对象")
    if services is not None and not isinstance(services, Mapping):
        raise RoleConfigError("roles.yaml 的 services 必须是对象")

    clean_team: dict[str, dict[str, Any]] = {}
    for role_name, raw_spec in (team or {}).items():
        if not isinstance(raw_spec, Mapping):
            raise RoleConfigError(f"team.{role_name} 必须是对象")
        tool = _clean_str(raw_spec.get("tool")).lower()
        if tool not in TEAM_TOOLS:
            raise RoleConfigError(f"team.{role_name} 的 tool={tool!r} 不支持，可选：{', '.join(sorted(TEAM_TOOLS))}")
        model = _clean_str(raw_spec.get("model"))
        settings_file = _clean_str(raw_spec.get("settings_file"))
        if str(role_name) != "scheduler" and not model and not settings_file:
            raise RoleConfigError(f"team.{role_name} 缺少必填字段 model")
        clean_team[str(role_name)] = dict(raw_spec)
        clean_team[str(role_name)]["tool"] = tool

    clean_services: dict[str, dict[str, Any]] = {}
    for svc_name, raw_spec in (services or {}).items():
        if not isinstance(raw_spec, Mapping):
            raise RoleConfigError(f"services.{svc_name} 必须是对象")
        tool = _clean_str(raw_spec.get("tool")).lower()
        if tool not in SERVICE_TOOLS:
            raise RoleConfigError(
                f"services.{svc_name} 的 tool={tool!r} 不支持，可选：{', '.join(sorted(SERVICE_TOOLS))}"
            )
        if not _clean_str(raw_spec.get("model")):
            raise RoleConfigError(f"services.{svc_name} 缺少必填字段 model")
        clean_services[str(svc_name)] = dict(raw_spec)
        clean_services[str(svc_name)]["tool"] = tool

    default_profile = _clean_str(data.get("default_profile")) or "scheduler"
    if clean_team and default_profile not in clean_team:
        raise RoleConfigError(f"default_profile={default_profile!r} 不在 team 角色列表中")

    return {
        "default_profile": default_profile,
        "team": clean_team,
        "services": clean_services,
    }


def load_roles_optional(hwf_home: Optional[Path] = None, *, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """按文件 mtime 热读 roles.yaml；文件不存在时返回 None（供隔离单测与兜底回退）。"""
    cfg_path = roles_path(hwf_home, path=path)
    try:
        st = cfg_path.stat()
    except OSError:
        return None
    cached = _ROLES_CACHE.get(cfg_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RoleConfigError(f"读取 {cfg_path} 失败：{exc}") from exc
    parsed = validate_roles_data(raw)
    _ROLES_CACHE[cfg_path] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def load_roles(hwf_home: Optional[Path] = None, *, path: Optional[Path] = None) -> dict[str, Any]:
    cfg_path = roles_path(hwf_home, path=path)
    parsed = load_roles_optional(hwf_home, path=path)
    if parsed is None:
        raise RoleConfigError(f"找不到角色配置文件：{cfg_path}")
    return parsed


# ---------- 交付团队角色编译（roles.yaml -> herdr profile） ----------


def _droid_settings_payload(model: str, thinking: str) -> str:
    session_cfg: dict[str, str] = {}
    if model:
        session_cfg["model"] = model
    if thinking:
        session_cfg["reasoningEffort"] = thinking
    return json.dumps({"sessionDefaultSettings": session_cfg}, ensure_ascii=False, indent=2) + "\n"


def _managed_droid_settings_path(role_name: str, hwf_home: Path) -> Path:
    return hwf_home / "herdr" / f"droid-{role_name}.json"


def compile_team_profile(
    name: str,
    spec: Mapping[str, Any],
    *,
    hwf_home: Optional[Path] = None,
    write_settings: bool = False,
    portable: bool = False,
) -> dict[str, Any]:
    """把单条 team 角色定义翻译成 Herdr 可直接启动的 {kind, args}。"""
    home = Path(hwf_home) if hwf_home is not None else HWF_HOME
    tool = _clean_str(spec.get("tool")).lower()
    if tool not in TEAM_TOOLS:
        raise RoleConfigError(f"角色 {name} 的工具 {tool!r} 不支持")

    model = _clean_str(spec.get("model"))
    thinking = _clean_str(spec.get("thinking")) or _clean_str(spec.get("reasoning_effort"))
    yolo = bool(spec.get("yolo", True))
    compact_raw = spec.get("auto_compact_tokens")
    compact = int(compact_raw) if compact_raw not in (None, "") else None
    omp_config = _clean_str(spec.get("omp_config"))
    settings_file = _clean_str(spec.get("settings_file"))
    extra_args = [str(a) for a in (spec.get("extra_args") or [])]

    args: list[str] = []
    if tool == "codex":
        if model:
            args.extend(["-m", model])
        if thinking:
            args.extend(["-c", f"model_reasoning_effort={thinking}"])
        if compact is not None:
            args.extend(["-c", f"model_auto_compact_token_limit={compact}"])
        if yolo:
            args.append("--dangerously-bypass-approvals-and-sandbox")
    elif tool == "agy":
        if model:
            args.extend(["--model", model])
        if thinking:
            args.extend(["--effort", thinking])
        if yolo:
            args.extend(["--mode", "auto", "--dangerously-skip-permissions"])
    elif tool == "omp":
        if omp_config:
            args.extend(["--config", _format_hwf_path(omp_config, home, portable=portable)])
        if model:
            args.extend(["--model", model])
        if thinking:
            args.extend(["--thinking", thinking])
        if yolo:
            args.extend(["--approval-mode", "yolo"])
    elif tool == "claude":
        if model:
            args.extend(["--model", model])
        if thinking:
            args.extend(["--effort", thinking])
        if compact is not None:
            args.extend(["--autocompact", str(compact)])
        if yolo:
            args.append("--dangerously-skip-permissions")
    elif tool == "droid":
        # 交互式 droid 没有 --model 参数，统一落到 --settings JSON 的 sessionDefaultSettings。
        settings_arg: Optional[str] = None
        if settings_file:
            settings_arg = _format_hwf_path(settings_file, home, portable=portable)
        elif model or thinking:
            resolved_settings = _managed_droid_settings_path(name, home)
            if write_settings:
                payload = _droid_settings_payload(model, thinking)
                resolved_settings.parent.mkdir(parents=True, exist_ok=True)
                if not resolved_settings.is_file() or resolved_settings.read_text(encoding="utf-8") != payload:
                    resolved_settings.write_text(payload, encoding="utf-8")
            settings_arg = f"~/.hwf/herdr/droid-{name}.json" if portable else str(resolved_settings)
        if settings_arg is not None:
            args.extend(["--settings", settings_arg])
        if yolo:
            args.extend(["--auto", "high"])
    elif tool == "pi":
        if model:
            args.extend(["--model", model])
        if thinking:
            args.extend(["--thinking", thinking])

    args.extend(extra_args)
    return {"kind": tool, "args": args}


def compile_herdr_config(
    roles_data: Mapping[str, Any],
    *,
    hwf_home: Optional[Path] = None,
    write_settings: bool = False,
    portable: bool = False,
) -> dict[str, Any]:
    validated = validate_roles_data(roles_data)
    profiles: dict[str, dict[str, Any]] = {}
    for role_name, spec in validated["team"].items():
        profiles[role_name] = compile_team_profile(
            role_name, spec, hwf_home=hwf_home, write_settings=write_settings, portable=portable
        )
    return {
        "profiles": profiles,
        "default_profile": validated["default_profile"],
    }


def render_herdr_config_yaml(
    roles_data: Mapping[str, Any],
    *,
    hwf_home: Optional[Path] = None,
    write_settings: bool = False,
    portable: bool = True,
) -> str:
    compiled = compile_herdr_config(roles_data, hwf_home=hwf_home, write_settings=write_settings, portable=portable)
    lines = [
        "# 本文件由 ~/.hwf/roles.yaml 自动生成，请勿直接手工修改。",
        "# 调整任何角色的工具、模型或思考强度，请编辑 ~/.hwf/roles.yaml 后运行：",
        "#   python3 ~/.hwf/scripts/role_config.py sync",
        "profiles:",
    ]
    for name, entry in compiled["profiles"].items():
        args_literal = json.dumps(entry["args"], ensure_ascii=False)
        lines.append(f"  {name}:")
        lines.append(f"    kind: {entry['kind']}")
        lines.append(f"    args: {args_literal}")
    lines.append(f"default_profile: {compiled['default_profile']}")
    return "\n".join(lines) + "\n"


def sync_herdr_config(hwf_home: Optional[Path] = None, *, check_only: bool = False) -> tuple[bool, Path]:
    """同步 roles.yaml 到 herdr/config.yaml。

    check_only=True 时返回 (是否完全同步, target_path)，不写文件；
    check_only=False 时返回 (文件是否发生改动, target_path)。
    """
    home = Path(hwf_home) if hwf_home is not None else HWF_HOME
    roles_data = load_roles(home)
    target = home / "herdr" / "config.yaml"
    expected = render_herdr_config_yaml(roles_data, hwf_home=home, write_settings=not check_only)

    if check_only:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            return False, target
        # 同时核对托管 droid-<role>.json 是否同步
        for role_name, spec in roles_data["team"].items():
            if spec.get("tool") == "droid" and not _clean_str(spec.get("settings_file")):
                model = _clean_str(spec.get("model"))
                thinking = _clean_str(spec.get("thinking")) or _clean_str(spec.get("reasoning_effort"))
                if model or thinking:
                    settings_path = _managed_droid_settings_path(role_name, home)
                    expected_json = _droid_settings_payload(model, thinking)
                    if not settings_path.is_file() or settings_path.read_text(encoding="utf-8") != expected_json:
                        return False, settings_path
        return True, target

    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    if current == expected:
        return False, target
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(expected, encoding="utf-8")
    tmp.replace(target)
    return True, target


# ---------- 运行时角色查询（热读 roles.yaml + 覆盖优先级） ----------


def resolve_team_role(
    role_name: str,
    *,
    hwf_home: Optional[Path] = None,
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    data = load_roles_optional(hwf_home, path=path)
    if not data:
        return None
    spec = data.get("team", {}).get(role_name)
    return dict(spec) if isinstance(spec, Mapping) else None


def resolve_service_role(
    service_name: str,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    hwf_home: Optional[Path] = None,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """合并外围服务配置：代码默认值 < roles.yaml（热读） < 显式本地/单测覆盖。"""
    merged: dict[str, Any] = dict(defaults or {})

    data = load_roles_optional(hwf_home, path=path)
    role_entry = (data or {}).get("services", {}).get(service_name)
    if isinstance(role_entry, Mapping):
        for key, value in role_entry.items():
            if value is not None:
                merged[key] = value
        # 同步别名字段，避免调用方读到旧默认值
        if "thinking" in role_entry and role_entry["thinking"] is not None:
            merged["reasoning_effort"] = role_entry["thinking"]
        if "timeout_s" in role_entry and role_entry["timeout_s"] is not None:
            merged["model_timeout_s"] = role_entry["timeout_s"]

    if overrides:
        for key, value in overrides.items():
            if value is not None:
                merged[key] = value
        if overrides.get("reasoning_effort") is not None and overrides.get("thinking") is None:
            merged["thinking"] = overrides["reasoning_effort"]
        elif overrides.get("thinking") is not None and overrides.get("reasoning_effort") is None:
            merged["reasoning_effort"] = overrides["thinking"]
        if overrides.get("model_timeout_s") is not None and overrides.get("timeout_s") is None:
            merged["timeout_s"] = overrides["model_timeout_s"]
        elif overrides.get("timeout_s") is not None and overrides.get("model_timeout_s") is None:
            merged["model_timeout_s"] = overrides["timeout_s"]

    if merged.get("tool"):
        merged["tool"] = _clean_str(merged["tool"]).lower()
    if merged.get("thinking") and not merged.get("reasoning_effort"):
        merged["reasoning_effort"] = merged["thinking"]
    elif merged.get("reasoning_effort") and not merged.get("thinking"):
        merged["thinking"] = merged["reasoning_effort"]
    if merged.get("timeout_s") and not merged.get("model_timeout_s"):
        merged["model_timeout_s"] = merged["timeout_s"]
    elif merged.get("model_timeout_s") and not merged.get("timeout_s"):
        merged["timeout_s"] = merged["model_timeout_s"]
    return merged


# ---------- 无头后台推理统一适配器（secretary / proactive / retrospect） ----------


def _merge_system_and_user_prompt(system_prompt: str, prompt: str) -> str:
    if not system_prompt:
        return prompt
    return f"{system_prompt}\n\n{prompt}"


def build_headless_command(
    tool: str,
    *,
    model: str,
    thinking: str = "",
    prompt: str = "",
    prompt_file: Optional[Path] = None,
    system_prompt: str = "",
    mode: str = "no_tools",
    cwd: Optional[Path] = None,
    extra_args: Sequence[str] = (),
    bin_resolver: Callable[[str, Sequence[Path]], str] = find_executable,
) -> list[str]:
    """按 tool 与运行模式构建无头命令行参数。

    mode:
      - "no_tools"：纯文本推理（秘书意图分类、主动接活规则分诊），严禁调用任何工具。
      - "readonly"：只读代码检索分析（主动接活 auto 档），绝不允许改文件。
      - "retrospect"：周期复盘深度只读分析（支持 prompt_file，默认沿用 droid exec 只读模式）。
    """
    norm_tool = _clean_str(tool).lower()
    if norm_tool not in HEADLESS_TOOLS:
        raise RoleConfigError(f"无头服务不支持工具 {norm_tool!r}，可选：{', '.join(sorted(HEADLESS_TOOLS))}")
    if mode not in ("no_tools", "readonly", "retrospect"):
        raise RoleConfigError(f"不支持的无头调用模式：{mode!r}")

    norm_model = _clean_str(model)
    norm_thinking = _clean_str(thinking)
    extra = [str(a) for a in extra_args]

    if prompt_file is not None and not prompt and not (norm_tool == "droid" and mode == "retrospect"):
        prompt = Path(prompt_file).read_text(encoding="utf-8")

    if norm_tool == "omp":
        cmd = ["omp", "-p", "--model", norm_model]
        if norm_thinking:
            cmd.extend(["--thinking", norm_thinking])
        cmd.extend(["--no-session", "--mode", "json"])
        if mode == "no_tools":
            # omp 在 --no-tools 下会返回空响应，沿用 --tools read 并由解析器拦截任何实际工具调用。
            cmd.extend(["--tools", "read"])
        else:
            cmd.extend(["--tools", "read,grep,find,ls"])
        if mode in ("no_tools", "readonly"):
            cmd.extend(["--no-context-files", "--no-skills", "--no-rules", "--no-extensions", "--no-lsp"])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    if norm_tool == "droid":
        droid_bin = bin_resolver("droid", DROID_FALLBACK_PATHS)
        cmd = [droid_bin, "exec", "-m", norm_model]
        if norm_thinking:
            cmd.extend(["-r", norm_thinking])
        if mode == "retrospect":
            if cwd is not None:
                cmd.extend(["--cwd", str(cwd)])
            if system_prompt:
                cmd.extend(["--append-system-prompt", system_prompt])
            cmd.extend(extra)
            if prompt_file is not None:
                cmd.extend(["-f", str(prompt_file)])
            else:
                cmd.append(prompt)
            return cmd
        if mode == "no_tools":
            # droid exec 不允许空 --only-tools，限定最小只读工具并用 stream-json 拒绝任何实际工具调用。
            cmd.extend(["-o", "stream-json", "--only-tools", "Read", "--disable-builtin-skills"])
        else:
            cmd.extend(["--only-tools", "Read,Grep,Glob,LS"])
        if cwd is not None:
            cmd.extend(["--cwd", str(cwd)])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    if norm_tool == "claude":
        cmd = ["claude", "-p", "--model", norm_model]
        if norm_thinking:
            cmd.extend(["--effort", norm_thinking])
        cmd.extend(["--no-session-persistence", "--output-format", "json"])
        if mode == "no_tools":
            cmd.extend(["--tools", "", "--disable-slash-commands"])
        else:
            cmd.extend(["--tools", "Read,Grep,Glob,LS", "--permission-mode", "plan"])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        cmd.extend(extra)
        cmd.append(prompt)
        return cmd

    if norm_tool == "codex":
        cmd = ["codex", "exec", "-m", norm_model]
        if norm_thinking:
            cmd.extend(["-c", f"model_reasoning_effort={norm_thinking}"])
        cmd.extend(["-s", "read-only", "--ephemeral", "--skip-git-repo-check"])
        if cwd is not None:
            cmd.extend(["-C", str(cwd)])
        cmd.append("--json")
        cmd.extend(extra)
        cmd.append(_merge_system_and_user_prompt(system_prompt, prompt))
        return cmd

    # agy
    cmd = ["agy", "-p", "--model", norm_model]
    if norm_thinking:
        cmd.extend(["--effort", norm_thinking])
    cmd.extend(["--mode", "plan"])
    if mode == "no_tools":
        cmd.extend(["--disable-slash-commands", "--output-format", "stream-json"])
    else:
        cmd.extend(["--output-format", "text"])
    cmd.extend(extra)
    cmd.append(_merge_system_and_user_prompt(system_prompt, prompt))
    return cmd


def _extract_usage_tokens(usage: Any) -> int:
    if not isinstance(usage, Mapping):
        return 0
    if usage.get("totalTokens") is not None:
        return int(usage["totalTokens"] or 0)
    if usage.get("total_tokens") is not None:
        return int(usage["total_tokens"] or 0)
    inp = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
    return inp + out


def _parse_omp_output(stdout: str, *, forbid_tools: bool) -> tuple[Optional[str], int, Optional[str]]:
    reply = ""
    usage = 0
    for line in (stdout or "").splitlines():
        if not forbid_tools and not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        etype = ev["type"]
        if forbid_tools and etype in ("tool_execution_start", "tool_execution_end"):
            return None, 0, "tool_used"
        if etype == "turn_end":
            message = ev.get("message")
            if not isinstance(message, dict):
                if forbid_tools:
                    return None, 0, "invalid_event"
                continue
            if message.get("stopReason") not in (None, "stop"):
                return None, 0, "interrupted_turn"
            content = message.get("content")
            if forbid_tools:
                if not isinstance(content, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in content
                ):
                    return None, 0, "invalid_event"
                if any(item.get("type") in {"toolCall", "tool_use", "tool_call"} for item in content):
                    return None, 0, "tool_used"
                texts = [item.get("text") for item in content if item.get("type") == "text"]
                if any(not isinstance(t, str) for t in texts):
                    return None, 0, "invalid_event"
                reply = "".join(texts)
            else:
                parts = [
                    item.get("text", "")
                    for item in (content or [])
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if parts:
                    reply = "".join(parts)
            usage = _extract_usage_tokens(message.get("usage")) or usage
        elif not forbid_tools and etype == "message_end":
            message = ev.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                parts = [
                    item.get("text", "")
                    for item in (message.get("content") or [])
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if parts:
                    reply = "".join(parts)
                usage = _extract_usage_tokens(message.get("usage")) or usage
    cleaned = reply.strip()
    if not cleaned:
        return None, usage, "empty_response"
    return cleaned, usage, None


def _parse_claude_output(stdout: str, *, forbid_tools: bool) -> tuple[Optional[str], int, Optional[str]]:
    raw = (stdout or "").strip()
    if not raw:
        return None, 0, "empty_response"
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except ValueError:
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(parsed, dict):
        return None, 0, "invalid_event"
    if parsed.get("is_error"):
        return None, 0, "interrupted_turn"
    if forbid_tools and parsed.get("num_turns") and int(parsed["num_turns"]) > 1:
        return None, 0, "tool_used"
    text = parsed.get("result")
    if not isinstance(text, str) or not text.strip():
        return None, _extract_usage_tokens(parsed.get("usage")), "empty_response"
    return text.strip(), _extract_usage_tokens(parsed.get("usage")), None


def _parse_codex_output(stdout: str, *, forbid_tools: bool) -> tuple[Optional[str], int, Optional[str]]:
    reply = ""
    usage = 0
    saw_json = False
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        if not isinstance(ev, dict):
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        saw_json = True
        etype = str(ev.get("type") or "")
        if etype in ("turn.failed", "error"):
            return None, 0, "interrupted_turn"
        if etype in ("item.started", "item.completed"):
            item = ev.get("item")
            if isinstance(item, dict):
                itype = str(item.get("type") or "")
                if forbid_tools and itype in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
                    return None, 0, "tool_used"
                if itype == "agent_message" and isinstance(item.get("text"), str):
                    reply = item["text"]
        elif etype == "turn.completed":
            usage = _extract_usage_tokens(ev.get("usage")) or usage
    if not saw_json and forbid_tools:
        return None, 0, "invalid_event"
    cleaned = reply.strip()
    if not cleaned:
        return None, usage, "empty_response"
    return cleaned, usage, None


def _parse_stream_or_text_output(
    stdout: str,
    *,
    mode: str,
    forbid_tools: bool,
) -> tuple[Optional[str], int, Optional[str]]:
    """解析 droid / agy 的 text 或 stream-json/json 输出。"""
    raw = (stdout or "").strip()
    if not raw:
        return None, 0, "empty_response"
    if mode in ("readonly", "retrospect"):
        return raw, 0, None

    reply = ""
    usage = 0
    saw_json = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        if not isinstance(ev, dict):
            if forbid_tools:
                return None, 0, "invalid_event"
            continue
        saw_json = True
        etype = str(ev.get("type") or "")
        if forbid_tools and etype in ("tool_call", "tool_use", "tool_result"):
            return None, 0, "tool_used"
        if ev.get("is_error") or etype == "error":
            return None, 0, "interrupted_turn"
        if isinstance(ev.get("finalText"), str):
            reply = ev["finalText"]
        elif isinstance(ev.get("result"), str):
            reply = ev["result"]
        elif etype == "message" and ev.get("role") == "assistant":
            if isinstance(ev.get("text"), str):
                reply = ev["text"]
            elif isinstance(ev.get("content"), list):
                if forbid_tools and any(
                    isinstance(c, dict) and c.get("type") not in ("text", "thinking") for c in ev["content"]
                ):
                    return None, 0, "tool_used"
                parts = [c.get("text", "") for c in ev["content"] if isinstance(c, dict) and c.get("type") == "text"]
                if parts:
                    reply = "".join(parts)
        elif etype == "assistant":
            msg = ev.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                if forbid_tools and any(
                    isinstance(c, dict) and c.get("type") not in ("text", "thinking") for c in msg["content"]
                ):
                    return None, 0, "tool_used"
                parts = [c.get("text", "") for c in msg["content"] if isinstance(c, dict) and c.get("type") == "text"]
                if parts:
                    reply = "".join(parts)
        usage = _extract_usage_tokens(ev.get("usage")) or usage

    if not saw_json:
        if forbid_tools:
            return None, 0, "invalid_event"
        return raw, 0, None
    cleaned = reply.strip()
    if not cleaned:
        return None, usage, "empty_response"
    return cleaned, usage, None


def parse_headless_output(
    tool: str,
    stdout: str,
    *,
    mode: str = "no_tools",
    forbid_tools: bool = False,
) -> tuple[Optional[str], int, Optional[str]]:
    """统一提取不同 CLI 工具的无头输出，返回 (文本, token用量, 错误码)。"""
    norm_tool = _clean_str(tool).lower()
    if norm_tool == "omp":
        return _parse_omp_output(stdout, forbid_tools=forbid_tools)
    if norm_tool == "claude":
        return _parse_claude_output(stdout, forbid_tools=forbid_tools)
    if norm_tool == "codex":
        return _parse_codex_output(stdout, forbid_tools=forbid_tools)
    if norm_tool in ("droid", "agy"):
        return _parse_stream_or_text_output(stdout, mode=mode, forbid_tools=forbid_tools)
    raise RoleConfigError(f"无头服务不支持解析工具 {norm_tool!r} 的输出")


# ---------- CLI ----------


def format_roles_table(hwf_home: Optional[Path] = None) -> str:
    home = Path(hwf_home) if hwf_home is not None else HWF_HOME
    data = load_roles(home)
    lines = [f"角色配置文件：{roles_path(home)}", "", "[交付团队 team]"]
    for name, spec in data["team"].items():
        compiled = compile_team_profile(name, spec, hwf_home=home, write_settings=False)
        model = _clean_str(spec.get("model")) or "(跟随当前会话)"
        thinking = _clean_str(spec.get("thinking")) or "(默认)"
        args_str = " ".join(compiled["args"])
        lines.append(f"  {name:<12} tool={compiled['kind']:<6} model={model:<22} thinking={thinking:<8} args={args_str}")
    lines.append("")
    lines.append("[外围服务 services]")
    for name, spec in data["services"].items():
        model = _clean_str(spec.get("model"))
        thinking = _clean_str(spec.get("thinking")) or "-"
        timeout = spec.get("timeout_s")
        extra = f" timeout={timeout}s" if timeout else ""
        lines.append(f"  {name:<12} tool={spec['tool']:<8} model={model:<22} thinking={thinking:<8}{extra}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=None, help="覆盖 ~/.hwf 根目录路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="打印全部角色生效的工具、模型、思考强度与展开参数")
    sub.add_parser("sync", help="从 roles.yaml 重新生成 herdr/config.yaml")
    sub.add_parser("check", help="校验 roles.yaml 合法性以及与 herdr/config.yaml 是否同步")
    args = parser.parse_args(argv)
    home = args.home or HWF_HOME
    try:
        if args.cmd == "show":
            print(format_roles_table(home))
            return 0
        if args.cmd == "sync":
            changed, target = sync_herdr_config(home, check_only=False)
            state = "已更新" if changed else "已是最新"
            print(f"[{state}] {target}")
            return 0
        if args.cmd == "check":
            in_sync, target = sync_herdr_config(home, check_only=True)
            if not in_sync:
                print(
                    f"[未同步] {target} 与 {roles_path(home)} 不一致，请运行：python3 ~/.hwf/scripts/role_config.py sync",
                    file=sys.stderr,
                )
                return 1
            print(f"[已同步] {roles_path(home)} -> {target}")
            return 0
    except (RoleConfigError, OSError, ValueError) as exc:
        print(f"role_config failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
