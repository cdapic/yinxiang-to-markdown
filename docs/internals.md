# 印象笔记本地库结构 & ENEX 加密原因

本文档面向对工具原理感兴趣、或需要排查异常的用户，介绍工具背后的实现细节。

## 为什么不能走 ENEX 导出

印象笔记中国版 Mac 客户端（`com.yinxiang.Mac`，9.x）的"导出笔记"菜单与 AppleScript `export` 命令**都能**产出 `.enex`，外层骨架也是标准格式：

```xml
<en-export export-date="…" application="Evernote" version="…">
  <note>
    <title>…</title>
    <content encoding="base64:aes"><![CDATA[RU5DMBnKfss…]]></content>
    <created>…</created>
    <updated>…</updated>
    <resource>…<data encoding="base64">…</data></resource>
  </note>
</en-export>
```

问题出在 `<content encoding="base64:aes">`：正文不是 ENML，而是一段 base64 数据，解码后以 `ENC0` 魔数开头——这是印象笔记自有的加密容器，只有客户端能用账户密钥打开。资源（`<data>`）多数仍是明文 base64，但正文读不出来，迁移没有意义。

实测 13 条样本，`encoding="base64:aes"` 命中率 13/13。这种 ENEX 在以下工具里**全部失败**：

- Obsidian Importer（把 base64 当 ENML 解析 → 解析失败或得到乱码）
- Evernote 官方迁回工具（同样无法解密，只能导回印象笔记自己）
- 任何把 ENEX 当 XML 直接解析的脚本

结论：ENEX 是"给印象笔记自己回导用的备份"，不是迁出通道。唯一可靠的迁出通道是**直接读本地库**——本地 `content.enml` 是明文。

## 本地库目录结构（macOS）

```
~/Library/Application Support/com.yinxiang.Mac/accounts/app.yinxiang.com/<账号ID>/
├── localNoteStore/
│   ├── LocalNoteStore.sqlite      # 元数据库（CoreData 风格）
│   ├── LocalNoteStore.sqlite-wal  # WAL 模式日志（运行客户端时才会出现）
│   └── LocalNoteStore.sqlite-shm  # 共享内存文件
└── content/
    └── <笔记ZLOCALUUID>/
        ├── content.enml           # 正文，UTF-8 编码的 ENML
        ├── <资源ZLOCALUUID>        # 图片/附件，文件名即 LocalUUID
        ├── <资源ZLOCALUUID>.png    # 或带扩展名
        ├── snippet.tiff           # 缩略图（不要复制）
        ├── card.png                 # 卡片缩略图（不要复制）
        └── <资源ZLOCALUUID>.en-reco  # 推荐文本（不要复制）
```

一个 `<笔记ZLOCALUUID>/` 目录下：

- 必有 `content.enml`（正文）
- 每条资源一个文件，文件名 = `<ZLOCALUUID>` 或 `<ZLOCALUUID>.<真实后缀>`
- 客户端自己还会派生 `*.en-reco` / `*.en-rstxt` / `*.en-pdf-text` / `snippet.tiff` / `snippet.txt` / `card.png` / `lower-card.png` 等旁路文件。这些**不是用户资源**，必须过滤。

## 数据库关键表（CoreData 风格）

`LocalNoteStore.sqlite` 里有大量 `Z_*` 前缀的表（CoreData 标准命名）。本工具只用以下几张：

| 表 | 字段 | 用途 |
|---|---|---|
| `ZENNOTEBOOK` | `Z_PK`, `ZNAME` | 笔记本 |
| `ZENTAG` | `Z_PK`, `ZNAME` | 标签 |
| `ZENNOTE` | `Z_PK`, `ZLOCALUUID`, `ZTITLE`, `ZNOTEBOOK`, `ZACTIVE`, `ZDATECREATED`, `ZDATEUPDATED`, `ZDATEDELETED`, `ZSOURCEURL` | 笔记 |
| `ZENRESOURCE` | `Z_PK`, `ZNOTE`, `ZLOCALUUID`, `ZDATAHASH`, `ZATTACHMENT`, `ZFILENAME`, `ZMIME` | 资源 |
| `Z_10TAGS` | `Z_10NOTES`, `Z_23TAGS` | 笔记↔标签 多对多关联 |

### 关键映射表

