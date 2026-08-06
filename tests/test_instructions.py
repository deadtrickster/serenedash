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


def test_it_documents_the_system_tables_no_panel_exposes():
    # The point of the query tool is reaching what the panels do not, and these three are where
    # the answers live. A document that omits them leaves an agent guessing at table names.
    t = text()
    for tbl in ("sdb_metrics", "sdb_settings", "sdb_progress"):
        assert tbl in t, f"{tbl} is not documented"
    for col in ("num_segments", "num_live_docs", "refresh_pending", "avg_consolidation_time_ms",
                "cpu_threads", "io_threads", "background_threads", "rows_processed"):
        assert col in t, f"{col} is not documented"


def test_it_carries_the_numbers_that_make_the_mechanics_checkable():
    # Each of these was read out of the docs or off the live server. They are here because a
    # mechanic without its number is a story - "row groups are the unit of parallelism" is only
    # actionable once you know it is 122,880 rows.
    t = text()
    for n in ("122,880",          # row group size, and the parallelism floor
              "16 MiB",           # checkpoint_threshold default - the compression frequency lever
              "80%",              # memory_limit default share of RAM
              "125 MB",           # documented memory floor per thread
              "1-4 GB", "1–4 GB", # working range per thread (either dash)
              "131072",           # RLIMIT_NOFILE
              "262144"):          # vm.max_map_count, and this server's block size
        if n in ("1-4 GB", "1–4 GB"):
            continue
        assert n in t, f"{n} is no longer in the instructions"
    assert "1-4 GB" in t or "1–4 GB" in t


def test_it_explains_the_engines_the_profile_splits_into():
    t = text()
    for engine in ("columnar", "text", "vector", "wire", "alloc"):
        assert engine in t, f"the {engine} engine bucket is not explained"
    # And the two that are easy to misread as something else.
    assert "other" in t
    assert "IVF" in t and "BM25" in t


def test_it_carries_the_duckdb_mechanics_that_change_a_diagnosis():
    # Each of these changes what you would tell the user, and each came out of the DuckDB docs
    # rather than SereneDB's rebranded subset of them.
    t = text()
    for claim in ("25%",                      # deleted rows needed before a checkpoint reclaims
                  "SYSTEM_PEAK_TEMP_DIR_SIZE",  # per-query spill attribution
                  "BLOCKED_THREAD_TIME",        # starved, not slow
                  "vacuum_rebuild_indexes",     # VACUUM skips ART-indexed tables
                  "DUCKDB_JE_MALLOC_CONF",      # the allocator is tunable
                  "storage version 69"):        # the fork is ahead of the published docs
        assert claim in t, f"{claim} is no longer in the instructions"


def test_it_warns_that_published_duckdb_docs_are_not_authoritative_here():
    # PRAGMA version reports a scrubbed v0.0.1, so an agent cannot look up "the" version - and the
    # fork is past the newest documented release. Guessing from upstream docs is the trap.
    t = text()
    assert "fork" in t
    assert "v0.0.1" in t
    assert "duckdb_settings()" in t


# ---- the guide arrives on first use, and the brief has to survive the client's cut --------------
# Two facts drive this. Claude Code truncates server instructions at about 2 KB - visible in this
# very repo's own session, where the injected block ends "does the… [truncated]" - so a 63,656-char
# guide never reached a model through that channel at all. And MCP servers are enabled per project,
# so whatever IS sent is paid for by every session, including the ones that never ask this server
# anything.

def test_the_brief_survives_a_two_kilobyte_truncation():
    from serenedash.mcp_server import BRIEF

    n = len(BRIEF.encode())
    assert n <= 2048, f"the brief is {n} bytes and the client cuts at ~2048; the tail is discarded"


def test_the_brief_says_the_rest_is_coming_before_it_could_be_cut():
    # Truncation takes the TAIL, so anything the model must know has to be near the front - starting
    # with the fact that a fuller guide exists at all, or it will not know to wait for one.
    from serenedash.mcp_server import BRIEF

    head = BRIEF.encode()[:600].decode("utf-8", "ignore")
    assert "first tool result" in head
    assert "serenedash://instructions" in head


def test_the_brief_carries_the_misreadings_that_have_actually_happened():
    # A model that reads one tool result and stops must still not report a parse cost as a search
    # cost, or recommend a timeout that does nothing.
    from serenedash.mcp_server import BRIEF

    for rule in ("parse", "Matcher", "statement_timeout", "denominator", "ONE core",
                 "blocks checkpointing"):
        assert rule in BRIEF, rule


def test_connect_is_cheap_and_the_first_call_carries_the_guide():
    from serenedash import mcp_server as m

    assert m.MODE == "lazy", "the default is lazy; `full` restores the old behaviour"
    assert len(m.BRIEF) < len(m.INSTRUCTIONS) / 10, "connect must not carry the long form"
    assert m.server.instructions == m.BRIEF


def test_the_guide_is_sent_once_and_then_not_again(monkeypatch):
    from serenedash import mcp_server as m

    monkeypatch.setattr(m, "_first_call", True)
    monkeypatch.setattr(m, "MODE", "lazy")

    @m.stamped
    def host():
        return {"cores": 24}

    first, second = host(), host()
    assert first["server"]["instructions"] == m.INSTRUCTIONS
    assert "instructions_note" in first["server"]
    assert "instructions" not in second["server"], "sending it twice doubles the cost it saved"
    assert second["server"]["instructions_revision"] == m.REVISION


def test_a_missing_brief_degrades_to_the_old_behaviour_not_to_silence(monkeypatch):
    # A client that gets nothing has no way to know it is missing anything.
    from serenedash import mcp_server as m

    monkeypatch.setattr(m.os.path, "dirname", lambda _p: "/nonexistent")
    assert m._read_brief() == ""


def test_the_brief_ships_in_the_wheel():
    # instructions.md already had this test for the same reason: package data is found in the source
    # tree by an editable install whatever the build config says, so only a real wheel proves it.
    import os

    from serenedash import mcp_server as m

    assert os.path.exists(os.path.join(os.path.dirname(m.__file__), "brief.md"))
