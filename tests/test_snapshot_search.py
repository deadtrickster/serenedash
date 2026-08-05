"""The search-engine payload, and the three collectors folded into the panels beside it.

Half of these assert what the payload must NOT say. `sdb_metrics` is the one source here that can
be missing entirely - a deployment with no inverted index, a server too old to have the table, a
connection that dropped - and an empty result rendered the same way as a healthy one is the mistake
this repo keeps a rules file about. The other half are the thresholds: one is a documented default
and one was chosen, and the payload has to say which is which.
"""
from serenedash import snapshot


def metrics(**over):
    """One busy index and one idle one, in db.search()'s shape. Numbers off the live server."""
    ix = {
        "num_docs": 11217241, "num_live_docs": 11217229, "num_buffered_docs": 0,
        "num_segments": 16, "num_files": 109, "index_size": 58083506834,
        "num_failed_commits": 0, "num_failed_cleanups": 0, "num_failed_consolidations": 0,
        "avg_commit_time_ms": 226, "avg_cleanup_time_ms": 1, "avg_consolidation_time_ms": 672,
    }
    ix.update(over)
    return {"server": {"pg_connections": 8, "http_connections": 0,
                       "refresh_active": 0, "refresh_pending": 0,
                       "compaction_active": 1, "compaction_pending": 0,
                       "cleanup_active": 0, "cleanup_pending": 0},
            "indexes": {"2000801": ix}}


def one(sr, what_contains):
    return [f for f in snapshot.search_findings(sr) if what_contains in f["what"]]


# --- absence of evidence ------------------------------------------------------------------------

def test_no_metrics_is_unavailable_with_a_reason_not_an_empty_index_list():
    # The whole point. `{"indexes": []}` from a server that could not be read is a payload saying
    # the indexes are fine, which is a claim nobody measured.
    out = snapshot.search(None)
    assert out["available"] is False
    assert out["reason"]
    assert "indexes" not in out
    assert "healthy" in out["note"]


def test_a_table_that_answered_with_nothing_is_also_unavailable():
    # The server-wide counters are emitted unconditionally, so zero rows is the view not answering.
    out = snapshot.search({"server": {}, "indexes": {}})
    assert out["available"] is False


def test_a_server_with_counters_but_no_index_says_so_rather_than_looking_empty():
    out = snapshot.search({"server": {"pg_connections": 3}, "indexes": {}})
    assert out["available"] is True
    assert out["indexes"] == [] and out["indexes_reported"] == 0
    assert "no per-index rows" in out["indexes_note"]


def test_an_unreadable_index_produces_no_findings_at_all():
    # Not "nothing tripped" - nothing was looked at, so nothing may be claimed either way.
    assert snapshot.search_findings(snapshot.search(None)) == []


# --- every rate beside its base -----------------------------------------------------------------

def test_deleted_documents_are_a_derived_count_with_the_total_they_divide():
    ix = snapshot.search(metrics())["indexes"][0]
    assert ix["deleted_docs"] == 12
    assert ix["num_docs"] == 11217241 and ix["num_live_docs"] == 11217229
    assert ix["deleted_share_of_docs"] == round(12 / 11217241, 6)


def test_the_derived_sizes_carry_the_counts_they_were_divided_by():
    ix = snapshot.search(metrics())["indexes"][0]
    assert ix["bytes_per_live_doc"] == round(58083506834 / 11217229)
    assert ix["live_docs_per_segment"] == round(11217229 / 16)
    assert ix["index_size_bytes"] == 58083506834 and ix["num_segments"] == 16


def test_the_note_says_index_size_and_du_are_different_measures():
    # 58.1 GB from the engine against 61.2 GB of du on the same directory here. Neither is wrong
    # and a reader who adds or reconciles them is being misled by the payload, not by the server.
    out = snapshot.search(metrics())
    assert "du" in out["note"] and "will not match" in out["note"]


def test_maintenance_pairs_keep_active_and_pending_together():
    m = snapshot.search(metrics())["server"]["maintenance"]
    assert set(m) == {"refresh", "compaction", "cleanup"}
    assert m["compaction"] == {"active": 1, "pending": 0}


def test_a_missing_metric_lands_as_null_rather_than_as_zero():
    out = snapshot.search({"server": {}, "indexes": {"7": {"num_docs": 10}}})
    ix = out["indexes"][0]
    assert ix["num_docs"] == 10
    assert ix["num_segments"] is None and ix["bytes_per_live_doc"] is None


# --- findings -----------------------------------------------------------------------------------

def test_any_non_zero_failure_counter_is_a_finding_carrying_its_counts():
    f = one(snapshot.search(metrics(num_failed_consolidations=3, num_failed_commits=1)), "failed")
    assert len(f) == 1
    assert f[0]["num_failed_consolidations"] == 3 and f[0]["num_failed_commits"] == 1
    assert "num_failed_cleanups" not in f[0], "a zero counter is not a finding"
    # Cumulative counters with no timestamp: the finding has to say it may be old.
    assert "may be old" in f[0]["detail"]
    assert f[0]["verify"]


