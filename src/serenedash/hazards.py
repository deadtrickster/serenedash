"""serenedash.hazards"""
import re

from .fmt import human, to_bytes


# ── settings worth an opinion ───────────────────────────────────────────────────────────────────
#
# `duckdb_settings()` already ships a description for all 297, so the `c` view shows the server's
# own text and does not restate it. What is added here is only what we have MEASURED on a real
# deployment — consequences, not documentation. Each entry is (why it matters, predicate on the
# value returning a warning string or None).
#
# Deliberately short. A hazard list that grows to fifty entries stops being read, and the value of
# this one is that every line in it cost someone a day.
def _rel_temp(v, _s):
    if not str(v).startswith("/"):
        return ("RELATIVE path. serened runs as uid 999 with cwd /, which is root-owned, so this "
                "resolves to /.tmp and CANNOT BE CREATED. Every spill fails with EACCES — silently, "
                "until the working set first exceeds memory_limit.")
    return None


# Statements that are writing. `states` counts active sessions but cannot say what they are
# running, and on this deployment the active ones are usually BM25 selects - counting those as
# writers would have the checkpoint predicate below firing on a read-only server.
#
# COPY is the RAGFlow ingest feeder here, so it counts - but only in the `COPY <table> FROM` form.
# `COPY (select …) TO` and `COPY <table> TO` are exports, and the second of those is why this
# matches the target-then-FROM shape rather than looking for the word `from` anywhere: a subquery
# inside an export carries one.
_WRITE = re.compile(r"^\s*((insert|update|delete|merge)\b|copy\s+[^\s(]+\s+from\b)", re.I)


def _writers(s):
    """Active sessions running a write statement. Reads only the head the sample already carries."""
    return sum(1 for q in s.get("queries", [])
               if len(q) >= 2 and q[0] == "active" and _WRITE.match(q[1] or ""))


def _ckpt(v, s):
    wal = s.get("wal", 0)
    thr = to_bytes(v)
    if thr and wal > max(thr * 50, 2**30):
        return (f"WAL is {human(wal)} against a threshold of {human(thr)} — {wal / thr:.0f}x. "
                "Automatic checkpointing is not completing; look for write errors, not for tuning.")
    # The other end of the same setting, and the one that is easy to read backwards. Compression
    # runs AT CHECKPOINT: a 103-second checkpoint on this deployment was roughly half compression
    # or the analysis deciding how to compress (FSST ~22%, float analyse+ALP ~14%). So how OFTEN it
    # runs is a checkpoint setting, not a codec choice — the threshold is WAL bytes between
    # checkpoints and every writer spends from the same 16 MiB, so the more of them there are the
    # less any one table grew before its row groups are analysed and rewritten again.
    #
    # Both cuts are shaped by the measured case, 16 MiB with 11 concurrent inserts: three writers
    # because two sharing a threshold is not yet a pattern, and 8 MiB each because a 1 GiB
    # threshold would need 128 concurrent writers to get there and that is a different problem.
    # The branch above returns rather than falling through, on purpose: a runaway WAL means
    # checkpoints are NOT landing, which is the opposite finding off the same number, and a row
    # carrying both would be the `1 active` / `nothing running` mistake inside one string.
    w = _writers(s)
    if thr and w >= 3 and thr / w < 8 * 2**20:
        return (f"{v} / {w} active writers = {human(thr / w)} each — compression re-runs at every "
                "checkpoint on row groups that never fill")
    return None


def _mem(v, s):
    if s.get("memlimit") and s.get("mem", 0) / s["memlimit"] > 0.9:
        return "over 90% of the limit — spilling is imminent, so temp_directory had better be valid"
    return None