| 目标 | 来源 |
|---|---|
| 笔记目录名 | `ZENNOTE.ZLOCALUUID`（**不是** `ZGUID`） |
| 资源文件名前缀 | `ZENRESOURCE.ZLOCALUUID`（**不是** `ZGUID`） |
| 正文中 `<en-media hash="…">` | `hex(ZENRESOURCE.ZDATAHASH)`（32 位 MD5 hex，大写比对） |
| 笔记标题/时间 | `ZENNOTE.ZTITLE` / `ZDATECREATED` / `ZDATEUPDATED` |
| 笔记本 | `ZENNOTE.ZNOTEBOOK` → `ZENNOTEBOOK.Z_PK.ZNAME` |
| 标签 | `Z_10TAGS(Z_10NOTES, Z_23TAGS)` → `ZENTAG.Z_PK.ZNAME` |
| 回收站/已删 | `ZENNOTE.ZACTIVE=0` 或 `ZDATEDELETED` 非空 |
| 附件标记 | `ZENRESOURCE.ZATTACHMENT=1`（内联图片为 0） |
| 源链接 | `ZENNOTE.ZSOURCEURL` |
| 加密正文 | ENML 中 `<en-crypt>`（本地 0 条，但代码里仍处理） |

## CoreData 时间戳

CoreData 用 2001-01-01 00:00:00 UTC 为基准存秒数。本地转换：

```python
from datetime import datetime, timedelta, timezone
COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
dt = COREDATA_EPOCH + timedelta(seconds=ZENNOTE.ZDATECREATED)
local = dt.astimezone()  # 转成本地时区
formatted = local.strftime("%Y-%m-%d %H:%M")
```

## 写入安全策略

工具在打开源库时严格用 `sqlite3` URI `mode=ro`：

```python
conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
```

这只读连接：

- 永远不会发出 `UPDATE` / `DELETE` / `INSERT`
- 永远不会创建临时表
- 不会触发 WAL 写（不过 sqlite3 在某些版本上会在文件系统创建空的 `-wal`/`-shm` 文件，这是 sqlite3 内部行为，工具本身没有显式写）

如果 `mode=ro` 模式下还担心，可以预先 `chmod -w` 源 `LocalNoteStore.sqlite`——测试里有覆盖。

## ENML 已知标签

印象笔记的 ENML 是 XHTML 子集。常用标签：

- 块级：`p`, `h1`-`h6`, `ul`/`ol`/`li`, `table`/`tr`/`th`/`td`, `pre`, `blockquote`, `hr`
- 内联：`a`, `img`, `strong`/`b`, `em`/`i`, `del`/`s`, `code`, `span`
- 自定义 `en-*`：`en-note`, `en-media`, `en-todo`, `en-crypt`

清洗规则：

1. 去掉 `<?xml …?>` 与 `<!DOCTYPE …>`
2. `<en-note>` → `<div>`
3. `<en-media hash=…>` → `<img src="attachments/<文件>">`
4. `<en-todo checked=true|false>` → `<span data-checkbox="x| ">`（HTML→MD 引擎再转 `☑ / ☐` 或 `- [x] / - [ ]`）
5. `<en-crypt>` → `[加密内容：印象笔记未解密，已丢弃]`
6. 样式相关属性（`style`/`class`/`id`/`lang`/`dir`/`align`/`valign`/`border`/`cellpadding`/`cellspacing`/`bgcolor`/`color`/`face`/`size`/`width`/`height`/`hspace`/`vspace`/`target`/`data-*`）一律删除；保留 `href`/`src`/`alt`/`title`/`colspan`/`rowspan`
7. 未知的 `en-*` 标签：丢弃标签，保留文本

## 不打算支持的元素

| 元素 | 处理 |
|---|---|
| `<en-crypt>` | 替换为占位（本地从未遇到过加密内容） |
| `<en-media type="application/pdf">` 等非图片 | 仍按 `<img>` 处理（pandoc 会显示破损图），更好的方案是给出"附件"链接 |
| 客户端独有的"素材"（手写 / 录音 / 思维导图） | 完全跳过 |
| 自定义 CSS（`<style>` 块） | 完全丢弃 |
| 数学公式（`<en-formula>`） | 替换为占位 |

如果以上任何一种对你的笔记很重要，请用印象笔记的官方客户端重新导出，并在 GitHub 上提 issue 描述用例。
