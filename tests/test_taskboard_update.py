"""taskboard `update`: advertised in the schema, now handled."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

TOOLS_SERVER = Path(__file__).parent.parent / "mcp" / "tools-server.py"


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "data").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("tools_taskboard_under_test", TOOLS_SERVER)
        m = importlib.util.module_from_spec(spec)
        sys.modules["tools_taskboard_under_test"] = m
        spec.loader.exec_module(m)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return m


def _tb(tools, **args):
    return tools.handle_core_tool("taskboard", args)


def test_add_update_then_read_back(tools):
    task = _tb(tools, action="add", title="write docs")["task"]
    out = _tb(tools, action="update", id=task["id"], status="in_progress",
              title="write the docs", notes="half done", assignee="amos")
    assert out["task"]["status"] == "in_progress"
    got = [t for t in _tb(tools, action="list")["tasks"] if t["id"] == task["id"]][0]
    assert got["title"] == "write the docs"
    assert got["notes"] == "half done"
    assert got["assignee"] == "amos"
    assert got["status"] == "in_progress"


def test_partial_update_leaves_other_fields(tools):
    task = _tb(tools, action="add", title="keep me")["task"]
    _tb(tools, action="update", id=task["id"], status="blocked")
    got = _tb(tools, action="list")["tasks"][0]
    assert got["title"] == "keep me" and got["status"] == "blocked"


def test_unknown_id_is_a_clear_error(tools):
    out = _tb(tools, action="update", id="task-nope", status="done")
    assert "not found" in out["error"].lower() and "task-nope" in out["error"]


def test_update_with_no_fields_is_an_error(tools):
    task = _tb(tools, action="add", title="x")["task"]
    assert "error" in _tb(tools, action="update", id=task["id"])


def test_schema_advertises_update_fields(tools):
    tb = [t for t in tools.CORE_TOOLS if t["name"] == "taskboard"][0]
    props = tb["inputSchema"]["properties"]
    assert "update" in props["action"]["enum"]
    assert {"notes", "assignee", "status", "title", "id"} <= set(props)
