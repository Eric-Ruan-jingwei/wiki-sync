#!/usr/bin/env python3
"""
wiki-sync — 把 AI Agent 的对话记录导入 Obsidian LLM-WIKI 知识库。

支持来源：Claude Code、Codex、ChatGPT（导出文件）。
零依赖，只用 Python 标准库。

用法:
    wiki-sync list                          列出可导入的对话（全部来源）
    wiki-sync list --source codex           只看某个来源
    wiki-sync import                         导入最近一条对话
    wiki-sync import --session <id>          导入指定对话
    wiki-sync import --all                   导入全部对话
    wiki-sync import --source chatgpt --file <export.json>
    wiki-sync config --vault <path>          设置 LLM-WIKI vault 路径
    wiki-sync config                         查看当前配置
    wiki-sync hook install                   开启 Claude Code 对话自动同步
    wiki-sync hook uninstall                 关闭自动同步
    wiki-sync hook status                    查看自动同步状态
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".config" / "wiki-sync" / "config.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_LOCAL_SETTINGS_PATH = Path.home() / ".claude" / "settings.local.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# 来源 key → 显示名
# 内置来源：这些 agent 的真实存储格式已验证，解析准确。
# format 决定用哪个解析器；path 是对话文件的 glob（{home} 会被替换成用户主目录）。
BUILTIN_SOURCES = {
    "claude": {
        "label": "Claude Code",
        "format": "claude-jsonl",
        "path": "{home}/.claude/projects/*/*.jsonl",
    },
    "codex": {
        "label": "Codex",
        "format": "codex-jsonl",
        "path": "{home}/.codex/sessions/**/rollout-*.jsonl",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "format": "chatgpt-export",
        "path": None,  # 需用户用 --file 或 config 指定导出文件
    },
}

# 自动同步默认只处理 >= 这个消息数的对话，避免把零碎闲聊也建页
DEFAULT_HOOK_MIN_MESSAGES = 4


def registered_sources():
    """内置来源 + 用户在 config 里注册的自定义来源（自定义可覆盖同名内置）。"""
    sources = {k: dict(v) for k, v in BUILTIN_SOURCES.items()}
    for name, spec in (load_config().get("custom_sources") or {}).items():
        sources[name] = dict(spec)
    return sources


def source_label(source):
    spec = registered_sources().get(source)
    return spec["label"] if spec and spec.get("label") else source


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def is_vault(p):
    """判断一个文件夹是不是可用的知识库：含 raw 文件夹的 Obsidian 仓库（有 .obsidian）。"""
    p = Path(p)
    return (p / ".obsidian").is_dir() and (p / "raw").is_dir()


def scan_vaults():
    """扫描本地常见位置，返回所有候选知识库路径（去重、按路径排序）。"""
    search_roots = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home(),
    ]
    found = []
    seen = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        # 以 raw 文件夹为线索：找含 raw 的 Obsidian 仓库
        for marker in root.glob("**/raw"):
            vault = marker.parent
            if len(vault.relative_to(root).parts) > 4:
                continue
            if is_vault(vault) and vault not in seen:
                seen.add(vault)
                found.append(vault)
    return sorted(found, key=lambda p: str(p))


def find_vault():
    """找到知识库：优先用配置里指定的；没配置时只在「唯一候选」才自动用，
    有多个就不猜——交给用户用 wiki-sync detect 选。"""
    cfg = load_config()
    if cfg.get("vault"):
        p = Path(cfg["vault"]).expanduser()
        if is_vault(p):
            return p
    candidates = scan_vaults()
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def oneline(text, maxlen=80):
    s = (text or "").strip().replace("\n", " ")
    s = re.sub(r"\s+", " ", s)
    if len(s) > maxlen:
        s = s[:maxlen].rstrip() + "…"
    return s


def is_noise_text(text):
    """判断是否是命令回显、指令注入等噪声，不该算作真实对话内容。"""
    if not text:
        return True
    t = text.lstrip()
    noise_prefixes = (
        # Claude Code 注入
        "<command-", "<local-command", "<user-prompt-submit-hook>", "Caveat:",
        # 指令 / 权限注入
        "<INSTRUCTIONS>", "<permissions instructions>", "# AGENTS.md", "<user_instructions",
        # Codex 上下文注入
        "<environment_context", "<turn_aborted", "<image ", "<image>", "<EOF", "<system",
    )
    return t.startswith(noise_prefixes)


def new_conv(source, session_id, full_path):
    return {
        "source": source,
        "sessionId": session_id,
        "title": None,
        "firstPrompt": None,
        "messages": [],        # [{"role", "text", "tools"}]
        "model": None,
        "created": None,
        "modified": None,
        "fullPath": str(full_path) if full_path else "",
        "messageCount": 0,
    }


def finalize_conv(conv):
    conv["messageCount"] = len(conv["messages"])
    if conv["title"] is None:
        conv["title"] = oneline(conv.get("firstPrompt") or "Untitled", 60)
    return conv


# ---------------------------------------------------------------------------
# 适配器：Claude Code
# ---------------------------------------------------------------------------

def claude_parse_file(jsonl_path, source="claude"):
    """解析一个 Claude Code 对话文件 → conv。"""
    conv = new_conv(source, Path(jsonl_path).stem, jsonl_path)
    try:
        lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return finalize_conv(conv)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if rec.get("type") == "ai-title" and rec.get("aiTitle"):
            conv["title"] = rec["aiTitle"]
            continue

        ts = rec.get("timestamp")
        if ts:
            if conv["created"] is None:
                conv["created"] = ts
            conv["modified"] = ts

        if rec.get("type") not in ("user", "assistant"):
            continue

        msg = rec.get("message", {})
        role = msg.get("role", rec.get("type"))
        if msg.get("model"):
            conv["model"] = msg["model"]

        content = msg.get("content", "")
        text_parts, tools = [], []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tools.append(block.get("name", "tool"))

        text = "\n".join(p for p in text_parts if p).strip()
        if not text and not tools:
            continue
        if is_noise_text(text):
            continue
        if role == "user" and conv["firstPrompt"] is None and text:
            conv["firstPrompt"] = text
        conv["messages"].append({"role": role, "text": text, "tools": tools})

    return finalize_conv(conv)




# ---------------------------------------------------------------------------
# 适配器：Codex
# ---------------------------------------------------------------------------

def codex_parse_file(jsonl_path, source="codex"):
    """解析一个 Codex rollout 文件 → conv。"""
    conv = new_conv(source, "", jsonl_path)
    try:
        lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return finalize_conv(conv)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = rec.get("type")
        payload = rec.get("payload", {}) or {}

        if rtype == "session_meta":
            conv["sessionId"] = payload.get("id", Path(jsonl_path).stem)
            conv["created"] = payload.get("timestamp") or rec.get("timestamp")
            conv["model"] = payload.get("model") or conv["model"]
            continue

        if rec.get("timestamp"):
            conv["modified"] = rec["timestamp"]

        if rtype != "response_item" or payload.get("type") != "message":
            continue

        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue  # 跳过 developer/system 注入

        text_parts = []
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                text_parts.append(block.get("text", ""))
        text = "\n".join(p for p in text_parts if p).strip()
        if not text or is_noise_text(text):
            continue

        if role == "user" and conv["firstPrompt"] is None:
            conv["firstPrompt"] = text
        conv["messages"].append({"role": role, "text": text, "tools": []})

    if not conv["sessionId"]:
        conv["sessionId"] = Path(jsonl_path).stem
    return finalize_conv(conv)


def generic_parse_file(jsonl_path, source):
    """通用 JSONL 解析器：尽力从常见字段形状里抽出 role + 文本。

    适配未知 agent 的 best-effort 方案。它会尝试识别每行里的：
      - 角色字段: role / author.role / sender / message.role
      - 文本字段: text / content（字符串）/ content.parts / content[].text / message.content
    解析不出来的行会被跳过。准确度取决于该 agent 的格式与常见形状的接近程度。
    """
    conv = new_conv(source, Path(jsonl_path).stem, jsonl_path)

    def dig_role(rec):
        for path in (("role",), ("author", "role"), ("sender",), ("message", "role")):
            cur = rec
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, str):
                return cur
        return None

    def dig_text(rec):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        content = msg.get("content", msg.get("text"))
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, str):
                    parts.append(b)
                elif isinstance(b, dict):
                    parts.append(b.get("text", b.get("content", "")) or "")
            return "\n".join(p for p in parts if p)
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            return "\n".join(p for p in content["parts"] if isinstance(p, str))
        return ""

    try:
        lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return finalize_conv(conv)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = rec.get("timestamp") or rec.get("time") or rec.get("created_at")
        if ts:
            if conv["created"] is None:
                conv["created"] = str(ts)
            conv["modified"] = str(ts)
        role = dig_role(rec)
        if role not in ("user", "assistant"):
            continue
        text = (dig_text(rec) or "").strip()
        if not text or is_noise_text(text):
            continue
        if role == "user" and conv["firstPrompt"] is None:
            conv["firstPrompt"] = text
        conv["messages"].append({"role": role, "text": text, "tools": []})
    return finalize_conv(conv)


# 格式 → 单文件解析器（chatgpt-export 是多对话，单独处理）
FORMAT_PARSERS = {
    "claude-jsonl": claude_parse_file,
    "codex-jsonl": codex_parse_file,
    "generic-jsonl": generic_parse_file,
}


# ---------------------------------------------------------------------------
# 适配器：ChatGPT（官方数据导出 conversations.json）
# ---------------------------------------------------------------------------

def chatgpt_parse_export(export_path, source="chatgpt"):
    """解析 ChatGPT 导出的 conversations.json → list[conv]。"""
    try:
        data = json.loads(Path(export_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("conversations", []) or [data]

    convs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        conv_id = item.get("id") or item.get("conversation_id") or ""
        conv = new_conv(source, conv_id, export_path)
        conv["title"] = item.get("title") or None
        ct = item.get("create_time")
        ut = item.get("update_time")
        if ct:
            conv["created"] = datetime.fromtimestamp(ct).isoformat()
        if ut:
            conv["modified"] = datetime.fromtimestamp(ut).isoformat()

        # 从 mapping 里抽取消息，按 create_time 排序
        mapping = item.get("mapping", {}) or {}
        rows = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            role = (msg.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            parts = (msg.get("content") or {}).get("parts", [])
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if not text or is_noise_text(text):
                continue
            rows.append((msg.get("create_time") or 0, role, text))
        rows.sort(key=lambda r: r[0])
        for _, role, text in rows:
            if role == "user" and conv["firstPrompt"] is None:
                conv["firstPrompt"] = text
            conv["messages"].append({"role": role, "text": text, "tools": []})

        finalize_conv(conv)
        if conv["messageCount"] > 0:
            convs.append(conv)
    return convs


# ---------------------------------------------------------------------------
# 汇总所有来源（注册表驱动）
# ---------------------------------------------------------------------------

def discover_source(name, spec, override_file=None):
    """按来源的 format/path 配置发现并解析它的全部对话。"""
    fmt = spec.get("format")

    # ChatGPT 类：单个导出文件含多个对话
    if fmt == "chatgpt-export":
        export = override_file or spec.get("file") or load_config().get("chatgpt_export")
        if export and Path(export).expanduser().exists():
            return chatgpt_parse_export(Path(export).expanduser(), source=name)
        return []

    # 其余：按 glob 扫文件，逐个解析
    parser = FORMAT_PARSERS.get(fmt)
    if not parser:
        return []
    pattern = spec.get("path")
    if not pattern:
        return []
    pattern = pattern.replace("{home}", str(Path.home()))
    convs = []
    for path in _glob_pattern(pattern):
        conv = parser(path, source=name)
        if conv["messageCount"] > 0:
            convs.append(conv)
    return convs


def _glob_pattern(pattern):
    """支持含 ** 的绝对 glob。"""
    p = Path(pattern)
    anchor = p.anchor or "/"
    rel = str(p)[len(anchor):]
    return Path(anchor).glob(rel)


def discover_all(source="all", override_file=None):
    convs = []
    sources = registered_sources()
    targets = sources if source == "all" else {source: sources.get(source)}
    for name, spec in targets.items():
        if not spec:
            continue
        convs += discover_source(name, spec, override_file=override_file)
    convs.sort(key=lambda e: e.get("modified") or e.get("created") or "", reverse=True)
    return convs


# ---------------------------------------------------------------------------
# 摘要（提取式，非 LLM）
# ---------------------------------------------------------------------------

def first_sentences(text, n=2, maxlen=160):
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？!?\n])", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    out = " ".join(parts[:n])
    return oneline(out, maxlen)


def summarize(conv):
    """从对话里提取一段简短概要（目标 + 结果 + 统计）。"""
    msgs = conv["messages"]
    user_msgs = [m for m in msgs if m["role"] == "user" and m["text"]]
    asst_text = [m for m in msgs if m["role"] == "assistant" and m["text"]]
    n_tools = sum(len(m["tools"]) for m in msgs)

    goal = oneline(conv.get("firstPrompt") or (user_msgs[0]["text"] if user_msgs else ""), 120)
    outcome = first_sentences(asst_text[-1]["text"], 2) if asst_text else ""

    stat_bits = [f"{len(user_msgs)} 轮用户提问"]
    if n_tools:
        stat_bits.append(f"{n_tools} 次工具调用")
    stat = " · ".join(stat_bits)

    return {"goal": goal, "outcome": outcome, "stat": stat}


# ---------------------------------------------------------------------------
# 生成 Markdown
# ---------------------------------------------------------------------------

def slugify(text, maxlen=50):
    text = (text or "untitled").strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r'[/\\:*?"<>|#^\[\]]', "", text)
    text = text.strip("-")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip("-")
    return text or "untitled"


def fmt_date(iso_str):
    if not iso_str:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return date.today().isoformat()


def build_conversation_markdown(conv):
    """一个对话 → 一个 md 文件：frontmatter + 摘要 + 涉及话题 + 完整原文。"""
    label = source_label(conv["source"])
    title = conv.get("title") or "Untitled"
    summary = summarize(conv)
    user_msgs = [m["text"] for m in conv["messages"] if m["role"] == "user" and m["text"]]

    out = [
        "---",
        "type: source",
        f"title: {title}",
        f"tags: [{conv['source']}-conversation, ai-chat]",
        "related: []",
        f"created: {fmt_date(conv.get('created'))}",
        f"updated: {fmt_date(conv.get('modified'))}",
        f"authors: [{label}]",
        f'venue: "{label}"',
        "---",
        "",
        f"# {title}",
        "",
        "## 对话信息",
        f"- **来源**: {label}",
        f"- **会话 ID**: {conv.get('sessionId', '')}",
        f"- **日期**: {fmt_date(conv.get('created'))}",
        f"- **消息数**: {len(conv['messages'])}",
        f"- **模型**: {conv.get('model') or '未知'}",
        "",
        "## 摘要",
        "",
        f"- **目标**: {summary['goal'] or '（无）'}",
    ]
    if summary["outcome"]:
        out.append(f"- **结果**: {summary['outcome']}")
    out.append(f"- **规模**: {summary['stat']}")
    out += [
        "",
        "> 以上摘要由 wiki-sync 从对话中自动提取，非 AI 生成。",
        "",
        "## 涉及话题",
        "",
    ]
    for um in user_msgs[:8]:
        out.append(f"- {oneline(um, 80)}")
    if not user_msgs:
        out.append("- （无）")
    out += ["", "## 完整对话", ""]
    for m in conv["messages"]:
        speaker = "🧑 用户" if m["role"] == "user" else "🤖 助手"
        out.append(f"### {speaker}")
        out.append("")
        if m["text"]:
            out.append(m["text"])
            out.append("")
        if m["tools"]:
            out.append(f"*（调用工具: {', '.join(m['tools'])}）*")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 写入 vault
# ---------------------------------------------------------------------------

def _imported_path(vault):
    # 去重记录放在 wiki-sync 自己的目录，不往用户库里塞 .llm-wiki
    return vault / ".wiki-sync" / "imported.json"


def load_imported(vault):
    f = _imported_path(vault)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_imported(vault, data):
    f = _imported_path(vault)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def source_folder(vault, source):
    """每个 agent 在 raw/ 下独立一个文件夹：raw/wiki-sync-<source>/。"""
    return vault / "raw" / f"wiki-sync-{source}"


def _append_log_block(vault, block):
    """把一段日志块插到 raw/wiki-sync-log.md 标题之后（最新在上）。"""
    raw_dir = vault / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log_path = raw_dir / "wiki-sync-log.md"

    if not log_path.exists():
        log_path.write_text(f"# wiki-sync 同步日志\n\n{block}", encoding="utf-8")
        return
    content = log_path.read_text(encoding="utf-8")
    if content.startswith("# "):
        nl = content.index("\n")
        content = content[:nl + 1] + "\n" + block + content[nl + 1:]
    else:
        content = block + "\n" + content
    log_path.write_text(content, encoding="utf-8")


def write_sync_log(vault, entries, n_ok, n_skip, n_err):
    """每次 sync 运行后，把本次结果追加到 raw/wiki-sync-log.md。

    第一性原理：
      - 操作必留痕：一次写库操作就记一条，无条件记录（空跑除外）。手动 sync 走这里，
        自动钩子走 write_hook_log，两条路径都留痕。
      - 归属自洽：日志是 wiki-sync 自己的产物，和它导入的对话一样放在 raw/ 下、
        统一用 wiki-sync-* 命名；绝不写进 wiki/ 等用户亲手整理的区域。
      - 以"运行"为单位：一条带时间戳的汇总 + 本次导入清单，便于审计和排错。
    """
    if n_ok == 0 and n_err == 0:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## [{stamp}] sync | 新增 {n_ok}，跳过 {n_skip}，失败 {n_err}"]
    for source, title in entries:
        lines.append(f"- {source_label(source)}: {title}")
    _append_log_block(vault, "\n".join(lines) + "\n")


def write_hook_log(vault, source, title, is_new):
    """自动钩子每次往库里写对话后留一条痕：新增 or 更新。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    action = "新增" if is_new else "更新"
    block = f"## [{stamp}] hook | {action}\n- {source_label(source)}: {title}\n"
    _append_log_block(vault, block)


