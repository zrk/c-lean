"""c_lean_core — backend for the single-call CLI.

One place that knows how to call a model with (almost) no harness context:
`call_model()`. Argument parsing and output formatting live in the executable.

Hard rules encoded here:
  * subscription only — the child environment is built from an ALLOW list, not
    from a deny list: only PATH/HOME/USER/... survive. Every ANTHROPIC_* and
    CLAUDE_* variable is dropped, so an API key, an OAuth token, a custom header,
    a foreign config dir or an output-token cap cannot ride in from the parent
    session. The CLI's API-key-only mode is never used: the call needs the
    subscription login.
  * every call runs in a fresh private empty directory, so no project CLAUDE.md,
    AGENTS.md or settings file is picked up.
  * the prompt is never on the command line: it goes to the child on stdin. A
    system prompt goes in a private file outside that directory, and
    `--system-prompt ""` is passed when the caller gave none: left out, the
    CLI puts its own built-in prompt in (see _claude_cmd). The prompt goes
    in as UTF-8 and the output is read as UTF-8, whatever the locale: that
    is what the CLI reads and writes.
  * the CLI is asked for its event stream, and the call is read from it after
    the process has ended: the `result` event is the document everything is
    decided by, and the `assistant` events are the only place that holds the
    whole text of an answer: `result` is the LAST text block alone (see
    read_stream). A CLI that has printed a whole `result` event and has not
    exited RESULT_GRACE seconds later is killed, and the call is judged by
    that event as if the CLI had exited (see _communicate).
  * no text block the model wrote is dropped: Result.blocks holds every one of
    them on every outcome where the CLI's stdout was read, so a caller can
    keep what it did not print (see save_blocks). Thinking is never kept.
  * the limit wall is NOT an ordinary failure: Result.wall is set, callers exit 75.
    It is decided by the STRUCTURE of the CLI answer, never by words in a
    document: see is_wall(). A document with is_error false is never a wall,
    whatever it quotes and whatever the process exit code was.
  * a child killed by a signal (Ctrl-C, SIGTERM, our own timeout kill) is
    Result.interrupted, not a failure: retrying it would spend a real call
    after the operator already said stop.
  * counters that could not be read are never silently reported as zero:
    Result.tokens_unknown travels into the usage log. It is set when a count
    is missing or is not an integer of 0 or more, when `iterations` is not a
    list or its messages do not all carry a counter, and for every call that
    the timeout or a signal killed. Nothing in a usage document can fail the
    call (see extract_usage). The log stores a sha256 hash of the prompt, not
    its text. This is not anonymization: anyone who holds a candidate prompt
    can confirm it against the hash.
  * the exit code in the trailer and in the usage log is the one the caller
    sets in Result.exit before it writes them: `c-lean` decides some exits
    after the call (an empty answer, a missing structured output), so it
    calls call_model(log=False) and log_call() itself.
"""
from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
import errno
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

# ---------------------------------------------------------------- exit codes
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64
EXIT_NOBIN = 69
EXIT_OSERR = 71   # a local resource the call needs could not be made (EX_OSERR)
EXIT_WALL = 75
EXIT_STREAM = 120  # stdout or stderr could no longer be written, as CPython has it
EXIT_TIMEOUT = 124
SIGNAL_EXIT_BASE = 128  # 128+signum, the shell convention: 130 SIGINT, 143 SIGTERM

TOOL_NAME = "c-lean"
ENV_PREFIX = TOOL_NAME.upper().replace("-", "_") + "_"
PIECES_KEY = TOOL_NAME.replace("-", "_") + "_pieces"


def state_path(override: str, name: str) -> str:
    """Resolve an explicit override before XDG_STATE_HOME or ~/.local/state.

    Read the environment when needed. An unresolved home must never create a
    literal `~` directory in the working directory.
    """
    root = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    path = os.path.expanduser(os.environ.get(override) or os.path.join(root, TOOL_NAME, name))
    if path.startswith("~"):
        raise OSError("cannot tell where %s is: the home directory is unknown (HOME is not "
                      "set); set %s to a path" % (path, override))
    return os.path.abspath(path)


def usage_log() -> str:
    """Path of the usage log, which stores a sha256 hash of the prompt.

    This is not anonymization: anyone who holds a candidate prompt can confirm
    it against the hash. The prompt text itself is never logged.
    """
    return state_path(ENV_PREFIX + "USAGE_LOG", "usage.jsonl")

# The child environment is a WHITELIST. A deny list cannot win here: the CLI
# reads ~70 ANTHROPIC_* and hundreds of CLAUDE_CODE_* names, and several of them
# (ANTHROPIC_CUSTOM_HEADERS, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CONFIG_DIR,
# CLAUDE_ENV_FILE) move the call off this subscription, while
# CLAUDE_CODE_MAX_OUTPUT_TOKENS / MAX_THINKING_TOKENS silently cut the answer.
# Only these names (plus the LC_* and C_LEAN_* families) reach the child.
ALLOW_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TEMP", "TMP", "TERM", "PWD",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "__CF_USER_TEXT_ENCODING", "COLORTERM",
)
ALLOW_ENV_PREFIX = ("LC_", ENV_PREFIX)
# Transport knobs. A proxy plus a foreign CA is a complete takeover of the call,
# and both used to ride in from the parent session in silence while
# ANTHROPIC_BASE_URL was being carefully stripped one line above. They are still
# available for a machine that needs them — but only by naming them in
# C_LEAN_PASS_ENV, and then the trailer says PROXY/CA so it is visible.
TRANSPORT_ENV = ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "no_proxy", "all_proxy")
# Never passed, whatever the allow list or C_LEAN_PASS_ENV says. NODE_/PYTHON/LD_/
# DYLD_/NPM_/BUN_/DENO_ are here because they are code loaders: the CLI is a node
# program, so NODE_OPTIONS="--require injected.js" runs a stranger's file inside
# the call, next to the prompt and subscription credentials. The transport
# opt-in must not become a way to load code.
DENY_ENV_PREFIX = ("ANTHROPIC_", "CLAUDE_", "AWS_", "GOOGLE_", "GCLOUD_",
                   "VERTEX_", "AZURE_", "OPENAI_",
                   "NODE_", "PYTHON", "LD_", "DYLD_", "NPM_", "BUN_", "DENO_",
                   "PERL", "RUBYOPT", "GEM_")

# ------------------------------------------------------------------ the wall
# A spent window is recognised by the SHAPE of the CLI answer, not by words in
# it. This is what one really looks like (recorded from a run that hit the weekly
# limit):
#
#   {"type": "result", "subtype": "success", "is_error": true,
#    "api_error_status": 429,
#    "result": "You've hit your weekly limit"}
#
# Three things matter and two do not:
#   * `is_error` is true — this is the load-bearing flag;
#   * `api_error_status` is 429 — the decisive marker, and the only part that
#     does not move when the wording changes (it went from "usage limit reached"
#     to "You've hit your weekly limit" once already, with no announcement);
#   * the error TYPE says rate_limit — recorded transcripts carry
#     `"error":"rate_limit"` next to the same sentence;
#   * `subtype` still says "success", so it proves nothing;
#   * the process exit code has been seen as 0 AND as 1 for the same wall, so it
#     cannot be required — and it cannot turn a healthy document into a wall.
# Hence the rule: words in a DOCUMENT are never read. `result` is data (a
# translation may answer "You've hit your weekly limit"), and a failed call
# whose text merely quotes the sentence is an ordinary failure. The sentence is
# looked for only when the CLI produced no document at all, and then only in what it wrote:
# stderr, and the lines of stdout that are not events of the stream (see the
# branch for a missing document in _call_claude).
WALL_HTTP_STATUS = (429,)
WALL_ERROR_TYPE_RE = re.compile(r"rate[_ -]?limit", re.I)
WALL_TEXT_RE = re.compile(
    r"(you'?ve\s+hit\s+your\b[^\n]{0,80}\blimit"
    r"|usage\s+limit\s+reached"
    r"|\bclaude\s+usage\s+limit\b"
    r"|\bout\s+of\s+usage(?:\s+credits)?\b"
    r"|\bupgrade\s+to\s+increase\s+your\s+usage\s+limit\b"
    r"|\blimit\s+reached\b[^\n]{0,40}\bresets?\b"
    r"|\brate[_ ]limit(?:ed|_error)?\b)",
    re.I,
)


def api_error_status(doc) -> int:
    """The HTTP status the CLI reports for the call, or 0 when it reports none."""
    if not isinstance(doc, dict):
        return 0
    v = doc.get("api_error_status")
    if isinstance(v, bool) or v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def error_type(doc) -> str:
    """The error type the CLI reports (`error` as a string, or its `type`), or ""."""
    e = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(e, dict):
        e = e.get("type")
    return e if isinstance(e, str) else ""