def test_clean_counters_produce_nothing():
    assert one(snapshot.search(metrics()), "failed") == []


def test_a_queue_behind_a_running_job_fires_and_a_queue_alone_does_not():
    sr = metrics()
    sr["server"]["refresh_pending"], sr["server"]["refresh_active"] = 4, 1
    f = one(snapshot.search(sr), "refresh")
    assert len(f) == 1 and f[0]["refresh_pending"] == 4 and f[0]["refresh_active"] == 1

    sr["server"]["refresh_active"] = 0
    assert one(snapshot.search(sr), "refresh") == [], "pending with nothing running drains itself"


def test_deleted_documents_fire_only_over_the_chosen_share_and_say_it_was_chosen():
    live = int(11217241 * (1 - snapshot.DELETED_SHARE - 0.01))
    f = one(snapshot.search(metrics(num_live_docs=live)), "deleted")
    assert len(f) == 1
    assert f[0]["threshold_is_chosen"] is True
    assert "CHOSEN threshold" in f[0]["detail"]
    assert f[0]["deleted_docs"] == 11217241 - live and f[0]["num_docs"] == 11217241
    # The share is only actionable as a size: a tenth of 58 GB of segments is the number.
    assert "of postings" in f[0]["detail"]


def test_twelve_deleted_documents_out_of_eleven_million_are_not_a_finding():
    assert one(snapshot.search(metrics()), "deleted") == []


def test_a_long_consolidation_reports_the_cost_and_not_a_backlog_it_did_not_measure():
    # 15,368 ms was read off this deployment an hour after it read 672. What the row may NOT say is
    # that consolidation is behind: this used to be titled "slower than its interval" and to
    # conclude "so segments accumulate", and both halves were inference. compaction_pending is the
    # backlog, it is measured, and on the deployment that produced the rule it was 0.
    f = one(snapshot.search(metrics(avg_consolidation_time_ms=15368)), "consolidation")
    assert len(f) == 1
    assert f[0]["avg_consolidation_time_ms"] == 15368
    assert f[0]["documented_interval_ms"] == snapshot.CONSOLIDATION_MS == 1000
    assert f[0]["interval_readable_here"] is False
    assert "compaction_pending" in f[0] and "compaction_active" in f[0]
    assert "compaction_interval" in f[0]["detail"] and "CREATE INDEX" in f[0]["detail"]
    # The conclusion the old row asserted, in the case where nothing corroborates it.
    assert "not an observed backlog" in f[0]["detail"]
    assert "slower than its interval" not in f[0]["what"]


def test_a_672ms_consolidation_is_under_the_interval_and_stays_quiet():
    assert one(snapshot.search(metrics()), "consolidation") == []


def test_every_search_finding_carries_numbers_and_a_way_to_check_it():
    sr = metrics(num_failed_commits=2, num_live_docs=1000, avg_consolidation_time_ms=15368)
    sr["server"]["cleanup_pending"], sr["server"]["cleanup_active"] = 2, 1
    fs = snapshot.search_findings(snapshot.search(sr))
    assert len(fs) == 4
    for f in fs:
        assert f["verify"], f["what"]
        assert any(isinstance(v, (int, float)) for v in f.values()), f["what"]


# --- sdb_progress, folded into activity ---------------------------------------------------------

def state(**over):
    s = {"db": "d", "size": 2**36, "wal": 2**20, "mem": 2**35, "memlimit": 2**37,
         "blocks": (10, 9, 1, 262144), "memtags": [("BASE_TABLE", 2**35)], "memspill": {},
         "states": {"active": 1, "idle": 2},
         "queries": [("active", "select 1", 8)], "settings": {}, "t": 0.0}
    s.update(over)
    return s


def prow(pid="1", **over):
    r = {"pid": pid, "state": "active", "command": "SELECT", "phase": "scan", "percent": 12.0,
         "rows_done": 100.0, "rows_total": 1000.0, "bytes_done": 0.0, "bytes_total": 0.0}
    r.update(over)
    return r


def test_no_progress_rows_at_all_is_unavailable_not_an_idle_server():
    # The connection that asks is active by construction and appears in its own result, so a call
    # that reached the view gets at least one row. Zero rows means the view did not answer.
    p = snapshot.progress([])
    assert p["available"] is False
    assert "not" in p["detail"] and "sdb_progress" in p["reason"]


def test_progress_says_that_one_row_is_the_collector_itself():
    # The bug this repeats otherwise: "1 active" printed above "nothing running", two definitions
    # of active one line apart. sessions excludes this connection and sdb_progress cannot.
    p = snapshot.progress([prow()])
    assert p["available"] is True
    assert "the connection that collected this" in p["note"]