def import_conversation(vault, conv, force=False):
    """导入单个对话 → raw/wiki-sync-<source>/<slug>.md，返回 (status, message)。"""
    key = f"{conv['source']}:{conv.get('sessionId', '')}"
    imported = load_imported(vault)
    is_new = key not in imported
    if not is_new and not force:
        return "skipped", f"已导入过，跳过: {imported[key].get('title', key)}"

    if not conv.get("messages"):
        return "error", "对话内容为空，跳过"

    title = oneline(conv.get("title") or "Untitled", 120)
    slug = f"{conv['source']}-{slugify(title)}"

    folder = source_folder(vault, conv["source"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{slug}.md").write_text(build_conversation_markdown(conv), encoding="utf-8")

    imported[key] = {
        "title": title,
        "slug": slug,
        "source": conv["source"],
        "importedAt": datetime.now().isoformat(),
    }
    save_imported(vault, imported)
    return "ok", f"已导入: {title}"


# ---------------------------------------------------------------------------
# 把自动同步装进各 agent（per-agent install）
# ---------------------------------------------------------------------------

CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
KNOWN_AGENTS = ("claude", "codex")


def wiki_sync_cmd():
    """返回已安装的 wiki-sync 可执行路径（供 agent 的钩子调用）。"""
    found = shutil.which("wiki-sync")
    if found:
        return found
    # 退而用当前脚本的绝对路径
    return str(Path(sys.argv[0]).resolve())


def detect_agent():
    """从环境变量猜当前所在的 agent。"""
    ai = os.environ.get("AI_AGENT", "")
    if os.environ.get("CLAUDECODE") == "1" or "claude-code" in ai:
        return "claude"
    if os.environ.get("CODEX_SANDBOX") is not None or "codex" in ai or os.environ.get("CODEX_HOME"):
        return "codex"
    return None


# ---- Claude Code：Stop 钩子 ----

CLAUDE_HOOK_CMD = f"{wiki_sync_cmd()} hook-run"


def _load_claude_settings():
    if CLAUDE_LOCAL_SETTINGS_PATH.exists():
        try:
            return json.loads(CLAUDE_LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_claude_settings(data):
    CLAUDE_LOCAL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_LOCAL_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _claude_is_installed(settings=None):
    settings = settings or _load_claude_settings()
    return any(
        str(h.get("command", "")).endswith("hook-run")
        for group in settings.get("hooks", {}).get("Stop", [])
        for h in group.get("hooks", [])
    )


def _migrate_from_main_settings():
    """把 settings.json 里残留的 wiki-sync hook 删掉（已迁到 settings.local.json）。"""
    if not CLAUDE_SETTINGS_PATH.exists():
        return
    try:
        main = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    stop = main.get("hooks", {}).get("Stop", [])
    changed = False
    new_groups = []
    for group in stop:
        kept = [h for h in group.get("hooks", []) if not str(h.get("command", "")).endswith("hook-run")]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
        if kept:
            group["hooks"] = kept
            new_groups.append(group)
    if changed:
        if new_groups:
            main["hooks"]["Stop"] = new_groups
        else:
            del main["hooks"]["Stop"]
        CLAUDE_SETTINGS_PATH.write_text(json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")


def _claude_ensure_hook():
    """自愈：如果 hook 丢了，悄悄装回去。静默，不打印。"""
    settings = _load_claude_settings()
    if not _claude_is_installed(settings):
        settings.setdefault("hooks", {}).setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": CLAUDE_HOOK_CMD}]}
        )
        _save_claude_settings(settings)


def _claude_install():
    # 先清理旧位置 settings.json 里可能残留的 hook（迁移到 settings.local.json）
    _migrate_from_main_settings()
    settings = _load_claude_settings()
    if _claude_is_installed(settings):
        print("✅ Claude Code 已经装好了，无需重复。")
        return
    settings.setdefault("hooks", {}).setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": CLAUDE_HOOK_CMD}]}
    )
    _save_claude_settings(settings)
    print("✅ 已装入 Claude Code。今后聊完会自动同步进知识库。")
    print(f"   （只同步消息数 ≥ {DEFAULT_HOOK_MIN_MESSAGES} 的对话）")


def _claude_uninstall():
    settings = _load_claude_settings()
    stop = settings.get("hooks", {}).get("Stop", [])
    removed = False
    new_groups = []
    for group in stop:
        kept = [h for h in group.get("hooks", []) if not str(h.get("command", "")).endswith("hook-run")]
        if len(kept) != len(group.get("hooks", [])):
            removed = True
        if kept:
            group["hooks"] = kept
            new_groups.append(group)
    if removed:
        settings["hooks"]["Stop"] = new_groups
        _save_claude_settings(settings)
        print("✅ 已从 Claude Code 卸载自动同步。")
    else:
        print("（Claude Code 本来就没装。）")


def _hook_run():
    """Claude Code Stop 钩子调用：从 stdin 读事件，同步当前对话。静默。"""
    _claude_ensure_hook()
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    transcript = event.get("transcript_path")
    if not transcript or not Path(transcript).exists():
        return
    vault = find_vault()
    if not vault:
        return
    conv = claude_parse_file(transcript)
    if conv["messageCount"] >= DEFAULT_HOOK_MIN_MESSAGES:
        key = f"{conv['source']}:{conv.get('sessionId', '')}"
        was_new = key not in load_imported(vault)
        status, _ = import_conversation(vault, conv, force=True)
        if status == "ok":
            title = oneline(conv.get("title") or "Untitled", 120)
            write_hook_log(vault, conv["source"], title, was_new)


# ---- Codex：config.toml 的 notify（单槽位，做接力保留原有）----

NOTIFY_RE = re.compile(r'(?m)^[ \t]*notify[ \t]*=[ \t]*(\[[^\]]*\])[ \t]*$')


def _toml_array(items):
    return "[" + ", ".join(json.dumps(x) for x in items) + "]"


def _codex_read_notify(text):
    """从 config.toml 文本里读出 notify 数组（单行）。返回 (列表, 匹配对象) 或 (None, None)。"""
    m = NOTIFY_RE.search(text)
    if not m:
        return None, None
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return items, m


def _codex_install():
    cmd = wiki_sync_cmd()
    ours = [cmd, "notify-run"]
    text = CODEX_CONFIG_PATH.read_text(encoding="utf-8") if CODEX_CONFIG_PATH.exists() else ""

    # 防止误判：有 notify 关键字但不是单行数组（多行写法），让用户手动处理
    if "notify" in text and NOTIFY_RE.search(text) is None and re.search(r'(?m)^[ \t]*notify[ \t]*=', text):
        print("⚠ 你的 config.toml 里 notify 是多行写法，自动改写不安全。")
        print(f"   请手动把它改成： notify = {_toml_array(ours)}")
        return

    existing, m = _codex_read_notify(text)
    if existing == ours:
        print("✅ Codex 已经装好了，无需重复。")
        return

    cfg = load_config()
    if existing and existing != ours:
        # 接力保留：记下原有的 notify，notify-run 跑完会再调用它
        cfg["codex_prev_notify"] = existing
        save_config(cfg)
        new_text = text[:m.start()] + f"notify = {_toml_array(ours)}" + text[m.end():]
        relay_note = f"（你原有的 notify 会被接力保留：{existing[0]} …）"
    elif existing == ours:
        return
    else:
        # 没有 notify，插到文件最前面（TOML 顶层键须在所有 [表] 之前）
        cfg.pop("codex_prev_notify", None)
        save_config(cfg)
        new_text = f"notify = {_toml_array(ours)}\n" + text
        relay_note = ""

    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG_PATH.write_text(new_text, encoding="utf-8")
    print("✅ 已装入 Codex。今后回合结束会自动同步进知识库。")
    if relay_note:
        print("   " + relay_note)


def _codex_uninstall():
    if not CODEX_CONFIG_PATH.exists():
        print("（Codex 没有 config.toml，本来就没装。）")
        return
    text = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    existing, m = _codex_read_notify(text)
    if not existing or existing[-1:] != ["notify-run"]:
        print("（Codex 里没有 wiki-sync 的 notify，无需卸载。）")
        return
    cfg = load_config()
    prev = cfg.get("codex_prev_notify")
    if prev:
        replacement = f"notify = {_toml_array(prev)}"
    else:
        replacement = ""  # 整行删除
    new_text = text[:m.start()] + replacement + text[m.end():]
    if not replacement:
        new_text = re.sub(r'\n\n+', '\n\n', new_text)  # 清理空行
    CODEX_CONFIG_PATH.write_text(new_text, encoding="utf-8")
    cfg.pop("codex_prev_notify", None)
    save_config(cfg)
    print("✅ 已从 Codex 卸载自动同步" + ("，并恢复了你原有的 notify。" if prev else "。"))


def _codex_is_installed():
    if not CODEX_CONFIG_PATH.exists():
        return False
    existing, _ = _codex_read_notify(CODEX_CONFIG_PATH.read_text(encoding="utf-8"))
    return bool(existing) and existing[-1:] == ["notify-run"]


def _notify_run():
    """Codex notify 调用：同步最近的对话，再接力调用原有 notify。静默。"""
    passthrough = sys.argv[2:]  # Codex 追加的事件 JSON
    vault = find_vault()
    if vault and CODEX_SESSIONS_DIR.is_dir():
        rollouts = sorted(
            CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if rollouts:
            conv = codex_parse_file(rollouts[0])
            if conv["messageCount"] >= DEFAULT_HOOK_MIN_MESSAGES:
                import_conversation(vault, conv, force=True)
    # 接力：调用用户原有的 notify
    prev = load_config().get("codex_prev_notify")
    if prev and prev[-1:] != ["notify-run"]:
        try:
            subprocess.run([*prev, *passthrough], timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass


# ---- 统一的 install / uninstall / status ----

def _resolve_agent(args):
    agent = getattr(args, "agent", None) or detect_agent()
    if agent not in KNOWN_AGENTS:
        print("请指定要装进哪个 agent：")
        print("  wiki-sync install claude    # Claude Code")
        print("  wiki-sync install codex     # Codex")
        return None
    return agent


def cmd_install(args):
    agent = _resolve_agent(args)
    if not agent:
        return
    if agent == "claude":
        _claude_install()
    elif agent == "codex":
        _codex_install()


def cmd_uninstall(args):
    agent = _resolve_agent(args)
    if not agent:
        return
    if agent == "claude":
        _claude_uninstall()
    elif agent == "codex":
        _codex_uninstall()


def cmd_status(args):
    print("自动同步安装状态：\n")
    print(f"  Claude Code : {'✅ 已装' if _claude_is_installed() else '⭕ 未装'}")
    print(f"  Codex       : {'✅ 已装' if _codex_is_installed() else '⭕ 未装'}")
    cur = detect_agent()
    if cur:
        print(f"\n（检测到你现在在 {cur} 里）")
    vault = find_vault()
    print(f"\n知识库: {vault or '⚠ 未找到，用 wiki-sync where <路径> 指定'}")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_list(args):
    convs = discover_all(args.source, args.file)
    if not convs:
        print("没有找到任何对话。")
        if args.source in ("all", "chatgpt"):
            print("（ChatGPT 来源需要先用 --file 指定导出的 conversations.json）")
        return
    vault = find_vault()
    imported = load_imported(vault) if vault else {}

    print(f"找到 {len(convs)} 个对话：\n")
    for i, e in enumerate(convs, 1):
        key = f"{e['source']}:{e.get('sessionId', '')}"
        mark = "✓ 已导入" if key in imported else "  未导入"
        src = source_label(e["source"])
        print(f"  {i:2d}. [{mark}] [{src}] {oneline(e.get('title'), 42)}")
        print(f"        {fmt_date(e.get('modified') or e.get('created'))} · "
              f"{e.get('messageCount')} 条消息 · {str(e.get('sessionId',''))[:8]}")
    print()
    if vault:
        print(f"知识库: {vault}")
    else:
        print("⚠ 还没找到知识库，先运行: wiki-sync where <vault 路径>")


def _require_vault():
    vault = find_vault()
    if not vault:
        print("⚠ 还没找到你的 Obsidian 知识库。")
        print("  试试自动检索： wiki-sync detect")
        print("  或手动指定：   wiki-sync where <知识库路径>")
        sys.exit(1)
    return vault


def _run_import(vault, targets, force=False, quiet_skips=False):
    """导入一批对话并打印结果汇总。"""
    n_ok = n_skip = n_err = 0
    imported_entries = []
    for e in targets:
        status, msg = import_conversation(vault, e, force=force)
        if status == "ok":
            imported_entries.append((e["source"], oneline(e.get("title") or "Untitled", 120)))
        if status == "skipped" and quiet_skips:
            n_skip += 1
            continue
        icon = {"ok": "✅", "skipped": "⏭ ", "error": "❌"}.get(status, "  ")
        print(f"  {icon} {msg}")
        n_ok += status == "ok"
        n_skip += status == "skipped"
        n_err += status == "error"
    print(f"\n完成：新导入 {n_ok}，已存在 {n_skip}，失败 {n_err}")
    write_sync_log(vault, imported_entries, n_ok, n_skip, n_err)


def cmd_sync(args):
    """默认命令：把所有还没导入的对话同步进知识库。"""
    vault = _require_vault()
    convs = discover_all("all")
    if not convs:
        print("没找到任何对话。")
        return
    new = [c for c in convs if f"{c['source']}:{c.get('sessionId','')}" not in load_imported(vault)]
    print(f"知识库: {vault}")
    if not new:
        print(f"\n✅ 全部 {len(convs)} 个对话都已经同步过了，没有新的。")
        return
    print(f"发现 {len(new)} 个新对话，开始同步…\n")
    _run_import(vault, new, force=False, quiet_skips=True)


def cmd_import(args):
    """高级：精确导入某条 / 某来源。"""
    vault = _require_vault()
    convs = discover_all(args.source, args.file)
    if not convs:
        print("没有找到任何对话。")
        sys.exit(1)

    if args.all:
        targets = convs
    elif args.session:
        targets = [e for e in convs if str(e.get("sessionId", "")).startswith(args.session)]
        if not targets:
            print(f"找不到 ID 以 '{args.session}' 开头的对话。")
            sys.exit(1)
    else:
        targets = [convs[0]]

    print(f"知识库: {vault}\n")
    _run_import(vault, targets, force=args.force)


def _ask_yes(prompt, default=True):
    """问一个 是/否。非交互环境（无终端）下直接返回默认值。"""
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return default
    return ans in ("y", "yes", "是", "好")


def cmd_setup(args):
    """新手指引：手把手走完 找知识库 → 装进 agent → 导入历史。"""
    print("👋 欢迎使用 wiki-sync —— 把 AI 对话自动存进你的 Obsidian 知识库。\n")
    print("这就带你走一遍，三步搞定。\n")

    # 第 1 步：知识库（列出来让用户选）
    print("── 第 1 步 / 共 3 步：选择你的知识库 ──")
    vault = find_vault()  # 已配置或唯一候选时直接用
    if vault:
        print(f"✅ 用这个知识库：{vault}")
    else:
        vault = choose_vault()
        if not vault:
            print("\n没选知识库，先到这。准备好后再跑 wiki-sync setup。")
            return

    # 第 2 步：装进当前 agent
    print("\n── 第 2 步 / 共 3 步：装进你正在用的 agent ──")
    agent = detect_agent()
    if agent:
        label = "Claude Code" if agent == "claude" else "Codex"
        if _ask_yes(f"检测到你在 {label} 里，现在装进去（聊完自动同步）？"):
            cmd_install(argparse.Namespace(agent=agent))
        else:
            print("  跳过。以后随时可以 wiki-sync install。")
    else:
        print("没认出当前 agent。在你用的 agent 终端里手动跑：")
        print("  wiki-sync install claude    # Claude Code")
        print("  wiki-sync install codex     # Codex")

    # 第 3 步：导入历史
    print("\n── 第 3 步 / 共 3 步：把以前的对话导进来 ──")
    if vault and _ask_yes("现在把历史对话一次性导入吗？"):
        print()
        cmd_sync(args)
    else:
        print("  跳过。以后随时可以直接运行 wiki-sync 来导入。")

    # 收尾
    print("\n🎉 设置完成！以后正常聊天就行，聊完会自动存进知识库。")
    print("   （装好后建议重开一次 agent 让自动同步生效）")
    print("   想看全部命令：wiki-sync --help")


def cmd_where(args):
    """查看或设置知识库（vault）路径。"""
    if not args.path:
        found = find_vault()
        cfg = load_config()
        print(f"当前知识库: {found or '⚠ 没找到，请用 wiki-sync where <路径> 指定'}")
        if cfg.get("vault"):
            print(f"（手动设置的路径: {cfg['vault']}）")
        return
    p = Path(args.path).expanduser().resolve()
    if not p.is_dir():
        print(f"⚠ 提醒：{p} 不存在或不是文件夹。仍已记录。")
    elif not (p / ".obsidian").is_dir():
        print(f"⚠ 提醒：{p} 看起来不是 Obsidian 仓库（没有 .obsidian）。仍已记录。")
    cfg = load_config()
    cfg["vault"] = str(p)
    save_config(cfg)
    print(f"✅ 知识库路径已设为: {p}")


def _vault_note_count(v):
    """大致数一下一个库里有多少篇笔记（帮用户认出哪个是常用的那个）。"""
    try:
        n = 0
        for p in Path(v).rglob("*.md"):
            if "wiki-sync-" in str(p):  # 不算我们自己导入的
                continue
            n += 1
            if n >= 9999:
                break
        return n
    except OSError:
        return 0


def choose_vault():
    """列出本地所有 Obsidian 知识库，让用户选一个，设为默认。返回选中的路径或 None。"""
    candidates = scan_vaults()
    if not candidates:
        print("没在本地找到 Obsidian 知识库。")
        print("（在 ~/Documents、~/Desktop、~/ 下找含 raw 文件夹的 Obsidian 库）")
        print("找到了但没识别？手动指定：wiki-sync where <知识库路径>")
        return None

    # 笔记多的（常用的）排前面
    candidates = sorted(candidates, key=lambda v: -_vault_note_count(v))

    print(f"在本地找到 {len(candidates)} 个 Obsidian 知识库：\n")
    print(f"  {'序号':<4}{'笔记数':<8}路径")
    for i, v in enumerate(candidates, 1):
        print(f"  {i:<5}{_vault_note_count(v):<8}{v}")

    if not sys.stdin.isatty():
        print("\n（当前非交互环境，没法选。请用： wiki-sync where <上面的路径>）")
        return None

    try:
        ans = input(f"\n用哪个？输入序号 1-{len(candidates)}（直接回车取消）： ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans.isdigit() or not (1 <= int(ans) <= len(candidates)):
        print("已取消，没有改动。")
        return None

    chosen = candidates[int(ans) - 1]
    cfg = load_config()
    cfg["vault"] = str(chosen)
    save_config(cfg)
    print(f"\n✅ 已设为你的知识库：{chosen}")
    return chosen


def cmd_detect(args):
    """列出本地的 Obsidian 知识库，让你选一个用。"""
    choose_vault()


def cmd_config(args):
    cfg = load_config()
    changed = False
    if args.vault:
        p = Path(args.vault).expanduser().resolve()
        cfg["vault"] = str(p)
        changed = True
        print(f"✅ 已设置 vault 路径: {p}")
    if args.chatgpt_export:
        p = Path(args.chatgpt_export).expanduser().resolve()
        cfg["chatgpt_export"] = str(p)
        changed = True
        print(f"✅ 已记住 ChatGPT 导出文件: {p}")
    if changed:
        save_config(cfg)
        return

    print("当前配置：")
    print(f"  配置文件: {CONFIG_PATH}")
    print(f"  vault: {cfg.get('vault') or '(未设置，将自动搜索)'}")
    print(f"  chatgpt_export: {cfg.get('chatgpt_export') or '(未设置)'}")
    found = find_vault()
    print(f"  实际使用 vault: {found or '⚠ 自动搜索未找到'}")


def cmd_source_list(args):
    print("已注册的对话来源：\n")
    custom = (load_config().get("custom_sources") or {})
    for name, spec in registered_sources().items():
        tag = "自定义" if name in custom else "内置"
        path = spec.get("path") or spec.get("file") or "（需 --file 指定导出文件）"
        print(f"  [{tag}] {name}  ({spec.get('label', name)})")
        print(f"         格式: {spec.get('format')}  路径: {path}")
    print("\n新增一个 agent：wiki-sync source add <name> --label <显示名> "
          "--path '<glob>' --format <格式>")


def cmd_source_add(args):
    if args.name in BUILTIN_SOURCES:
        print(f"⚠ '{args.name}' 是内置来源，换个名字，或直接用它。")
        return
    if args.fmt != "chatgpt-export" and not args.path:
        print("⚠ 非 chatgpt-export 格式必须用 --path 指定对话文件的 glob。")
        return
    cfg = load_config()
    custom = cfg.setdefault("custom_sources", {})
    spec = {"label": args.label or args.name, "format": args.fmt}
    if args.path:
        spec["path"] = args.path
    if args.file:
        spec["file"] = str(Path(args.file).expanduser().resolve())
    custom[args.name] = spec
    save_config(cfg)
    print(f"✅ 已注册自定义来源 '{args.name}'（{spec['label']}）。")
    print(f"   试试: wiki-sync list --source {args.name}")
    if args.fmt == "generic-jsonl":
        print("   提示: 用的是通用解析器，若解析不准，把该 agent 的一个对话文件发我，我加精准适配。")


def cmd_source_remove(args):
    cfg = load_config()
    custom = cfg.get("custom_sources") or {}
    if args.name in custom:
        del custom[args.name]
        cfg["custom_sources"] = custom
        save_config(cfg)
        print(f"✅ 已删除自定义来源 '{args.name}'。")
    else:
        print(f"（没有名为 '{args.name}' 的自定义来源。）")


def _main_cli():
    parser = argparse.ArgumentParser(
        prog="wiki-sync",
        description="把 AI 对话记录同步进 Obsidian 知识库。\n\n"
                    "第一次用？跑这个，手把手带你设置：\n"
                    "  wiki-sync setup\n\n"
                    "之后日常：\n"
                    "  wiki-sync install   装进当前 agent（聊完自动同步）\n"
                    "  wiki-sync           手动同步一次历史对话\n"
                    "  wiki-sync status    看哪些 agent 装了\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # —— 新手指引 ——
    p_setup = sub.add_parser("setup", help="新手指引：手把手走完全部设置")
    p_setup.set_defaults(func=cmd_setup, source="all", file=None)

    # —— 常用命令 ——
    p_install = sub.add_parser("install", help="把自动同步装进 agent（claude / codex）")
    p_install.add_argument("agent", nargs="?", help="claude / codex；不填则自动识别当前 agent")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="从 agent 卸载自动同步")
    p_uninstall.add_argument("agent", nargs="?", help="claude / codex；不填则自动识别")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_status = sub.add_parser("status", help="看哪些 agent 装了自动同步")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync", help="手动同步所有新对话（= 直接运行 wiki-sync）")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="看看有哪些对话")
    p_list.add_argument("--source", default="all", help="只看某个来源")
    p_list.add_argument("--file", help="ChatGPT 等导出文件路径")
    p_list.set_defaults(func=cmd_list)

    p_detect = sub.add_parser("detect", help="自动检索本地，找到你的 Obsidian 知识库并设为默认")
    p_detect.set_defaults(func=cmd_detect)

    p_where = sub.add_parser("where", help="查看/设置知识库位置：where 或 where <路径>")
    p_where.add_argument("path", nargs="?", help="LLM-WIKI 知识库（vault）路径")
    p_where.set_defaults(func=cmd_where)

    # —— 高级命令（少用）——
    p_import = sub.add_parser("import", help="[高级] 精确导入某条 / 某来源")
    p_import.add_argument("--source", default="all", help="对话来源（默认全部）")
    p_import.add_argument("--session", help="指定对话 ID（可只写前几位）")
    p_import.add_argument("--all", action="store_true", help="导入全部对话")
    p_import.add_argument("--force", action="store_true", help="强制重新导入（覆盖）")
    p_import.add_argument("--file", help="ChatGPT 等导出文件路径")
    p_import.set_defaults(func=cmd_import)

    p_source = sub.add_parser("source", help="[高级] 接入新 agent：source list / add / remove")
    src_sub = p_source.add_subparsers(dest="source_action")
    sp_list = src_sub.add_parser("list", help="列出所有已注册来源")
    sp_list.set_defaults(func=cmd_source_list)
    sp_add = src_sub.add_parser("add", help="注册一个自定义 agent 来源")
    sp_add.add_argument("name", help="来源名（小写，如 opencode）")
    sp_add.add_argument("--label", help="显示名（如 OpenCode）")
    sp_add.add_argument("--path", help="对话文件 glob，可用 {home}，如 {home}/.opencode/**/*.jsonl")
    sp_add.add_argument("--format", dest="fmt", default="generic-jsonl",
                        choices=["claude-jsonl", "codex-jsonl", "chatgpt-export", "generic-jsonl"],
                        help="解析格式（默认 generic-jsonl 通用解析）")
    sp_add.add_argument("--file", help="若格式是 chatgpt-export，指定导出文件路径")
    sp_add.set_defaults(func=cmd_source_add)
    sp_rm = src_sub.add_parser("remove", help="删除一个自定义来源")
    sp_rm.add_argument("name", help="要删除的来源名")
    sp_rm.set_defaults(func=cmd_source_remove)
    p_source.set_defaults(func=cmd_source_list)

    p_config = sub.add_parser("config", help="[高级] 查看配置 / 记住 ChatGPT 导出文件")
    p_config.add_argument("--vault", help="设置知识库路径（也可用 wiki-sync where）")
    p_config.add_argument("--chatgpt-export", dest="chatgpt_export",
                          help="记住 ChatGPT 导出文件路径")
    p_config.set_defaults(func=cmd_config)

    args = parser.parse_args()
    if not args.command:
        # 第一次用（还没有任何配置）→ 自动进新手指引；否则 = 同步
        if not CONFIG_PATH.exists():
            cmd_setup(argparse.Namespace(source="all", file=None))
        else:
            cmd_sync(args)
        return
    args.func(args)


def main():
    # 内部命令优先拦截：它们会带 agent 追加的原始参数（如 JSON），不走 argparse
    if len(sys.argv) >= 2 and sys.argv[1] == "hook-run":
        _hook_run()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "notify-run":
        _notify_run()
        return
    _main_cli()


if __name__ == "__main__":
    main()