def is_wall(doc, blob: str = "") -> bool:
    """Is this answer a spent window? Structure only.

    `doc` is the parsed CLI document, or None when there was none — and then
    `blob` is what the CLI itself said instead (stderr, and the lines of stdout
    that are not events), the one place where the sentence itself may decide,
    because no model wrote it.
    """
    if isinstance(doc, dict):
        if not doc.get("is_error"):
            return False  # a document that is not an error is an answer, whatever it says
        return (api_error_status(doc) in WALL_HTTP_STATUS
                or bool(WALL_ERROR_TYPE_RE.search(error_type(doc))))
    return bool(WALL_TEXT_RE.search(blob or ""))

# The CLI reports why the answer ended. A healthy call says stop_reason
# "end_turn" / terminal_reason "completed" (measured live on haiku 2026-09-17,
# CLI 2.1.258). These values mean the text was cut off at the output cap: the
# answer looks well-formed but its tail is missing, so it must not be taken for
# a result.
TRUNCATED_STOP = ("max_tokens", "max_output_tokens", "length")
# Three places carry it. Only the first was read, and which one the CLI fills is
# a matter of version: `terminal_reason` and a nested `message.stop_reason` both
# exist in the envelope, and a document that says end_turn on top and
# max_output_tokens underneath used to be written down as a clean result.
STOP_REASON_PATHS = (("stop_reason",), ("terminal_reason",), ("message", "stop_reason"))


def truncated_stop(doc) -> str:
    """The cut-off marker found anywhere the CLI is known to put it, or ""."""
    if not isinstance(doc, dict):
        return ""
    for path in STOP_REASON_PATHS:
        node = doc
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node in TRUNCATED_STOP:
            return node
    return ""

# The output cap the child is given, as CLAUDE_CODE_MAX_OUTPUT_TOKENS. A model
# whose default cap is below its upper bound then writes up to the upper bound:
# fewer continuations, fewer seams. The CLI reads the variable like this
# (measured 2026-10-03, CLI 2.1.288): empty, not a number or <= 0 means the
# default cap of the model; above the upper bound of the model it is cut down to
# that bound; else it is taken as it is. The cut is silent: 128000 and 1000000
# on haiku (upper bound 64000) ran with not a word about the variable on either
# stream. So a number above every bound is safe, and this one is the largest
# bound there is today (opus-5 and the three others 128000, haiku-4-5 64000).
# Raise it when some model gets an upper bound above it: that model would be
# held down here (a call then says so, see over_cap).
OUTPUT_CAP = 128000

ZERO_USAGE = {"in": 0, "cw": 0, "cr": 0, "out": 0, "think": 0}

# ------------------------------------------------------------------ timeouts
# The largest --timeout accepted, in seconds (30 days). Past about 3e11 the wait in
# communicate() fails with an OverflowError, and nobody means a call of that length.
MAX_TIMEOUT = 2592000
# Seconds for one call of `c-lean` when --timeout is not given. Measured on opus
# at effort max (2026-10-03, CLI 2.1.288): 131,688 output tokens in 1236 s,
# about 106 tokens a second, so one message that runs to the 128K output cap
# takes about 1200 s. A flat 600 s killed exactly the calls that cost the most,
# with exit 124 and nothing to show for the tokens. The numbers are generous on
# purpose: waiting longer is cheaper than losing an answer that is already
# paid for. The CLI continues an answer up to 3 times, i.e. up to 4 messages,
# and 6000 s covers that worst case: four messages that each run to the cap.
DEFAULT_TIMEOUT = 1200
EFFORT_TIMEOUT = {"xhigh": 3600, "max": 6000}
# The values of --effort the CLI takes (CLI 2.1.312 `--help`). What it does
# with any other value is up to it, so -e takes these alone.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def default_timeout(effort) -> int:
    """Seconds a call may take at this effort when nobody said otherwise."""
    return EFFORT_TIMEOUT.get((effort or "").strip().lower(), DEFAULT_TIMEOUT)


def timeout_plan(explicit, effort):
    """(seconds, where the limit comes from). An explicit --timeout always wins."""
    if explicit is not None:
        return explicit, "set by --timeout"
    name = (effort or "").strip()
    return default_timeout(effort), (("the default for -e %s" % name) if name
                                     else "the default with no -e")


def seconds_shown(secs) -> str:
    return ("%d" % secs) if float(secs).is_integer() else ("%s" % secs)


def timeout_message(secs, source, finished, blocks=0) -> str:
    """What the operator needs after a kill by the timeout: where the limit
    came from, how to raise it, how far the model had got, and what is left of
    it. `finished` is Stream.finished of the output received before the kill,
    `blocks` is how many text blocks had come whole by then."""
    n, kept = int(finished or 0), int(blocks or 0)
    state = ("%d message%s of the model had finished and the next one had not"
             % (n, "" if n == 1 else "s")) if n else (
                 "0 messages of the model had finished: the first one was still being written")
    left = ("but %d text block%s had come whole before the kill and %s kept: see the next line"
            % (kept, "" if kept == 1 else "s", "is" if kept == 1 else "are")) if kept else (
                "and no text block had come whole before the kill, so there is nothing to keep")
    return ("timeout after %ss (%s; raise it with --timeout SEC). When the call was killed, "
            "%s. What was spent until then is billed. The message that was still being "
            "written cannot be recovered, %s." % (seconds_shown(secs), source, state, left))


class BadArg(Exception):
    """A command-line argument that cannot work — report it and exit 64."""


class Result:
    """`continued` is how many times the CLI continued the answer (see
    Stream.continued). `text` is always the `result` of the CLI document, i.e.
    the last text block of the answer. `pieces` is the text of every text
    block of an answer that came in two or more, in order, and only when they
    can be trusted to be one answer (see _pieces). `warn` is a line for stderr
    about blocks that could not be trusted.
    `blocks` is every non-empty text block the model wrote, in order, as it
    came: filled on every outcome where the CLI's stdout was read, a timeout
    and a kill by a signal included, so that the caller can keep what it does
    not print. Thinking blocks are never in it.
    `why` is one fixed phrase for what a failed call came to; unlike `err` it
    never quotes the CLI or the model. `went_on` says what the CLI went on
    after, once for each continuation.
    `finished` is set on a timeout only: see Stream.finished.
    `cap_over` is (model id, number) when the document named a default output
    cap above OUTPUT_CAP for that model, i.e. our number held it down; None
    when it did not or nothing could be read (see over_cap).
    `linger` is a line for stderr when the CLI had not exited after its
    `result` event and was killed, and the call was judged by that event;
    None otherwise."""
    __slots__ = ("ok", "exit", "text", "structured", "usage", "secs", "wall",
                 "err", "raw", "model", "effort", "backend", "interrupted",
                 "tokens_unknown", "binary", "transport",
                 "continued", "pieces", "warn", "finished", "blocks", "why",
                 "went_on", "cap_over", "linger")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))
        if self.usage is None:
            self.usage = dict(ZERO_USAGE)
        self.interrupted = bool(self.interrupted)
        self.tokens_unknown = bool(self.tokens_unknown)
        self.continued = int(self.continued or 0)
        self.blocks = list(self.blocks or [])
        self.went_on = list(self.went_on or [])

    @property
    def total_in(self) -> int:
        u = self.usage
        return int(u.get("in") or 0) + int(u.get("cw") or 0) + int(u.get("cr") or 0)


def scrubbed_env(source=None) -> dict:
    """Build the child environment from the allow list (see ALLOW_ENV).

    C_LEAN_PASS_ENV="A,B" adds names for an unusual machine. The denied prefixes
    hold even then, with one deliberate exception: the transport names above may
    be let back in by name, because a corporate proxy or CA is a real need and
    refusing it outright would just push somebody into editing this file.
    """
    src = dict(os.environ if source is None else source)
    extra = tuple(n.strip() for n in (src.get(ENV_PREFIX + "PASS_ENV") or "").split(",") if n.strip())
    env = {}
    for k, v in src.items():
        if k in extra and k in TRANSPORT_ENV:
            env[k] = v
            continue
        if k.startswith(DENY_ENV_PREFIX):
            continue
        if k in ALLOW_ENV or k in extra or k.startswith(ALLOW_ENV_PREFIX):
            env[k] = v
    return env


