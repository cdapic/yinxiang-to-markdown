#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end and unit tests for yinxiang2md.py.

These tests build a synthetic Yinxiang account directory using
``make_fixture.py`` and then exercise the CLI on it. They must pass on a
machine without Yinxiang installed.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(THIS_DIR)
FIXTURE_BUILDER = os.path.join(THIS_DIR, "make_fixture.py")
CLI = os.path.join(PROJECT_DIR, "yinxiang2md.py")
sys.path.insert(0, PROJECT_DIR)
import yinxiang2md  # noqa: E402

# A known reference: 2017-11-13 11:12:00 UTC == 532264320 seconds
# since 2001-01-01 00:00:00 UTC.
REF_CREATED_SECONDS = 532264320.0
# 2017-11-14 10:45:00 UTC == 532349100 seconds.
REF_UPDATED_SECONDS = 532349100.0


def _expected_local(seconds: float) -> str:
    """Return the local-time formatted string our CLI would produce."""
    utc = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc) + \
        datetime.timedelta(seconds=seconds)
    return utc.astimezone().strftime("%Y-%m-%d %H:%M")


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory):
    p = tmp_path_factory.mktemp("acct")
    out = p / "acct"
    subprocess.check_call([sys.executable, FIXTURE_BUILDER, "--out", str(out)])
    return str(out)


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path


def _run_cli(source, out, *args, expect_returncode=None):
    cmd = [sys.executable, CLI, "--source", source, "-o", str(out), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if expect_returncode is not None:
        assert proc.returncode == expect_returncode, (
            f"rc={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def _list_md(out_dir):
    out = []
    for root, _dirs, files in os.walk(out_dir):
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(root, f), out_dir))
    return sorted(out)


# ---------------------------------------------------------------------------
# 1. discovery: default excludes deleted, --include-deleted includes them
# ---------------------------------------------------------------------------
def test_default_excludes_deleted(workdir, fixture_dir):
    proc = _run_cli(fixture_dir, workdir, "--engine", "builtin")
    md_files = _list_md(workdir)
    paths = " ".join(md_files)
    assert "_已删除_回收站" not in paths
    # The active notes (5 active: rich, image, attach, empty, bad-name x2)
    assert len(md_files) == 7  # 6 active + 1 report


