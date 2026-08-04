"""The document the MCP client puts in front of a model before it calls anything.

It is the only part of this project read by something that cannot ask a follow-up question, and it
is injected once and then held for a whole session. So the tests here are about it being *there*,
being *current*, and saying so.
"""
import pytest

mcp_server = pytest.importorskip("serenedash.mcp_server")


def text():
    return mcp_server.INSTRUCTIONS


def test_the_file_is_found_and_not_the_fallback():
    # The fallback is one sentence. Shipping it by accident - a wheel that dropped the package
    # data, a rename that missed a caller - would leave an agent with almost no context and no
    # sign that anything was missing.
    assert len(text()) > 5000, "looks like the one-line fallback, not instructions.md"
    assert mcp_server.REVISION != "none"


def test_every_placeholder_is_substituted():
    # A `{{REVISION}}` left in the shipped text is worse than no revision at all: the agent would
    # compare a literal against a hash and report staleness on every single call.
    assert "{{" not in text() and "}}" not in text()
    assert mcp_server.REVISION in text(), "the text must carry the revision it is stamped with"
    assert mcp_server.VERSION in text()


def test_the_revision_is_a_hash_of_the_file():
    import hashlib
    import pathlib
    raw = (pathlib.Path(mcp_server.__file__).parent / "instructions.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest()[:12] == mcp_server.REVISION


def test_the_document_tells_the_agent_how_to_notice_it_is_stale():
    t = text()
    assert "instructions_revision" in t, "no way to compare context against the running server"
    assert mcp_server.INSTRUCTIONS_URI in t, "no way to re-read the current text"


def test_the_stamp_carries_what_a_comparison_needs():
    st = mcp_server._stamp()
    assert st["instructions_revision"] == mcp_server.REVISION
    assert st["version"] == mcp_server.VERSION
    assert st["instructions_uri"] == mcp_server.INSTRUCTIONS_URI


def test_the_stamp_does_not_overwrite_a_tool_that_reports_its_own():
    # setdefault, not assignment: a tool is allowed to say something more specific.
    def tool():
        return {"server": {"version": "mine"}}
    assert mcp_server.stamped(tool)()["server"] == {"version": "mine"}


def test_stamping_leaves_the_signature_the_schema_is_built_from():
    # The SDK derives each tool's JSON schema and description from the function. A decorator that
    # dropped either would leave the tools callable and undocumented.
    import inspect

    def tool(sql: str, max_rows: int = 200) -> dict:
        """Doc."""
        return {}
    wrapped = mcp_server.stamped(tool)
    assert list(inspect.signature(wrapped).parameters) == ["sql", "max_rows"]
    assert wrapped.__doc__ == "Doc."
    assert wrapped.__name__ == "tool"


def test_a_non_dict_result_passes_through_untouched():
    assert mcp_server.stamped(lambda: [1, 2])() == [1, 2]


def test_the_vocabulary_the_document_explains_is_the_vocabulary_the_tools_emit():
    # The failure this guards is drift: a field renamed in snapshot.py while the document keeps
    # explaining the old name. Every key here is one the instructions single out as misreadable.
    import inspect

    from serenedash import snapshot
    src = inspect.getsource(snapshot)
    t = text()
    for key in ("spill_orphaned_bytes", "spill_live_bytes", "cpu_percent_of_one_core",
                "resident_bytes", "swapped_bytes", "duckdb_memory_bytes", "nothing_running",
                "query_chars", "wal_over_database", "memory_limit_fraction_of_ram"):
        assert key in src, f"{key} is documented but no longer emitted"
        assert key in t, f"{key} is emitted but no longer documented"


def test_it_names_the_tools_that_exist():
    t = text()
    for name in ("status", "storage", "memory", "activity", "threads", "profile", "callgraph",
                 "host", "config", "query", "anomalies", "set_setting"):
        assert f"`{name}`" in t, f"{name} is not mentioned in the instructions"