def over_cap(doc):
    """(model id, maxOutputTokens) of the model in the `modelUsage` of a result
    document whose default cap is the largest one above OUTPUT_CAP, or None.
    A number above OUTPUT_CAP means that CLAUDE_CODE_MAX_OUTPUT_TOKENS lowered
    the cap of that model instead of raising it. What cannot be read (no
    `modelUsage`, not a dict, an entry that is not a dict, no field, a value
    that is not an integer, a bool) is no information and gives None."""
    usage = doc.get("modelUsage") if isinstance(doc, dict) else None
    if not isinstance(usage, dict):
        return None
    found = None
    for name, entry in usage.items():
        n = entry.get("maxOutputTokens") if isinstance(entry, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and n > OUTPUT_CAP:
            if found is None or n > found[1]:
                found = (str(name), n)
    return found


def cap_over_note(r) -> str:
    """The line for stderr when OUTPUT_CAP held a model down, or ""."""
    if not r.cap_over:
        return ""
    name, n = r.cap_over
    return ("the default output cap of %s is %d, above the %d this tool sets in "
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS, so the call may have been held below it: raise "
            "OUTPUT_CAP in c_lean_core.py" % (name, n, OUTPUT_CAP))


def transport_notes(env: dict) -> str:
    """What of the transport travelled with this call, for the trailer. Silence
    here is the normal case; anything printed means the call could be watched or
    redirected by something outside this tool."""
    got = [k for k in TRANSPORT_ENV if k in env]
    return (" · " + " ".join(sorted(set(k.upper() for k in got)))) if got else ""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- usage
_USAGE_FIELDS = {
    "in": ("input_tokens",),
    "cw": ("cache_creation_input_tokens", "cache_creation_tokens"),
    "cr": ("cache_read_input_tokens", "cache_read_tokens"),
    "out": ("output_tokens",),
}
_THINK_NAMES = ("thinking_tokens", "reasoning_output_tokens", "reasoning_tokens")
# A counter the document holds under a known name, with a value that is no
# count (see _count). Such a total is not exact.
_BAD = object()


def _count(v) -> bool:
    """Is `v` a count of tokens: a JSON integer that is not negative? A float
    is none (1e999 is infinity), and neither is a bool, which Python takes for
    the integer 1."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _top(d, names):
    """The count under the first of `names` that THIS dict holds, with no
    descent into nested values: None when it holds none of them (null counts
    as not there), _BAD when the value there is not a count."""
    if not isinstance(d, dict):
        return None
    for n in names:
        v = d.get(n)
        if v is not None:
            return v if _count(v) else _BAD
    return None


def _think_of(d):
    """Thinking tokens of one usage-like dict: top level, then its *_details."""
    if not isinstance(d, dict):
        return None
    v = _top(d, _THINK_NAMES)
    if v is not None:
        return v
    for key, val in d.items():
        if key.endswith("_details") and isinstance(val, dict):
            v = _top(val, _THINK_NAMES)
            if v is not None:
                return v
    return None


def _total(per_message):
    """(count, exact) of a whole call from the count of each of its messages.
    The count is None when no message has one, else the sum of the counts
    there are. That sum is exact only when every message has a count: one
    that lacks it, or holds a value that is no count, makes it a lower bound."""
    got = [v for v in per_message if _count(v)]
    if all(v is None for v in per_message):
        return None, True
    return sum(got), len(got) == len(per_message)


def extract_usage(doc: dict):
    """(counters, tokens_unknown) for one CLI answer. The counters are always
    integers of 0 or more, and nothing a usage document holds raises here: a
    value that cannot be counted makes tokens_unknown true instead.

    Flat keys are read from the TOP level of `usage` only. The old code searched
    the whole tree for the first matching number, so a format where the totals
    moved into the per-message `iterations` list would have reported the FIRST
    iteration as the whole call (100 of 300 input tokens) without a word. Now:
    top level first, else the sum over `iterations`, else tokens_unknown.
    """
    u = doc.get("usage") if isinstance(doc, dict) else None
    if not isinstance(u, dict):
        return dict(ZERO_USAGE), True
    vals = {k: _top(u, names) for k, names in _USAGE_FIELDS.items()}
    vals["think"] = _think_of(u)
    exact = True
    iters = u.get("iterations")
    # An entry that is not a dict has none of the counters: see _total.
    if vals["in"] is None and vals["out"] is None and isinstance(iters, list):
        for k, names in _USAGE_FIELDS.items():
            vals[k], whole = _total([_top(i, names) for i in iters])
            exact = exact and whole
        if vals["think"] is None:
            vals["think"], whole = _total([_think_of(i) for i in iters])
            exact = exact and whole
    # A missing `in` or `out` makes the whole line a lower bound. The old rule
    # wanted BOTH to be missing, so a document that kept input_tokens and lost
    # output_tokens was logged as an exact "out 0". The cache counters are
    # different: a CLI that reports none has simply nothing cached, and treating
    # their absence as "unreadable" turned --check red on a healthy pipe.
    unknown = vals["in"] is None or vals["out"] is None
    # So does a value that is no count (counted as 0), and a sum over messages
    # that do not all have a count.
    if not exact or any(v is _BAD for v in vals.values()):
        unknown = True
    counters = {k: (v if _count(v) else 0) for k, v in vals.items()}
    return counters, unknown


def log_usage(rec: dict, path=None) -> None:
    """Append counters and the prompt's sha256 hash, never its text.

    This is not anonymization: anyone who holds a candidate prompt can confirm
    it against the hash.
    """
    try:
        path = usage_log() if path is None else path
        # A relative C_LEAN_USAGE_LOG has no directory part, and makedirs("")
        # raised: the accounting switched itself off with one warning line.
        parent = os.path.dirname(path)
        if parent:
            _private_dir(parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            os.fchmod(fd, 0o600)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # logging must never break a run
        # nor may a stderr that can no longer be written: the text blocks of
        # the call are saved after this line
        _say_during_call(f"{TOOL_NAME}: WARN could not write usage log: %s" % e)


# --------------------------------------------------------- children & signals
_LIVE_LOCK = threading.Lock()
_LIVE = set()
_INTERRUPT = {"signum": 0}
_READING_PROMPT = False
_TREE_WARNED = False
# The signals that stop a run: Ctrl-C, `kill`, and a closed terminal.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def interrupted() -> int:
    """Non-zero once a stop signal was seen: no new call may be started."""
    return _INTERRUPT["signum"]


def stop_signal_ignored() -> bool:
    """An inherited ignore requires isolating the call from job signals."""
    return any(signal.getsignal(s) is signal.SIG_IGN for s in STOP_SIGNALS)


class PromptStopped(Exception):
    """A stop interrupted a read of the prompt, --system-file or --schema,
    including opening a FIFO."""


@contextmanager
def reading_prompt():
    global _READING_PROMPT
    _READING_PROMPT = True
    try:
        if interrupted():
            raise PromptStopped()
        yield
    finally:
        _READING_PROMPT = False


def interrupt(signum: int = signal.SIGTERM) -> None:
    """Stop accepting calls and kill every child we started, whole tree.

    Without this a SIGTERM (supervisor timeout, `kill`) or a SIGHUP (closed
    terminal, `kill -HUP`) killed the wrapper and left children of `claude`
    running: they kept computing, kept being billed, and their answers went
    nowhere.
    """
    if not _INTERRUPT["signum"]:
        _INTERRUPT["signum"] = int(signum)
    # Called from a signal handler, i.e. possibly inside the very thread that is
    # holding _LIVE_LOCK: a plain `with` here could deadlock the stop itself.
    # A slightly racy read of the set is the lesser evil.
    got = _LIVE_LOCK.acquire(timeout=1)
    try:
        procs = list(_LIVE)
    except RuntimeError:  # the set changed under us
        procs = [p for p in _LIVE.copy()] if hasattr(_LIVE, "copy") else []
    finally:
        if got:
            _LIVE_LOCK.release()
    for p in procs:
        _kill_tree(p)


def install_signal_handlers() -> None:
    """Every stop signal is handled the same way: no new call starts, and the
    running children are killed with their trees.

    Every inherited SIG_IGN stays ignored; the call then leaves the job
    (see _call_claude). Raising during a prompt read prevents PEP 475 from
    restarting that read after the handler returns."""
    def handler(signum, frame):  # noqa: ARG001
        interrupt(signum)
        if _READING_PROMPT:
            raise PromptStopped()
    for s in STOP_SIGNALS:
        if signal.getsignal(s) is signal.SIG_IGN:
            continue
        try:
            signal.signal(s, handler)
        except (ValueError, OSError):  # not the main thread / not supported here
            pass


def ignore_stop_signals() -> None:
    """Stop listening: from here on no stop signal can change the exit code.

    Called on the way out of main(), once the call has ended and the exit code
    has been decided.

    At interpreter shutdown CPython puts SIG_DFL back for every signal that has
    a Python handler, and leaves SIG_IGN alone. A Ctrl-C landing there, after
    the answer was printed and the call was done, ended the process with -2
    instead of exit 0. SIG_IGN survives the shutdown: the window is gone, not
    narrowed."""
    for s in STOP_SIGNALS:
        try:
            signal.signal(s, signal.SIG_IGN)
        except (ValueError, OSError):  # not the main thread / not supported here
            pass


# ------------------------------------------------------------------ backends
def claude_bin() -> str:
    """The binary that will be run. It comes from C_LEAN_CLAUDE_BIN or from the
    PARENT's PATH, so it is worth printing: a swap is otherwise invisible."""
    raw = os.environ.get(ENV_PREFIX + "CLAUDE_BIN") or shutil.which("claude") or "claude"
    return os.path.abspath(os.path.expanduser(raw)) if os.sep in raw else raw


def system_prompt_arg(system) -> str:
    """The system prompt the CLI is given. Whitespace alone becomes the empty
    string: the API refuses a system block with no non-whitespace text (HTTP
    400), while the CLI takes "" as an empty prompt of its own."""
    return system if (system or "").strip() else ""


def _claude_cmd(binary, system_file, model, effort, schema):
    """The command line of one call. Neither the prompt nor the system prompt
    is on it: the prompt goes to the child on stdin, and a system prompt is
    the file `system_file` (see _system_file). On the command line the CLI
    took a prompt that begins with "-" for an option of its own, Linux refuses
    any one argument of 128 KiB or more, and every local user can read it in
    the process list. The JSON schema is the one value of any size left on it."""
    cmd = [binary, "-p",
           "--tools", "",
           "--setting-sources", "",
           "--strict-mcp-config",
           "--disable-slash-commands",
           "--no-session-persistence",
           # One JSON line per event. The single document of `json` carries only
           # the LAST message of an answer the CLI continued after the output
           # cap; the events carry them all (see read_stream).
           "--output-format", "stream-json", "--verbose"]
    if system_file:
        cmd += ["--system-prompt-file", system_file]
    else:
        # Always, even when empty. Without the flag the CLI puts in its own
        # built-in prompt, the Claude Code persona: 6,598 input tokens a call
        # against 406 with "" (measured 2026-10-03 on haiku, CLI 2.1.288).
        cmd += ["--system-prompt", ""]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if schema:
        cmd += ["--json-schema", schema]
    return cmd


def _system_file(text) -> str:
    """Write a system prompt to a new private file and return its path.

    mkstemp makes it in the temporary directory, not in the cwd of the call,
    which stays empty. _call_claude removes it where it removes that cwd."""
    fd, path = tempfile.mkstemp(prefix=TOOL_NAME + "-system-", suffix=".txt")
    try:
        # the CLI reads it as UTF-8; a lone surrogate goes in as its escape
        with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace", newline="") as f:
            os.fchmod(fd, 0o600)  # a umask can take bits off the 0600 of mkstemp
            f.write(text)
    except BaseException:
        os.unlink(path)
        raise
    return path


# What a value on the command line comes from, in the words of the operator.
ARG_SOURCES = {"--json-schema": "the JSON schema of --schema", "--model": "the model of -m",
               "--effort": "the effort of -e",
               "--system-prompt-file": "the path of the system prompt file"}


def _arg_name(cmd, i) -> str:
    return ARG_SOURCES.get(cmd[i - 1] if i else "") or "argument %d of the command line" % i


def _too_long(binary, cmd, env, why) -> str:
    """The message for a command line the system refused as too long (E2BIG):
    its biggest part, and how big that is. The prompt and the system prompt are
    never on it, so as a rule that part is the JSON schema."""
    sizes = [len(a.encode("utf-8", "replace")) for a in cmd]
    i = max(range(len(cmd)), key=sizes.__getitem__)
    env_size = sum(len(("%s=%s" % kv).encode("utf-8", "replace")) for kv in env.items())
    name, size = ("the environment", env_size) if env_size > sizes[i] else (_arg_name(cmd, i),
                                                                             sizes[i])
    return ("cannot start the claude binary %s: its command line is too long (%s). The biggest "
            "part of it is %s, %d bytes%s"
            % (binary, why, name, size,
               ": a schema goes on the command line whole, so make it smaller"
               if name == ARG_SOURCES["--json-schema"] else ""))


def _argv_bytes(cmd) -> list:
    """The command line as UTF-8 bytes, whatever the filesystem encoding.

    Under a locale that is not UTF-8 (C on Linux, with Python's coercion and
    UTF-8 mode off) Popen encoded a str argument as ASCII, so "é" in a
    --schema ended in a UnicodeEncodeError traceback. An argument that came
    from our own command line holds its undecodable bytes as surrogate
    escapes: they go out as the bytes they were."""
    return [a.encode("utf-8", "surrogateescape") for a in cmd]


def _nul_arg(cmd):
    """What holds a NUL character on the command line, or None when nothing does."""
    for i, a in enumerate(cmd):
        if "\0" in a:
            return _arg_name(cmd, i)
    return None


def print_err(line) -> None:
    """Print one line on stderr. A stderr closed from the start (`2>&-`) is
    None, and print() to None writes to stdout, the channel of the answer:
    the line is dropped then."""
    if sys.stderr is not None:
        print(line, file=sys.stderr)


def _say_during_call(line) -> None:
    """Print one line on stderr while a call runs, from a signal handler, or
    while the usage log is written.

    A stderr that can no longer be written (a pipe whose reader has exited, a
    closed stream) raised out of the read loop or the signal handler, and the
    run ended with exit 120 and no usage-log line. Such a line is lost, and the
    call is still judged and logged. A RuntimeError is a write that a signal
    handler started while the main thread was in the middle of one."""
    if sys.stderr is None:
        return  # closed from the start: see print_err
    try:
        print(line, file=sys.stderr)
    except (OSError, ValueError, RuntimeError):
        pass


def _tree_warning(reason) -> None:
    global _TREE_WARNED
    if not _TREE_WARNED:
        _TREE_WARNED = True
        _say_during_call(f"{TOOL_NAME}: WARN could not list the processes of the call "
                         "(%s); some may still run" % reason)


def _descendants(pid: int) -> list:
    """Collect the whole tree before killing anything; parents precede children."""
    children = {}
    if sys.platform.startswith("linux"):
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        # Bytes: comm is whatever the process set, in no encoding.
                        with open(entry.path + "/stat", "rb") as f:
                            # comm can contain spaces and parentheses. After its
                            # LAST ')' come state and ppid, in that order.
                            parent = int(f.read().rsplit(b")", 1)[1].split()[1])
                        children.setdefault(parent, []).append(int(entry.name))
                    except (OSError, ValueError, IndexError):
                        # exited during the snapshot (a read can then come back
                        # empty or cut short), or not readable: this entry only
                        continue
        except OSError as e:
            _tree_warning("/proc: %s" % e)
    found, todo, seen = [], [pid], {pid}
    pgrep = None if sys.platform.startswith("linux") else (
        shutil.which("pgrep") or "/usr/bin/pgrep")
    for parent in todo:
        kids = children.get(parent, [])
        if pgrep:
            try:
                out = subprocess.run([pgrep, "-P", str(parent)], capture_output=True,
                                     text=True, timeout=5)
                if out.returncode >= 2 or out.returncode < 0:
                    raise OSError("pgrep exited %d" % out.returncode)
                kids = [int(t) for t in out.stdout.split() if t.isdigit()]
            except subprocess.TimeoutExpired as e:
                # what was found is kept: one kill waits for at most one timeout
                _tree_warning(str(e))
                break
            except (OSError, subprocess.SubprocessError) as e:
                _tree_warning(str(e))
                continue  # the parents after this one in the queue are still asked
        for kid in kids:
            if kid not in seen:
                seen.add(kid)
                found.append(kid)
                todo.append(kid)
    return found


def _kill_tree(proc) -> None:
    """Kill the child AND anything it spawned.

    subprocess.run's own timeout path kills only the direct child; a grandchild
    survives (proven with a fake binary that spawns `sleep`). Even one call
    must not leak processes. The child stays in our process group
    on purpose, except when a stop signal was inherited as ignored.
    """
    for pid in reversed(_descendants(proc.pid)):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    if getattr(proc, "c_lean_own_session", False):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    if getattr(proc, "c_lean_killed_at", None) is None:
        proc.c_lean_killed_at = time.monotonic()


# ---------------------------------------------------------------- the stream
# What `--output-format stream-json --verbose` prints: one JSON object per
# line, of type `system`, `assistant`, `user`, `rate_limit_event` or `result`.
# Recorded 2026-10-03 on haiku, CLI 2.1.288, for an ordinary call and for an
# answer continued after the output cap; everything else below was read off
# the code of that CLI and has not been seen live.
#   * `result` has the keys the single document of `--output-format json` had.
#     Its `result` string is the text of the LAST CONTENT BLOCK of the last
#     `assistant` event, not of the whole last message, and every `user` event
#     resets it. A message of two text blocks gets only the second one into it.
#   * Every content block gets an `assistant` event of its own; the events of
#     one model message share `message.id`. `message.stop_reason` is null in
#     all of them, a message cut at the cap included, and `message.usage` is a
#     snapshot taken at the start of the message: neither is read here.
#   * The CLI goes on with an answer by itself, up to 3 times a call. It then
#     puts a `user` event with a top level `isSynthetic: true` before the next
#     message, and the first words of that event are the only thing that says
#     what it asked the model for (see RESUME_KINDS). The call ends like any
#     other: is_error false, stop_reason end_turn.
#   * One continuation has no `user` event at all. A message that was cut
#     inside its only thinking block is asked for again, and every `assistant`
#     event of the new request carries a top level
#     `resumed_from_incomplete_thinking: true`.
#   * A message can be taken back: `supersedes` in an `assistant` event, or
#     `retracted_message_uuids` in a `system` event, replaces earlier messages.
#   * When the last continuation hits the cap too, an `assistant` event comes
#     that the CLI wrote itself (`message.model` "<synthetic>", top level
#     `is_api_error_message` and `error`) with the text "API Error: …", and
#     `result` says is_error true, api_error "max_output_tokens".
SYNTHETIC_MODEL = "<synthetic>"

# Why words are read here, when everywhere else only the structure is.
# `isSynthetic` says that the CLI wrote the event, not what it asked for, and
# the answer is put together differently for each request. The strings below
# are constants of the CLI's code, not words of a model, and an event is told
# by the beginning of its text. After these three the model goes on with the
# same answer (after the last one it had written no text yet). After anything
# else nobody knows whether the next message goes on or starts again: a
# response stopped by a safety classifier, a hook's feedback, a wording that a
# later CLI brings. A text not listed here takes the safe branch: nothing is
# joined, the `result` of the document is printed and every block is kept.
RESUME_KINDS = (
    ("Output token limit hit. Resume directly", "the output cap"),
    ("Your response above was cut off mid-stream. Resume directly from where it stops",
     "a response cut off mid-stream"),
    ("[Your previous response had no visible output.", "a response with no visible text"),
)
RESUMED_THINKING = "thinking cut at the output cap"
JOIN, UNKNOWN = "join", "unknown"
UNKNOWN_SHOWN = 60    # characters of an unknown request quoted in the warning


class Stream:
    """One run of the CLI as its event stream tells it.

    doc        the last `result` event, or None when there is none
    events     how many lines of the output were JSON objects
    blocks     every non-empty text block the model wrote, in order, as it
               came: pairs (number of its message, text). A message is the
               events that share one `message.id`; a request of the CLI, or
               the mark of resumed thinking, starts a new one whatever the id.
    links      one entry for each message: how it is tied to the one before.
               (JOIN, what the CLI went on after) when the model was asked to
               go on with the answer, (UNKNOWN, the text of the request) when
               the CLI asked for something not known here, None when nothing
               ties the two. None for the first message.
    continued  how many messages are tied by JOIN: the times the CLI went on
               with the answer
    retracted  the CLI took a message back somewhere in the stream
    finished   messages known to be over: each one that something follows (a
               later message, or a request of the CLI). The last message
               seen is not counted without that: its events come block by
               block, and nothing in them says the message has ended.
    """
    __slots__ = ("doc", "events", "blocks", "links", "continued", "retracted", "finished")

    def texts(self):
        """The text of every block, in order."""
        return [t for _n, t in self.blocks]

    def joined_after(self):
        """What the CLI went on after, once for each continuation."""
        return [l[1] for l in self.links if l and l[0] == JOIN]


def _user_text(ev) -> str:
    """The text of a `user` event. Its content is a list of blocks in the
    stream and a plain string inside the CLI: both are read."""
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    return "".join(b["text"] for b in (content if isinstance(content, list) else ())
                   if isinstance(b, dict) and b.get("type") == "text"
                   and isinstance(b.get("text"), str))


def resume_kind(text):
    """What the CLI went on after, when `text` is one of its requests to go on
    with the answer; None for any other text."""
    for start, kind in RESUME_KINDS:
        if (text or "").startswith(start):
            return kind
    return None


def read_stream(raw_out) -> Stream:
    """Parse the stdout of one call. A line that is not a JSON object, or is
    an event of a type not known here, is skipped: the CLI adds event types
    between versions, and node prints warnings wherever it likes."""
    doc, events, msgs, blocks, retracted = None, 0, [], [], False
    asked = []    # the CLI's requests since the last message: (kind or None, text)
    # "\n" and nothing else: splitlines() also breaks on U+2028, U+2029 and
    # U+0085, which JSON leaves unescaped inside a string. An answer holding one
    # would split its own event into two halves that do not parse.
    for line in (raw_out or "").split("\n"):
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — whatever it is, it is not an event
            continue
        if not isinstance(ev, dict):
            continue
        events += 1
        kind = ev.get("type")
        if kind == "result":
            doc = ev
        elif kind == "system":
            if ev.get("retracted_message_uuids"):
                retracted = True
        elif kind == "user":
            # A tool_result under --json-schema is a `user` event too, and it
            # has no isSynthetic: it is no request of the CLI and ties nothing.
            if ev.get("isSynthetic") is True:
                text = _user_text(ev)
                asked.append((resume_kind(text), text))
        elif kind == "assistant":
            if ev.get("supersedes"):
                retracted = True
            msg = ev.get("message")
            if (not isinstance(msg, dict) or msg.get("model") == SYNTHETIC_MODEL
                    or ev.get("is_api_error_message") or ev.get("error")):
                continue  # the CLI's own "API Error: …" is not the model's answer
            mid = msg.get("id")
            resumed = ev.get("resumed_from_incomplete_thinking") is True
            # A resumed request is a message of its own even if the server
            # gives it the id of the one it resumes: which it does is not known.
            if (not msgs or asked or msgs[-1]["id"] != mid
                    or (resumed and not msgs[-1]["resumed"])):
                link = None
                if msgs:  # a tie counts only BETWEEN two messages
                    strange = [t for k, t in asked if k is None]
                    known = [k for k, _t in asked if k is not None]
                    if strange:  # one request nobody can explain is enough
                        link = (UNKNOWN, strange[0])
                    elif known:
                        link = (JOIN, known[-1])
                    elif resumed:
                        link = (JOIN, RESUMED_THINKING)
                msgs.append({"id": mid, "link": link, "resumed": resumed})
            asked = []
            content = msg.get("content")
            for block in content if isinstance(content, list) else ():
                # text blocks only: thinking is never kept, and neither is the
                # input of a tool_use
                if (isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str) and block["text"]):
                    blocks.append((len(msgs) - 1, block["text"]))
    s = Stream()
    s.doc = doc
    s.events = events
    s.blocks = blocks
    s.links = [m["link"] for m in msgs]
    s.continued = len(s.joined_after())
    s.retracted = retracted
    s.finished = (len(msgs) - 1 + (1 if asked else 0)) if msgs else 0
    return s


