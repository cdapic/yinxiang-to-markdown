---
name: yinxiang-to-markdown
description: "Use when migrating a macOS 印象笔记 (Yinxiang) local library to Markdown / Obsidian, or when a Yinxiang .enex export turns out to be AES-encrypted and unreadable by third-party tools. Converts each note to one Markdown file with optional frontmatter and attachments."
version: 1.0.0
author: Contributors
license: MIT
platforms: [darwin]
language: zh-CN, en
metadata:
  hermes:
    tags: [yinxiang, evernote, markdown, obsidian, migration, enml, macos]
    category: productivity
    related_skills: [obsidian, note-taking]
---

# yinxiang-to-markdown skill

This skill teaches an AI agent (Hermes / Claude Code / similar) how to operate the `yinxiang2md.py` CLI to migrate a user's macOS 印象笔记 (Yinxiang) local library into portable Markdown.

## When to use this skill

Trigger when the user asks for any of:

- "把印象笔记导出成 Markdown"
- "把 印象笔记中国版 转成 Obsidian"
- "迁出印象笔记到 Markdown"
- "read my Yinxiang local library"
- "convert Yinxiang (印象笔记) to .md"
- The user provides a path under `~/Library/Application Support/com.yinxiang.Mac/...` and asks to migrate it

Do **not** trigger when:

- The user is on Windows / Linux (this tool only supports macOS).
- The user wants to migrate the overseas **Evernote** client (different local schema; not yet supported).
- The user wants to read a `.enex` file (the macOS Chinese client's ENEX is encrypted and not recoverable by this tool).

## What this tool is NOT

- Not a network service; runs entirely offline.
- Not an importer into any specific app; it just produces `.md` files plus optional image attachments.
- Not a sync tool; the source library is left untouched.

## Required environment

- macOS (Apple Silicon or Intel).
- Python 3.8+ with no third-party packages.
- Optional: `pandoc` on `PATH` for higher-fidelity HTML→Markdown conversion.

## Step-by-step workflow

1. **Detect the library root.**
   - First try `python3 yinxiang2md.py --list-accounts`. This prints account directories under the default macOS root `~/Library/Application Support/com.yinxiang.Mac/accounts/app.yinxiang.com/<账号ID>/`.
   - If exactly one account is returned, use it.
   - If multiple, ask the user which one. **Never silently pick.**
   - If none, ask the user to point `--source` at the right path.

2. **Confirm with the user.**
   - Before any conversion, present:
     - Detected account(s) and how many notes will be processed.
     - Output directory (where the `.md` files will land).
     - Whether to include trashed notes (`--include-deleted`).
     - Engine choice (`auto` / `pandoc` / `builtin`).
   - Suggest running `--dry-run` first if the user is uncertain.

3. **Run with `--dry-run`** to print the planned output without writing anything.

4. **Run the real conversion:**

   ```bash
   python3 yinxiang2md.py --source "<账号目录>" -o "<输出目录>" [--include-deleted]
   ```

5. **Open the report.** After conversion, `out/_迁移报告.md` (Chinese) summarises counts, failures and any missing media. Surface the appendix to the user.

6. **Verify idempotency if asked.** Re-running the same command should produce no new files and identical contents. The `_迁移报告.md` timestamp will change; everything else should be byte-identical.

## CLI surface (quick reference)

```
python3 yinxiang2md.py [options]

数据源
  --source PATH         账号目录（含 localNoteStore/ 与 content/）
  --list-accounts       仅列出探测到的账号目录后退出

输出
  -o, --out DIR         输出目录（必填，除非 --dry-run/--list-accounts）
  --layout {notebook,flat}
  --assets {folder,flat,none}
  --include-deleted     含回收站笔记
  --notebook NAME       只转换指定笔记本（可重复）
  --limit N             最多 N 条
  --dry-run             只打印计划，不写文件
  --report FILE         自定义报告路径

转换
  --engine {auto,pandoc,builtin}
  --no-frontmatter      不写 YAML frontmatter
  --frontmatter-style {obsidian,plain}
  --todo-style {char,task}

其它
  --jobs N              并行转换数
  --quiet / --verbose
  --version
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All notes converted successfully |
| 1 | Argument or source-library error (e.g. invalid `--source`) |
| 2 | Partial failure: some notes failed but the run still produced the report and successful notes |

Single-note exceptions are caught; the tool never crashes on a malformed note.

## Sandbox / safety rules

- **Read-only on the source**: the sqlite is opened `mode=ro`. Do not delete, rename or otherwise mutate files inside the source directory.
- **No network**: never pipe the source library over the internet.
- **No personal data in user-facing output**: do not echo usernames, account IDs, note titles or paths back to the user in chat messages unless they ask. The README and report may include real note titles because they live in the user's filesystem already.

## Common troubleshooting

- **"数据库被锁" / "database is locked"**: another process (often the desktop client itself) is writing. Ask the user to quit 印象笔记 first.
- **`pandoc` not found**: the tool falls back to the built-in HTML→Markdown engine. Output will be slightly less polished. Suggest installing `pandoc` via `brew install pandoc` for the auto path.
- **`[附件缺失: …]` in markdown**: the source library referenced an image hash whose file is missing under `content/<uuid>/`. The user can either re-add the file, or accept the placeholder.

## Testing the skill

Before claiming success, run:

```bash
cd /path/to/yinxiang-to-markdown
python3 -m pytest tests/ -q
python3 yinxiang2md.py --help
```

Both must succeed on a machine without 印象笔记 installed (the fixture is built on the fly).