def test_progress_counters_arrive_beside_the_totals_they_are_shares_of():
    r = snapshot.progress([prow()])["rows"][0]
    assert r["rows_done"] == 100.0 and r["rows_total"] == 1000.0
    assert r["percent"] == 12.0


def test_a_row_with_no_phase_is_counted_apart_from_one_with_progress():
    rows = [prow("1"), prow("2", command="", phase="", percent=0.0, rows_done=0.0,
                            rows_total=0.0)]
    p = snapshot.progress(rows)
    assert p["rows_reported"] == 2
    assert p["rows_with_a_phase"] == 1
    assert "carries no progress information" in p["note"]


def test_progress_is_bounded_and_says_it_was_cut():
    # activity returned 1.66 MB once by shipping unbounded server text. Nothing new gets to be
    # unbounded, including a row count.
    p = snapshot.progress([prow(str(i)) for i in range(500)], max_rows=25)
    assert len(p["rows"]) == 25
    assert p["rows_reported"] == 500 and p["rows_truncated"] is True
    assert "25 of 500" in p["note"]


def test_activity_folds_progress_in_and_omits_it_when_it_was_not_collected():
    a = snapshot.activity(state(), 400, [prow()])
    assert a["progress"]["available"] is True
    assert a["sessions"] and a["nothing_running"] is False
    assert "progress" not in snapshot.activity(state(), 400)


# --- temp files held, and per-pool spill --------------------------------------------------------

HOST = {"uptime": 3600, "rss": 2**33, "swap": 0, "ram_total": 2**37, "cores": 24}
SZ = {"duck": 2**35, "index": 2**34, "temp": 2**30, "total": 2**36, "temp_files": [], "temp_d": 0}


def test_held_temp_files_are_reported_beside_the_orphaned_split():
    out = snapshot.storage(state(), SZ, HOST, (0, 0))
    assert out["server_temp_files_held"] == 0 and out["server_temp_files_held_bytes"] == 0
    assert "duckdb_temporary_files()" in out["server_temp_files_note"]


def test_not_having_asked_the_server_is_null_and_not_zero():
    out = snapshot.storage(state(), SZ, HOST, None)
    assert out["server_temp_files_held"] is None
    assert "null means" in out["server_temp_files_note"]


def test_the_orphan_finding_quotes_the_server_holding_none_of_them():
    sz = dict(SZ, temp=72 * 2**30, temp_files=[(1.0, 72 * 2**30)])
    f = [x for x in snapshot.findings(state(), sz, HOST, held=(0, 0))
         if x["what"] == "orphaned temp files"]
    assert len(f) == 1
    assert f[0]["server_temp_files_held"] == 0
    assert "holding 0 files open" in f[0]["detail"]
    assert "duckdb_temporary_files()" in f[0]["verify"]


def test_the_orphan_finding_does_not_claim_proof_it_does_not_have():
    sz = dict(SZ, temp=72 * 2**30, temp_files=[(1.0, 72 * 2**30)])
    f = [x for x in snapshot.findings(state(), sz, HOST) if x["what"] == "orphaned temp files"][0]
    assert f["server_temp_files_held"] is None
    assert "holding 0 files open" not in f["detail"]


def test_which_pool_spilled_not_just_that_spill_happened():
    m = snapshot.memory(state(memspill={"ORDER_BY": 2**30}), HOST)
    assert m["spilled_bytes_by_pool"] == {"ORDER_BY": 2**30}
    assert m["spilled_bytes_total"] == 2**30


def test_no_pool_reporting_spill_does_not_mean_the_temp_directory_is_empty():
    m = snapshot.memory(state(), HOST)
    assert m["spilled_bytes_by_pool"] == {} and m["spilled_bytes_total"] == 0
    assert "spill_orphaned_bytes" in m["spill_note"]


def test_every_finding_carries_a_kind_it_can_be_counted_by():
    # The summary line on the findings screen counts by kind, and the kind is set at the source -
    # a categoriser reading the wording of `what` would quietly re-file a finding the moment a
    # measurement was corrected, which happens here regularly.
    from serenedash import snapshot

    sr = metrics(num_failed_commits=2, avg_consolidation_time_ms=15368)
    s = {"size": 10**9, "wal": 10**11, "mem": 10**9, "memlimit": 10**9,
         "settings": {"checkpoint_threshold": "16.0 MiB"}, "queries": [], "memtags": {},
         "blocks": (0, 0, 0, 0)}
    host = {"uptime": 100, "swap": 10**9, "rss": 10**9, "ram_total": 10**11}
    sz = {"temp_files": [(0, 10**9)], "temp": 10**9}
    found = snapshot.findings(s, sz, host, {}, sr=snapshot.search(sr), held=(0, 0))
    assert found, "this fixture is meant to trip several"
    assert all(f.get("kind") for f in found), [f["what"] for f in found if not f.get("kind")]
    assert {"storage", "memory", "search"} <= {f["kind"] for f in found}