def _pieces(stream, text):
    """(pieces, warning) for a healthy document whose `result` is `text`.

    One text block, or none, is the ordinary call: the `result` of the
    document is that block, and there is nothing to put together. Two or more
    are given out as one answer only when they can be trusted to be one:
      * no message was taken back;
      * every message from the one after the first text block to the one of
        the last text block continues the message before it (the blocks of
        one message always belong together);
      * the last text block is what the document calls the result, and that
        result is not empty.
    Anything else is not joined: two messages with nothing between them may be
    a regeneration, and joining would print the text twice. Then only `result`
    goes out, with a warning, because that is what the caller got before this
    code existed, and the caller keeps the blocks (Result.blocks)."""
    blocks = stream.blocks
    if len(blocks) < 2:
        return None, None
    why = None
    if stream.retracted:
        why = ("the CLI took back a message of this call (`supersedes` or "
               "`retracted_message_uuids` in the stream), and which text blocks still stand "
               "cannot be told")
    for n in range(blocks[0][0] + 1, blocks[-1][0] + 1):
        if why:
            break
        link = stream.links[n]
        if link is None:
            why = ("message %d came after message %d with no continuation between them (a "
                   "regeneration, joined, would print the text twice)" % (n + 1, n))
        elif link[0] == UNKNOWN:
            why = ("before message %d the CLI asked the model for something that is not known "
                   "here as a continuation of the answer (its request begins %s), and whether "
                   "the model went on or started again cannot be told"
                   % (n + 1, json.dumps(link[1][:UNKNOWN_SHOWN], ensure_ascii=False)))
    if why is None and not text.strip():
        # The CLI empties `result` on every `user` event and sets "" when the
        # last block is not text: an empty `result` means something came after
        # the last text (a request left unanswered, a block that is not text).
        # A whitespace last block would agree with it below, and mean nothing.
        why = ("the `result` of the CLI's document is empty although text blocks came, so "
               "what the model wrote last was not text, or was asked for and not given")
    if why is None and blocks[-1][1].strip() != text:
        why = ("the event stream does not agree with the result document: the last text "
               "block of the stream is not the `result`")
    if why is None:
        return stream.texts(), None
    return None, why + (", so the %d text blocks of this call are not joined: only the `result` "
                        "of the CLI's document is printed, and the start of the answer may be "
                        "missing from it" % len(blocks))


