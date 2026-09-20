#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_fixture.py - Build a synthetic Yinxiang account directory for tests.

This script writes a directory that *looks* like a Yinxiang local library:
    <out>/localNoteStore/LocalNoteStore.sqlite
    <out>/content/<note_uuid>/content.enml
    <out>/content/<note_uuid>/<resource_uuid>[.ext]

It contains 3 notebooks, 2 tags and 6 notes covering the various edge cases
yinxiang2md.py must handle. Everything is purely synthetic - no real data.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys

# A fixed reference timestamp for predictable CoreData math tests:
# 2017-11-13 11:12:00 UTC
# seconds between 2001-01-01 UTC and 2017-11-13 11:12:00 UTC.
REFERENCE_COREDATA = 532264320.0  # 2017-11-13 11:12:00 UTC
# 2017-11-14 10:45:00 UTC
REFERENCE_COREDATA_UPD = 532349100.0  # 2017-11-14 10:45:00 UTC


def _ensure_dir(p):
    os.makedirs(p, exist_ok=True)


def _connect(path):
    # ensure parent exists
    _ensure_dir(os.path.dirname(path))
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    if fresh:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _create_schema(c):
    # CoreData-style schema (subset). Indexes omitted - test data is tiny.
    c.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME TEXT, Z_SUPER INTEGER, Z_MAX INTEGER)")
    c.execute("CREATE TABLE Z_METADATA (Z_ENT INTEGER, Z_NAME TEXT, Z_VALUE INTEGER)")
    c.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, Z_ENT INTEGER, ZNAME TEXT)")
    c.execute("CREATE TABLE ZENTAG (Z_PK INTEGER, Z_ENT INTEGER, ZNAME TEXT)")
    c.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, Z_ENT INTEGER, "
              "ZLOCALUUID TEXT, ZGUID TEXT, ZACTIVE INTEGER, "
              "ZTITLE TEXT, ZNOTEBOOK INTEGER, "
              "ZDATECREATED REAL, ZDATEUPDATED REAL, ZDATEDELETED REAL, "
              "ZSOURCEURL TEXT)")
    c.execute("CREATE TABLE ZENRESOURCE (Z_PK INTEGER, Z_ENT INTEGER, "
              "ZNOTE INTEGER, ZLOCALUUID TEXT, ZGUID TEXT, "
              "ZDATAHASH BLOB, ZATTACHMENT INTEGER, "
              "ZFILENAME TEXT, ZMIME TEXT)")
    c.execute("CREATE TABLE Z_10TAGS (Z_10NOTES INTEGER, Z_23TAGS INTEGER)")
    for tbl in ("ZENNOTEBOOK", "ZENTAG", "ZENNOTE", "ZENRESOURCE"):
        c.execute("INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) "
                  "VALUES (%d, '%s', 1, 100)" % (
                      {"ZENNOTEBOOK": 6, "ZENTAG": 11, "ZENNOTE": 4, "ZENRESOURCE": 5}[tbl], tbl))


