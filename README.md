# yinxiang-to-markdown

A standalone CLI that exports the local library of the macOS **印象笔记 (Yinxiang)** desktop client into one Markdown file per note, with optional Obsidian-friendly YAML frontmatter and attachments.

> Convert the local, plaintext Yinxiang library into portable Markdown — without depending on the ENEX export (which on the macOS Chinese client encrypts every note body and is unusable by third-party tools).

---

## 这是什么

`yinxiang2md.py` 是一个 Python 3 命令行工具。读取印象笔记 Mac 客户端的本地数据库与 `content/` 目录，把每条笔记转成一份 Markdown：

- 一条笔记一个 `.md` 文件
- 可选 YAML frontmatter（Obsidian 风格或精简风格）
- 图片内联引用、附件自动复制到 `attachments/`
- 文件名安全化、回收站笔记单独目录、重复标题自动 `-2/-3` 后缀
- 转换过程**只读**，绝不修改源库

## 为什么需要它

印象笔记中国版 Mac 客户端（9.x）的 ENEX 导出功能有一个坑：导出的每条笔记正文都是 `<content encoding="base64:aes">…</content>`（实测本地库 13 条全部如此），包括 Obsidian Importer、官方迁回工具在内的第三方工具**都无法读取**这份 ENEX。

但本地库本身是明文：

```
~/Library/Application Support/com.yinxiang.Mac/accounts/app.yinxiang.com/<账号ID>/
├── localNoteStore/LocalNoteStore.sqlite    # 元数据库（CoreData 风格）
└── content/<笔记UUID>/content.enml         # 正文，明文 XHTML 子集
    content/<笔记UUID>/<资源UUID>[.ext]     # 图片/附件
```

本工具直接读这份本地库，跳过 ENEX 加密层。

## 数据在哪

macOS 默认位置：

```
~/Library/Application Support/com.yinxiang.Mac/accounts/app.yinxiang.com/<账号ID>/
```

如果同一台 Mac 登录过多个账号，每个账号都有一个这样的子目录。运行 `python3 yinxiang2md.py --list-accounts` 可以列出它们。如果想直接指定，也可以用 `--source PATH` 指向任意一个账号目录（路径下要有 `localNoteStore/LocalNoteStore.sqlite`）。

## 安装

**不需要安装**。只要 macOS 自带的 Python 3（3.8+ 推荐）即可：

```bash
git clone https://github.com/cdapic/yinxiang-to-markdown.git
cd yinxiang-to-markdown
python3 yinxiang2md.py --help
```