def splice(pieces):
    """(answer, seams) for Result.pieces: the pieces joined as they came, and
    the position of every seam in the answer.

    Nothing is put in at a seam. After the output cap the model goes on from
    where it was cut, but a whitespace character can be lost there (a newline
    in 12 of 12 measured seams), and which one cannot be told from the text:
    a seam in the middle of a word needs nothing. The outer whitespace is
    stripped, as it is for the `result` of an ordinary call.

    A seam is a boundary between two pieces that lies inside the printed
    answer. A piece with no text adds no text and no seam of its own (its two
    boundaries are one place), and a piece of whitespace alone at either end
    of the answer leaves none."""
    pieces = list(pieces or [])
    joined = "".join(pieces)
    lead = len(joined) - len(joined.lstrip())
    answer = joined.strip()
    seams, pos = [], 0
    for p in pieces[:-1]:
        pos += len(p)
        at = pos - lead
        if 0 < at < len(answer) and at not in seams:
            seams.append(at)
    return answer, seams


# ---------------------------------------------------- text that was not printed
# Nothing of an answer that was paid for is dropped: a text block the model
# wrote either goes to stdout or is kept in a file here. The CLI decides which
# and calls save_blocks(). Thinking is never kept: it is not in Result.blocks
# to begin with.
BLOCK_MARK = "===== %s: text block %%d of %%d =====" % TOOL_NAME
PARTIAL_NAMES = 1000    # names tried for one second and one prompt before giving up


