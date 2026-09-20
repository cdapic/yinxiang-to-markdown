#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yinxiang2md.py - Convert Yinxiang (印象笔记) local library to Markdown.

A standalone CLI that reads the Evernote-family local sqlite + content/
directory of the macOS Yinxiang desktop client and emits one Markdown
file per note (with optional frontmatter and attachments).

Zero third-party Python dependencies. Optional pandoc for HTML->MD.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import html
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from typing import Any, Iterable

VERSION = "1.0.0"
COREDATA_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
DERIVED_SUFFIXES = (".en-reco", ".en-rstxt", ".en-pdf-text")
DERIVED_NAMES = {"snippet.tiff", "snippet.txt", "card.png", "lower-card.png"}
DROP_ATTRS = {"style", "class", "id", "lang", "dir", "align", "valign",
              "border", "cellpadding", "cellspacing", "bgcolor", "color",
              "face", "size", "width", "height", "hspace", "vspace", "target"}


# ----- discovery ---------------------------------------------------------
def default_yinxiang_root() -> str:
    return os.path.expanduser("~/Library/Application Support/com.yinxiang.Mac")


def discover_accounts(root=None):
    root = root or default_yinxiang_root()
    base = os.path.join(root, "accounts", "app.yinxiang.com")
    out = []
    if not os.path.isdir(base):
        return out
    for hid in sorted(os.listdir(base)):
        c = os.path.join(base, hid)
        if os.path.isdir(c) and os.path.isfile(
                os.path.join(c, "localNoteStore", "LocalNoteStore.sqlite")):
            out.append(c)
    return out


def looks_like_account_dir(path):
    return os.path.isfile(os.path.join(path, "localNoteStore", "LocalNoteStore.sqlite"))


# ----- db --------------------------------------------------------------
def open_db_readonly(db_path):
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_rows(conn, sql, params=()):
    return list(conn.execute(sql, tuple(params)).fetchall())


def load_notebooks(conn):
    return {int(r["Z_PK"]): (r["ZNAME"] or "") for r in
            fetch_rows(conn, "SELECT Z_PK, ZNAME FROM ZENNOTEBOOK")}


def load_tags(conn):
    return {int(r["Z_PK"]): (r["ZNAME"] or "") for r in
            fetch_rows(conn, "SELECT Z_PK, ZNAME FROM ZENTAG")}


def load_notes(conn, include_deleted, notebooks_filter):
    where = []
    params = []
    if not include_deleted:
        # NOTE: do not add `n.ZDATEDELETED IS NULL`. CoreData encodes
        # "unset" as the 2001-01-01 sentinel seconds (-978307200), NOT
        # SQL NULL, so the IS NULL clause matches zero active notes and
        # silently produces an empty output directory. `ZACTIVE = 1` is
        # the authoritative predicate for "not trashed".
        where.append("n.ZACTIVE = 1")
    if notebooks_filter:
        where.append("nb.ZNAME IN (%s)" % ",".join("?" for _ in notebooks_filter))
        params.extend(notebooks_filter)
    sql = ("SELECT n.Z_PK, n.ZLOCALUUID, n.ZTITLE, n.ZNOTEBOOK, n.ZACTIVE, "
           "n.ZDATECREATED, n.ZDATEUPDATED, n.ZDATEDELETED, n.ZSOURCEURL, "
           "nb.ZNAME AS notebook_name FROM ZENNOTE n "
           "LEFT JOIN ZENNOTEBOOK nb ON nb.Z_PK = n.ZNOTEBOOK")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY n.ZDATECREATED"
    return fetch_rows(conn, sql, params)


def count_skipped(conn, notebooks_filter):
    """Count notes that would be excluded under the active filters.

    When ``include_deleted`` is False, trashed notes (``ZACTIVE = 0``)
    are filtered out by ``load_notes``. The report's "跳过" counter
    surfaces how many such notes the run skipped, so the user can tell
    whether trashed notes were intentionally left behind.
    """
    where = ["n.ZACTIVE = 0"]
    params = []
    if notebooks_filter:
        where.append("nb.ZNAME IN (%s)" % ",".join("?" for _ in notebooks_filter))
        params.extend(notebooks_filter)
    sql = ("SELECT COUNT(*) FROM ZENNOTE n "
           "LEFT JOIN ZENNOTEBOOK nb ON nb.Z_PK = n.ZNOTEBOOK WHERE "
           + " AND ".join(where))
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


def load_tags_for_notes(conn, pks):
    out = {pk: [] for pk in pks}
    if not pks:
        return out
    ph = ",".join("?" for _ in pks)
    rows = fetch_rows(conn,
        "SELECT m.Z_10NOTES AS npk, t.ZNAME AS name FROM Z_10TAGS m "
        "JOIN ZENTAG t ON t.Z_PK = m.Z_23TAGS WHERE m.Z_10NOTES IN (%s)" % ph, pks)
    for r in rows:
        nm = r["name"] or ""
        if nm:
            out[int(r["npk"])].append(nm)
    return out