HAZARDS = {
    "temp_directory": ("where spills go; a wrong value fails only under load", _rel_temp),
    "checkpoint_threshold": ("WAL size that should trigger an automatic checkpoint", _ckpt),
    "memory_limit": ("working set ceiling before spilling to temp_directory", _mem),
    # No predicate, and the reason is the entry. The threshold applies to the column's AVERAGE
    # length, so "does zstd ever fire on this table" needs avg(length(col)) over the column —
    # 10.39M rows across 47 columns here, a full scan and a round trip, not a column that can ride
    # along on a statement `sample()` already runs. It was measured once, through query(): the
    # widest text column, `content_with_weight`, averages 1891 characters against this 4096, so it
    # is FSST-only however large the table gets — 2205 characters short, and growing the table
    # cannot close that. Nothing on the tick path can see any of it, so the row states the rule and
    # claims nothing about this server. Worth knowing it is a speed-over-ratio trade by design: a
    # column below the threshold is not misconfigured, it is uncompressed by zstd on purpose.
    "zstd_min_string_length": ("applies to the column AVERAGE length, so a text column under it "
                               "stays FSST-only however large the table gets", None),
    # Also no predicate, for a different reason. The unit is not in the server's description ("the
    # estimated WAL write size"), and it is BYTES: duck_transaction_manager.cpp compares
    # `undo_properties.estimated_size` — undo buffer positions, sizeof(row_t) per deleted row, index
    # sizes, plus local storage for appends — against this setting, and only for the commit that is
    # already taking a checkpoint. What `sample()` carries is statement TEXT length, which is a
    # different quantity in the direction that would make the warning wrong: a 1024-dim embedding
    # arrives here as a 21,684-character literal and stores as 4 KiB of floats, so text length is an
    # over-estimate of the commit it produces. Firing off it would print a blocked-commits claim
    # that the number cannot support.
    "auto_checkpoint_skip_wal_threshold": ("bytes of estimated commit size; above it a "
                                           "checkpointing commit skips the WAL and BLOCKS "
                                           "concurrent commits while it runs", None),
    "threads": ("parallelism; also multiplies per-thread memory", None),
    "preserve_insertion_order": ("false lets large sorts/inserts avoid materialising in order, "
                                 "which cuts both spill volume and time", None),
}


# ── which engine a hot symbol belongs to ────────────────────────────────────────────────────────
#
# The useful question about a profile here is not user-vs-kernel, it is WHICH ENGINE is burning the
# cycles: SereneDB is DuckDB columnar storage + IResearch text index + FAISS/BLAS vector search
# behind one pg-wire front end, and they fail in completely different ways.
#
# Measured examples that motivated each bucket:
#   vector   sgemm_kernel at 16% — IVF k-means retraining its centroids, single-threaded, while
#            23 cores waited. A matmul dominating means clustering, not search.
#   text     irs::FieldData::add_term, DelimitedTokenizer::next — inverted index insertion.
#   columnar duckdb::RLEState<float>::UpdateFlatValid — column encode.
#   wire     sdb::message::Buffer::ReadableSize at 96% of a fourteen-hour load — the COPY feeder
#            walking its whole chunk list per message to test a five-byte threshold.
#   parse    duckdb::ListMatcher/KeywordMatcher/ChoiceMatcher::MatchParseResult at 9.9% combined.
#            These are the PEG grammar's matchers, i.e. reading the statement TEXT. They land in the
#            duckdb:: namespace, so `columnar` used to claim them and the panel reported a tenth of
#            the profile as storage work when it was the parser. SereneDB's own developer spotted
#            the same misreading off a screenshot of this dashboard, which is how it was found.
#            A large share here means the statements themselves are expensive to read - on this
#            deployment, 1024-dim embeddings sent as 21,684-character text literals.
KERNELS = (
    ("vector", ("gemm", "sgemm", "faiss", "IndexIVF", "Quantizer", "distance", "l2_", "knn",
                "cblas", "openblas", "simsimd", "hnsw")),
    ("text", ("irs::", "BM25", "Posting", "FieldData", "Tokenizer", "analysis::", "term_",
              "iresearch")),
    # Before `columnar`, which would otherwise take every duckdb:: symbol including these.
    # Deliberately NOT a bare "Matcher": `RowMatcher` is the hash-join row comparator and
    # `ExpressionMatcher` is the optimizer's rewriter, and neither reads statement text. Every PEG
    # grammar matcher goes through `MatchParseResult`, which is the precise handle.
    ("parse", ("MatchParseResult", "PEGParser", "PEGTransformer", "peg::", "duckdb::Parser",
               "ParseResult", "MatcherFactory", "MatcherList", "MatcherToken",
               "MatchNumberLiteral", "MatchIdentifier", "MatchOperator")),
    ("columnar", ("duckdb::", "RLE", "ColumnData", "RowGroup", "Vector::", "Compress")),
    ("wire", ("sdb::message", "pg_wire", "Buffer::", "CopyEod")),
    ("alloc", ("je_", "malloc", "free", "arena", "tcache")),
)


def kernel_of(sym):
    low = sym.lower()
    for name, pats in KERNELS:
        if any(pt.lower() in low for pt in pats):
            return name
    return "kernel" if sym.startswith("[k]") else "other"