def partial_dir() -> str:
    """Where unprinted text is kept: the override, then the XDG state directory.
    Read when needed; an unknown home raises OSError (see state_path)."""
    return state_path(ENV_PREFIX + "PARTIAL_DIR", "partial")


def _private_dir(path) -> None:
    """Make the directory, and every missing one above it, with mode 0700.
    One that is already there is left as it is."""
    if os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and parent != path:
        _private_dir(parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)  # the mode given to mkdir is cut by the umask


def save_blocks(blocks, prompt, reason, model=None, effort=None, tool=TOOL_NAME, now=None):
    """Write text blocks that are not going to stdout into a new file of their
    own, and return its full path. Raises OSError when that cannot be done.

    The first line says what the file is; every block follows a line that
    numbers it, as it came, with one newline added after it so that the next
    such line starts a line of its own. The file is named after the time and
    the prompt (its sha256, never its text), and an existing file is never
    written over: a counter is added to the name instead."""
    blocks = list(blocks)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    digest = sha256(prompt)
    directory = partial_dir()
    _private_dir(directory)
    base = "%s-%s" % (now.strftime("%Y%m%dT%H%M%SZ"), digest[:8])
    for n in range(1, PARTIAL_NAMES + 1):
        path = os.path.join(directory, base + ("" if n == 1 else "-%d" % n) + ".txt")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise OSError("%d files named %s*.txt are already in %s"
                      % (PARTIAL_NAMES, base, directory))
    # no newline translation, and a lone surrogate (JSON can carry one) is
    # written as its escape instead of failing the whole file
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace", newline="") as f:
            os.fchmod(fd, 0o600)  # the mode given to open is cut by the umask
            f.write("# %s %s · model %s · effort %s · NOT a result: %s · %d text block%s, %d "
                    "characters, one newline added after each block · prompt sha256 %s\n"
                    % (tool, now.strftime("%Y-%m-%dT%H:%M:%SZ"), model or "?", effort or "none",
                       " ".join((reason or "the text was not printed").split()),
                       len(blocks), "" if len(blocks) == 1 else "s",
                       sum(len(b) for b in blocks), digest))
            for i, block in enumerate(blocks, 1):
                f.write((BLOCK_MARK % (i, len(blocks))) + "\n" + block + "\n")
    except BaseException:
        # an empty or cut-off file is worse than none: it says the text was
        # kept when it was not
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


# Seconds between two looks at what the child has printed so far (see
# _communicate). It also keeps selector waits far below the ~24 days at
# which poll() overflows: it takes milliseconds in a C int.
WATCH_SLICE = 0.5
# Seconds the CLI has to exit once it has printed a whole `result` event. The
# answer is in that event: a CLI still alive after this is killed rather than
# waited for until the timeout, which would cost the answer.
RESULT_GRACE = 5


# Seconds to drain output after a kill, even if an orphan holds a pipe.
KILL_DRAIN = 3


def _feed(fd, data) -> None:
    """Write the prompt to the child's stdin and close it. This runs in a
    thread of its own so a child that does not read it cannot block output
    reading or stop handling. A child that exits before it has read all of it (EPIPE) costs
    nothing here: the run is judged by its exit code and stderr."""
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        pass
    finally:
        os.close(fd)


def _holds_result(lines: bytes) -> bool:
    """Do these lines (a whole JSON tail included) hold a `result` event?"""
    for line in lines.split(b"\n"):
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — whatever it is, it is not an event
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return True
    return False


def _communicate(proc, timeout):
    """Read both pipes ourselves, independently of child exit and pipe EOF.

    Return text, the reason for a kill (if any), and the child's exit code
    before that kill. No Popen buffers or TimeoutExpired partial output are used.
    The c_lean_* attributes are our own call state, not Popen internals.
    """
    if not 0 <= timeout <= MAX_TIMEOUT:  # nan would never run out below
        raise ValueError("timeout must be between 0 and %d seconds, got %r" % (MAX_TIMEOUT, timeout))
    end = time.monotonic() + timeout
    scanned, result_at, exited_at = 0, None, None
    reason, gone = None, None
    buffers = [bytearray(), bytearray()]
    pipes = (proc.stdout, proc.stderr)
    with selectors.DefaultSelector() as selector:
        try:
            for i, pipe in enumerate(pipes):
                selector.register(pipe, selectors.EVENT_READ, i)
            while True:
                now = time.monotonic()
                rc = proc.poll()
                if rc is not None and exited_at is None:
                    exited_at = now
                killed_at = getattr(proc, "c_lean_killed_at", None)
                if interrupted() and killed_at is None:
                    _kill_tree(proc)
                    killed_at = proc.c_lean_killed_at
                if killed_at is not None and reason is None:
                    reason = "stop"
                if rc is not None and not selector.get_map():
                    break
                if killed_at is None:
                    if exited_at is not None and now - exited_at >= RESULT_GRACE:
                        reason = "pipes"
                    elif rc is None and result_at is not None and now - result_at >= RESULT_GRACE:
                        reason = "result"
                    elif now >= end:
                        reason = "timeout"
                    if reason:
                        gone = rc
                        _kill_tree(proc)
                        killed_at = proc.c_lean_killed_at
                if killed_at is not None:
                    deadline = killed_at + KILL_DRAIN
                    if now >= deadline:
                        if selector.get_map():
                            _say_during_call(f"{TOOL_NAME}: WARN a process started by the call "
                                             "still held its output open after %ss; it may "
                                             "still run" % seconds_shown(KILL_DRAIN))
                        break
                else:
                    deadlines = [end]
                    if result_at is not None and rc is None:
                        deadlines.append(result_at + RESULT_GRACE)
                    if exited_at is not None:
                        deadlines.append(exited_at + RESULT_GRACE)
                    deadline = min(deadlines)
                wait = max(0.0, min(deadline - time.monotonic(), WATCH_SLICE))
                if not selector.get_map():
                    time.sleep(wait)  # EOF on both pipes does not imply child exit
                    continue
                for key, _ in selector.select(wait):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(data)
                    if key.data == 0 and result_at is None:
                        out = buffers[0]
                        # Include the tail: JSON parsing accepts a whole object
                        # without a newline, and rejects every cut object.
                        if _holds_result(out[scanned:]):
                            result_at = time.monotonic()
                        scanned = out.rfind(b"\n") + 1
        finally:
            for pipe in pipes:
                pipe.close()
    # Match TextIOWrapper's UTF-8/backslashreplace and universal newlines,
    # including a CRLF split between reads and a final incomplete character.
    stdout, stderr = (bytes(data).decode(pipe.encoding, pipe.errors)
                      .replace("\r\n", "\n").replace("\r", "\n")
                      for data, pipe in zip(buffers, pipes))
    return stdout, stderr, reason, gone


