"""serenedash.hazards"""
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


def _ckpt(v, s):
    wal = s.get("wal", 0)
    thr = to_bytes(v)
    if thr and wal > max(thr * 50, 2**30):
        return (f"WAL is {human(wal)} against a threshold of {human(thr)} — {wal / thr:.0f}x. "
                "Automatic checkpointing is not completing; look for write errors, not for tuning.")
    return None


def _mem(v, s):
    if s.get("memlimit") and s.get("mem", 0) / s["memlimit"] > 0.9:
        return "over 90% of the limit — spilling is imminent, so temp_directory had better be valid"
    return None


HAZARDS = {
    "temp_directory": ("where spills go; a wrong value fails only under load", _rel_temp),
    "checkpoint_threshold": ("WAL size that should trigger an automatic checkpoint", _ckpt),
    "memory_limit": ("working set ceiling before spilling to temp_directory", _mem),
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
KERNELS = (
    ("vector", ("gemm", "sgemm", "faiss", "IndexIVF", "Quantizer", "distance", "l2_", "knn",
                "cblas", "openblas", "simsimd", "hnsw")),
    ("text", ("irs::", "BM25", "Posting", "FieldData", "Tokenizer", "analysis::", "term_",
              "iresearch")),
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