可选：如果系统装了 [`pandoc`](https://pandoc.org/)，工具会自动用它做 HTML → Markdown 转换。没有也能跑，只是回退到内置实现。

## 快速开始

```bash
# 1. 看看自动探测到的账号目录
python3 yinxiang2md.py --list-accounts

# 2. 把整个库转成 Markdown 到 ~/Notes
python3 yinxiang2md.py --source ~/Library/Application\ Support/com.yinxiang.Mac/accounts/app.yinxiang.com/<账号ID> \
                      -o ~/Notes

# 3. 含回收站笔记
python3 yinxiang2md.py --source <账号目录> -o ~/Notes --include-deleted
```

## 参数表

### 数据源

| 参数 | 说明 |
|---|---|
| `--source PATH` | 直接指定账号目录（含 `localNoteStore/` 与 `content/`）。省略时自动探测 macOS 默认位置下所有账号目录；若多于一个则报错要求 `--source` |
| `--list-accounts` | 仅列出探测到的账号目录后退出 |

### 输出

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --out DIR` | 必填（除非 `--dry-run/--list-accounts`） | 输出目录 |
| `--layout {notebook,flat}` | `notebook`：`out/<笔记本名>/<标题>.md`；`flat`：`out/<标题>.md`（重名加 `-2/-3`） |
| `--assets {folder,flat,none}` | `folder`：每条笔记同级 `attachments/` 子目录；`flat`：`out/_assets/` 统一存放；`none`：不复制资源 |
| `--include-deleted` | 排除回收站 | 含回收站笔记，输出到 `_已删除_回收站/<笔记本>/` |
| `--notebook NAME` | — | 只转换指定笔记本（可重复） |
| `--limit N` | — | 最多转换 N 条（调试用） |
| `--dry-run` | — | 只打印将要做什么与统计，不写任何文件 |
| `--report FILE` | `out/_迁移报告.md` | 额外写一份 Markdown 转换报告 |

### 转换

| 参数 | 默认 | 说明 |
|---|---|---|
| `--engine {auto,pandoc,builtin}` | `auto` | `auto`：有 `pandoc` 用 `pandoc`，否则回退到内置引擎；`pandoc`：强制 `pandoc`，缺失则报错退出；`builtin`：强制内置引擎 |
| `--no-frontmatter` | — | 不写 YAML frontmatter |
| `--frontmatter-style {obsidian,plain}` | `obsidian` | `obsidian`：`title/notebook/tags/created/updated/source/yinxiang_id`；`plain`：仅 `title/created/updated` |
| `--todo-style {char,task}` | `char` | `char`：`☑ / ☐` 前缀；`task`：GFM 任务列表 `- [x]` / `- [ ]` |

### 其它

| 参数 | 默认 | 说明 |
|---|---|---|
| `--jobs N` | 1 | 并行转换数（用 `concurrent.futures`） |
| `--quiet / --verbose` | — | 静默 / 详细 |
| `--version` | — | 打印版本号并退出 |

### 退出码

- `0` 全部成功
- `1` 参数或源库问题
- `2` 部分笔记失败（仍写出成功的部分与报告）

任何单条笔记抛异常都不会让整个工具崩——会跳过并记入报告。

## 输出示例

```
~/Notes/
├── 日常/
│   ├── 富文本示例.md
│   ├── 图片笔记.md
│   ├── 空笔记.md
│   ├── a_b_c_d_e_f_g_h_i.md
│   ├── a_b_c_d_e_f_g_h_i-2.md     # 同标题自动重命名
│   └── attachments/
│       ├── res-uuid-0200.png
│       └── res-uuid-0201.png
├── 学习/
│   ├── 附件与待办.md
│   └── attachments/
│       └── res-uuid-0202.xlsx
├── _已删除_回收站/
│   └── 学习/
│       └── 已删除笔记.md
└── _迁移报告.md
```

每条笔记的 frontmatter：

```yaml
---
title: "富文本示例"
notebook: "日常"
tags: ["工作"]
created: 2017-11-13 19:12
updated: 2017-11-14 18:45
source: ""
yinxiang_id: note-uuid-0100
---
```

## 常见问题

**附件缺失怎么办？**

正文里 `<en-media hash="...">` 指向的资源找不到（已经被你手动从 `content/<uuid>/` 删掉了，或者哈希不匹配），会在正文里写 `[附件缺失: <hash 前 8 位>]`，并计入 `_迁移报告.md`。

**图片不显示？**

- Markdown 里写的是相对路径 `attachments/...`，确保图片文件和 `.md` 在同一个目录树里就行。
- 用了 `--assets flat`？那图片在 `out/_assets/`，路径不会指向 `attachments/`——需要 `--assets folder` 才能就地打开。
- 用了 `--assets none`？产物里**不会**留下 `attachments/` 引用，图片/附件会被改写成纯文本占位符 `[图片: <文件名>]` / `[附件: <文件名>]`；报告会记录「未复制资源 N 个」。

**没有 pandoc 怎么办？**

回退到内置 HTML→Markdown 引擎，覆盖段落、标题、列表（含嵌套）、表格（GFM 管道表）、代码块（围栏）、引用、分隔线、图片、链接、粗体/斜体/删除线。复杂样式（自定义 CSS、复杂表格、嵌套很深）会被简化甚至丢弃，但不会输出原始 HTML。

**Windows / Linux 能用吗？**

理论上能跑（纯 Python，标准库），但印象笔记本地库路径 `com.yinxiang.Mac/...` 是 macOS 专属；其他平台没有这个本地库。这个工具**只支持 macOS 上的印象笔记中国版**。

**加密笔记（`<en-crypt>`）怎么办？**

印象笔记中国版客户端不会在本地明文存加密笔记（已实测本地 0 条）；如果遇到，会被替换成 `[加密内容：印象笔记未解密，已丢弃]`。请去客户端手动导出原文。

## 隐私与安全

- **纯本地运行**：脚本不联网、不上传任何数据。
- **只读源库**：以 `sqlite3` URI `mode=ro` 打开数据库，从不调用任何会写源库的操作。
- **不修改任何源文件**：`content/<uuid>/` 下的 `content.enml` 与所有资源文件**不会被移动、复制回写或删除**。
- **输出目录外**没有任何副作用。

## 局限

- 不支持印象笔记的素材（手写、录音、思维导图）。
- 复杂自定义样式（`style`/`class` 噪声）会被清洗掉——网页剪藏的复杂排版会塌缩成更朴素的 Markdown。
- 客户端内的实时同步功能不会触发；导完以后源库与输出是两套独立文件。
- 只支持 macOS 上的印象笔记中国版（`com.yinxiang.Mac`）；海外版 Evernote 桌面端的库结构类似但表名/字段可能不同。

## 运行测试

```bash
python3 -m pytest tests/ -q
```

测试**全部离线**、**不触碰真实印象笔记库**：每个用例都用 `tests/make_fixture.py` 现造一份仅含合成内容的本地库，跑完即弃。

## 许可证

MIT，详见 `LICENSE`。