def build(out_dir, verbose=False):
    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    _ensure_dir(out_dir)

    db_path = os.path.join(out_dir, "localNoteStore", "LocalNoteStore.sqlite")
    conn = _connect(db_path)
    _create_schema(conn)

    # Notebooks
    notebooks = [("日常",), ("学习",), ("回收站",)]
    for i, (name,) in enumerate(notebooks, start=1):
        conn.execute("INSERT INTO ZENNOTEBOOK (Z_PK, Z_ENT, ZNAME) VALUES (?, ?, ?)",
                     (i, 6, name))
    # Tags
    tags = [("工作",), ("生活",)]
    for i, (name,) in enumerate(tags, start=1):
        conn.execute("INSERT INTO ZENTAG (Z_PK, Z_ENT, ZNAME) VALUES (?, ?, ?)", (i, 11, name))

    content_dir = os.path.join(out_dir, "content")
    _ensure_dir(content_dir)

    # Helpers
    pk_note = 100
    pk_res = 200

    def add_note(title, notebook_pk, body_xml, tags_pks=(), active=True,
                 deleted=None, source="", created=REFERENCE_COREDATA,
                 updated=REFERENCE_COREDATA_UPD):
        nonlocal pk_note
        note_uuid = "note-uuid-%04d" % pk_note
        conn.execute(
            "INSERT INTO ZENNOTE (Z_PK, Z_ENT, ZLOCALUUID, ZGUID, ZACTIVE, "
            "ZTITLE, ZNOTEBOOK, ZDATECREATED, ZDATEUPDATED, ZDATEDELETED, ZSOURCEURL) "
            "VALUES (?, 4, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pk_note, note_uuid, note_uuid, 1 if active else 0, title,
             notebook_pk, created, updated, deleted, source))
        for tp in tags_pks:
            conn.execute("INSERT INTO Z_10TAGS (Z_10NOTES, Z_23TAGS) VALUES (?, ?)",
                         (pk_note, tp))
        # Write content.enml
        _ensure_dir(os.path.join(content_dir, note_uuid))
        with open(os.path.join(content_dir, note_uuid, "content.enml"),
                  "w", encoding="utf-8") as f:
            f.write(body_xml)
        cur_pk = pk_note
        pk_note += 1
        return note_uuid, cur_pk

    def add_resource(note_pk, note_uuid, ext, bdata, attachment=False,
                     filename=None):
        nonlocal pk_res
        res_uuid = "res-uuid-%04d" % pk_res
        # md5 hash as 16 raw bytes (this is what ZDATAHASH stores)
        import hashlib
        digest = hashlib.md5(bdata).digest()
        # Determine filename: for inline images we want <res_uuid>.<ext>
        fname = filename if filename else (res_uuid + ext)
        conn.execute(
            "INSERT INTO ZENRESOURCE (Z_PK, Z_ENT, ZNOTE, ZLOCALUUID, ZGUID, "
            "ZDATAHASH, ZATTACHMENT, ZFILENAME, ZMIME) "
            "VALUES (?, 5, ?, ?, ?, ?, ?, ?, ?)",
            (pk_res, note_pk, res_uuid, res_uuid, digest,
             1 if attachment else 0, fname,
             "image/png" if ext == ".png" else "application/octet-stream"))
        with open(os.path.join(content_dir, note_uuid, fname), "wb") as f:
            f.write(bdata)
        cur_pk = pk_res
        pk_res += 1
        return res_uuid

    # ------------------------------------------------------------------
    # Note 1: rich text - headings, lists, table, code, link.
    enml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
        '<en-note>'
        '<h1>一级标题</h1>'
        '<p>这是一段普通段落。<strong>加粗</strong>与<em>斜体</em>。</p>'
        '<ul><li>项目一</li><li>项目二<ul><li>嵌套</li></ul></li></ul>'
        '<ol><li>有序一</li><li>有序二</li></ol>'
        '<pre><code>print("hello")\nx = 1\n</code></pre>'
        '<table><thead><tr><th>列A</th><th>列B</th></tr></thead>'
        '<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr>'
        '</tbody></table>'
        '<blockquote>引用文字</blockquote>'
        '<p>链接 <a href="https://example.com">示例</a></p>'
        '</en-note>')
    add_note("富文本示例", 1, enml, tags_pks=(1,))

    # ------------------------------------------------------------------
    # Note 2: inline images + derived binary (which must be filtered).
    enml2 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<en-note><p>看这张图：'
        '<en-media hash="IMG_HASH_PLACEHOLDER_A" type="image/png"/>'
        '<en-media hash="IMG_HASH_PLACEHOLDER_B" type="image/png"/>'
        '</p></en-note>')
    n2_uuid, n2_pk = add_note("图片笔记", 1, enml2, tags_pks=(2,))
    img_a = b"FAKE-IMAGE-A" * 16
    img_b = b"FAKE-IMAGE-B" * 16
    add_resource(n2_pk, n2_uuid, ".png", img_a)
    add_resource(n2_pk, n2_uuid, ".png", img_b)
    # Now patch the enml with the actual MD5 hashes (upper-case hex)
    import hashlib
    h_a = hashlib.md5(img_a).hexdigest().upper()
    h_b = hashlib.md5(img_b).hexdigest().upper()
    p = os.path.join(content_dir, n2_uuid, "content.enml")
    with open(p, "r", encoding="utf-8") as f:
        body = f.read()
    body = body.replace("IMG_HASH_PLACEHOLDER_A", h_a)
    body = body.replace("IMG_HASH_PLACEHOLDER_B", h_b)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    # Drop a derived binary alongside: must NOT be copied.
    last_res_uuid = "res-uuid-0201"
    with open(os.path.join(content_dir, n2_uuid, last_res_uuid + ".en-reco"),
              "wb") as f:
        f.write(b"reco junk")
    with open(os.path.join(content_dir, n2_uuid, "snippet.tiff"), "wb") as f:
        f.write(b"\x00\x01\x02")

    # ------------------------------------------------------------------
    # Note 3: attachment + todo boxes.
    enml3 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<en-note>'
        '<en-todo checked="true"/>已完成<br/>'
        '<en-todo checked="false"/>未完成<br/>'
        '</en-note>')
    n3_uuid, n3_pk = add_note("附件与待办", 2, enml3, tags_pks=(1, 2))
    add_resource(n3_pk, n3_uuid, ".xlsx", b"FAKE-XLSX", attachment=True,
                 filename="res-uuid-0202.xlsx")

    # ------------------------------------------------------------------
    # Note 4: trashed note.
    enml4 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<en-note><p>这条在回收站。</p></en-note>')
    add_note("已删除笔记", 2, enml4, active=False,
             deleted=REFERENCE_COREDATA + 1000)

    # ------------------------------------------------------------------
    # Note 5: empty body (near-empty).
    add_note("空笔记", 1, "<en-note></en-note>")

    # ------------------------------------------------------------------
    # Note 6: title with all forbidden chars.
    bad = "a/b:c*d?e\"f<g>h|i"
    enml6 = '<?xml version="1.0" encoding="UTF-8"?><en-note><p>x</p></en-note>'
    add_note(bad, 1, enml6)
    # + one more with the same bad title to force -2 suffix
    add_note(bad, 1, enml6)

    conn.commit()
    conn.close()
    if verbose:
        print("Fixture at %s" % out_dir)


def main(argv=None):
    p = argparse.ArgumentParser(prog="make_fixture.py")
    p.add_argument("--out", required=True, help="Output directory.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    build(args.out, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