def _cannot_make(what, e, t0) -> Result:
    """The call could not start because a local resource it needs could not
    be made: a full or read-only temporary directory, a quota, no file
    descriptor or thread left. That is exit 71, with the trailer and the
    usage-log line of any other call that did not start."""
    reason = getattr(e, "strerror", None) or str(e) or type(e).__name__
    if getattr(e, "filename", None):
        reason += " (%s)" % e.filename
    return Result(ok=False, exit=EXIT_OSERR, text="", raw=None, wall=False,
                  secs=(_dt.datetime.now() - t0).total_seconds(),
                  err="cannot start the call: %s: %s" % (what, reason))


def _call_claude(prompt, system, model, effort, schema, timeout):
    if interrupted():
        return Result(ok=False, exit=SIGNAL_EXIT_BASE + interrupted(), text="", wall=False,
                      err="stopped before the call (signal %d)" % interrupted(),
                      secs=0.0, raw=None, interrupted=True)
    binary = claude_bin()
    system = system_prompt_arg(system)
    env = scrubbed_env()
    # Ours alone: scrubbed_env() never lets the caller's value in, and what is
    # put here after it is not the caller's either (see OUTPUT_CAP).
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(OUTPUT_CAP)
    t0 = _dt.datetime.now()
    proc = workdir = system_file = feed = None
    # What is being made for the call while it is made, None once it is: an
    # OSError (or a thread that cannot start) there is exit 71, not a
    # traceback, and the `finally` below removes whatever was made before it.
    making = "the working directory"
    try:
        workdir = tempfile.mkdtemp(prefix=TOOL_NAME + "-run-")
        os.chmod(workdir, 0o700)
        making = "the system-prompt file"
        system_file = _system_file(system) if system else None
        cmd = _claude_cmd(binary, system_file, model, effort, schema)
        # stdin is a pipe of our own: _feed writes the prompt into it
        making = "the stdin pipe"
        stdin_end, feed = os.pipe()
        making = None
        try:
            # The CLI installs handlers even over inherited ignores. A session
            # of its own shields it from those signals sent to the whole job.
            own_session = stop_signal_ignored()
            proc = subprocess.Popen(
                _argv_bytes(cmd), stdin=stdin_end, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="backslashreplace", cwd=workdir, env=env,
                start_new_session=own_session,
            )
        except OSError as e:
            if e.errno == errno.E2BIG:
                # Not the binary: a value the operator gave, the schema as a rule
                return Result(ok=False, exit=EXIT_USAGE, text="", raw=None, secs=0.0, wall=False,
                              err=_too_long(binary, cmd, env, e.strerror or e))
            # Not just "not found": no +x after a cp or a restore, a broken
            # shebang (Exec format error), a path that is a directory. Each of
            # those must report exit 69 instead of a traceback.
            return Result(ok=False, exit=EXIT_NOBIN, text="", raw=None, secs=0.0, wall=False,
                          err="cannot run the claude binary %s: %s"
                              % (binary, e.strerror or e))
        except ValueError:
            # A file given to --schema can hold a NUL character; a command line cannot.
            where = _nul_arg(cmd)
            if where is None:
                raise
            return Result(ok=False, exit=EXIT_USAGE, text="", raw=None, secs=0.0, wall=False,
                          err="cannot start the claude binary %s: %s holds a NUL character, "
                              "and a command line cannot carry one" % (binary, where))
        finally:
            os.close(stdin_end)
        proc.c_lean_own_session = own_session
        with _LIVE_LOCK:
            _LIVE.add(proc)
        if interrupted():  # the signal landed between Popen and the registration
            _kill_tree(proc)
        making = "the feeder thread"
        threading.Thread(target=_feed, args=(feed, prompt.encode("utf-8", "backslashreplace")),
                         name=TOOL_NAME + "-stdin", daemon=True).start()
        making = None
        feed = None  # the thread closes it
        linger = None
        stdout, stderr, reason, gone = _communicate(proc, timeout)
        rc = proc.returncode
        cut = read_stream(stdout)
        if reason == "timeout" and not isinstance(cut.doc, dict) and not interrupted():
            secs = (_dt.datetime.now() - t0).total_seconds()
            return Result(ok=False, exit=EXIT_TIMEOUT, text="", err="timeout after %ss" % timeout,
                          why="the call was killed by the timeout after %ss"
                              % seconds_shown(timeout),
                          finished=cut.finished, blocks=cut.texts(),
                          continued=cut.continued, went_on=cut.joined_after(),
                          secs=secs, wall=False, raw=None, tokens_unknown=True)
        if reason == "pipes":
            rc = gone
            linger = ("the claude CLI exited but a process it started still held its output "
                      "open %ss later: its process tree was killed, and the call is judged "
                      "by what it printed and its own exit code" % seconds_shown(RESULT_GRACE))
        elif reason in ("result", "timeout"):
            rc = 0 if gone is None else gone
            linger = ("the claude CLI printed its `result` event and had not exited %s: it "
                      "was killed with its process tree, and the call is judged by that event"
                      % (("%ss later" % seconds_shown(RESULT_GRACE)) if reason == "result"
                         else "when the timeout of %ss ran out" % seconds_shown(timeout)))
    except (OSError, RuntimeError) as e:
        if making is None:
            raise
        if proc is not None:
            # The child waits for its prompt: it is killed before the `finally`
            # closes its stdin, so that it never goes on with an empty one.
            _kill_tree(proc)
        return _cannot_make(making, e, t0)
    finally:
        if feed is not None:
            os.close(feed)
        if proc is not None:
            # Whatever left the read loop, the child must not go on being computed and
            # paid for with nobody to read the answer.
            try:
                if proc.poll() is None:
                    _kill_tree(proc)
                    proc.wait(timeout=WATCH_SLICE)
            except Exception:  # noqa: BLE001 — the exception in flight is the one to report
                pass
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            with _LIVE_LOCK:
                _LIVE.discard(proc)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass
    secs = (_dt.datetime.now() - t0).total_seconds()

    raw_out, raw_err = stdout or "", stderr or ""
    stream = read_stream(raw_out)
    # Every text block the model wrote, whatever the call comes to below.
    blocks = stream.texts()

    caught = interrupted()
    good_result = isinstance(stream.doc, dict) and not stream.doc.get("is_error")
    if caught and good_result:
        rc = 0
        # c-lean killed the CLI only when the CLI died of the SIGKILL of a kill
        # made in this call. One that had exited by itself keeps its own code:
        # a stop that came later killed nothing.
        killed = (getattr(proc, "c_lean_killed_at", None) is not None
                  and proc.returncode == -signal.SIGKILL)
        if killed:
            linger = ("the stop (signal %d) came after the answer: the call was killed with its "
                      "process tree, and the call is judged by its `result` event" % caught)
        else:
            linger = "the stop (signal %d) came after the answer, and the answer stands" % caught
    if caught and not good_result:
        # The stop line alone says what happened: a WARN of the kill before
        # the stop would say the call is judged by something it is not.
        return Result(ok=False, exit=SIGNAL_EXIT_BASE + caught, text="", wall=False, raw=None,
                      err="call stopped by signal %d" % caught, secs=secs, interrupted=True,
                      why="the call was stopped by signal %d" % caught, blocks=blocks,
                      continued=stream.continued, went_on=stream.joined_after(),
                      tokens_unknown=True)

    if rc is not None and rc < 0:
        # Killed by a signal — our own stop, or the Ctrl-C that reached the whole
        # process group. Not an ordinary failure: retrying it would be a real,
        # paid call made after the operator already said stop. The model may
        # have spent tokens by then, and no document says how many.
        return Result(ok=False, exit=SIGNAL_EXIT_BASE - rc, text="", wall=False, raw=None,
                      err="call killed by signal %d" % (-rc), secs=secs, interrupted=True,
                      why="the call was killed by signal %d" % (-rc), blocks=blocks, linger=linger,
                      continued=stream.continued, went_on=stream.joined_after(),
                      tokens_unknown=True)

    # The document of the call is the last `result` event of the stream. What
    # was decided by the single document of `--output-format json` is decided
    # by it, in the same way.
    doc = stream.doc
    continued = stream.continued

    if not isinstance(doc, dict):
        # No document at all: only then may the sentence decide, and only where
        # the CLI itself wrote it: on stderr, and in every line of stdout that
        # does NOT begin with `{`. A line that does is an event, whole or cut
        # short, and events hold a model's words: they never decide, wherever
        # in the output they stand. The stream begins with `system/init`, so
        # "stdout before the first `{`" was nothing at all for a live CLI.
        own = "\n".join(l for l in raw_out.split("\n") if not l.lstrip().startswith("{"))
        said = (own[-4000:] + "\n" + raw_err[-4000:]).strip()
        wall = is_wall(None, said)
        if wall:
            err, why = "limit wall: " + said[-400:], "the call hit the limit wall"
        elif stream.events:
            # the CLI did start: saying "no JSON" here sent the reader looking
            # for a broken binary
            why = "the stream of the CLI ended with no `result` event"
            err = ("the stream of claude ended with no `result` event (%d event%s came before "
                   "it ended, rc=%d): %s"
                   % (stream.events, "" if stream.events == 1 else "s", rc,
                      said[-400:] or "the CLI said nothing else"))
        else:
            why = "the CLI printed no JSON"
            err = "no JSON from claude (rc=%d): %s" % (
                rc, (raw_out + "\n" + raw_err)[-4000:].strip()[-400:])
        return Result(ok=False, exit=(EXIT_WALL if wall else EXIT_FAIL), text="", wall=wall,
                      err=err, why=why, blocks=blocks,
                      secs=secs, raw=None, tokens_unknown=True, continued=continued, linger=linger)

    usage, tokens_unknown = extract_usage(doc)
    text = (doc.get("result") or "")
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = text.strip()
    structured = doc.get("structured_output")
    is_err = bool(doc.get("is_error"))
    # what every outcome below has in common
    common = dict(text=text, structured=structured, usage=usage, secs=secs, raw=doc,
                  tokens_unknown=tokens_unknown, continued=continued, blocks=blocks,
                  went_on=stream.joined_after(), cap_over=over_cap(doc), linger=linger)

    if is_wall(doc):
        return Result(ok=False, exit=EXIT_WALL, wall=True, why="the call hit the limit wall",
                      err="limit wall (api_error_status=%s error=%s): %s"
                          % (api_error_status(doc) or "none", error_type(doc) or "none",
                             (text or raw_err.strip())[:300]), **common)
    if is_err and "max_output_tokens" in (doc.get("api_error"), error_type(doc)):
        # The CLI continues an answer that hit the cap, and gives up when the
        # last continuation hits it too. `result` is then the CLI's own "API
        # Error: …" line and `subtype` still says success, so the plain
        # message below would read like an outage, not like an answer that is
        # too long (or thinking that ate the cap) and will be on a retry too.
        # The marker has two names in the CLI's code, `api_error` and `error`,
        # and a document is not promised to carry both.
        return Result(ok=False, exit=EXIT_FAIL, wall=False,
                      why="the answer hit the output cap and the CLI ran out of continuations",
                      err="the answer hit the output cap and the CLI ran out of continuations "
                          "(%d made, the last message was cut off too; error "
                          "max_output_tokens, rc=%d) — the answer never finished, not a "
                          "result: %s" % (continued, rc, (text or raw_err)[:200]), **common)
    if is_err or (rc != 0):
        return Result(ok=False, exit=EXIT_FAIL, wall=False, why="the CLI reported a failure",
                      err="claude reported failure (rc=%d subtype=%s): %s"
                          % (rc, doc.get("subtype"), (text or raw_err)[:300]), **common)
    stop_reason = truncated_stop(doc)
    if stop_reason:
        return Result(ok=False, exit=EXIT_FAIL, wall=False,
                      why="the answer was cut off by the output cap",
                      err="answer cut off by the output cap (stop_reason=%s) — the tail is "
                          "missing, not a result" % stop_reason, **common)
    # Under a schema the answer is `structured_output` of the document, and
    # what a continuation looks like there has not been measured: nothing is
    # put together from the text blocks.
    pieces, warn = (None, None) if schema else _pieces(stream, text)
    return Result(ok=True, exit=EXIT_OK, wall=False, err=None, pieces=pieces, warn=warn,
                  **common)