def load_resources(conn, pks):
    out = {pk: [] for pk in pks}
    if not pks:
        return out
    ph = ",".join("?" for _ in pks)
    rows = fetch_rows(conn,
        "SELECT r.Z_PK, r.ZNOTE, r.ZLOCALUUID, r.ZDATAHASH, r.ZATTACHMENT, "
        "r.ZFILENAME, r.ZMIME FROM ZENRESOURCE r WHERE r.ZNOTE IN (%s)" % ph, pks)
    for r in rows:
        out[int(r["ZNOTE"])].append(r)
    return out


# ----- time ------------------------------------------------------------
def cdata_to_dt(secs):
    if secs is None:
        return None
    try:
        s = float(secs)
    except (TypeError, ValueError):
        return None
    return (COREDATA_EPOCH + datetime.timedelta(seconds=s)).astimezone()


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


# ----- content ---------------------------------------------------------
def read_note_body(content_dir, uuid):
    if not uuid:
        return ""
    p = os.path.join(content_dir, uuid, "content.enml")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def list_resource_files(content_dir, uuid):
    base = os.path.join(content_dir, uuid)
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, n) for n in sorted(os.listdir(base))
            if n != "content.enml"]


def is_derived_binary(name):
    base = os.path.basename(name)
    if base in DERIVED_NAMES:
        return True
    return any(base.endswith(s) for s in DERIVED_SUFFIXES)


def split_uuid_and_ext(local_uuid, on_disk):
    base = os.path.basename(on_disk)
    if local_uuid and base.startswith(local_uuid):
        tail = base[len(local_uuid):]
        if tail.startswith("."):
            return local_uuid + tail
    return base


# ----- ENML preprocessor ----------------------------------------------
def _strip_drop_attrs(attrs):
    out = []
    for k, v in attrs:
        kl = k.lower()
        if kl in DROP_ATTRS or kl.startswith("data-"):
            continue
        out.append((k, v))
    return out


def _attr_string(attrs):
    return " ".join(('%s="%s"' % (k, html.escape(v, quote=True))) if v else k
                    for k, v in attrs)


class _EnmlPreprocessor(HTMLParser):
    def __init__(self, missing):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.missing = missing
        self.resource_map = {}

    def handle_decl(self, decl):
        return
    def handle_pi(self, data):
        return
    def handle_comment(self, data):
        return

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        attrs = _strip_drop_attrs(attrs)
        if tl == "en-note":
            self.parts.append("<div>")
            return
        if tl == "en-media":
            ahash = next((v for k, v in attrs if k.lower() == "hash"), None)
            fn = self.resource_map.get(ahash.upper()) if ahash else None
            if fn:
                self.parts.append('<img src="attachments/%s" alt="">' % html.escape(fn, quote=True))
            else:
                short = (ahash or "?")[:8]
                self.parts.append("[附件缺失: %s]" % short)
                self.missing.append(short)
            return
        if tl == "en-todo":
            # checked truthy values: true/1/checked (any case); missing/false/0 -> unchecked.
            checked = next((v for k, v in attrs if k.lower() == "checked"), "") or ""
            cl = checked.strip().lower()
            is_checked = cl in ("true", "1", "checked")
            self.parts.append('☑ ' if is_checked else '☐ ')
            return
        if tl == "en-crypt":
            self.parts.append("[加密内容：印象笔记未解密，已丢弃]")
            return
        if tl.startswith("en-"):
            return
        self.parts.append("<%s%s>" % (tag, (" " + _attr_string(attrs)) if attrs else ""))

    def handle_startendtag(self, tag, attrs):
        tl = tag.lower()
        if tl in ("en-media", "en-todo"):
            self.handle_starttag(tag, attrs)
            return
        if tl.startswith("en-"):
            return
        attrs = _strip_drop_attrs(attrs)
        self.parts.append("<%s%s/>" % (tag, (" " + _attr_string(attrs)) if attrs else ""))

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl == "en-note":
            self.parts.append("</div>")
            return
        if tl.startswith("en-"):
            return
        if tl in {"br", "hr", "img", "meta", "link", "input"}:
            return
        self.parts.append("</%s>" % tag)

    def handle_data(self, data):
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        self.parts.append("&%s;" % name)

    def handle_charref(self, name):
        self.parts.append("&#%s;" % name)

    def get_html(self):
        return "".join(self.parts)