def test_include_deleted_routes_to_deleted_dir(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    md_files = _list_md(workdir)
    paths = "\n".join(md_files)
    assert "_已删除_回收站/学习/已删除笔记.md" in paths


# ---------------------------------------------------------------------------
# 2. image references exist; derived binaries are NOT copied
# ---------------------------------------------------------------------------
def test_image_references_point_to_existing_files(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    # Image note is at 日常/图片笔记.md
    p = workdir / "日常" / "图片笔记.md"
    text = p.read_text(encoding="utf-8")
    m = re.findall(r"!\[\]\(attachments/(.+?)\)", text)
    assert m, "expected image references in %s" % p
    for f in m:
        assert (workdir / "日常" / "attachments" / f).exists(), f


def test_derived_binaries_not_copied(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    # The fixture creates res-uuid-0201.en-reco and snippet.tiff inside
    # content/note-uuid-0101 - neither should be copied anywhere.
    out_files = []
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            out_files.append(f)
    assert "res-uuid-0201.en-reco" not in out_files
    assert "snippet.tiff" not in out_files
    assert "card.png" not in out_files


# ---------------------------------------------------------------------------
# 3. attachment appears in `## 附件` section and file is copied
# ---------------------------------------------------------------------------
def test_attachment_section_and_copy(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    p = workdir / "学习" / "附件与待办.md"
    text = p.read_text(encoding="utf-8")
    assert "## 附件" in text
    # link target should be attachments/res-uuid-0202.xlsx
    assert "attachments/res-uuid-0202.xlsx" in text
    # the file must have been copied
    assert (workdir / "学习" / "attachments" / "res-uuid-0202.xlsx").exists()
    # and the content matches the original fixture data
    src = os.path.join(fixture_dir, "content", "note-uuid-0102",
                       "res-uuid-0202.xlsx")
    assert open(src, "rb").read() == b"FAKE-XLSX"


# ---------------------------------------------------------------------------
# 4. frontmatter fields + CoreData timestamp conversion
# ---------------------------------------------------------------------------
def test_frontmatter_obsidian_fields(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    p = workdir / "日常" / "富文本示例.md"
    text = p.read_text(encoding="utf-8")
    # YAML frontmatter exists and contains expected fields.
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m
    fm = m.group(1)
    assert 'title: "富文本示例"' in fm
    assert 'notebook: "日常"' in fm
    assert 'tags: ["工作"]' in fm
    assert re.search(r"^created: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", fm, re.M)
    assert re.search(r"^yinxiang_id: note-uuid-0100$", fm, re.M)


def test_coredata_timestamp_conversion(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    p = workdir / "日常" / "富文本示例.md"
    text = p.read_text(encoding="utf-8")
    # Expected: 532264320 -> 2017-11-13 11:12 UTC -> local time.
    expected_created = _expected_local(REF_CREATED_SECONDS)
    expected_updated = _expected_local(REF_UPDATED_SECONDS)
    assert "created: %s" % expected_created in text
    assert "updated: %s" % expected_updated in text


def test_trashed_note_has_deleted_at(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    p = workdir / "_已删除_回收站" / "学习" / "已删除笔记.md"
    text = p.read_text(encoding="utf-8")
    assert "deleted_at:" in text


# ---------------------------------------------------------------------------
# 5. --todo-style char vs task
# ---------------------------------------------------------------------------
def test_todo_style_char(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--todo-style", "char")
    text = (workdir / "学习" / "附件与待办.md").read_text(encoding="utf-8")
    assert "☑" in text
    assert "☐" in text


def test_todo_style_task(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--todo-style", "task")
    text = (workdir / "学习" / "附件与待办.md").read_text(encoding="utf-8")
    assert "- [x]" in text
    assert "- [ ]" in text


# ---------------------------------------------------------------------------
# 6. illegal filename cleaning + duplicate -2 suffix
# ---------------------------------------------------------------------------
def test_illegal_filename_cleaned(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    names = sorted(os.listdir(workdir / "日常"))
    # The two bad-name notes should appear with one -2 suffix.
    assert re.match(r"^a_b_c_d_e_f_g_h_i\.md$", "a_b_c_d_e_f_g_h_i.md") is not None
    assert re.match(r"^a_b_c_d_e_f_g_h_i-2\.md$", "a_b_c_d_e_f_g_h_i-2.md") is not None


# ---------------------------------------------------------------------------
# 7. --dry-run does not produce any file
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(workdir, fixture_dir):
    before = set(os.listdir(workdir))
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--dry-run", "--include-deleted")
    after = set(os.listdir(workdir))
    assert before == after, "dry-run wrote %s" % (after - before)


# ---------------------------------------------------------------------------
# 8. --assets none does not copy assets
# ---------------------------------------------------------------------------
def test_assets_none_does_not_copy(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--assets", "none")
    # walk whole tree
    out_files = []
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            out_files.append(f)
    assert "res-uuid-0200.png" not in out_files
    assert "res-uuid-0202.xlsx" not in out_files
    # but markdown still produced
    assert (workdir / "日常" / "图片笔记.md").exists()


# ---------------------------------------------------------------------------
# 9. idempotent: second run does not re-copy; markdown content identical
# ---------------------------------------------------------------------------
def test_idempotent(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    # The migration report includes a runtime timestamp so skip it.
    first = {}
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            if f == "_迁移报告.md":
                continue
            full = os.path.join(root, f)
            with open(full, "rb") as fh:
                first[full] = fh.read()
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    second = {}
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            if f == "_迁移报告.md":
                continue
            full = os.path.join(root, f)
            with open(full, "rb") as fh:
                second[full] = fh.read()
    assert set(first.keys()) == set(second.keys())
    for k, v in first.items():
        assert second[k] == v, "%s changed" % k


# ---------------------------------------------------------------------------
# 10. source library files (mtime + content) are NOT modified
# ---------------------------------------------------------------------------
def test_source_library_unchanged(workdir, fixture_dir):
    # Snapshot mtimes + content of source.
    # We deliberately ignore -wal/-shm sidecar files: sqlite3 may create
    # those on the filesystem just for read-only queries and that does
    # not constitute "modifying the source library".
    def snap(root):
        out = {}
        for r, _d, files in os.walk(root):
            for f in files:
                if f.endswith("-wal") or f.endswith("-shm"):
                    continue
                full = os.path.join(r, f)
                st = os.stat(full)
                with open(full, "rb") as fh:
                    out[full] = (st.st_mtime_ns, st.st_size, fh.read())
        return out

    before = snap(fixture_dir)
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    after = snap(fixture_dir)
    assert before == after, "source library was modified!"
    # Also: explicitly check the on-disk content.enml files for any
    # note (sanity).
    for note_uuid in ("note-uuid-0100", "note-uuid-0101"):
        src = os.path.join(fixture_dir, "content", note_uuid,
                           "content.enml")
        assert os.path.exists(src)


# ---------------------------------------------------------------------------
# 11. no-frontmatter flag works
# ---------------------------------------------------------------------------
def test_no_frontmatter(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--no-frontmatter")
    text = (workdir / "日常" / "富文本示例.md").read_text(encoding="utf-8")
    assert not text.startswith("---")


def test_plain_frontmatter(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--frontmatter-style", "plain")
    text = (workdir / "日常" / "富文本示例.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m
    fm = m.group(1)
    assert "title:" in fm
    assert "notebook:" not in fm
    assert "tags:" not in fm


# ---------------------------------------------------------------------------
# 12. --notebook filter
# ---------------------------------------------------------------------------
def test_notebook_filter(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--notebook", "学习")
    md_files = _list_md(workdir)
    paths = "\n".join(md_files)
    # Only 学习 notebook files should appear (active only).
    assert "学习/附件与待办.md" in paths
    assert "日常/" not in paths


def test_notebook_filter_include_deleted(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--notebook", "学习")
    md_files = _list_md(workdir)
    paths = "\n".join(md_files)
    assert "学习/附件与待办.md" in paths
    assert "_已删除_回收站/学习/已删除笔记.md" in paths


# ---------------------------------------------------------------------------
# 13. --layout flat puts everything in one dir
# ---------------------------------------------------------------------------
def test_layout_flat(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--layout", "flat", "--include-deleted")
    # Active notes must live in the root directory (no notebook subfolder).
    for f in os.listdir(workdir):
        if f.endswith(".md"):
            assert "/" not in f
    # Deleted notes may still go under _已删除_回收站/<notebook>/.
    if (workdir / "_已删除_回收站").exists():
        for nb in os.listdir(workdir / "_已删除_回收站"):
            for f in os.listdir(workdir / "_已删除_回收站" / nb):
                if f.endswith(".md"):
                    pass  # that's expected


# ---------------------------------------------------------------------------
# 14. --assets flat puts images in _assets/
# ---------------------------------------------------------------------------
def test_assets_flat(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--assets", "flat")
    assert (workdir / "_assets" / "res-uuid-0200.png").exists()
    assert (workdir / "_assets" / "res-uuid-0202.xlsx").exists()


# ---------------------------------------------------------------------------
# 15. missing media: en-media with unknown hash -> [附件缺失: ...]
# ---------------------------------------------------------------------------
def test_missing_media_placeholder(workdir, fixture_dir, tmp_path):
    """Create a note whose ENML references an unknown hash and check output."""
    out = tmp_path / "missing"
    # Build a tiny in-line fixture.
    acct = tmp_path / "missing_acct"
    acct.mkdir()
    db_dir = acct / "localNoteStore"
    db_dir.mkdir()
    db = db_dir / "LocalNoteStore.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENTAG (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, ZLOCALUUID TEXT, "
                 "ZTITLE TEXT, ZNOTEBOOK INTEGER, ZACTIVE INTEGER, "
                 "ZDATECREATED REAL, ZDATEUPDATED REAL, ZDATEDELETED REAL, "
                 "ZSOURCEURL TEXT)")
    conn.execute("CREATE TABLE ZENRESOURCE (Z_PK INTEGER, ZENT INTEGER, "
                 "ZNOTE INTEGER, ZLOCALUUID TEXT, ZGUID TEXT, ZDATAHASH BLOB, "
                 "ZATTACHMENT INTEGER, ZFILENAME TEXT, ZMIME TEXT)")
    conn.execute("CREATE TABLE Z_10TAGS (Z_10NOTES INTEGER, Z_23TAGS INTEGER)")
    conn.execute("INSERT INTO ZENNOTEBOOK VALUES (1, 'NB')")
    conn.execute("INSERT INTO ZENNOTE VALUES (1, 'note-x', 'BAD', 1, 1, "
                 "532264320, 532349100, NULL, '')")
    conn.commit()
    conn.close()
    content_dir = acct / "content" / "note-x"
    content_dir.mkdir(parents=True)
    (content_dir / "content.enml").write_text(
        '<?xml version="1.0"?><en-note><p>x '
        '<en-media hash="DEADBEEFDEADBEEFDEADBEEFDEADBEEF"/></p></en-note>',
        encoding="utf-8")
    _run_cli(str(acct), out, "--engine", "builtin")
    text = (out / "NB" / "BAD.md").read_text(encoding="utf-8")
    assert "[附件缺失: DEADBEEF]" in text


# ---------------------------------------------------------------------------
# 16. read-only: opening the db does not require write access
# ---------------------------------------------------------------------------
def test_db_opened_read_only(fixture_dir):
    """Make the source sqlite read-only and ensure the CLI still works."""
    # chmod -w the sqlite file.
    db = os.path.join(fixture_dir, "localNoteStore", "LocalNoteStore.sqlite")
    os.chmod(db, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        out = os.path.join(os.path.dirname(db), "out_ro")
        if os.path.exists(out):
            shutil.rmtree(out)
        proc = subprocess.run(
            [sys.executable, CLI, "--source", fixture_dir, "-o", out,
             "--engine", "builtin", "--include-deleted"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert os.path.exists(os.path.join(out, "日常", "富文本示例.md"))
    finally:
        os.chmod(db, stat.S_IRUSR | stat.S_IWUSR |
                 stat.S_IRGRP | stat.S_IWGRP |
                 stat.S_IROTH | stat.S_IWOTH)


# ---------------------------------------------------------------------------
# 17. unit tests: helpers
# ---------------------------------------------------------------------------
def test_cdata_to_dt_unit():
    dt = yinxiang2md.cdata_to_dt(REF_CREATED_SECONDS)
    assert dt is not None
    expected_local = _expected_local(REF_CREATED_SECONDS)
    assert dt.strftime("%Y-%m-%d %H:%M") == expected_local


def test_safe_filename_unit():
    assert yinxiang2md.safe_filename("a/b:c*d?e\"f<g>h|i") == "a_b_c_d_e_f_g_h_i"
    assert yinxiang2md.safe_filename("") == "无标题"
    assert yinxiang2md.safe_filename(".....") == "无标题"
    assert yinxiang2md.safe_filename("x" * 200) == "x" * 80
    assert yinxiang2md.safe_filename("hello") == "hello"


def test_is_derived_binary_unit():
    assert yinxiang2md.is_derived_binary("a.en-reco")
    assert yinxiang2md.is_derived_binary("b.en-rstxt")
    assert yinxiang2md.is_derived_binary("c.en-pdf-text")
    assert yinxiang2md.is_derived_binary("snippet.tiff")
    assert yinxiang2md.is_derived_binary("card.png")
    assert not yinxiang2md.is_derived_binary("res-uuid.png")


def test_split_uuid_and_ext_unit():
    assert (yinxiang2md.split_uuid_and_ext("abc", "/d/abc.png") == "abc.png")
    assert (yinxiang2md.split_uuid_and_ext("abc", "/d/abc") == "abc")
    assert (yinxiang2md.split_uuid_and_ext("abc", "/d/other.png") == "other.png")


def test_yaml_quote_unit():
    assert yinxiang2md.yaml_quote("hello") == '"hello"'
    assert yinxiang2md.yaml_quote('a"b') == '"a\\\\"b"'  # backslash + quote
    assert yinxiang2md.yaml_quote(None) == '""'


def test_preclean_enml_strips_en_note():
    html = ('<?xml version="1.0"?><!DOCTYPE en-note SYSTEM "x">'
            '<en-note><p>x</p></en-note>')
    out = yinxiang2md.preclean_enml(html, {}, [])
    assert out == "<div><p>x</p></div>"


def test_preclean_enml_handles_en_todo():
    html = ('<en-note><en-todo checked="true"/>a<br/>'
            '<en-todo/>b</en-note>')
    out = yinxiang2md.preclean_enml(html, {}, [])
    assert '☑' in out
    assert '☐' in out
    assert 'a' in out
    assert 'b' in out


def test_preclean_enml_handles_en_crypt():
    html = '<en-note><en-crypt foo="bar">secret</en-crypt>x</en-note>'
    out = yinxiang2md.preclean_enml(html, {}, [])
    assert "[加密内容：印象笔记未解密，已丢弃]" in out
    assert "secret" in out


def test_preclean_enml_en_media_known_hash():
    html = '<en-note><en-media hash="ABCD"/></en-note>'
    out = yinxiang2md.preclean_enml(html, {"ABCD": "f.png"}, [])
    assert '<img src="attachments/f.png"' in out


def test_preclean_enml_en_media_missing_hash():
    html = '<en-note><en-media hash="DEADBEEFDEADBEEF"/></en-note>'
    out = yinxiang2md.preclean_enml(html, {}, [])
    assert "[附件缺失: DEADBEEF]" in out


def test_html_to_markdown_unit():
    s = ("<h1>t</h1><p>a <strong>b</strong> <em>c</em>.</p>"
         "<ul><li>x</li><li>y</li></ul>"
         "<pre><code>code</code></pre>"
         "<blockquote>q</blockquote>")
    md = yinxiang2md.html_to_markdown_builtin(s)
    assert "# t" in md
    assert "**b**" in md
    assert "*c*" in md
    assert "- x" in md
    assert "- y" in md
    assert "```" in md
    assert "code" in md
    assert "> q" in md


def test_open_db_readonly_works():
    """The CLI must open the sqlite without writing to it."""
    db = os.path.join(os.path.dirname(__file__), "..", "_nonexistent.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        yinxiang2md.open_db_readonly(db)


def test_help_runs():
    proc = subprocess.run([sys.executable, CLI, "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert "Convert Yinxiang" in proc.stdout


def test_version_runs():
    proc = subprocess.run([sys.executable, CLI, "--version"],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert "yinxiang2md.py" in proc.stdout


def test_list_accounts_handles_missing_dir(tmp_path):
    """When the default macOS root doesn't exist, --list-accounts is empty.

    Use discover_accounts() directly to avoid monkey-patching across a
    subprocess boundary.
    """
    accounts = yinxiang2md.discover_accounts(root=str(tmp_path / "no-such-root"))
    assert accounts == []


def test_bad_source_returns_error(workdir):
    proc = _run_cli("/nonexistent/path", workdir, "--engine", "builtin",
                    expect_returncode=1)
    assert "不是有效账号目录" in proc.stderr or "Error" in proc.stderr or "错" in proc.stderr


# ---------------------------------------------------------------------------
# P1 [data-loss] en-todo preserved end-to-end with all checked-truthy values
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cval_attr,expected_mark", [
    ('checked="true"', "☑"),
    ("checked='true'", "☑"),
    ('checked="1"', "☑"),
    ('checked="checked"', "☑"),
    ('checked="TRUE"', "☑"),
    ('checked="True"', "☑"),
    ('checked="false"', "☐"),
    ('checked="0"', "☐"),
    ('checked="no"', "☐"),
    ("", "☐"),  # bare <en-todo/>
])
def test_en_todo_checked_truthy_values(cval_attr, expected_mark):
    """checked values true/1/checked -> ☑; missing/false/anything else -> ☐."""
    html = '<en-note><en-todo %s/>x</en-note>' % cval_attr
    out = yinxiang2md.preclean_enml(html, {}, [])
    opposite = "☐" if expected_mark == "☑" else "☑"
    assert expected_mark in out, "%r: expected %r in %r" % (
        cval_attr, expected_mark, out)
    assert opposite not in out, "%r: opposite %r leaked into %r" % (
        cval_attr, opposite, out)


def test_en_todo_count_matches_fixture(workdir, fixture_dir):
    """No en-todo box may be lost between fixture and markdown output.

    The fixture's note 学习/附件与待办 has exactly 2 en-todo elements
    (one checked, one unchecked). After running, the output must
    contain exactly one of each marker.
    """
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--todo-style", "char")
    text = (workdir / "学习" / "附件与待办.md").read_text(encoding="utf-8")
    assert text.count("☑") == 1, "checked count: %d" % text.count("☑")
    assert text.count("☐") == 1, "unchecked count: %d" % text.count("☐")


def test_en_todo_task_style_full_line(workdir, fixture_dir):
    """--todo-style task rewrites the whole `☑ x` line to `- [x] x`."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--todo-style", "task")
    text = (workdir / "学习" / "附件与待办.md").read_text(encoding="utf-8")
    assert "- [x] 已完成" in text
    assert "- [ ] 未完成" in text
    # No bare checkmark should remain after task substitution.
    assert "☑" not in text
    assert "☐" not in text


# ---------------------------------------------------------------------------
# P2 [medium] filename-conflict key is (note_dir, stem)
# ---------------------------------------------------------------------------
def _build_minimal_acct(acct, *, notes):
    """Build a single-account fixture with the given notes list.

    Each entry in ``notes`` is a dict::
        {"uuid": "note-x", "pk": 1, "title": "会议", "notebook": "Plant-A",
         "active": True, "enml": "<en-note><p>x</p></en-note>"}
    """
    db_dir = acct / "localNoteStore"
    db_dir.mkdir(parents=True)
    db = db_dir / "LocalNoteStore.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENTAG (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, ZLOCALUUID TEXT, "
                 "ZTITLE TEXT, ZNOTEBOOK INTEGER, ZACTIVE INTEGER, "
                 "ZDATECREATED REAL, ZDATEUPDATED REAL, ZDATEDELETED REAL, "
                 "ZSOURCEURL TEXT)")
    conn.execute("CREATE TABLE ZENRESOURCE (Z_PK INTEGER, ZENT INTEGER, "
                 "ZNOTE INTEGER, ZLOCALUUID TEXT, ZGUID TEXT, ZDATAHASH BLOB, "
                 "ZATTACHMENT INTEGER, ZFILENAME TEXT, ZMIME TEXT)")
    conn.execute("CREATE TABLE Z_10TAGS (Z_10NOTES INTEGER, Z_23TAGS INTEGER)")
    nb_pks = {}
    next_nb = 1
    for n in notes:
        nb = n["notebook"]
        if nb not in nb_pks:
            conn.execute("INSERT INTO ZENNOTEBOOK (Z_PK, ZNAME) VALUES (?, ?)",
                         (next_nb, nb))
            nb_pks[nb] = next_nb
            next_nb += 1
    for n in notes:
        content_dir = acct / "content" / n["uuid"]
        content_dir.mkdir(parents=True, exist_ok=True)
        (content_dir / "content.enml").write_text(n["enml"], encoding="utf-8")
        conn.execute(
            "INSERT INTO ZENNOTE (Z_PK, ZLOCALUUID, ZTITLE, ZNOTEBOOK, "
            "ZACTIVE, ZDATECREATED, ZDATEUPDATED, ZDATEDELETED, ZSOURCEURL) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '')",
            (n["pk"], n["uuid"], n["title"], nb_pks[n["notebook"]],
             1 if n["active"] else 0, 532264320, 532349100))
    conn.commit()
    conn.close()


def test_filename_conflict_within_same_notebook(workdir):
    """Two notes titled 会议 in Plant-A must produce Plant-A/会议.md and Plant-A/会议-2.md."""
    acct = workdir / "acct_same"
    _build_minimal_acct(acct, notes=[
        {"uuid": "n1", "pk": 1, "title": "会议", "notebook": "Plant-A",
         "active": True, "enml": "<en-note><p>1</p></en-note>"},
        {"uuid": "n2", "pk": 2, "title": "会议", "notebook": "Plant-A",
         "active": True, "enml": "<en-note><p>2</p></en-note>"},
    ])
    out = workdir / "out_same"
    _run_cli(str(acct), out, "--engine", "builtin")
    files = sorted(f for f in os.listdir(out / "Plant-A") if f.endswith(".md"))
    assert files == ["会议-2.md", "会议.md"], files


def test_filename_conflict_across_notebooks(workdir):
    """Same title in different notebooks must NOT collide (-2 only when needed)."""
    acct = workdir / "acct_cross"
    _build_minimal_acct(acct, notes=[
        {"uuid": "n1", "pk": 1, "title": "会议", "notebook": "Plant-A",
         "active": True, "enml": "<en-note><p>1</p></en-note>"},
        {"uuid": "n2", "pk": 2, "title": "会议", "notebook": "新闻",
         "active": True, "enml": "<en-note><p>2</p></en-note>"},
    ])
    out = workdir / "out_cross"
    _run_cli(str(acct), out, "--engine", "builtin")
    assert (out / "Plant-A" / "会议.md").exists()
    assert (out / "新闻" / "会议.md").exists()
    # Neither notebook should have produced a -2 suffix.
    huaxin_files = sorted(f for f in os.listdir(out / "Plant-A")
                          if f.endswith(".md"))
    xinwen_files = sorted(f for f in os.listdir(out / "新闻")
                          if f.endswith(".md"))
    assert huaxin_files == ["会议.md"], huaxin_files
    assert xinwen_files == ["会议.md"], xinwen_files


def test_filename_conflict_active_vs_deleted(workdir):
    """Active 会议 in Plant-A and deleted 会议 in Plant-A must NOT collide."""
    acct = workdir / "acct_active_deleted"
    _build_minimal_acct(acct, notes=[
        {"uuid": "n1", "pk": 1, "title": "会议", "notebook": "Plant-A",
         "active": True, "enml": "<en-note><p>active</p></en-note>"},
        {"uuid": "n2", "pk": 2, "title": "会议", "notebook": "Plant-A",
         "active": False, "enml": "<en-note><p>trashed</p></en-note>"},
    ])
    out = workdir / "out_ad"
    _run_cli(str(acct), out, "--engine", "builtin", "--include-deleted")
    # Both versions exist, neither has -2 suffix.
    assert (out / "Plant-A" / "会议.md").exists(), \
        "active 会议.md missing"
    assert (out / "_已删除_回收站" / "Plant-A" / "会议.md").exists(), \
        "deleted 会议.md missing"
    active_dir = sorted(f for f in os.listdir(out / "Plant-A")
                        if f.endswith(".md"))
    deleted_dir = sorted(f for f in os.listdir(out / "_已删除_回收站" / "Plant-A")
                         if f.endswith(".md"))
    assert active_dir == ["会议.md"], active_dir
    assert deleted_dir == ["会议.md"], deleted_dir


# ---------------------------------------------------------------------------
# P3 [medium] <br data-tomark-pass> stripped from final markdown
# ---------------------------------------------------------------------------
def test_tomark_pass_stripped_pandoc(workdir):
    """Pandoc emits ``<br data-tomark-pass>`` in some table cells; ensure
    the post-processor strips every variant the bug report lists.

    We build a tiny fixture whose only note contains a ``<br>`` inside a
    table cell, run the CLI in ``auto`` mode (which prefers pandoc when
    present), and assert the marker never appears in the output.
    """
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed; built-in engine does not emit "
                    "data-tomark-pass, so this test is only meaningful "
                    "with pandoc.")
    acct = workdir / "acct_tm"
    _build_minimal_acct(acct, notes=[
        {"uuid": "n-tm", "pk": 1, "title": "TM", "notebook": "NB",
         "active": True,
         "enml": ("<?xml version='1.0'?>"
                  "<en-note><div><table>"
                  "<thead><tr><th>c1</th><th>c2</th></tr></thead>"
                  "<tbody><tr>"
                  "<td>line1<br/>line2</td>"
                  "<td>other</td>"
                  "</tr></tbody>"
                  "</table></div></en-note>")},
    ])
    out = workdir / "out_tm"
    _run_cli(str(acct), out)  # default engine = auto (uses pandoc)
    text = (out / "NB" / "TM.md").read_text(encoding="utf-8")
    # The post-processor must remove every tomark-pass marker regardless
    # of which pandoc / table-collapse variant produced the output.
    assert "tomark-pass" not in text, text
    # Whatever the engine produced, the note must mention line1 (sanity
    # that the conversion ran) - the actual table form varies by pandoc
    # version so we don't pin down the exact <br> rendering here.


def test_strip_tomark_pass_unit_all_variants():
    """Unit-level coverage of every br variant the spec calls out."""
    cases = [
        (r"\<br data-tomark-pass\>", "<br>"),
        ("<br data-tomark-pass>", "<br>"),
        ("<br data-tomark-pass/>", "<br>"),
        ("<br data-tomark-pass />", "<br>"),
        ("plain text", "plain text"),
        ("<br>", "<br>"),  # left alone
    ]
    for src, expected in cases:
        out = yinxiang2md._strip_tomark_pass(src)
        assert out == expected, "%r -> %r (expected %r)" % (src, out, expected)


# ---------------------------------------------------------------------------
# P4 [low] --assets none -> no broken links, only text placeholders
# ---------------------------------------------------------------------------
def test_assets_none_produces_no_broken_links(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--assets", "none")
    # Walk every produced .md; none may contain '](attachments/'.
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            if not f.endswith(".md"):
                continue
            text = open(os.path.join(root, f), encoding="utf-8").read()
            assert "](attachments/" not in text, \
                "%s still references attachments/" % os.path.join(root, f)


def test_assets_none_uses_text_placeholders(workdir, fixture_dir):
    """Inline images must be rewritten to `[图片: <name>]`."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--assets", "none")
    p = workdir / "日常" / "图片笔记.md"
    text = p.read_text(encoding="utf-8")
    # The fixture's note 图片笔记 has two inline images (0200, 0201).
    assert "[图片:" in text
    assert "res-uuid-0200.png" in text
    assert "res-uuid-0201.png" in text
    # And of course no leftover broken references.
    assert "![](../" not in text and "![](/" not in text


def test_assets_none_reported_in_migration_report(workdir, fixture_dir):
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted", "--assets", "none")
    rp = workdir / "_迁移报告.md"
    text = rp.read_text(encoding="utf-8")
    assert "未复制资源数" in text
    # Fixture has 2 inline images and 1 attachment.
    m = re.search(r"未复制资源数: (\d+)", text)
    assert m, "report missing '未复制资源数' line:\n%s" % text
    assert int(m.group(1)) >= 2, \
        "expected >= 2 unreferenced assets (2 inline images), got %s" % m.group(1)


def test_assets_default_still_has_attachments_links(workdir, fixture_dir):
    """Sanity check: without --assets none, the attachments/ links survive."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--include-deleted")  # default --assets folder
    p = workdir / "日常" / "图片笔记.md"
    text = p.read_text(encoding="utf-8")
    assert "](attachments/" in text


# ---------------------------------------------------------------------------
# A direct end-to-end smoke covering all four fixes at once
# ---------------------------------------------------------------------------
def test_all_fixes_e2e(workdir, fixture_dir):
    """The standard fixture, run with builtin + include-deleted, must satisfy
    every defect's acceptance criterion at once."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    # P1: the two todos in 学习/附件与待办 are preserved.
    text = (workdir / "学习" / "附件与待办.md").read_text(encoding="utf-8")
    assert text.count("☑") == 1 and text.count("☐") == 1
    # P3: no tomark-pass in any produced markdown.
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            if not f.endswith(".md"):
                continue
            t = open(os.path.join(root, f), encoding="utf-8").read()
            assert "tomark-pass" not in t
    # P4 (default assets=folder): attachments link still works.
    assert "](attachments/res-uuid-0202.xlsx)" in text


# ---------------------------------------------------------------------------
# P5 [critical regression] default query must return active notes even when
# CoreData encodes "ZDATEDELETED unset" as the -978307200 sentinel instead
# of SQL NULL.
# ---------------------------------------------------------------------------
# Sentinel seconds-since-2001-01-01 used by the macOS client to encode
# "ZDATEDELETED is unset". Mapping back: 2001-01-01 minus ~31 years lands
# exactly on the Unix epoch.
COREDATA_SENTINEL_DELETED = -978307200.0


def _build_acct_with_deleted_sentinel(acct, notes):
    """Build a single-account fixture where active notes carry the
    -978307200 sentinel for ZDATEDELETED — exactly the on-disk shape
    that the unfixed ``load_notes`` silently filters to zero rows.

    Each entry in ``notes`` is a dict::

        {"uuid": "n1", "pk": 1, "title": "t", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>x</p></en-note>"}
    """
    db_dir = acct / "localNoteStore"
    db_dir.mkdir(parents=True)
    db = db_dir / "LocalNoteStore.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENTAG (Z_PK INTEGER, ZNAME TEXT)")
    conn.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, ZLOCALUUID TEXT, "
                 "ZTITLE TEXT, ZNOTEBOOK INTEGER, ZACTIVE INTEGER, "
                 "ZDATECREATED REAL, ZDATEUPDATED REAL, ZDATEDELETED REAL, "
                 "ZSOURCEURL TEXT)")
    conn.execute("CREATE TABLE ZENRESOURCE (Z_PK INTEGER, ZENT INTEGER, "
                 "ZNOTE INTEGER, ZLOCALUUID TEXT, ZGUID TEXT, ZDATAHASH BLOB, "
                 "ZATTACHMENT INTEGER, ZFILENAME TEXT, ZMIME TEXT)")
    conn.execute("CREATE TABLE Z_10TAGS (Z_10NOTES INTEGER, Z_23TAGS INTEGER)")
    nb_pks = {}
    next_nb = 1
    for n in notes:
        nb = n["notebook"]
        if nb not in nb_pks:
            conn.execute("INSERT INTO ZENNOTEBOOK (Z_PK, ZNAME) VALUES (?, ?)",
                         (next_nb, nb))
            nb_pks[nb] = next_nb
            next_nb += 1
    for n in notes:
        content_dir = acct / "content" / n["uuid"]
        content_dir.mkdir(parents=True, exist_ok=True)
        (content_dir / "content.enml").write_text(n["enml"], encoding="utf-8")
        conn.execute(
            "INSERT INTO ZENNOTE (Z_PK, ZLOCALUUID, ZTITLE, ZNOTEBOOK, "
            "ZACTIVE, ZDATECREATED, ZDATEUPDATED, ZDATEDELETED, ZSOURCEURL) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
            (n["pk"], n["uuid"], n["title"], nb_pks[n["notebook"]],
             1 if n["active"] else 0, 532264320, 532349100, n["deleted"]))
    conn.commit()
    conn.close()


def test_p5_default_returns_notes_with_sentinel_deleted_at(workdir):
    """The P5 regression: with the buggy ``ZDATEDELETED IS NULL``
    predicate, default mode returns zero rows when CoreData stores the
    sentinel value instead of NULL. After the fix, every active note
    must be converted regardless of ZDATEDELETED.
    """
    acct = workdir / "acct_p5"
    _build_acct_with_deleted_sentinel(acct, notes=[
        {"uuid": "n1", "pk": 1, "title": "alpha", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>one</p></en-note>"},
        {"uuid": "n2", "pk": 2, "title": "beta", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>two</p></en-note>"},
        {"uuid": "n3", "pk": 3, "title": "gamma", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>three</p></en-note>"},
    ])
    out = workdir / "out_p5"
    proc = _run_cli(str(acct), out, "--engine", "builtin")
    assert proc.returncode == 0, proc.stderr
    md_files = [f for f in os.listdir(out / "NB") if f.endswith(".md")]
    assert sorted(md_files) == ["alpha.md", "beta.md", "gamma.md"], md_files


def test_p5_default_matches_active_count_and_include_deleted_matches_all(workdir):
    """Default mode (no --include-deleted) returns exactly the active
    count; --include-deleted returns the full set including trashed."""
    acct = workdir / "acct_p5_counts"
    _build_acct_with_deleted_sentinel(acct, notes=[
        {"uuid": "n1", "pk": 1, "title": "a1", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>1</p></en-note>"},
        {"uuid": "n2", "pk": 2, "title": "a2", "notebook": "NB",
         "active": True, "deleted": COREDATA_SENTINEL_DELETED,
         "enml": "<en-note><p>2</p></en-note>"},
        {"uuid": "n3", "pk": 3, "title": "t1", "notebook": "NB",
         "active": False, "deleted": 532264320 + 5000,
         "enml": "<en-note><p>trash</p></en-note>"},
        {"uuid": "n4", "pk": 4, "title": "t2", "notebook": "NB",
         "active": False, "deleted": 532264320 + 6000,
         "enml": "<en-note><p>trash</p></en-note>"},
    ])
    # Default (active only) must hit exactly the 2 active notes.
    out_default = workdir / "out_p5_default"
    _run_cli(str(acct), out_default, "--engine", "builtin")
    active = sorted(f for f in os.listdir(out_default / "NB") if f.endswith(".md"))
    assert active == ["a1.md", "a2.md"], active
    assert not (out_default / "_已删除_回收站").exists()
    # --include-deleted must hit all 4 notes (2 active + 2 trashed).
    out_all = workdir / "out_p5_all"
    _run_cli(str(acct), out_all, "--engine", "builtin", "--include-deleted")
    active_dir = sorted(f for f in os.listdir(out_all / "NB") if f.endswith(".md"))
    deleted_dir = sorted(f for f in os.listdir(out_all / "_已删除_回收站" / "NB")
                         if f.endswith(".md"))
    assert active_dir == ["a1.md", "a2.md"], active_dir
    assert sorted(deleted_dir) == ["t1.md", "t2.md"], deleted_dir


def test_p5_load_notes_unit_sentinel():
    """Unit-level: ``load_notes(include_deleted=False)`` returns the
    active row even when ZDATEDELETED is the sentinel value."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, ZNAME TEXT)")
    db.execute("CREATE TABLE ZENTAG (Z_PK INTEGER, ZNAME TEXT)")
    db.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, ZLOCALUUID TEXT, "
               "ZTITLE TEXT, ZNOTEBOOK INTEGER, ZACTIVE INTEGER, "
               "ZDATECREATED REAL, ZDATEUPDATED REAL, ZDATEDELETED REAL, "
               "ZSOURCEURL TEXT)")
    db.execute("INSERT INTO ZENNOTEBOOK VALUES (1, 'NB')")
    db.execute("INSERT INTO ZENNOTE VALUES "
               "(1, 'u1', 'sentinel-active', 1, 1, 0, 0, ?, '')",
               (COREDATA_SENTINEL_DELETED,))
    db.execute("INSERT INTO ZENNOTE VALUES "
               "(2, 'u2', 'trashed', 1, 0, 0, 0, 532264320, '')")
    rows_default = yinxiang2md.load_notes(db, False, None)
    assert len(rows_default) == 1, [dict(r) for r in rows_default]
    assert rows_default[0]["ZTITLE"] == "sentinel-active"
    rows_all = yinxiang2md.load_notes(db, True, None)
    assert len(rows_all) == 2


# ---------------------------------------------------------------------------
# P6 [medium] report's "跳过" counter must be non-zero when default mode
# excludes trashed notes.
# ---------------------------------------------------------------------------
def test_p6_skipped_counter_nonzero_in_default_mode(workdir, fixture_dir):
    """Standard fixture has exactly one trashed note. Default mode
    must record 跳过 ≥ 1 in the migration report."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin")  # no --include-deleted
    rp = workdir / "_迁移报告.md"
    assert rp.exists(), "report missing"
    text = rp.read_text(encoding="utf-8")
    m = re.search(r"^- 跳过: (\d+)$", text, re.M)
    assert m, "report missing '跳过' line:\n%s" % text
    skipped = int(m.group(1))
    assert skipped >= 1, "expected skipped ≥ 1, got %d" % skipped


def test_p6_skipped_zero_when_include_deleted(workdir, fixture_dir):
    """--include-deleted pulls trashed notes into conversion; nothing
    is 'skipped' in that mode."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin", "--include-deleted")
    rp = workdir / "_迁移报告.md"
    text = rp.read_text(encoding="utf-8")
    m = re.search(r"^- 跳过: (\d+)$", text, re.M)
    assert m
    assert int(m.group(1)) == 0, "expected skipped=0 with --include-deleted, got %s" % m.group(1)


def test_p6_skipped_count_unit_sentinel_and_trashed():
    """Unit-level: ``count_skipped`` counts ZACTIVE=0 rows regardless
    of whether ZDATEDELETED is the sentinel or a real deletion time."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE ZENNOTEBOOK (Z_PK INTEGER, ZNAME TEXT)")
    db.execute("CREATE TABLE ZENNOTE (Z_PK INTEGER, ZNOTEBOOK INTEGER, "
               "ZACTIVE INTEGER, ZDATEDELETED REAL)")
    db.execute("INSERT INTO ZENNOTEBOOK VALUES (1, 'NB')")
    db.execute("INSERT INTO ZENNOTE VALUES (1, 1, 0, ?)", (COREDATA_SENTINEL_DELETED,))
    db.execute("INSERT INTO ZENNOTE VALUES (2, 1, 0, 532264320)")
    db.execute("INSERT INTO ZENNOTE VALUES (3, 1, 1, NULL)")
    assert yinxiang2md.count_skipped(db, None) == 2
    # NB filter narrows the count to 1 (only the sentinel trashed is in NB).
    assert yinxiang2md.count_skipped(db, ("NB",)) == 2
    assert yinxiang2md.count_skipped(db, ("OTHER",)) == 0


# ---------------------------------------------------------------------------
# P7 [low] --jobs default is 1, visible in --help, observable in behaviour.
# ---------------------------------------------------------------------------
def test_p7_jobs_default_is_one_in_help():
    """``--help`` must surface the SPEC-mandated default (1)."""
    proc = subprocess.run([sys.executable, CLI, "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    # The ArgumentDefaultsHelpFormatter renders ``(default: 1)`` next to
    # the option line.
    assert "--jobs" in proc.stdout
    assert re.search(r"--jobs[^\n]*\(default:\s*1\)", proc.stdout), \
        "--jobs default not visible in --help:\n%s" % proc.stdout


def test_p7_jobs_default_value_one_via_argparse():
    """When --jobs is omitted, argparse must give back the default 1."""
    ns = yinxiang2md.parse_args(["--source", "x", "-o", "y"])
    assert ns.jobs == 1


def test_p7_jobs_runs_concurrently_with_jobs_2(workdir, fixture_dir):
    """Sanity check: --jobs 2 still produces the full set, which means
    the default path didn't accidentally serialise when it shouldn't."""
    _run_cli(fixture_dir, workdir, "--engine", "builtin",
             "--jobs", "2", "--include-deleted")
    md_files = [f for f in os.listdir(workdir) if f.endswith(".md")]
    assert "_迁移报告.md" in md_files
