# wiki-sync

把 Claude Code、Codex、ChatGPT 的对话记录，一键导入 Obsidian [LLM-WIKI](https://github.com/stello-agent) 知识库。

和 AI 聊完天，对话就躺在本地某个目录的 JSONL 文件里，想找找不到、想回顾也不方便。`wiki-sync` 把这步自动化：扫描你的对话，自动提取摘要，转成符合 LLM-WIKI 规范的 Markdown 页面，存进你的 Obsidian vault。还能挂上 Claude Code 的钩子，对话结束自动同步。

零依赖，只用 Python 标准库，macOS 自带 Python 即可运行。

## 使用前提

1. **一台 Mac**（自带 Python 3，开箱即用）。
2. **Obsidian**，并且有一个 LLM-WIKI 知识库（带 `raw` 文件夹的 Obsidian 仓库）。

> wiki-sync 会自动检索本地、找到这个知识库，你不用手动填路径。

## 安装

一键安装（推荐，新用户用这个，不用 clone 仓库）：

```bash
curl -sSL https://raw.githubusercontent.com/Eric-Ruan-jingwei/wiki-sync/main/install.sh | sh
```

或者你已经 clone 了本仓库，在项目目录里：

```bash
sh install.sh
```

如果安装后提示 `command not found`，运行 `source ~/.zshrc` 再试；仍不行就在 `~/.zshrc` 末尾加一行后重开终端：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 第一次用（三步）

```bash
# 1) 列出本地的 Obsidian 知识库，你选一个
wiki-sync detect

# 2) 装进你正在用的 agent（在 Claude Code / Codex 的终端里跑）
wiki-sync install

# 3) 把以前的对话先导进来
wiki-sync
```

之后正常聊天即可，聊完自动同步。装好后建议重开一次 agent 让自动同步生效。

> `detect` 会在 `~/Documents`、`~/Desktop`、`~/` 下找含 `raw` 文件夹的 Obsidian 库，
> 列成表格（带笔记数，方便你认出常用的那个），由**你输序号选**——工具不替你猜。

## 用法

核心思路：**在你用的每个 agent 里装一次，之后聊完自动同步。** 换 agent 就在那个 agent 里再装一下。

```bash
# 在 Claude Code 的终端里跑
wiki-sync install        # 自动识别当前 agent 并装入

# 在 Codex 里跑
wiki-sync install        # 同样自动识别；也可显式 wiki-sync install codex
```

装好后，那个 agent 每次对话结束会自动把对话同步进知识库（消息数 ≥ 4 才建页，避免零碎闲聊刷屏；对话变长会更新同一页面）。无需再手动操作。

### 管理安装

```bash
wiki-sync install [claude|codex]    # 装入（不带参数自动识别当前 agent）
wiki-sync uninstall [claude|codex]  # 卸载
wiki-sync status                    # 看哪些 agent 装了
```

> Codex 的 `notify` 只有一个槽位。若你已设了别的 notify（如 Computer Use），
> wiki-sync 会**接力保留**它——同步完再调用你原来的程序，两者都不耽误；卸载时自动恢复。

### 手动同步历史

第一次用，可以把过去的对话一次性导进来（之后靠自动同步即可）：

```bash
wiki-sync          # 同步所有还没导入的历史对话
wiki-sync list     # 先看看有哪些对话
```

### 知识库位置

wiki-sync 会检索本地的 Obsidian 知识库（含 `raw` 文件夹的库），列成表格让你挑。相关命令：

```bash
wiki-sync detect              # 列出本地知识库（带笔记数），你输序号选
wiki-sync where               # 看当前用的是哪个知识库
wiki-sync where <vault 路径>   # 直接指定
```

### 高级用法（一般用不到）

```bash
# 精确导入某一条 / 某个来源
wiki-sync import --session 3089            # 只导某条对话
wiki-sync import --source codex            # 只导 Codex 最近一条
wiki-sync import --force                   # 覆盖重导
wiki-sync import --source chatgpt --file ~/Downloads/conversations.json

# 记住 ChatGPT 导出文件，之后 list/import 免带 --file
wiki-sync config --chatgpt-export ~/Downloads/conversations.json
```

## 支持的来源

内置三个（存储格式已验证，解析准确）：

| 来源 | 读取位置 | 说明 |
|------|---------|------|
| **Claude Code** | `~/.claude/projects/*/*.jsonl` | 自动扫描，支持 hook 自动同步 |
| **Codex** | `~/.codex/sessions/**/rollout-*.jsonl` | 自动扫描 |
| **ChatGPT** | 官网导出的 `conversations.json` | 需手动导出后用 `--file` 指定 |

### 接入新的 agent（hermes / opencode / openclaw …）

每个 agent 的"对话结束自动触发"机制都不一样（Claude Code 是 Stop 钩子、Codex 是 `notify`）。
要让一个新 agent 支持 `wiki-sync install`，得先知道它的钩子机制，再加一个对应的 installer。
**如果你用上了某个新 agent，把它的钩子/扩展机制告诉作者，就能加上精准支持。**

在那之前，对有本地对话文件的 agent，可以用扫描式的"自定义来源"做手动导入兜底：

```bash
wiki-sync source add opencode \
    --label "OpenCode" \
    --path "{home}/.opencode/**/*.jsonl" \
    --format generic-jsonl     # 通用解析器，自动识别 role/text 字段
wiki-sync import --source opencode   # 手动导入（无自动同步）
wiki-sync source remove opencode
```

## 导入后的结构

每个 agent 在 `raw/` 下有独立文件夹，**不碰你 `wiki/sources/` 里整理好的主笔记**，只往 `wiki/log.md` 追加一条活动记录：

```
你的 vault/
├── raw/
│   ├── wiki-sync-claude/
│   │   ├── claude-<标题>.md      # 一个对话一个文件（frontmatter + 摘要 + 全文）
│   │   └── _index.md            # 该 agent 的对话目录
│   ├── wiki-sync-codex/
│   │   └── ...
│   └── wiki-sync-opencode/      # 自定义 agent 也各自一个文件夹
├── wiki/log.md                  # 追加导入日志（LLM-WIKI 的活动日志）
└── .llm-wiki/wiki-sync-imported.json   # 已导入记录（去重用）
```

每个 md 文件含 YAML frontmatter + 自动提取的摘要（目标 + 结果 + 规模）+ 完整对话原文，符合 LLM-WIKI 的 `source` schema，可在 Obsidian 里双链引用。

> 摘要由 wiki-sync 从对话里**提取**（取首个用户问题作目标、末条助手回复作结果），不调用任何 LLM，因此零成本、纯本地。

## 工作原理

**采用 push 模型**：程序装进每个 agent，agent 每轮对话结束主动把对话推给 wiki-sync，而不是 wiki-sync 去翻各家的存储目录。

- **Claude Code**: 注册一个 Stop 钩子 `wiki-sync hook-run`，对话结束时 Claude Code 把事件（含 transcript 路径）从 stdin 传入，立即同步当前对话。
- **Codex**: 设置 `config.toml` 的 `notify = ["wiki-sync", "notify-run"]`，回合结束时被调用。Codex 的 notify 只有一个槽位，所以会**接力**调用你原有的 notify。同步时取最近修改的 `rollout-*.jsonl`（刚结束的会话）解析。

各 agent 的对话被解析成统一的对话模型（`conv`），后续摘要、Markdown 生成、写入 vault 共用一套逻辑。手动导入历史时，扫描式来源也复用同一套解析。

- **Claude Code**: 直接扫描 jsonl 对话文件（不依赖 `sessions-index.json` 缓存，那个缓存常和实际文件对不上），从 `ai-title` 记录取标题。
- **Codex**: 解析 `rollout-*.jsonl` 里的 `response_item` 消息，过滤掉 AGENTS.md、environment_context 等系统注入。
- **ChatGPT**: 解析导出 `conversations.json` 的 `mapping` 结构，按时间排序还原对话。
- **通用 (generic-jsonl)**: 逐行尝试从常见字段形状（`role` / `author.role`、`content` / `text` / `content.parts`）抽取消息，适配未知 agent。

## 路线图

- [x] **V0.1** — 单条/批量导入、去重
- [x] **V0.2** — 对话摘要自动提取
- [x] **V0.3** — 多来源（Claude Code / Codex / ChatGPT）+ 统一对话模型
- [x] **按 agent 分文件夹** — 每个来源独立 `raw/wiki-sync-<source>/`，不污染主笔记
- [x] **push 模型** — `install` 把自动同步装进 agent（Claude Code Stop 钩子 / Codex notify 接力）
- [ ] 更多 agent 的 installer（按需添加，需先知道其钩子机制）

## License

MIT