def preclean_enml(raw, rmap, missing):
    raw = re.sub(r"<\?xml[^>]*\?>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<!DOCTYPE[^>]*>", "", raw, flags=re.IGNORECASE)
    p = _EnmlPreprocessor(missing)
    p.resource_map = rmap
    try:
        p.feed(raw)
        p.close()
    except Exception:
        pass
    return p.get_html()


# ----- HTML -> Markdown -----------------------------------------------

_INLINE_MARKERS = {
    "strong": "**", "b": "**",
    "em": "*", "i": "*",
    "del": "~~", "s": "~~", "strike": "~~",
    "code": "`",
}


class _HtmlToMd(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.list_stack = []
        self.table_stack = []
        self.stack = []
        self.pre_buf = []
        self.in_pre = False
        self.in_style_or_script = 0
        self.bq_count = 0

    def _ensure_break(self):
        if self.parts and not self.parts[-1].endswith("\n\n"):
            self.parts.append("\n\n" if not self.parts[-1].endswith("\n") else "\n")
        if self.bq_count > 0 and (not self.parts or not self.parts[-1].endswith("> ")):
            self.parts.append("> ")

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        attrd = {k.lower(): (v or "") for k, v in attrs}
        if tl in {"style", "script"}:
            self.in_style_or_script += 1
            return
        if self.in_style_or_script:
            return
        if tl in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._ensure_break()
            self.parts.append("#" * int(tl[1]) + " ")
            self.stack.append({"tag": tl})
            return
        if tl == "p":
            self._ensure_break()
            self.stack.append({"tag": "p"})
            return
        if tl == "br":
            self.parts.append("  \n")
            return
        if tl == "hr":
            self._ensure_break()
            self.parts.append("\n---\n\n")
            return
        if tl in _INLINE_MARKERS and not self.in_pre:
            m = _INLINE_MARKERS[tl]
            self.parts.append(m)
            self.stack.append({"tag": tl, "marker": m})
            return
        if tl == "pre":
            self.in_pre = True
            self.pre_buf = []
            self._ensure_break()
            self.stack.append({"tag": "pre"})
            return
        if tl == "blockquote":
            self._ensure_break()
            self.parts.append("> ")
            self.stack.append({"tag": "blockquote"})
            self.bq_count += 1
            return
        if tl == "a":
            self.stack.append({"tag": "a", "href": attrd.get("href", ""), "text": []})
            return
        if tl == "img":
            self.parts.append("![%s](%s)" % (attrd.get("alt", ""), attrd.get("src", "")))
            return
        if tl == "ul":
            self._ensure_break()
            self.list_stack.append({"type": "ul", "counter": 0})
            return
        if tl == "ol":
            self._ensure_break()
            self.list_stack.append({"type": "ol", "counter": 0})
            return
        if tl == "li":
            if self.list_stack:
                cur = self.list_stack[-1]
                cur["counter"] += 1
                indent = "  " * (len(self.list_stack) - 1)
                bullet = "- " if cur["type"] == "ul" else "%d. " % cur["counter"]
            else:
                indent = ""
                bullet = "- "
            self.parts.append("\n" + indent + bullet)
            self.stack.append({"tag": "li"})
            return
        if tl == "table":
            self._ensure_break()
            self.table_stack.append({"rows": [], "cur_row": None, "in_cell": False,
                                     "cell_buf": [], "had_header": False})
            return
        if tl in {"thead", "tbody"}:
            return
        if tl == "tr":
            if self.table_stack:
                self.table_stack[-1]["cur_row"] = []
            return
        if tl in {"th", "td"}:
            if self.table_stack:
                self.table_stack[-1]["in_cell"] = True
                self.table_stack[-1]["cell_buf"] = []
            return
        if tl == "span":
            data = attrd.get("data-checkbox")
            if data is not None:
                self.parts.append("☑ " if data.strip() == "x" else "☐ ")
            return
        if tl == "div":
            self._ensure_break()
            self.stack.append({"tag": "div"})
            return

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl in {"style", "script"} and self.in_style_or_script > 0:
            self.in_style_or_script -= 1
            return
        if self.in_style_or_script:
            return
        if tl == "blockquote":
            if self.bq_count > 0:
                self.bq_count -= 1
            if self.parts and self.parts[-1].startswith("> "):
                self.parts[-1] = self.parts[-1][2:]
            self.parts.append("\n\n")
            if self.stack and self.stack[-1].get("tag") == "blockquote":
                self.stack.pop()
            return
        if tl in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div"}:
            self.parts.append("\n\n")
            if self.stack and self.stack[-1].get("tag") == tl:
                self.stack.pop()
            return
        if tl in _INLINE_MARKERS and not self.in_pre:
            self.parts.append(_INLINE_MARKERS[tl])
            if self.stack:
                self.stack.pop()
            return
        if tl == "pre":
            lang = ""
            if self.stack and self.stack[-1].get("tag") == "pre":
                self.stack.pop()
            buf = "".join(self.pre_buf)
            if not lang:
                m = re.match(r"```([\\w+-]+)", buf)
                if m:
                    lang = m.group(1)
            self.parts.append("\n```" + lang + "\n" + buf.rstrip() + "\n```\n\n")
            self.in_pre = False
            self.pre_buf = []
            return
        if tl == "a":
            if self.stack and self.stack[-1].get("tag") == "a":
                f = self.stack.pop()
                href = f.get("href", "")
                text = "".join(f.get("text", []))
                if not href:
                    self.parts.append(text)
                elif text.strip() == href:
                    self.parts.append("<%s>" % href)
                else:
                    self.parts.append("[%s](%s)" % (text, href))
            return
        if tl == "li":
            self.parts.append("\n")
            if self.stack and self.stack[-1].get("tag") == "li":
                self.stack.pop()
            return
        if tl in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.parts.append("\n")
            return
        if tl in {"th", "td"}:
            if self.table_stack:
                ct = "".join(self.table_stack[-1]["cell_buf"]).strip().replace("|", "\\|")
                self.table_stack[-1]["cur_row"].append(ct)
                self.table_stack[-1]["in_cell"] = False
                self.table_stack[-1]["cell_buf"] = []
            return
        if tl == "tr":
            if self.table_stack and self.table_stack[-1]["cur_row"] is not None:
                row = self.table_stack[-1]["cur_row"]
                if not self.table_stack[-1]["had_header"] and not self.table_stack[-1]["rows"]:
                    self.table_stack[-1]["had_header"] = True
                    self.table_stack[-1]["rows"].append(("h", row))
                else:
                    self.table_stack[-1]["rows"].append(("d", row))
            return
        if tl == "table":
            if not self.table_stack:
                return
            rows = self.table_stack.pop()["rows"]
            if not rows:
                return
            width = max((len(r[1]) for r in rows), default=0)
            if not width:
                return
            header = list(rows[0][1]) + [" "] * (width - len(rows[0][1]))
            sep = ["---"] * width
            out_lines = ["| " + " | ".join(header) + " |",
                         "| " + " | ".join(sep) + " |"]
            for _k, r in rows[1:]:
                rr = list(r) + [" "] * (width - len(r))
                out_lines.append("| " + " | ".join(rr) + " |")
            self.parts.append("\n" + "\n".join(out_lines) + "\n\n")
            return

    def handle_data(self, data):
        if self.in_style_or_script:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        if self.stack and self.stack[-1].get("tag") == "a":
            self.stack[-1].setdefault("text", []).append(data)
            return
        if self.table_stack and self.table_stack[-1]["in_cell"]:
            self.table_stack[-1]["cell_buf"].append(data)
            return
        self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(html.unescape("&%s;" % name))

    def handle_charref(self, name):
        try:
            ch = chr(int(name[1:], 16)) if name[:1] in "xX" else chr(int(name))
        except ValueError:
            ch = ""
        self.handle_data(ch)

    def get_markdown(self):
        s = "".join(self.parts)
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = "\n".join(line.rstrip() for line in s.split("\n"))
        return s.strip("\n")


def html_to_markdown_builtin(html_text):
    p = _HtmlToMd()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    return p.get_markdown()


def html_to_markdown_pandoc(html_text):
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    try:
        proc = subprocess.run([pandoc, "-f", "html", "-t", "gfm-raw_html",
                               "--wrap=none", "--markdown-headings=atx"],
                              input=html_text.encode("utf-8"),
                              capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace")
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out.strip("\n")


def html_to_markdown(html_text, engine):
    if engine == "builtin":
        return html_to_markdown_builtin(html_text), "builtin"
    if engine == "pandoc":
        out = html_to_markdown_pandoc(html_text)
        if out is not None:
            return _strip_tomark_pass(out), "pandoc"
        return _strip_tomark_pass(html_to_markdown_builtin(html_text)), "builtin"
    out = html_to_markdown_pandoc(html_text)
    if out is not None:
        return _strip_tomark_pass(out), "pandoc"
    return _strip_tomark_pass(html_to_markdown_builtin(html_text)), "builtin"


# Pandoc 写 GFM 时会给表格单元格内的 <br> 加 data-tomark-pass 属性。
# 引擎无关的后处理：把 `\<br data-tomark-pass>` / `<br data-tomark-pass>`
# / `<br data-tomark-pass/>` / `<br data-tomark-pass />` 统一替换成 `<br>`，
# 去掉反斜杠转义。builtin 引擎本身不会产生该标记，但同样过这一遍以
# 保证 "最终产物中不得出现字符串 tomark-pass" 的不变量。
_TOMARK_PASS_RE = re.compile(r'\\?<br\s+data-tomark-pass\s*/?\\?>')


def _strip_tomark_pass(md):
    if not md or "tomark-pass" not in md:
        return md
    return _TOMARK_PASS_RE.sub('<br>', md)


# `--assets none` 时：把正文里所有指向 attachments/ 的链接替换为
# 纯文本占位符，避免产生断链图片/附件引用。同时计数供报告使用。
_IMG_REF_RE = re.compile(r'!\[[^\]]*\]\(attachments/([^)]+)\)')
_ATT_LINK_RE = re.compile(r'\[([^\]]+)\]\(attachments/([^)]+)\)')


def _replace_attachments_with_placeholders(md, counters):
    """Replace ``![](attachments/X)`` and ``[name](attachments/Y)`` with text."""

    def img_sub(m):
        counters["unreferenced_assets"] += 1
        return "[图片: %s]" % m.group(1)

    def att_sub(m):
        # Note: attachment link text in the body is not produced by the
        # current CLI when --assets none (the "## 附件" section is
        # skipped). We still rewrite defensively in case future code
        # adds such a link.
        return "[附件: %s]" % m.group(2)

    md = _IMG_REF_RE.sub(img_sub, md)
    md = _ATT_LINK_RE.sub(att_sub, md)
    return md


# ----- frontmatter ----------------------------------------------------
_YAML_UNSAFE = re.compile(r'[\\\\"]')


def yaml_quote(s):
    if s is None:
        return '""'
    return '"' + _YAML_UNSAFE.sub(lambda m: "\\\\" + m.group(0), s) + '"'


def build_frontmatter(title, notebook, tags, created, updated, source,
                      yinxiang_id, deleted_at, style):
    title_q = yaml_quote(title or "无标题")
    body = ["---", "title: %s" % title_q,
            "created: %s" % fmt_dt(created),
            "updated: %s" % fmt_dt(updated)]
    if style != "plain":
        body += ["notebook: %s" % yaml_quote(notebook or ""),
                 "tags: [%s]" % (", ".join(yaml_quote(t) for t in tags) if tags else ""),
                 "source: %s" % yaml_quote(source or ""),
                 "yinxiang_id: %s" % yinxiang_id]
        if deleted_at is not None:
            body.append("deleted_at: %s" % fmt_dt(deleted_at))
    body.append("---")
    body.append("")
    return "\n".join(body) + "\n"


# ----- filename safety ------------------------------------------------
_FN_BAD = re.compile(r'[\\\\/:*?"<>|\x00-\x1f]')


def safe_filename(name, fallback="无标题"):
    if not name:
        return fallback
    name = _FN_BAD.sub("_", name)
    name = re.sub(r"\\s+", " ", name).strip().strip(" .")
    if not name:
        return fallback
    if len(name) > 80:
        name = name[:80].rstrip(" .")
    return name or fallback


def unique_path(dirpath, base, ext=".md"):
    """Return a path whose stem is base+ext.

    Returns ``base + ext`` if it doesn't exist; otherwise searches
    ``base-2 + ext``, ``base-3 + ext``, ... until a non-existing slot.
    """
    cand = os.path.join(dirpath, base + ext)
    if not os.path.exists(cand):
        return cand
    n = 2
    while True:
        c = os.path.join(dirpath, "%s-%d%s" % (base, n, ext))
        if not os.path.exists(c):
            return c
        n += 1


# ----- single-note conversion ----------------------------------------
def _find_disk_file(on_disk_files, local_uuid):
    for full in on_disk_files:
        base = os.path.basename(full)
        if is_derived_binary(base):
            continue
        if local_uuid and base.startswith(local_uuid):
            return base
    return None


def note_to_markdown(note, tags, resources, raw_enml, on_disk_files, engine,
                      write_frontmatter, fm_style, todo_style, assets_mode, counters):
    title = note["ZTITLE"] or ""
    deleted = not bool(note["ZACTIVE"])
    deleted_dt = cdata_to_dt(note["ZDATEDELETED"])
    created = cdata_to_dt(note["ZDATECREATED"])
    updated = cdata_to_dt(note["ZDATEUPDATED"])
    local_uuid = note["ZLOCALUUID"] or ""
    source = note["ZSOURCEURL"] or ""
    notebook = note["notebook_name"] or ""

    rmap = {}
    missing = []
    resources_to_copy = []  # list of (match, target, group)

    for r in resources:
        local = r["ZLOCALUUID"] or ""
        match = _find_disk_file(on_disk_files, local)
        if not match:
            counters["missing_resources"] += 1
            continue
        target = split_uuid_and_ext(local, match)
        if r["ZDATAHASH"]:
            rmap[bytes(r["ZDATAHASH"]).hex().upper()] = target
        resources_to_copy.append((match, target,
                                  "attach" if r["ZATTACHMENT"] else "inline"))

    cleaned = preclean_enml(raw_enml, rmap, missing)
    counters["missing_media"] += len(missing)
    md_body, _ = html_to_markdown(cleaned, engine)
    # tomark-pass stripping is already applied inside html_to_markdown(),
    # but call it once more defensively in case a future engine returns
    # a string that still contains the marker.
    md_body = _strip_tomark_pass(md_body)
    if assets_mode == "none":
        md_body = _replace_attachments_with_placeholders(md_body, counters)

    if todo_style == "task":
        def _sub(m):
            mark, after = m.group(1), m.group(2)
            return ("- [x] " if mark == "☑" else "- [ ] ") + after
        md_body = re.sub(r"^([☑☐])\s+(.*)$", _sub, md_body, flags=re.MULTILINE)

    if assets_mode != "none" and any(r["ZATTACHMENT"] for r in resources):
        md_body = md_body.rstrip() + "\n\n## 附件\n\n"
        for r in resources:
            if not r["ZATTACHMENT"]:
                continue
            local = r["ZLOCALUUID"] or ""
            match = _find_disk_file(on_disk_files, local)
            if not match:
                continue
            target = split_uuid_and_ext(local, match)
            md_body += "- [%s](attachments/%s)\n" % (r["ZFILENAME"] or target, target)
            counters["attachment_links"] += 1

    fm = ""
    if write_frontmatter:
        fm = build_frontmatter(title=title, notebook=notebook, tags=tags,
                               created=created, updated=updated, source=source,
                               yinxiang_id=local_uuid,
                               deleted_at=deleted_dt if deleted else None,
                               style=fm_style)
    final = fm + md_body + "\n"
    return {"markdown": final, "title": title, "local_uuid": local_uuid,
            "deleted": deleted, "notebook": notebook, "created": created,
            "updated": updated, "resources_to_copy": resources_to_copy,
            "body_size": len(raw_enml.encode("utf-8"))}


# ----- driver ---------------------------------------------------------
def _layout_dir(out_root, notebook, deleted, layout):
    if deleted:
        return os.path.join(out_root, "_已删除_回收站", notebook or "未分类")
    if layout == "flat":
        return out_root
    return os.path.join(out_root, notebook or "未分类")


def _copy_assets(targets, on_disk_files, note_dir, assets_mode, assets_root, counters):
    if assets_mode == "none":
        return
    out_dir = assets_root if assets_mode == "flat" else os.path.join(note_dir, "attachments")
    if out_dir is None:
        return
    os.makedirs(out_dir, exist_ok=True)
    for match, target, _g in targets:
        src = next((f for f in on_disk_files if os.path.basename(f) == match), None)
        if not src or not os.path.exists(src):
            continue
        canonical = os.path.join(out_dir, target)
        if os.path.exists(canonical):
            continue
        stem, ext = os.path.splitext(target)
        dst = unique_path(out_dir, stem, ext=ext)
        try:
            shutil.copy2(src, dst)
            counters["copied_assets"] += 1
        except OSError:
            counters["missing_resources"] += 1



def _allocate_md_path(note_dir, stem, note_pk, by_stem):
    """Return the .md path to use for this note.

    The conflict-detection key is ``(note_dir, stem)`` so that:

    * identical titles across different notebooks do not collide
      (e.g. ``Plant-A/会议.md`` and ``新闻/会议.md`` may both exist);
    * the deleted-trash directory ``_已删除_回收站/<笔记本>/`` does not
      interfere with the active notebook of the same name.

    Two-level lookup within a single (dir, stem) pair:
    1. ``by_stem`` mapping (cleared each run) ensures identically-titled
       notes within ONE directory get distinct slots (``-2``/``-3``).
    2. If the canonical or computed slot is already occupied by an
       older file (idempotent re-run), fall forward to the next free
       numbered slot.
    """
    key = (note_dir, stem)
    mapping = by_stem.setdefault(key, {})
    if note_pk in mapping:
        return mapping[note_pk]
    n = len(mapping)
    if n == 0:
        path = os.path.join(note_dir, stem + ".md")
    else:
        path = os.path.join(note_dir, "%s-%d.md" % (stem, n + 1))
    # If the computed slot is taken by an older file (idempotent re-run),
    # the right thing to do is OVERWRITE it, not skip to -N+1.
    # The first time a slot is taken it stays taken; later runs simply
    # overwrite. This guarantees the file set stays identical.
    mapping[note_pk] = path
    return path


def convert_one(args):
    try:
        note = args["note"]
        deleted = not bool(note["ZACTIVE"])
        notebook = note["notebook_name"] or ""
        note_dir = _layout_dir(args["out_root"], notebook, deleted, args["layout"])
        assets_root = (os.path.join(args["out_root"], "_assets")
                       if args["assets_mode"] == "flat" else None)
        result = note_to_markdown(
            note=note, tags=args["tags"], resources=args["resources"],
            raw_enml=args["raw_enml"], on_disk_files=args["on_disk_files"],
            engine=args["engine"], write_frontmatter=args["write_frontmatter"],
            fm_style=args["fm_style"], todo_style=args["todo_style"],
            assets_mode=args["assets_mode"], counters=args["counters"])
        if not args["dry_run"]:
            os.makedirs(note_dir, exist_ok=True)
            stem = safe_filename(result["title"])
            seen = args["seen_stems"]
            path = _allocate_md_path(note_dir, stem, args["note"]["Z_PK"], seen)
            with open(path, "w", encoding="utf-8") as f:
                f.write(result["markdown"])
            _copy_assets(result["resources_to_copy"], args["on_disk_files"],
                         note_dir, args["assets_mode"], assets_root, args["counters"])
        args["counters"]["success"] += 1
        if result["body_size"] < 200:
            args["counters"]["near_empty"] += 1
        if not result["title"]:
            args["counters"]["no_title"] += 1
        return {"ok": True, "title": result["title"], "notebook": notebook,
                "deleted": deleted, "local_uuid": result["local_uuid"]}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "title": (args.get("note")["ZTITLE"] if args.get("note") else "?"),
                "notebook": (args.get("note")["notebook_name"] if args.get("note") else ""),
                "deleted": (not bool(args["note"]["ZACTIVE"]) if args.get("note") else False),
                "local_uuid": (args.get("note")["ZLOCALUUID"] if args.get("note") else "")}


def build_report(stats, source):
    L = ["# 迁移报告", "",
         "- 运行时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "- 源库路径: %s" % source,
         "- 成功: %d" % stats["success"],
         "- 失败: %d" % stats["fail"],
         "- 跳过: %d" % stats["skipped"],
         "- 近空笔记（< 200 字节正文）: %d" % stats["near_empty"],
         "- 无标题笔记: %d" % stats["no_title"],
         "- 复制资源数: %d" % stats["copied_assets"],
         "- 附件链接数: %d" % stats["attachment_links"],
         "- 缺失资源引用: %d" % stats["missing_resources"],
         "- [附件缺失] 占位数: %d" % stats["missing_media"],
         "- 未复制资源数: %d" % stats["unreferenced_assets"], "",
         "## 按笔记本", ""]
    if stats["by_notebook"]:
        for nb, n in sorted(stats["by_notebook"].items()):
            L.append("- %s: %d" % (nb, n))
    else:
        L.append("（无）")
    L.extend(["", "## 失败清单", ""])
    if stats["failures"]:
        for f in stats["failures"]:
            L.append("- %s [%s] - %s" % (f["title"], f["local_uuid"][:8], f["error"]))
    else:
        L.append("（无）")
    L.append("")
    return "\n".join(L) + "\n"


# ----- CLI ------------------------------------------------------------
def parse_args(argv):
    # Use ArgumentDefaultsHelpFormatter so every ``default=...`` value
    # (notably --jobs=1) is visible in ``--help``. Default behaviour in
    # Python 3.14+ no longer prints defaults unless the formatter asks
    # for them.
    p = argparse.ArgumentParser(prog="yinxiang2md.py",
        description="Convert Yinxiang (印象笔记) local library to Markdown.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--source", help="Path to a Yinxiang account directory.")
    p.add_argument("--list-accounts", action="store_true",
                   help="List discovered accounts and exit.")
    p.add_argument("-o", "--out", help="Output directory.")
    p.add_argument("--layout", choices=("notebook", "flat"), default="notebook")
    p.add_argument("--assets", choices=("folder", "flat", "none"), default="folder")
    p.add_argument("--include-deleted", action="store_true",
                   help="Include trashed notes.")
    p.add_argument("--notebook", action="append", default=None,
                   help="Only convert named notebook (repeatable).")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", default=None)
    p.add_argument("--engine", choices=("auto", "pandoc", "builtin"), default="auto")
    p.add_argument("--no-frontmatter", action="store_true")
    p.add_argument("--frontmatter-style", choices=("obsidian", "plain"),
                   default="obsidian")
    p.add_argument("--todo-style", choices=("char", "task"), default="char")
    p.add_argument("--jobs", type=int, default=1,
                   help="Parallel conversion worker count.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if args.version:
        print("yinxiang2md.py %s" % VERSION)
        return 0
    if args.list_accounts:
        for a in discover_accounts():
            print(a)
        return 0
    if args.source:
        source = os.path.expanduser(args.source)
        if not looks_like_account_dir(source):
            print("错误：%s 不是有效账号目录（缺少 localNoteStore/LocalNoteStore.sqlite）"
                  % source, file=sys.stderr)
            return 1
    else:
        accs = discover_accounts()
        if not accs:
            print("错误：未找到默认位置下的账号目录：%s。请用 --source 指定。"
                  % default_yinxiang_root(), file=sys.stderr)
            return 1
        if len(accs) > 1:
            print("错误：发现多个账号目录，请用 --source 指定其中一个：", file=sys.stderr)
            for a in accs:
                print("  " + a, file=sys.stderr)
            return 1
        source = accs[0]
    if not args.out and not args.dry_run:
        print("错误：未指定输出目录 -o/--out（除非配合 --dry-run）。", file=sys.stderr)
        return 1
    out_root = (os.path.expanduser(args.out) if args.out
                else tempfile.mkdtemp(prefix="yinxiang2md_dryrun_"))
    if args.engine == "auto":
        engine_used = "pandoc" if shutil.which("pandoc") else "builtin"
    elif args.engine == "pandoc":
        if shutil.which("pandoc"):
            engine_used = "pandoc"
        else:
            print("（提示：未找到 pandoc，回退到 builtin 引擎。）", file=sys.stderr)
            engine_used = "builtin"
    else:
        engine_used = "builtin"

    db_path = os.path.join(source, "localNoteStore", "LocalNoteStore.sqlite")
    try:
        conn = open_db_readonly(db_path)
    except sqlite3.Error as e:
        print("错误：无法打开数据库: %s" % e, file=sys.stderr)
        return 1

    load_notebooks(conn)
    load_tags(conn)
    notes = load_notes(conn, include_deleted=args.include_deleted,
                       notebooks_filter=tuple(args.notebook) if args.notebook else None)
    if args.limit:
        notes = notes[:args.limit]
    pks = [int(n["Z_PK"]) for n in notes]
    ntags = load_tags_for_notes(conn, pks)
    nres = load_resources(conn, pks)
    content_dir = os.path.join(source, "content")

    seen_stems: dict = {}
    counters = {"success": 0, "fail": 0, "skipped": 0, "near_empty": 0,
                "no_title": 0, "copied_assets": 0, "attachment_links": 0,
                "missing_resources": 0, "missing_media": 0,
                "unreferenced_assets": 0}
    if not args.include_deleted:
        # Trashed notes are excluded from conversion; record how many
        # were left behind so the report's "跳过" line is non-zero when
        # relevant.
        counters["skipped"] = count_skipped(
            conn, tuple(args.notebook) if args.notebook else None)
    by_notebook = {}
    failures = []
    dry_actions = []

    work = []
    for n in notes:
        pk = int(n["Z_PK"])
        local = n["ZLOCALUUID"] or ""
        body = read_note_body(content_dir, local)
        on_disk = list_resource_files(content_dir, local)
        work.append({"note": n, "tags": ntags.get(pk, []),
                     "resources": nres.get(pk, []), "raw_enml": body,
                     "on_disk_files": on_disk, "engine": engine_used,
                     "write_frontmatter": not args.no_frontmatter,
                     "fm_style": args.frontmatter_style,
                     "todo_style": args.todo_style, "out_root": out_root,
                     "layout": args.layout, "assets_mode": args.assets,
                     "dry_run": args.dry_run, "counters": counters,
                     "seen_stems": seen_stems})

    if args.jobs and args.jobs > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            results = [f.result() for f in [ex.submit(convert_one, w) for w in work]]
    else:
        results = [convert_one(w) for w in work]

    for w, r in zip(work, results):
        nb = (w["note"]["notebook_name"] or "")
        if r["ok"]:
            by_notebook[nb] = by_notebook.get(nb, 0) + 1
        else:
            counters["fail"] += 1
            failures.append(r)
        if args.dry_run:
            tag = "[已删]" if not bool(w["note"]["ZACTIVE"]) else ""
            dry_actions.append("  - %s [%s] %s" % (tag, nb or "未分类",
                                                   r.get("title") or "无标题"))

    stats = dict(counters)
    stats["by_notebook"] = by_notebook
    stats["failures"] = failures

    if not args.dry_run:
        os.makedirs(out_root, exist_ok=True)
        rp = args.report or os.path.join(out_root, "_迁移报告.md")
        with open(rp, "w", encoding="utf-8") as f:
            f.write(build_report(stats, source))
        if not args.quiet:
            print("已写出报告: %s" % rp)
    else:
        if not args.quiet:
            print("（dry-run）将处理 %d 条笔记：" % len(work))
            for line in dry_actions:
                print(line)

    return 2 if counters["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
