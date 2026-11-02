"""
bgscan.py -- run the Streamlit page's slow scans on ONE background worker thread.

Why: a click on a table row (the click-to-copy summary) only re-runs that table's fragment, but
Streamlit queues that fragment re-run until the full script run in progress has finished. When the
page body blocks on minutes of scans, the summary doesn't appear until they all complete. Here the
page asks fetch() for each scan instead: it returns immediately with whatever is ready (or None
while the first result is still being computed) and the page finishes its run in seconds, so row
clicks are answered right away. The page polls done_count() from a tiny fragment and reruns itself
as each scan lands, so sections fill in on their own.

One worker per lane (not a pool) on purpose: the scans push the sidebar criteria into module globals
(apply_criteria / apply_spread_criteria), and running them one at a time, in page order, is what the
blocking script did before. Work that doesn't touch those globals and is slow on its own (the
earnings-date lookups) takes a separate lane so it can't hold up the screeners queued behind it.
Results are cached process-wide for `ttl` seconds like st.cache_data was; past that the previous
result keeps showing while a refresh runs behind it.
"""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_pools = {}           # lane name -> its single-worker executor
_lock = threading.Lock()
_jobs = {}            # key -> {"running", "stamp", "value", "has_value", "error"}
_completed = [0]      # how many scans have finished (successfully or not), ever
PRUNE_AFTER_S = 3600  # forget idle results this old (different sidebar criteria leave one behind each)


def _arg_key(a):
    if hasattr(a, "to_csv"):                      # DataFrame (open positions): hash its contents
        return "df:" + hashlib.md5(a.to_csv().encode()).hexdigest()
    return repr(a)


def _key(fn, args, kwargs):
    return (fn.__module__, fn.__name__, tuple(_arg_key(a) for a in args),
            tuple(sorted((k, _arg_key(v)) for k, v in kwargs.items())))


def _run(job, fn, args, kwargs):
    try:
        value, error = fn(*args, **kwargs), None
    except BaseException as e:                    # surfaced to the page by fetch(), like a blocking call would
        value, error = None, e
    with _lock:
        if error is None:
            job["value"], job["has_value"] = value, True
        job["error"], job["stamp"], job["running"] = error, time.time(), False
        _completed[0] += 1


def fetch(fn, *args, ttl=600, lane="main", **kwargs):
    """(value, pending). Never blocks. value is None until the first result exists; once it does,
    an expired one is returned (pending=True) while a refresh runs. Re-raises the scan's own
    exception when it failed and there is no earlier result to fall back on, so the page's
    try/except still works."""
    key = _key(fn, args, kwargs)
    now = time.time()
    with _lock:
        for k in [k for k, j in _jobs.items() if not j["running"] and now - (j["stamp"] or now) > PRUNE_AFTER_S]:
            del _jobs[k]
        job = _jobs.setdefault(key, {"running": False, "stamp": None, "value": None,
                                     "has_value": False, "error": None})
        if not job["running"] and (job["stamp"] is None or now - job["stamp"] > ttl):
            job["running"] = True
            if lane not in _pools:
                _pools[lane] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"bgscan-{lane}")
            _pools[lane].submit(_run, job, fn, args, kwargs)
        running, value, has_value, error = job["running"], job["value"], job["has_value"], job["error"]
    if running:
        return (value if has_value else None), True
    if error is not None and not has_value:
        raise error
    return value, False


def done_count():
    """Total scans finished so far -- the page reruns itself when this changes."""
    return _completed[0]


def clear():
    """Forget every cached result (the page's 'Refresh data' button)."""
    with _lock:
        _jobs.clear()