BACKENDS = {"claude": _call_claude}


def call_model(prompt, *, system=None, model=None, effort=None, schema=None,
               timeout=600, backend="claude", tag="", tool=TOOL_NAME, log=True):
    """The single funnel every model call goes through.

    Returns a Result. Appends counters and the prompt's sha256 hash to the
    usage log (see usage_log). This is not anonymization: anyone who holds a
    candidate prompt can confirm it against the hash. With log=False the
    caller writes that line itself with log_call(), once it has decided the
    exit code.
    """
    fn = BACKENDS.get(backend)
    if fn is None:
        raise ValueError("unknown backend %r (have: %s)" % (backend, ", ".join(sorted(BACKENDS))))
    r = fn(prompt, system, model, effort, schema, timeout)
    r.backend, r.model, r.effort = backend, model, effort
    if backend == "claude":
        r.binary = claude_bin()
        r.transport = transport_notes(scrubbed_env())
    if log:
        log_call(r, prompt, tag=tag, tool=tool)
    return r


def log_call(r: Result, prompt, tag="", tool=TOOL_NAME) -> None:
    """Append the usage-log line of one call, with r.exit as its exit code."""
    log_usage({
        "ts": now_iso(), "tool": tool, "tag": tag, "backend": r.backend or "",
        "model": r.model or "", "effort": r.effort or "",
        "in": r.usage.get("in", 0), "cw": r.usage.get("cw", 0),
        "cr": r.usage.get("cr", 0), "out": r.usage.get("out", 0),
        "think": r.usage.get("think", 0),
        "tokens_unknown": bool(r.tokens_unknown),
        "interrupted": bool(r.interrupted),
        # times the CLI went on with the answer (the output cap, as a
        # rule): the tokens of such a call are sums over all of its messages
        "continued": int(r.continued or 0),
        "secs": round(r.secs or 0.0, 2), "exit": r.exit, "wall": bool(r.wall),
        "prompt_sha256": sha256(prompt),
        "prompt_chars": len(prompt),
    })


def trailer(r: Result, tool=TOOL_NAME) -> str:
    """`think` is part of `out`, so it is printed inside brackets: a reader who
    added the two numbers would overstate the spend. NOT COUNTED means the
    numbers are not exact (Result.tokens_unknown). The exit code is r.exit,
    which the caller sets to the exit code of the process first."""
    u = r.usage
    return ("[%s] %s%s · tokens in %s cw %s cr %s out %s (think %s)%s · %.1fs · exit %d%s%s%s%s\n"
            % (tool, r.model or "?", ("/" + r.effort) if r.effort else "",
               u.get("in", 0), u.get("cw", 0), u.get("cr", 0), u.get("out", 0), u.get("think", 0),
               " NOT COUNTED" if r.tokens_unknown else "",
               r.secs or 0.0, r.exit,
               " · LIMIT WALL" if r.wall else (" · INTERRUPTED" if r.interrupted else ""),
               # the CLI went on with the answer N times (see Stream.links)
               (" · CONTINUED x%d" % r.continued) if r.continued else "",
               # which binary actually ran, so a swapped one is visible
               (" · bin %s" % r.binary) if r.binary else "",
               r.transport or ""))


def decode_arg(data: bytes, source: str) -> str:
    """Text the operator gave in a file or on stdin, decoded as UTF-8 whatever
    the locale: the CLI reads UTF-8, and a locale that is not UTF-8 turned
    "é" into a UnicodeDecodeError traceback. Bytes that are not UTF-8 are an
    operator error (64) that names the first of them."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BadArg("%s: not UTF-8: the first bad byte is 0x%02x at byte offset %d"
                     % (source, data[e.start], e.start))


def read_file_arg(flag: str, path: str) -> str:
    """A file named on the command line. A missing one is an operator error (64),
    not a traceback out of the middle of a run."""
    try:
        with open(os.path.expanduser(path), "rb") as f:
            data = f.read()
    except OSError as e:
        raise BadArg("%s %s: %s" % (flag, path, e.strerror or e))
    # universal newlines, as the file had them when it was read as text
    return decode_arg(data, "%s %s" % (flag, path)).replace("\r\n", "\n").replace("\r", "\n")


def read_stdin_arg(source: str) -> str:
    """The prompt on stdin, decoded as UTF-8 whatever the locale. A closed
    stdin (`c-lean <&-`) holds nothing: sys.stdin is None then."""
    if sys.stdin is None:
        return ""
    try:
        data = sys.stdin.buffer.read()
    except OSError as e:
        raise BadArg("%s: %s" % (source, e.strerror or e))
    return decode_arg(data, source)


def read_text_arg(value: str) -> str:
    """`--schema` style argument: inline JSON if it starts with '{', else a file path."""
    v = value.strip()
    if not v:
        # an empty value switched the schema off without a word
        raise BadArg("--schema takes inline JSON or a file, and its value is empty")
    if v.startswith("{") or v.startswith("["):
        return v
    text = read_file_arg("--schema", value)
    if not text.strip():
        # an empty file switched the schema off without a word, as "" did
        raise BadArg("--schema takes inline JSON or a file, and the file %s is empty" % value)
    return text


CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def looks_english(text: str) -> bool:
    """Cheap language gate. A leaked `language` setting turns the English probe
    answer into another language; this gate notices Cyrillic only."""
    return not CYRILLIC.search(text or "")
