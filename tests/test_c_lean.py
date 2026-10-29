#!/usr/bin/env python3
"""Tests for the single-call CLI against a fake `claude` binary.

Run:  python3 tests/test_c_lean.py

No model is called: C_LEAN_CLAUDE_BIN points the tool at fake_claude.py.
The tool under test lives in the repository's bin/ directory; C_LEAN_BIN_DIR
overrides that (redphase.py uses it to test a deliberately broken copy).

Every check here was first seen RED on its own assert by breaking the wrapper on
purpose (the rule "never trust a test you have not seen fail") — redphase.py
replays those breakages on a throwaway copy.
"""
import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("C_LEAN_BIN_DIR") or os.path.join(os.path.dirname(TESTS), "bin")
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
# The core under test, for the checks that need no process: the
# timeout table (nobody waits 6000 s in a test), the stream reader, and the
# writer of the files of unprinted text
sys.path.insert(0, BIN)
import c_lean_core as B  # noqa: E402

CLI = os.path.join(BIN, B.TOOL_NAME)
FAKE = os.path.join(TESTS, "fake_claude.py")
# `c-lean` keeps text it did not print in ~/.local/state/c-lean/partial unless
# C_LEAN_PARTIAL_DIR says otherwise, and no test may write there. Set here, the
# variable reaches every child this suite starts and every call it makes in
# its own process; each test then points it at a directory of its own (setUp).
PARTIAL_SANDBOX = tempfile.mkdtemp(prefix="c-lean-partial-sandbox-")
os.environ["C_LEAN_PARTIAL_DIR"] = PARTIAL_SANDBOX
atexit.register(shutil.rmtree, PARTIAL_SANDBOX, True)
# Even a mutation that runs in HOME must stay inside test-created directories.
HOME_SANDBOX = tempfile.mkdtemp(prefix="c-lean-home-")
os.environ["HOME"] = HOME_SANDBOX
os.environ["XDG_STATE_HOME"] = ""
os.environ["C_LEAN_USAGE_LOG"] = os.path.join(HOME_SANDBOX, "usage.jsonl")
atexit.register(shutil.rmtree, HOME_SANDBOX, True)
# Process discovery uses the real `pgrep`. Where a sandbox forbids listing
# processes, C_LEAN_TEST_FAKE_PGREP=1 puts a fixture first on PATH instead:
# it reports the process trees the fake recorded, and the timeout and signal
# tests still have to kill the real child processes.
PGREP_SANDBOX = None
if os.environ.get("C_LEAN_TEST_FAKE_PGREP") == "1":
    PGREP_SANDBOX = tempfile.mkdtemp(prefix="c-lean-process-fixture-")
    os.symlink(FAKE, os.path.join(PGREP_SANDBOX, "pgrep"))
    atexit.register(shutil.rmtree, PGREP_SANDBOX, True)
EXIT_WALL = 75
# the three messages of the fake's `cont3` answer (fake_claude.CONT_PIECES)
CONT_PIECES = ["alpha 1\nalpha 2\nalpha 3\nalpha 4\nalpha 5\nalpha 6",
               "beta 1\nbeta 2", "gamma 1\ngamma 2"]
# the four cut messages of `contfail` (fake_claude.CUT_PIECES): the second one
# has whitespace at both ends, which a saved file must keep
CUT_PIECES = ["1\n2\n3", "\n4\n5 ", "6\n7", "8\n9"]
# the two text blocks of the one message of `twoblocks` (fake_claude.TWO_BLOCKS)
TWO_BLOCKS = ["BLOCK one, then thinking, ", "then BLOCK two"]
# How the CLI's own requests begin (constants of CLI 2.1.288). After the first
# three the model goes on with the same answer; the last one is the request
# after a response that a safety classifier stopped, which nobody can read.
RESUME_STARTS = ("Output token limit hit. Resume directly",
                 "Your response above was cut off mid-stream. Resume directly from where it stops",
                 "[Your previous response had no visible output.")
# whole requests of the CLI, as they begin in the stream
CAP_ASK = RESUME_STARTS[0] + " — no apology, no recap of what you were doing."
CUT_ASK = RESUME_STARTS[1] + " — no apology, no recap."
SAFETY_START_60 = "Your response above was stopped by a safety classifier — thi"
BLOCK_MARK_RE = re.compile(r"(?m)^===== c-lean: text block (\d+) of (\d+) =====\n")


def ev_block(mid, block, **top):
    """One `assistant` event of a stream: one content block of message `mid`."""
    return dict({"type": "assistant", "session_id": "test",
                 "message": {"id": mid, "model": "claude-test-1", "role": "assistant",
                             "type": "message", "content": [block], "stop_reason": None}}, **top)


def ev_text(mid, text, **top):
    return ev_block(mid, {"type": "text", "text": text}, **top)


def ev_think(mid, text="", **top):
    return ev_block(mid, {"type": "thinking", "thinking": text, "signature": "c2ln"}, **top)


def ev_ask(content):
    """A request the CLI makes of the model by itself: a `user` event it marks."""
    return {"type": "user", "isSynthetic": True, "session_id": "test",
            "message": {"role": "user", "content": content}}


def ev_result(result, **extra):
    return dict({"type": "result", "subtype": "success", "is_error": False, "result": result,
                 "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 7}},
                **extra)


def stream_text(*events):
    """The stdout of a call that printed these events, one JSON line each."""
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
# Loads `c-lean` as a module and runs its main() with the model call replaced, to
# see what the tool hands to the call without making one. In a process of its
# own: main() installs signal handlers and writes to both streams.
TIMEOUT_PROBE = r'''
import importlib.machinery, importlib.util, json, sys
loader = importlib.machinery.SourceFileLoader("c_lean_cli", sys.argv[1])
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("c_lean_cli", loader))
loader.exec_module(mod)
seen = {}


def call_model(prompt, **kw):
    seen["timeout"] = kw.get("timeout")
    return mod.B.Result(ok=True, exit=0, text="x", secs=0.0)


mod.B.call_model = call_model
rc = mod.main(sys.argv[2:])
sys.stderr.write("PROBE " + json.dumps(seen) + "\n")
sys.exit(rc)
'''
# Loads `c-lean` as a module and runs its main() for real, with a fake binary under
# it, after two optional changes made in this process alone: PROBE_NO_HOME takes
# HOME away and makes `~` unexpandable (a machine where the user database does
# not know the user either: here it always does, and the real home must never be
# written), PROBE_NOW fixes the clock of the files of saved text.
KEEP_PROBE = r'''
import datetime, importlib.machinery, importlib.util, os, sys
loader = importlib.machinery.SourceFileLoader("c_lean_cli", sys.argv[1])
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("c_lean_cli", loader))
loader.exec_module(mod)
if os.environ.get("PROBE_NO_HOME"):
    os.environ.pop("HOME", None)
    os.path.expanduser = lambda p: p
if os.environ.get("PROBE_NOW"):
    real, now = mod.B.save_blocks, datetime.datetime(2026, 10, 3, 10, 15, 0,
                                                     tzinfo=datetime.timezone.utc)
    mod.B.save_blocks = lambda *a, **k: real(*a, now=now, **k)
sys.exit(mod.main(sys.argv[2:]))
'''
# every signal that stops a run: Ctrl-C, `kill`, a closed terminal
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# the names that must never reach the child: keys, tokens, transport switches,
# a foreign config dir, the thinking cap, and one plain unrelated name
LEAKY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_HOST",
             "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_FOUNDRY_API_KEY",
             "ANTHROPIC_IDENTITY_TOKEN", "CLAUDE_CODE_API_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
             "CLAUDE_CONFIG_DIR", "CLAUDE_ENV_FILE", "MAX_THINKING_TOKENS", "LEAK_CANARY_TOKEN")


def child_env(mode="ok", state=None, extra_env=None):
    env = dict(os.environ)
    env["C_LEAN_CLAUDE_BIN"] = FAKE
    env["C_LEAN_FAKE_MODE"] = mode
    env["C_LEAN_FAKE_STATE"] = state or tempfile.mkdtemp(prefix="c-lean-state-")
    env["C_LEAN_FAKE_ASSERT_ENV"] = "1"
    if extra_env:
        for name, value in extra_env.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
    if PGREP_SANDBOX:
        env["PATH"] = PGREP_SANDBOX + os.pathsep + env.get("PATH", "")
    return env


def default_sighup():
    """preexec_fn: the tool under test starts with SIGHUP at its default action,
    whatever the runner inherited. A suite run under nohup would pass SIG_IGN on,
    and every SIGHUP check would then test nohup instead. SIGINT too: a runner
    started with `&` from a shell without job control inherits it ignored, and
    a tool that has no handler yet then lives through the Ctrl-C a check sends
    it (the red phase lost one case that way, in the background only)."""
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def ignore_sighup():
    """preexec_fn: the tool starts with SIGHUP ignored, as `nohup` leaves it."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def run(cmd, mode="ok", state=None, extra_env=None, stdin="", cwd=None):
    env = child_env(mode, state, extra_env)
    # a hang is a finding, not a reason for the suite to wait for ever
    p = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, env=env,
                       input=stdin, cwd=cwd, timeout=180)
    return p


class Case(unittest.TestCase):
    def setUp(self):
        self.tmpdirs = []
        self.dir = self.mktemp("c-lean-test-")
        self.state = self.mktemp("c-lean-state-")
        # keep the real usage log clean: redirect it into the temp dir
        self.log = os.path.join(self.dir, "usage.jsonl")
        # where `c-lean` keeps the text it did not print: not made here, so a
        # test can tell "no file" from "no directory"
        self.partial = os.path.join(self.dir, "partial")
        self.env = {"C_LEAN_USAGE_LOG": self.log, "C_LEAN_PARTIAL_DIR": self.partial}

    def tearDown(self):
        # Each test removes its own temporary directories.
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def mktemp(self, prefix):
        d = tempfile.mkdtemp(prefix=prefix)
        self.tmpdirs.append(d)
        return d

    def path(self, name):
        return os.path.join(self.dir, name)


    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def saved(self, directory=None):
        """The files of unprinted text that `c-lean` left, in name order: for
        each one (name, first line, the blocks as they came, the whole text)."""
        d = directory or self.partial
        out = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, name), newline="", encoding="utf-8") as f:
                whole = f.read()
            head, _, rest = whole.partition("\n")
            parts = BLOCK_MARK_RE.split(rest)
            self.assertEqual(parts[0], "", "nothing may stand between the first line and the "
                             "first block of %s" % name)
            numbers = [(int(parts[i]), int(parts[i + 1])) for i in range(1, len(parts), 3)]
            self.assertEqual(numbers, [(k, len(numbers)) for k in range(1, len(numbers) + 1)],
                             "every block of %s must be numbered `k of n`" % name)
            for i in range(3, len(parts), 3):
                self.assertTrue(parts[i].endswith("\n"), "one newline must follow every block")
            # the one newline c-lean adds after a block is not part of the block
            out.append((name, head, [parts[i][:-1] for i in range(3, len(parts), 3)], whole))
        return out

    def saved_blocks(self, directory=None):
        """The blocks of every saved file: a list of lists."""
        return [blocks for _n, _h, blocks, _w in self.saved(directory)]

    def warns(self, p):
        return [l for l in p.stderr.splitlines() if l.startswith("c-lean: WARN")]

    def saved_lines(self, p):
        return [l for l in p.stderr.splitlines() if l.startswith("c-lean: SAVED")]

    def calls(self):
        """How many times the fake binary was started."""
        try:
            with open(os.path.join(self.state, "calls.count")) as f:
                return int(f.read().strip() or 0)
        except OSError:
            return 0


    def alive(self, pid):
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False

    def wait_for(self, fn, secs=20, what="condition"):
        t0 = time.time()
        while time.time() - t0 < secs:
            if fn():
                return True
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def sleeper_pids(self):
        p = os.path.join(self.state, "pids.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [tuple(l.split()) for l in f if l.strip()]

    def test_single_success(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "hello"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ECHO:hello", p.stdout)
        self.assertIn("[c-lean] haiku", p.stderr)
        recs = self.jsonl(self.log)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["exit"], 0)
        self.assertEqual(recs[0]["tool"], "c-lean", "the usage log must name the tool")
        self.assertEqual(len(recs[0]["prompt_sha256"]), 64)
        self.assertEqual(recs[0]["prompt_sha256"], hashlib.sha256(b"hello").hexdigest(),
                         "the usage log must store the prompt's sha256 hash")
        self.assertNotIn("hello", json.dumps(recs[0]), "prompt text must not reach the usage log")

    def test_single_runs_in_empty_private_dir(self):
        p = run([CLI, "-m", "haiku", "x"], mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        cwds = [l.strip() for l in open(os.path.join(self.state, "cwd.log"))]
        self.assertTrue(cwds and "c-lean-run-" in cwds[0], cwds)

    def test_single_requires_model(self):
        p = run([CLI, "hello"], mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64)
        self.assertIn("-m MODEL is required", p.stderr)

    def test_api_key_is_stripped(self):
        # the fake exits 90 if it sees a banned var; the wrapper must remove it
        p = run([CLI, "-m", "haiku", "x"], mode="single", state=self.state,
                extra_env=dict(self.env, ANTHROPIC_API_KEY="sk-should-be-removed",
                               ANTHROPIC_BASE_URL="https://not.anthropic.example",
                               CLAUDE_CODE_USE_BEDROCK="1"))
        self.assertNotIn("banned env var", p.stderr, "a key or provider switch reached the child")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_child_env_is_an_allow_list(self):
        """A deny list let 11 of 14 dangerous names through (OAuth token, custom
        headers, config dir, env file, thinking cap). Only allow-listed names may pass.
        (CLAUDE_CODE_MAX_OUTPUT_TOKENS is not here: the child gets the tool's own
        value of it, see test_the_child_gets_the_output_cap_of_the_tool.)"""
        leak = {k: "leak-%s" % k for k in LEAKY_ENV}
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="envdump",
                state=self.state, extra_env=dict(self.env, **leak))
        self.assertEqual(p.returncode, 0, p.stderr)
        env = json.load(open(os.path.join(self.state, "env.json")))
        passed = sorted(k for k in LEAKY_ENV if k in env)
        self.assertEqual(passed, [], "these reached the child: %s" % passed)
        for needed in ("PATH", "HOME"):
            self.assertIn(needed, env, "the child still needs %s to start" % needed)

    def test_schema(self):
        schema = '{"type":"object","properties":{"verdict":{"type":"string"}},"required":["verdict"]}'
        p = run([CLI, "-m", "haiku", "--schema", schema, "q"], mode="schema",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"verdict": "yes"})

    def test_single_limit_wall_exit_75(self):
        p = run([CLI, "-m", "haiku", "x"], mode="limit", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, EXIT_WALL, p.stderr)
        self.assertIn("LIMIT WALL", p.stderr)
        recs = self.jsonl(self.log)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["wall"])

    def test_wall_wording_without_the_structural_marker_is_not_a_wall(self):
        """`is_error` true, the wall sentence in `result`, no api_error_status and
        no error type. The sentence used to decide it: a failed call whose text
        (billing support data) quoted the wall stopped everything at 75
        and the operator went off to wait for a window that was never closed.
        Words in a document are never read; this is an ordinary failure."""
        p = run([CLI, "-m", "haiku", "x"], mode="credits", state=self.state, extra_env=self.env)
        self.assertNotEqual(p.returncode, EXIT_WALL, p.stderr)
        self.assertNotIn("LIMIT WALL", p.stderr)
        self.assertEqual(p.returncode, 1, p.stderr)

    def test_rate_limit_error_type_is_a_wall(self):
        """The other structural marker seen in real transcripts: error type
        rate_limit. A CLI that reports it without an HTTP status is still a wall."""
        p = run([CLI, "-m", "haiku", "x"], mode="ratelimit_type", state=self.state,
                extra_env=self.env)
        self.assertEqual(p.returncode, EXIT_WALL, p.stderr)
        self.assertIn("LIMIT WALL", p.stderr)

    def test_a_healthy_document_with_a_nonzero_exit_code_is_not_a_wall(self):
        p = run([CLI, "-m", "haiku", "x"], mode="wallwords", state=self.state,
                extra_env=dict(self.env, C_LEAN_FAKE_RC="1"))
        self.assertNotIn("LIMIT WALL", p.stderr)
        self.assertNotEqual(p.returncode, EXIT_WALL, p.stderr)
        self.assertEqual(p.returncode, 1, p.stderr)


    def test_reply_cut_off_at_the_output_cap_is_not_a_result(self):
        p = self.call("maxtokens")
        self.assertEqual(p.stdout, "", "a cut-off answer must not be written as a result")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("cut off by the output cap", p.stderr)


    def test_answer_mentioning_rate_limit_is_not_a_wall(self):
        p = run([CLI, "-m", "haiku", "x"], mode="ratewords", state=self.state,
                extra_env=self.env)
        self.assertNotIn("LIMIT WALL", p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)


    def test_single_timeout_zero_refused(self):
        p = run([CLI, "-m", "haiku", "--timeout", "0", "--system", "S", "x"],
                mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertIn("--timeout must be > 0", p.stderr)

    def test_unknown_backend_refused_before_the_call(self):
        p = run([CLI, "-m", "haiku", "--backend", "missing", "x"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertIn("unknown --backend", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.calls(), 0)


    def test_missing_system_file_is_a_usage_error(self):
        p = run([CLI, "-m", "haiku", "--system-file", self.path("nope.md"), "x"],
                mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("--system-file", p.stderr)

    def test_missing_prompt_file_is_a_usage_error(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "-f", self.path("nope.md")],
                mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_help_says_the_timeout_is_built_in(self):
        s = run([CLI, "-h"], state=self.state, extra_env=self.env)
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("the timeout is built in", s.stdout)
        # the default is no longer one number: the help must show the table
        for phrase in ("no -e, low, medium, high   1200 s", "xhigh                      3600 s",
                       "max                        6000 s", "An explicit --timeout\n    always wins"):
            self.assertIn(phrase, s.stdout, "c-lean -h must give the default timeouts: %s" % phrase)
        for stale in ("default 600 s)", " 600 s", "1800 s", "3000 s"):
            self.assertNotIn(stale, s.stdout, "c-lean -h must not promise the old timeout: %s" % stale)

    def test_trailer_prints_think_inside_out(self):
        """`think` is part of `out`; printed side by side the two invited a sum
        that overstated the spend."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stderr, r"out \d+ \(think \d+\)")

    def test_timed_out_call_is_marked_not_counted(self):
        """A failed call was logged with zeroes, so the only place that adds up
        the spend of every c-lean call understated it."""
        p = run([CLI, "-m", "haiku", "--system", "S", "--timeout", "2", "x"],
                mode="hang", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 124, p.stderr)
        recs = self.jsonl(self.log)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["tokens_unknown"], "a timed-out call must not report a clean zero")
        self.assertIn("NOT COUNTED", p.stderr)
        for _pid in open(os.path.join(self.state, "hang.pids")).read().split():
            try:
                os.kill(int(_pid), 9)
            except OSError:
                pass


    def test_usage_iterations_are_summed_not_sampled(self):
        """When the flat totals are absent, the per-message list must be summed:
        taking the first number reported 100 of 300 input tokens as the whole call."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="usage_iters",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self.jsonl(self.log)[0]
        self.assertEqual((rec["in"], rec["out"]), (300, 50))
        self.assertFalse(rec["tokens_unknown"])

    def test_timeout_kills_the_whole_process_tree(self):
        p = run([CLI, "-m", "haiku", "--timeout", "2", "x"], mode="hang",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 124, p.stderr)
        parent, kid = open(os.path.join(self.state, "hang.pids")).read().split()
        alive = []
        for pid in (parent, kid):
            try:
                os.kill(int(pid), 0)
                alive.append(pid)
                os.kill(int(pid), 9)
            except OSError:
                pass
        self.assertEqual(alive, [], "timeout left %s alive (child or grandchild)" % alive)


    def start_call(self, args, mode, sleep="20", new_session=False, nohup=False):
        env = child_env(mode, self.state, dict(self.env, C_LEAN_FAKE_SLEEP=sleep))
        return subprocess.Popen([sys.executable] + args, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=new_session,
                                preexec_fn=ignore_sighup if nohup else default_sighup)


    # The wall is a SHAPE, not a sentence
    #
    # What a spent window really looks like (recorded, not guessed):
    #   {"type":"result","subtype":"success","is_error":true,
    #    "api_error_status":429,"result":"You've hit your weekly limit · resets 3pm"}
    # `subtype` still says success and the process exit code has been seen as both
    # 0 and 1 — `is_error` plus `api_error_status` 429 are the only reliable parts.
    # Therefore: a SUCCESSFUL answer can never be a wall, whatever it says.

    def test_single_answer_that_quotes_the_wall_is_not_a_wall(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="wallwords",
                state=self.state, extra_env=self.env)
        self.assertNotEqual(p.returncode, EXIT_WALL, p.stderr)
        self.assertNotIn("LIMIT WALL", p.stderr)

    def test_wall_is_recognised_by_shape_even_with_unknown_wording(self):
        p = run([CLI, "-m", "haiku", "x"], mode="wall429", state=self.state,
                extra_env=self.env)
        self.assertEqual(p.returncode, EXIT_WALL, p.stderr)

    def test_wall_on_stderr_without_json_still_stops(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="wallstderr",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, EXIT_WALL, p.stderr)


    def unusable_bin(self):
        p = self.path("claude-not-executable")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho hi\n")
        os.chmod(p, 0o644)
        return p


    def test_single_unusable_binary_is_69(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], state=self.state,
                extra_env=dict(self.env, C_LEAN_CLAUDE_BIN=self.unusable_bin()))
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 69, p.stderr)


    def test_check_fails_when_the_counters_cannot_be_read(self):
        """The only guard against the prefix quietly growing back from 0.6K to
        6.6K opened wide in exactly the case it was built for: counters unreadable
        -> total_in 0 -> "0 > 1500" is false -> OK."""
        p = run([CLI, "--check"], mode="usagenone", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("usage counters", p.stderr)

    def test_partial_usage_is_not_a_clean_zero(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="usagepartial",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self.jsonl(self.log)[0]
        self.assertTrue(rec["tokens_unknown"],
                        "half a usage document must not be logged as an exact zero")
        self.assertIn("NOT COUNTED", p.stderr)

    def test_pass_env_cannot_smuggle_a_code_loader(self):
        """C_LEAN_PASS_ENV is for an odd machine, not for NODE_OPTIONS="--require
        injected.js": that runs someone else's file inside the call, next to the
        prompt and subscription credentials."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="envdump", state=self.state,
                extra_env=dict(self.env, C_LEAN_PASS_ENV="NODE_OPTIONS,PYTHONPATH,NPM_CONFIG_SCRIPT",
                               NODE_OPTIONS="--require " + self.path("injected.js"),
                               PYTHONPATH=self.path("injected"),
                               NPM_CONFIG_SCRIPT="x"))
        self.assertEqual(p.returncode, 0, p.stderr)
        env = json.load(open(os.path.join(self.state, "env.json")))
        got = sorted(k for k in ("NODE_OPTIONS", "PYTHONPATH", "NPM_CONFIG_SCRIPT") if k in env)
        self.assertEqual(got, [], "these reached the child: %s" % got)

    def test_proxy_and_ca_do_not_travel_by_default(self):
        """A proxy plus a foreign CA is a complete transport takeover, and it rode
        in from the parent session without a word. It is still available on
        purpose — but only by naming it in C_LEAN_PASS_ENV."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="envdump", state=self.state,
                extra_env=dict(self.env, HTTPS_PROXY="http://evil:8080",
                               NODE_EXTRA_CA_CERTS=self.path("rogue.pem"), no_proxy="x"))
        self.assertEqual(p.returncode, 0, p.stderr)
        env = json.load(open(os.path.join(self.state, "env.json")))
        got = sorted(k for k in ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "no_proxy") if k in env)
        self.assertEqual(got, [], "these reached the child: %s" % got)

    def test_trailer_names_the_binary_it_used(self):
        """The binary comes from C_LEAN_CLAUDE_BIN or the parent's PATH, so a swap
        must be visible in the one line the operator reads."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(FAKE, p.stderr)


    def test_timeout_nan_and_inf_are_refused(self):
        """`nan` and `inf` walked past `<= 0` and died inside select() with
        "cannot convert float NaN to integer" — a traceback where a usage error
        belonged. (`--timeout=-inf` needs the = form: an unadorned `-inf` is an option
        as far as argparse is concerned, and that is argparse's own exit 2.)"""
        for bad in ("--timeout=nan", "--timeout=inf", "--timeout=-inf"):
            s = run([CLI, "-m", "haiku", "--system", "S", bad, "x"],
                    mode="single", state=self.state, extra_env=self.env)
            self.assertEqual(s.returncode, 64, "%s: %s" % (bad, s.stderr))
            self.assertNotIn("Traceback", s.stderr)
        self.assertEqual(self.calls(), 0, "no call may be spent on an impossible timeout")

    def test_relative_usage_log_is_still_written(self):
        """A relative C_LEAN_USAGE_LOG silently switched the token accounting off:
        makedirs("") raised, the warning scrolled by, the call went through."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="single",
                state=self.state, extra_env={"C_LEAN_USAGE_LOG": "rel-usage.jsonl"},
                cwd=self.dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("could not write usage log", p.stderr)
        self.assertEqual(len(self.jsonl(self.path("rel-usage.jsonl"))), 1,
                         "a relative usage-log override must resolve against cwd")

    def test_truncation_reported_in_terminal_reason_is_caught(self):
        p = self.call("terminal")
        self.assertEqual(p.stdout, "", "a cut-off answer must not be written as a result")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("cut off by the output cap", p.stderr)

    def test_truncation_reported_in_a_nested_message_is_caught(self):
        p = self.call("msgstop")
        self.assertEqual(p.stdout, "", "a cut-off answer must not be written as a result")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("cut off by the output cap", p.stderr)

    def test_single_empty_answer_is_a_failure(self):
        """`c-lean … > answer.md` with an empty result wrote an empty file and exited
        0, so the next step of the pipeline took the step as done."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="emptysingle",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("empty answer", p.stderr)

    def test_single_allow_empty_accepts_it(self):
        p = run([CLI, "-m", "haiku", "--system", "S", "--allow-empty", "x"],
                mode="emptysingle", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_json_still_prints_the_envelope_when_the_answer_is_empty(self):
        """A hole opened by the empty-answer rule itself: --json asks for the CLI
        document, and the new `return` fired before it was printed. The caller got
        exit 1 and an empty stdout — which is exactly the state it needs the
        envelope to explain (was it a cut-off, a refusal, a wall?). The failure
        signal is right; swallowing the thing that was asked for is not."""
        p = run([CLI, "-m", "haiku", "--system", "S", "--json", "x"],
                mode="emptysingle", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("empty answer", p.stderr)
        doc = json.loads(p.stdout or "null")
        self.assertIsInstance(doc, dict, "--json must still print the envelope: %r" % p.stdout)
        self.assertIn("is_error", doc)


    def test_check_fails_on_an_empty_answer(self):
        """--check exists to prove the pipe works before a paid call. An empty
        answer is the pipe not working, and it printed OK."""
        p = run([CLI, "--check"], mode="emptysingle", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("empty answer", p.stderr)

    def test_check_passes_without_cache_counters(self):
        """A CLI that reports input and output tokens but no cache counters is
        readable: the prefix can be verified. --check went red on it."""
        p = run([CLI, "--check"], mode="usageinout", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("NOT COUNTED", p.stderr)


    def test_bad_flag_values_exit_64_not_2(self):
        """argparse errors use the documented usage exit code."""
        p = run([CLI, "-m", "haiku", "--timeout", "-inf", "x"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertEqual(self.calls(), 0)


    # Without a document only the CLI's own words decide the limit wall.

    def test_the_cli_own_words_after_the_stream_began_still_decide(self):
        """The other side of the same line: a sentence the CLI printed on
        stdout as plain text is the CLI's, and it decides. The stream begins
        with `system/init`, so "stdout before the first `{`" was nothing at all
        for a live CLI: the rule is every line that does not begin with `{`."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="wallstdout",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, EXIT_WALL, "the CLI's own words on stdout must still "
                         "decide: rc %d %s" % (p.returncode, p.stderr))
        # this one checks the FAKE, not `c-lean`: it only guards the fake against drift.
        # The behaviour is what the exit code above says, and the two tests of the
        # wall after a long noise (test_wall_phrase_after_long_*)
        self.assertTrue(self.fake("wallstdout", "stream-json").startswith('{"type": "system"'),
                        "the fake must print `system/init` first, as the live CLI does")

    def test_a_line_that_begins_with_a_brace_never_decides_the_wall(self):
        """An event that broke off in the middle of the model's text, indented
        at that, and a whole JSON object of a type nobody knows: both hold the
        wall sentence, neither is the CLI speaking for itself."""
        p = run([CLI, "-m", "haiku", "--system", "S", "x"], mode="cutwalljson",
                state=self.state, extra_env=self.env)
        self.assertNotIn("LIMIT WALL", p.stderr, "a line of stdout that begins with `{` "
                         "decided the wall")
        self.assertEqual(p.returncode, 1, p.stderr)


    def test_help_lists_the_known_limitations(self):
        s = run([CLI, "-h"], state=self.state, extra_env=self.env)
        self.assertIn("Known limitations", s.stdout)
        self.assertIn("never been observed live", s.stdout)
        # `c-lean` has a start window too: its codes must match its help as well
        for phrase in ("Until the signal handlers are installed", "no call has been made",
                       "KeyboardInterrupt traceback", "Fatal Python error: init_"):
            self.assertIn(phrase, s.stdout, "c-lean -h must describe the start window: %s" % phrase)

    # The signal windows. The signal lands at an exact point of the tool's life,
    # not after a guessed sleep: a sitecustomize.py on PYTHONPATH holds the tool
    # there, writes held.<pid> and waits until the test has sent the signal.
    # It loads into the TOOL only: PYTHONPATH is a code loader and never reaches
    # the fake child, and held.<pid> is checked against the tool's own pid.
    # The hold runs while CPython destroys module globals, after resetting its
    # Python signal handlers. SIG_IGN must survive that shutdown window.
    SIGPROBE = r'''
import os, sys, time
_dir = os.environ.get("SIGPROBE_DIR")
if _dir:
    # everything the hold needs is bound as a default: at shutdown the module
    # globals may already be gone
    def _hold(_dir=_dir, _pid=os.getpid, _join=os.path.join, _exists=os.path.exists,
              _open=open, _sleep=time.sleep, _now=time.monotonic):
        with _open(_join(_dir, "held.%d" % _pid()), "w") as f:
            f.write("held")
        t0 = _now()
        while not _exists(_join(_dir, "sent")) and _now() - t0 < 60:
            _sleep(0.01)
        _sleep(0.3)  # the signal has been sent: let it land here

    class _AtShutdown:
        def __del__(self, _hold=_hold):
            _hold()
    _probe = _AtShutdown()
'''

    def signal_at_shutdown(self, cmd, sig, mode="single"):
        """Hold cmd at interpreter shutdown, send `sig`, then let it end.
        Returns (rc, stdout, stderr)."""
        site = self.mktemp("c-lean-probe-")
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write(self.SIGPROBE)
        env = child_env(mode, self.state, dict(self.env, PYTHONPATH=site, SIGPROBE_DIR=site))
        proc = subprocess.Popen([sys.executable] + cmd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                preexec_fn=default_sighup)
        held = os.path.join(site, "held.%d" % proc.pid)
        try:
            self.wait_for(lambda: os.path.exists(held) or proc.poll() is not None, secs=60,
                          what="the probe to hold the tool at shutdown")
            if not os.path.exists(held):
                out, err = proc.communicate(timeout=30)
                self.fail("the probe never held the tool at shutdown (is sitecustomize loaded?): rc %s %s"
                          % (proc.returncode, err[-300:]))
            os.kill(proc.pid, sig)
            open(os.path.join(site, "sent"), "w").close()
            out, err = proc.communicate(timeout=90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        return proc.returncode, out, err


    def test_a_late_signal_does_not_replace_a_refusal(self):
        for sig in STOP_SIGNALS:
            rc, _o, err = self.signal_at_shutdown([CLI, "-m", "haiku", "--timeout", "0", "x"],
                                         sig)
            self.assertIn("--timeout must be > 0", err)
            self.assertEqual(rc, 64, "the run refused with 64, yet signal %d at shutdown made the "
                                     "refusal %s" % (sig, rc))


    def test_single_late_signal_does_not_replace_exit_0(self):
        """`c-lean` printed the answer and returned 0; a signal during the
        interpreter shutdown made the OS report -2 / -15 instead."""
        for sig in STOP_SIGNALS:
            rc, out, err = self.signal_at_shutdown([CLI, "-m", "haiku", "--system", "S", "hello"],
                                          sig, mode="single")
            self.assertIn("ECHO:hello", out, err[-300:])
            self.assertEqual(rc, 0, "the answer was printed and the exit decided, yet signal %d at "
                                    "shutdown made it %s" % (sig, rc))

    # SIGHUP — a closed terminal, `kill -HUP` — had no handler. The tool died on
    # the spot: no trailer, no exit code of its own, and the children, alive
    # without their parent, finished calls whose answers nobody read. It is a
    # stop signal like SIGTERM now, except under nohup, where it stays ignored.
    def kill_leftovers(self):
        """Kill every recorded child still alive; return the ones that were."""
        still = [p for pair in self.sleeper_pids() for p in pair if self.alive(p)]
        for p in still:
            os.kill(int(p), signal.SIGKILL)
        return still


    def test_single_sighup_to_the_process_group_stops_the_call(self):
        """SIGHUP to the whole job stops the call with exit 128+1, whether
        the CLI exits first or the wrapper's SIGKILL reaches it first."""
        proc = self.start_call([CLI, "-m", "haiku", "--system", "S", "hello"],
                                mode="sleeper", new_session=True)
        try:
            self.wait_for(lambda: len(self.sleeper_pids()) >= 1, what="the live child")
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            _o, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            still = self.kill_leftovers()
        self.assertEqual(proc.returncode, 129, "c-lean must stop the call on SIGHUP and exit 128+1, "
                                               "not die of it: %s" % err[-300:])
        self.assertIn("INTERRUPTED", err)
        self.assertEqual(still, [], "children left alive after SIGHUP: %s" % still)


    def test_single_nohup_call_outlives_a_hangup_of_the_job(self):
        """`c-lean` under nohup, the terminal hangs up the whole job mid-call: the
        answer must still come. With the `cli` fake as the child (see above)
        the call used to end with exit 1, `no JSON from claude (rc=129)`."""
        proc = self.start_call([CLI, "-m", "haiku", "--system", "S", "hello"],
                                mode="cli", sleep="1.5", new_session=True, nohup=True)
        try:
            self.wait_for(lambda: len(self.sleeper_pids()) >= 1, what="the call in flight")
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            self.kill_leftovers()
        self.assertEqual(proc.returncode, 0, "under nohup a hangup of the whole job must not end "
                                             "the call, yet c-lean exited %s: %s"
                                             % (proc.returncode, err[-300:]))
        self.assertIn("ECHO:hello", out, err[-300:])

    def test_help_lists_sighup_with_the_other_stop_signals(self):
        s = run([CLI, "-h"], state=self.state, extra_env=self.env).stdout
        self.assertIn("the child runs in a session\n    of its own", s,
                      "c-lean -h must say why nohup alone does not save the call")
        for phrase in ("SIGINT/SIGTERM/SIGHUP kill the child",
                       "130/143/129 stopped by SIGINT/SIGTERM/SIGHUP", "or SIGHUP in this window"):
            self.assertIn(phrase, s, "c-lean -h must list SIGHUP: %s" % phrase)
        self.assertEqual(s.count("nohup"), 1, "c-lean -h must mention nohup once")

    # The big call. The CLI's built-in system prompt never applies; an answer
    # the CLI continued after the output cap is printed whole; the default
    # timeout depends on the effort. The fake speaks the event stream of the
    # live CLI (`--output-format stream-json --verbose`), so every test above
    # goes through the stream reader too.
    SCHEMA = '{"type":"object","properties":{"verdict":{"type":"string"}},"required":["verdict"]}'

    def argv(self, n=-1):
        """The argv the fake binary was started with, call number n."""
        return self.jsonl(os.path.join(self.state, "argv.jsonl"))[n]

    def flag(self, name, n=-1):
        """The value that follows `name` in that argv, or None when it is absent."""
        argv = self.argv(n)
        return argv[argv.index(name) + 1] if name in argv else None

    def received(self, n=-1):
        """What the fake binary was given in call number n: its argv, where
        the prompt came from, the system prompt and its file (fake_claude.note)."""
        got = self.jsonl(os.path.join(self.state, "received.jsonl"))
        self.assertTrue(got, "the fake binary recorded no call")
        return got[n]

    def call(self, mode, *args):
        return run([CLI, "-m", "haiku", "--system", "S"] + list(args) + ["hello"], mode=mode,
                   state=self.state, extra_env=self.env)

    def fake(self, mode, fmt):
        """What the fake binary prints in this mode, started directly. Its guard
        against a leaked environment is off: nothing scrubs the environment on
        this path, and the suite itself may run inside a session that has one."""
        env = child_env(mode, self.state, self.env)
        del env["C_LEAN_FAKE_ASSERT_ENV"]
        p = subprocess.run([sys.executable, FAKE, "-p", "--output-format", fmt, "--verbose",
                            "hello"], capture_output=True, text=True, timeout=60, env=env)
        return p.stdout

    def stream_of(self, mode):
        """The stream the fake prints in this mode, read by the reader under test."""
        return B.read_stream(self.fake(mode, "stream-json"))

    def timeout_given_to_the_call(self, *args):
        """The timeout `c-lean` hands to the model call for these arguments."""
        p = subprocess.run([sys.executable, "-c", TIMEOUT_PROBE, CLI] + list(args),
                           capture_output=True, text=True, timeout=60,
                           env=child_env("single", self.state, self.env))
        lines = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(lines, "the probe did not run: rc %s %s" % (p.returncode, p.stderr[-300:]))
        return json.loads(lines[-1][len("PROBE "):])["timeout"]

    def test_system_prompt_is_always_passed(self):
        """Without --system-prompt the CLI puts its own built-in prompt in: 6,598
        input tokens a call against 406, and the Claude Code persona with it.
        `c-lean` used to leave the flag out and print a WARN that nobody read."""
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("--system-prompt-file", self.argv(),
                         "with no system prompt there is no file to pass, only --system-prompt \"\"")
        self.assertIn("--system-prompt", self.argv(),
                      "--system-prompt must be passed even when no system prompt is given")
        self.assertEqual(self.flag("--system-prompt"), "",
                         "with no --system the system prompt must be passed empty")
        p = self.call("single")
        self.assertEqual(p.returncode, 0, p.stderr)
        got = self.received()
        self.assertEqual((got["system_flags"], got["system_sha256"]),
                         (["--system-prompt-file"], hashlib.sha256(b"S").hexdigest()),
                         "a given system prompt travels as it is, in a file")

    def test_whitespace_only_system_prompt_is_passed_empty(self):
        """A system prompt of one space is refused by the API (HTTP 400: text
        content blocks must contain non-whitespace text); "" is taken."""
        p = run([CLI, "-m", "haiku", "--system", "  \n\t ", "hello"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.flag("--system-prompt"), "",
                         "a system prompt of whitespace alone must be passed as the empty string")

    def test_no_warn_about_a_missing_system_prompt(self):
        """The WARN said the CLI's built-in prompt applies. It no longer does."""
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("WARN", p.stderr, "c-lean must not warn about a missing system prompt")
        self.assertNotIn("built-in", p.stderr)


    def test_the_cli_is_asked_for_its_event_stream(self):
        """The single document of `--output-format json` holds only the last
        text block of an answer; the events hold them all."""
        p = self.call("single")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.flag("--output-format"), "stream-json",
                         "the CLI must be asked for stream-json")
        self.assertIn("--verbose", self.argv(), "stream-json comes with --verbose")

    def test_an_answer_continued_after_the_output_cap_is_printed_whole(self):
        """opus at effort max: thinking ate the 128K output cap, the CLI asked
        the model to go on, and `result` carried the second message only. The
        answer file began in the middle of a word, exit 0."""
        p = self.call("cont3")
        whole = "".join(CONT_PIECES)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, whole + "\n", "every message of a continued answer must be "
                         "printed, joined with nothing in between")
        self.assertNotIn("THINKING", p.stdout, "thinking is never the answer")
        trailer = [l for l in p.stderr.splitlines() if l.startswith("[c-lean]")][0]
        self.assertIn(" · CONTINUED x2", trailer, "the trailer must say how many times the "
                      "CLI continued the answer")
        self.assertIn("c-lean: NOTE the CLI continued the answer 2 times (after the output cap x2)",
                      p.stderr, "a joined answer must be announced on stderr")
        self.assertIn("a space or a newline may be missing", p.stderr)
        for n, at in ((1, len(CONT_PIECES[0])), (2, len(CONT_PIECES[0]) + len(CONT_PIECES[1]))):
            seam = "c-lean: NOTE seam %d after %d characters: %s | %s" % (
                n, at, json.dumps(whole[at - 40:at]), json.dumps(whole[at:at + 40]))
            self.assertIn(seam, p.stderr.splitlines(), "the NOTE must show seam %d with its "
                          "position and 40 characters on each side, newlines as \\n" % n)
        self.assertEqual(self.jsonl(self.log)[0].get("continued"), 2,
                         "the usage log must say how many times the call was continued")
        self.assertEqual(self.saved(), [], "an answer that was printed whole leaves no file")

    def test_each_request_of_the_cli_to_go_on_joins_the_answer(self):
        """Three requests of the CLI mean "go on with the same answer": after
        the output cap, after a response cut off mid-stream, after a response
        with no visible text. The fake sends the CLI's own constants, whole."""
        for mode, want, n, after in (
                ("cont3", "".join(CONT_PIECES), 2, "the output cap x2"),
                ("contcut", "CUT one CUT two", 1, "a response cut off mid-stream"),
                ("contnovis", "NOVIS one NOVIS three", 2,
                 "the output cap, a response with no visible text")):
            p = self.call(mode)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, want + "\n", "the text blocks must be joined when the CLI "
                             "went on after %s" % after)
            self.assertIn(" · CONTINUED x%d" % n, p.stderr)
            self.assertIn("continued the answer %d time%s (after %s)"
                          % (n, "" if n == 1 else "s", after), p.stderr,
                          "the NOTE must say what the CLI went on after")
            self.assertEqual(self.warns(p), [])
            self.assertEqual(self.saved(), [], "a joined answer leaves no file")

    def test_a_request_is_told_by_the_beginning_of_its_text(self):
        """The words are constants of the CLI, and the kind of a request is
        its beginning: the same words further on are somebody quoting them."""
        for start in RESUME_STARTS:
            self.assertIsNotNone(B.resume_kind(start + " and whatever a later CLI adds"),
                                 "a request that begins %r must be known as a continuation" % start)
            self.assertIsNone(B.resume_kind("The hook says: " + start),
                              "a request is told by the BEGINNING of its text")
        self.assertIsNone(B.resume_kind(SAFETY_START_60))
        self.assertIsNone(B.resume_kind(""))
        # two requests before one message, a known one among them
        s = B.read_stream(stream_text(
            ev_text("m1", "A"), ev_ask([{"type": "text", "text": RESUME_STARTS[0] + " — go on"}]),
            ev_ask([{"type": "text", "text": "Stop hook feedback: no"}]), ev_text("m2", "B"),
            ev_result("B")))
        self.assertEqual(s.links, [None, (B.UNKNOWN, "Stop hook feedback: no")],
                         "one request that is not known is enough, whatever stands next to it")
        self.assertEqual(s.continued, 0)
        # inside the CLI the content of a request is a plain string, in the
        # recorded stream a list of blocks: both are read
        s = B.read_stream(stream_text(ev_text("m1", "A"), ev_ask(RESUME_STARTS[0] + " — go on"),
                                      ev_text("m2", "B"), ev_result("B")))
        self.assertEqual(s.links, [None, (B.JOIN, "the output cap")],
                         "a request whose content is a plain string must be read too")

    def test_a_request_the_tool_does_not_know_is_not_joined(self):
        """A response stopped by a safety classifier: the CLI asks the model
        to go on without the withheld part, and whether the model continues
        its text or starts again is not known. Any request that is not one of
        the three known ones takes the same branch: nothing is joined, the
        `result` of the document is printed, every block is saved."""
        self.assertEqual(len(SAFETY_START_60), 60)
        p = self.call("safety")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "SAFE two\n", "text blocks around a request that is not known "
                         "as a continuation must not be joined")
        self.assertNotIn("CONTINUED", p.stderr, "a request that is not known is not counted as a "
                         "continuation")
        warns = self.warns(p)
        self.assertEqual(len(warns), 1, "blocks that were not joined must be warned about: %r"
                         % p.stderr)
        self.assertIn("(its request begins %s)" % json.dumps(SAFETY_START_60, ensure_ascii=False),
                      warns[0], "the WARN must quote the first 60 characters of the request")
        self.assertIn("only the `result` of the CLI's document is printed", warns[0])
        self.assertEqual(self.saved_blocks(), [["SAFE one ", "SAFE two"]],
                         "every text block of an answer that was not joined must be saved")
        name = self.saved()[0][0]
        self.assertEqual(self.saved_lines(p), [
            "c-lean: SAVED 17 characters in 2 text blocks to %s — the text of an answer that did "
            "not finish or was not printed, NOT a result" % os.path.join(self.partial, name)],
            "one line on stderr must give the size and the full path of the file")

    def test_thinking_that_was_resumed_is_a_continuation_with_no_request(self):
        """The cut message was one thinking block: the CLI asks for it again
        with no `user` event, and marks every event of the new request."""
        p = self.call("thinkresume")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "FULL ANSWER, after thinking that was resumed\n")
        self.assertIn(" · CONTINUED x1", p.stderr, "a message marked "
                      "resumed_from_incomplete_thinking must be counted as a continuation")
        for word in ("NOTE", "WARN", "SAVED", "THINKING"):
            self.assertNotIn(word, p.stderr + p.stdout)
        self.assertEqual(self.saved(), [])
        # whether the resumed message gets a new id is the server's business:
        # under the old id it must still be a message of its own
        mark = {"resumed_from_incomplete_thinking": True}
        s = B.read_stream(stream_text(ev_think("msg_1"), ev_think("msg_1", **mark),
                                      ev_text("msg_1", "A", **mark), ev_result("A")))
        self.assertEqual(s.links, [None, (B.JOIN, B.RESUMED_THINKING)],
                         "a resumed request is a message of its own even under the id of the "
                         "message it resumes")
        self.assertEqual((s.continued, s.texts()), (1, ["A"]))

    def test_one_message_of_two_text_blocks_is_printed_whole(self):
        """`result` is the LAST CONTENT BLOCK of the last event, not the last
        message: text, thinking, text in one message gets only the second
        text into it. Nothing was continued, and the trailer must not say so."""
        p = self.call("twoblocks")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "".join(TWO_BLOCKS) + "\n", "both text blocks of one message "
                         "must be printed, joined: `result` carries only the second")
        self.assertNotIn("CONTINUED", p.stderr, "one message of two text blocks must not be "
                         "marked CONTINUED")
        self.assertIn("c-lean: NOTE the answer came in 2 text blocks of one message, and the result "
                      "document of the CLI carries only the last of them", p.stderr,
                      "the NOTE must say that the answer came in several blocks")
        self.assertIn('c-lean: NOTE seam 1 after 26 characters: "BLOCK one, then thinking, " | '
                      '"then BLOCK two"', p.stderr.splitlines())
        self.assertNotIn("THINKING", p.stdout + p.stderr)
        self.assertEqual((self.warns(p), self.saved()), ([], []))
        doc = json.loads(self.call("twoblocks", "--json").stdout)
        self.assertEqual(doc.get("c_lean_pieces"), TWO_BLOCKS)
        self.assertEqual(doc["result"], TWO_BLOCKS[1])

    def test_a_message_the_cli_took_back_is_not_joined(self):
        """`supersedes` in an `assistant` event, or `retracted_message_uuids`
        in a `system` event: earlier messages were replaced, and which text
        still stands cannot be told. Everything else speaks for a join here:
        the request to go on is there, and the last block is the `result`."""
        for mode, key in (("supersedes", "`supersedes`"),
                          ("retracted", "`retracted_message_uuids`")):
            shutil.rmtree(self.partial, ignore_errors=True)
            p = self.call(mode)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "KEPT two\n", "nothing may be joined once the stream "
                             "says %s" % key)
            warns = self.warns(p)
            self.assertEqual(len(warns), 1, p.stderr)
            self.assertIn("the CLI took back a message of this call", warns[0])
            self.assertEqual(self.saved_blocks(), [["TAKEN-BACK one ", "KEPT two"]])
            self.assertEqual(len(self.saved_lines(p)), 1)

    def test_json_carries_the_pieces_of_a_continued_answer(self):
        p = self.call("cont3", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc.get("c_lean_pieces"), CONT_PIECES,
                         "--json must carry the text of every message under c_lean_pieces")
        # nothing else differs: the rest is the document the fake prints as
        # `--output-format json`, where the call is that one document
        plain = json.loads(self.fake("cont3", "json"))
        self.assertEqual(plain["result"], CONT_PIECES[-1])
        for d in (doc, plain):
            d.pop("uuid", None)
        self.assertEqual({k: v for k, v in doc.items() if k != "c_lean_pieces"}, plain,
                         "apart from c_lean_pieces the document must be the CLI's own")
        one = json.loads(self.call("single", "--json").stdout)
        self.assertNotIn("c_lean_pieces", one, "an ordinary call has no pieces to carry")

    def test_json_of_blocks_that_were_not_joined_is_the_document_alone(self):
        """--json with a WARN: the document goes out as the CLI gave it, with
        no c_lean_pieces (the key is there only when the blocks were joined),
        one WARN on stderr, and the blocks in a file."""
        p = self.call("twomsg", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertNotIn("c_lean_pieces", doc, "c_lean_pieces must be in --json only when the blocks "
                         "were joined")
        plain = json.loads(self.fake("twomsg", "json"))
        for d in (doc, plain):
            d.pop("uuid", None)
        self.assertEqual(doc, plain, "the document must be the CLI's own")
        self.assertEqual(len(self.warns(p)), 1, "--json of blocks that were not joined must come "
                         "with exactly one WARN: %r" % p.stderr)
        self.assertEqual(self.saved_blocks(), [["DRAFT one", "FINAL two"]])

    def test_a_first_message_that_is_all_thinking_is_an_ordinary_answer(self):
        """The cap fell inside the thinking: the first message has no text at
        all, the second has all of it. One text block is the `result` of the
        document: nothing is put together, and there is nothing to announce."""
        p = self.call("contthink")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "FULL ANSWER, after a message that was all thinking\n")
        self.assertIn(" · CONTINUED x1", p.stderr)
        self.assertNotIn("c-lean: NOTE", p.stderr, "a call with one text block has nothing to "
                         "announce: nothing was joined")
        self.assertEqual((self.warns(p), self.saved()), ([], []))

    def test_splice_counts_a_seam_once_and_only_inside_the_answer(self):
        self.assertEqual(B.splice(["ab", "", "cd"]), ("abcd", [2]),
                         "an empty piece in the middle must not make a second seam")
        self.assertEqual(B.splice(["ab", "cd\n"]), ("abcd", [2]))
        self.assertEqual(B.splice(["ab\n", " cd"]), ("ab\n cd", [3]),
                         "whitespace at a seam is kept as it came")

    def test_splice_leaves_no_seam_at_whitespace_at_either_end(self):
        self.assertEqual(B.splice(["ab", "\n"]), ("ab", []),
                         "a boundary outside the printed answer must not be a seam")
        self.assertEqual(B.splice(["\n", "ab"]), ("ab", []),
                         "a boundary outside the printed answer must not be a seam")

    def test_a_whitespace_last_block_with_a_blank_result_is_not_joined(self):
        """The text came in the first message and the continuation added one
        newline: the CLI's `result` is blank. A blank `result` means something
        came after the last text, and a whitespace last block would "agree"
        with it for nothing. Nothing is joined: a WARN says why, the empty
        answer is a failure (a success with --allow-empty), and both blocks
        are saved."""
        p = self.call("conttail")
        self.assertEqual((p.returncode, p.stdout), (1, ""),
                         "a blank `result` must not be joined: %s" % p.stderr[-300:])
        self.assertIn("c-lean: empty answer", p.stderr)
        self.assertEqual(len(self.warns(p)), 1, p.stderr)
        self.assertIn("is empty although text blocks came", self.warns(p)[0])
        self.assertNotIn("NOTE", p.stderr)
        self.assertEqual(self.saved_blocks(), [["PART-ONE and a newline after it", "\n"]])
        shutil.rmtree(self.partial)
        p = self.call("conttail", "--allow-empty")
        self.assertEqual((p.returncode, p.stdout), (0, "\n"), p.stderr[-300:])
        self.assertEqual(self.saved_blocks(), [["PART-ONE and a newline after it", "\n"]])

    def test_a_continuation_with_no_text_is_an_empty_answer_and_its_text_is_saved(self):
        """The first message carries the text and the continuation is all
        thinking: the last block of the answer is not text, so the CLI leaves
        `result` empty. One text block is not put together from anything: the
        empty `result` is what the call gave, a failure, and the text goes to
        a file. With --allow-empty the call is good, and the block that was
        not printed is still not lost."""
        p = self.call("contempty")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("c-lean: empty answer", p.stderr)
        self.assertEqual(self.saved_blocks(), [["PART-ONE and nothing after it"]],
                         "the text of a call that ended in an empty answer must be saved")
        shutil.rmtree(self.partial)
        p = self.call("contempty", "--allow-empty")
        self.assertEqual((p.returncode, p.stdout), (0, "\n"), p.stderr)
        self.assertEqual(self.saved_blocks(), [["PART-ONE and nothing after it"]],
                         "a text block that is not the printed `result` must be saved even when "
                         "the call is good")
        self.assertEqual(len(self.saved_lines(p)), 1)

    def test_continuations_that_run_out_are_a_failure_with_no_answer(self):
        """Four messages, every one cut at the cap. The CLI gives up with a
        message of its own, "API Error: …", is_error true, subtype success.
        Nothing goes to stdout, and the four cut texts go to a file as they
        came: the CLI's own line is not text of the model, and thinking is
        never kept."""
        p = self.call("contfail", "-e", "max")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stdout, "", "a call whose continuations ran out has no answer")
        self.assertIn("the answer hit the output cap and the CLI ran out of continuations",
                      p.stderr, "the failure must say that the answer hit the output cap and "
                      "that the CLI ran out of continuations")
        self.assertIn(" · CONTINUED x3", p.stderr)
        self.assertNotIn("LIMIT WALL", p.stderr)
        saved = self.saved()
        self.assertEqual(len(saved), 1, "the text of a call whose continuations ran out must be "
                         "saved to one file: %r" % p.stderr)
        name, head, blocks, whole = saved[0]
        self.assertNotIn("API Error", whole, "the CLI's own error line is not text of the model: "
                         "it must not be in the file, the first line included")
        self.assertEqual(blocks, CUT_PIECES, "the file must hold the four text blocks as they "
                         "came, whitespace at their ends included")
        self.assertEqual(self.saved_lines(p), [
            "c-lean: SAVED 16 characters in 4 text blocks to %s — the text of an answer that did "
            "not finish or was not printed, NOT a result" % os.path.join(self.partial, name)])
        sha = self.jsonl(self.log)[0]["prompt_sha256"]
        self.assertRegex(name, r"\A\d{8}T\d{6}Z-%s\.txt\Z" % sha[:8])
        self.assertRegex(head, r"\A# c-lean \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ · model haiku · effort "
                         r"max · NOT a result: the answer hit the output cap and the CLI ran out "
                         r"of continuations · 4 text blocks, 16 characters, one newline added "
                         r"after each block · prompt sha256 %s\Z" % sha,
                         "the first line must name the tool, the time, the model and effort, the "
                         "reason and the prompt's sha256")
        self.assertNotIn("hello", whole, "the prompt itself must never be in the file")

    def test_the_cap_named_only_under_error_is_still_continuations_that_ran_out(self):
        """The marker has two names in the CLI's code, `api_error` and
        `error`, and a document is not promised to carry both."""
        p = self.call("contfailerr")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("the answer hit the output cap and the CLI ran out of continuations",
                      p.stderr, "a document that names max_output_tokens only under `error` must "
                      "be read as continuations that ran out")
        self.assertEqual(self.saved_blocks(), [CUT_PIECES])

    def test_thinking_is_never_saved(self):
        """A failed call whose message holds a thinking block with text next
        to its text block: the text is kept, the thinking is not."""
        raw = self.fake("cutstream", "stream-json")
        self.assertIn("THINKING-NOT-THE-ANSWER-1", raw, "the fake must send thinking with text")
        p = self.call("cutstream")
        self.assertEqual(p.returncode, 1, p.stderr)
        saved = self.saved()
        self.assertEqual(len(saved), 1, p.stderr)
        self.assertNotIn("THINKING", saved[0][3], "thinking must never be saved")
        self.assertEqual(saved[0][2], ["the start of an answer that never en"])
        self.assertNotIn("THINKING", p.stdout + p.stderr)

    def test_a_failure_with_no_text_block_saves_nothing(self):
        p = self.call("fail")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertFalse(os.path.exists(self.partial), "a failed call with no text block of the "
                         "model must leave no file: %r" % self.saved())
        self.assertEqual(self.saved_lines(p) + self.warns(p), [],
                         "nothing was saved, so nothing may be said about saving")

    def test_a_file_that_cannot_be_written_costs_a_warning_and_nothing_else(self):
        """The directory is under a regular file. The call's own report, its
        stdout and its exit code stay exactly what they were."""
        blocker = self.path("a-file")
        open(blocker, "w").close()
        env = dict(self.env, C_LEAN_PARTIAL_DIR=os.path.join(blocker, "partial"))
        for mode, rc, out, said in (("twomsg", 0, "FINAL two\n", "no continuation between them"),
                                    ("contfail", 1, "", "ran out of continuations")):
            p = run([CLI, "-m", "haiku", "--system", "S", "hello"], mode=mode, state=self.state,
                    extra_env=env)
            self.assertEqual((p.returncode, p.stdout), (rc, out), "a file that cannot be written "
                             "must not change the exit code or stdout (%s): %s"
                             % (mode, p.stderr[-300:]))
            self.assertIn(said, p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            lost = [l for l in self.warns(p) if "could not save" in l]
            self.assertEqual(len(lost), 1, "a file that cannot be written must cost exactly one "
                             "WARN: %r" % p.stderr)
            self.assertIn("text block", lost[0])
            self.assertEqual(self.saved_lines(p), [])

    def test_saved_text_is_private_and_a_file_is_never_written_over(self):
        d = os.path.join(self.dir, "not-there-yet", "partial")
        now = datetime(2026, 10, 3, 10, 15, 0, tzinfo=timezone.utc)
        old = os.umask(0)   # the modes must not lean on the umask of the caller
        try:
            with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": d}):
                one = B.save_blocks(["first\n", " second "], "the prompt", "a reason\nof two lines",
                                    model="opus", effort="max", now=now)
                two = B.save_blocks(["other"], "the prompt", "reason", now=now)
        finally:
            os.umask(old)
        sha = B.sha256("the prompt")
        self.assertEqual(one, os.path.join(d, "20261003T101500Z-%s.txt" % sha[:8]),
                         "the file must be named <UTC time>-<8 characters of the prompt's "
                         "sha256>.txt")
        self.assertEqual(two, os.path.join(d, "20261003T101500Z-%s-2.txt" % sha[:8]),
                         "a name that is taken must get a counter: a file is never written over")
        self.assertEqual(self.saved_blocks(d), [["other"], ["first\n", " second "]])
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700,
                         "the directory must be made with mode 0700")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(d)).st_mode), 0o700)
        for path in (one, two):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600,
                             "the file must be made with mode 0600")
        head = self.saved(d)[1][1]
        self.assertEqual(head, "# c-lean 2026-10-03T10:15:00Z · model opus · effort max · NOT a "
                         "result: a reason of two lines · 2 text blocks, 14 characters, one "
                         "newline added after each block · prompt sha256 " + sha)
        # a directory that is already there keeps the mode it had before
        mine = self.path("mine")
        os.mkdir(mine, 0o750)
        with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": mine}):
            B.save_blocks(["x"], "p", "r", now=now)
        self.assertEqual(stat.S_IMODE(os.stat(mine).st_mode), 0o750)

    def test_partial_falls_back_when_xdg_unset(self):
        """With no C_LEAN_PARTIAL_DIR the file goes to ~/.local/state/c-lean/partial.
        HOME is a temp directory here: the real one must never be written."""
        home = self.mktemp("c-lean-home-")
        p = run([CLI, "-m", "haiku", "--system", "S", "hello"], mode="contfail",
                state=self.state, extra_env=dict(self.env, HOME=home, XDG_STATE_HOME=None,
                                                C_LEAN_PARTIAL_DIR=""))
        self.assertEqual(p.returncode, 1, p.stderr)
        d = os.path.join(home, ".local", "state", "c-lean", "partial")
        self.assertEqual(self.saved_blocks(d), [CUT_PIECES], "with no C_LEAN_PARTIAL_DIR the text "
                         "unset XDG_STATE_HOME must use the home state directory: %r" % p.stderr[-300:])
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertFalse(os.path.exists(self.partial))

    def test_a_call_that_died_of_a_signal_leaves_its_text_in_a_file(self):
        p = self.call("sigdeath")
        self.assertEqual(p.returncode, 137, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("INTERRUPTED", p.stderr)
        self.assertEqual(self.saved_blocks(), [["text that had come before the process died"]],
                         "the text of a call that died of a signal must be saved")
        self.assertIn("NOT a result: the call was killed by signal 9", self.saved()[0][1])

    def test_check_saves_no_files(self):
        """A probe is not an answer, so its unprinted text is not saved."""
        p = run([CLI, "--check"], mode="cutstream", state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertFalse(os.path.exists(self.partial), "c-lean --check must save no file")

    def test_the_cli_own_error_message_is_not_a_message_of_the_model(self):
        s = self.stream_of("contfail")
        self.assertEqual(s.texts(), CUT_PIECES,
                         "the CLI's own 'API Error' message must never be taken for a message "
                         "of the model")
        self.assertEqual(s.continued, 3)
        # this reads the document the FAKE printed, not the logic of read_stream; the
        # marks of the CLI's own message are checked one at a time in
        # test_each_mark_of_the_cli_own_error_message_is_enough
        self.assertTrue(s.doc["is_error"])

    def test_only_text_blocks_are_the_answer(self):
        s = self.stream_of("cont3")
        self.assertEqual(s.texts(), CONT_PIECES, "only text blocks are the answer: thinking and "
                         "tool_use blocks must never get into it")
        self.assertEqual((s.continued, [n for n, _t in s.blocks]), (2, [0, 1, 2]))
        # a tool_use block carries its payload under `input`, and a model can
        # put anything there, a key named `text` included
        s = B.read_stream(stream_text(
            ev_think("m1", "THINKING-NOT-THE-ANSWER"), ev_text("m1", "the text"),
            ev_block("m1", {"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput",
                            "input": {"text": "TOOL-INPUT-NOT-THE-ANSWER", "verdict": "yes"}}),
            ev_block("m1", {"type": "redacted_thinking", "data": "REDACTED-NOT-THE-ANSWER"}),
            ev_result("the text")))
        self.assertEqual(s.texts(), ["the text"], "the input of a tool_use block must never get "
                         "into the text")

    def test_an_event_line_of_a_megabyte_is_read_whole(self):
        """One text block is one line of the stream, however long the block."""
        big = ("line of an answer, with a quote \" and a newline\n" * 22000)
        raw = stream_text(ev_think("m1"), ev_text("m1", big), ev_result(big))
        self.assertGreater(max(len(l) for l in raw.split("\n")), 1 << 20)
        s = B.read_stream(raw)
        self.assertEqual(s.texts(), [big], "an event line of a megabyte must be read whole")
        self.assertEqual((s.doc["result"], s.events), (big, 3))

    def test_two_messages_with_no_continuation_between_them_are_not_joined(self):
        """Two messages with text and a `user` event between them that is NOT
        a request of the CLI. It may be a regeneration, and joining would
        print the text twice: only `result` goes out, with a warning, and the
        blocks go to a file."""
        p = self.call("twomsg")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "FINAL two\n", "messages with no continuation between them "
                         "must not be joined")
        self.assertNotIn("CONTINUED", p.stderr)
        warns = self.warns(p)
        self.assertEqual(len(warns), 1, "messages that were not joined must be warned about: %r"
                         % p.stderr)
        for phrase in ("no continuation between them",
                       "only the `result` of the CLI's document is printed",
                       "the start of the answer may be missing"):
            self.assertIn(phrase, warns[0])
        self.assertEqual(self.saved_blocks(), [["DRAFT one", "FINAL two"]],
                         "the text blocks that were not joined must be saved")
        self.assertIn("NOT a result: 2 text blocks came and were not joined", self.saved()[0][1])

    def test_a_stream_that_disagrees_with_the_document_is_not_joined(self):
        """The last text block of the stream is not the document's `result`:
        the stream cannot be trusted to be the answer, so only `result` goes
        out."""
        p = self.call("contmismatch")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "SOMETHING ELSE\n", "a stream that does not end in the "
                         "document's result must not be joined")
        self.assertIn(" · CONTINUED x1", p.stderr)
        warns = self.warns(p)
        self.assertEqual(len(warns), 1, "a stream that disagrees with the document must be "
                         "warned about: %r" % p.stderr)
        for phrase in ("does not agree with the result document",
                       "only the `result` of the CLI's document is printed",
                       "the start of the answer may be missing"):
            self.assertIn(phrase, warns[0])
        self.assertEqual(self.saved_blocks(), [["PART-A ", "PART-B"]])

    def test_a_chain_with_one_plain_boundary_is_not_joined(self):
        """Three messages, the request to go on before the second only. One
        boundary nobody can explain is enough: nothing is joined."""
        p = self.call("contbroken")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "LINK three\n", "a chain with a boundary that is not a "
                         "continuation must not be joined")
        self.assertIn("c-lean: WARN message 3 came after message 2 with no continuation", p.stderr)
        self.assertEqual(self.saved_blocks(), [["LINK one ", "LINK two ", "LINK three"]])

    def test_schema_output_is_printed_as_it_is_never_joined(self):
        """Under --json-schema the stream is one message (thinking, tool_use),
        then a `user` event with the tool result, and num_turns 2: none of it
        is a continuation. And if the cap is ever hit there, nothing is joined,
        and a good call saves no file whatever text came with it."""
        p = self.call("schema", "--schema", self.SCHEMA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, '{"verdict": "yes"}\n')
        for word in ("CONTINUED", "c-lean: NOTE", "c-lean: WARN"):
            self.assertNotIn(word, p.stderr, "a tool result is not a continuation")
        p = self.call("schemacont", "--schema", self.SCHEMA)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, '{"verdict": "yes"}\n')
        self.assertIn(" · CONTINUED x1", p.stderr, "the continuation is still counted")
        for word in ("c-lean: NOTE", "c-lean: WARN"):
            self.assertNotIn(word, p.stderr, "under --schema nothing is joined")
        self.assertFalse(os.path.exists(self.partial), "a good call under --schema must save no "
                         "file, whatever text blocks came with it")
        doc = json.loads(self.call("schemacont", "--schema", self.SCHEMA, "--json").stdout)
        self.assertNotIn("c_lean_pieces", doc, "under --schema nothing is joined")
        self.assertFalse(os.path.exists(self.partial))

    def test_schema_without_structured_output_prints_the_result_and_saves_nothing_twice(self):
        """--schema given, no structured_output came: the `result` goes to
        stdout with exit 1, as before. Its one text block IS that `result`,
        so it is on stdout already: what is saved never goes to stdout too."""
        p = self.call("single", "--schema", self.SCHEMA)
        self.assertEqual((p.returncode, p.stdout), (1, "ECHO:hello\n"), p.stderr)
        self.assertIn("returned no structured_output", p.stderr)
        self.assertFalse(os.path.exists(self.partial), "a text block that was printed must not "
                         "be saved as well")

    def test_noise_in_the_stream_does_not_break_the_call(self):
        """A node warning before the stream; inside it a JSON line that broke
        off, JSON that is not an object, a blank line, an unknown event type."""
        p = self.call("noise")
        self.assertEqual(p.returncode, 0, "a line that is not an event must be skipped, not "
                         "fail the call: %s" % p.stderr[-300:])
        self.assertEqual(p.stdout, "ECHO:hello\n")
        self.assertNotIn("Traceback", p.stderr)

    def test_a_line_separator_inside_the_answer_does_not_split_its_event(self):
        """JSON leaves U+2028, U+2029 and U+0085 unescaped inside a string, and
        str.splitlines() breaks on all three: the event of such an answer came
        apart into halves that do not parse, and the `result` with it."""
        p = self.call("linesep")
        self.assertEqual(p.returncode, 0, "a line separator inside the answer must not split "
                         "its event: %s" % p.stderr[-300:])
        self.assertEqual(p.stdout, "first line\u2028second line\u0085third line\u2029end\n")

    def test_a_stream_with_no_result_event_is_a_failure(self):
        """The stream ended after the first message: there is no document, and
        the text that did come is not an answer. The CLI did start, so the
        message says what happened, not "no JSON"; the text goes to a file."""
        p = self.call("cutstream")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("the stream of claude ended with no `result` event (5 events came before "
                      "it ended, rc=1)", p.stderr, "a stream with no `result` event has no "
                      "document. Events came and `result` did not: the message must say so, "
                      "with the number of events and the exit code")
        self.assertNotIn("no JSON", p.stderr)
        self.assertNotIn("LIMIT WALL", p.stderr)
        self.assertTrue(self.jsonl(self.log)[0]["tokens_unknown"])
        self.assertEqual(self.saved_blocks(), [["the start of an answer that never en"]],
                         "the text of a stream that ended with no `result` must be saved")
        self.assertIn("NOT a result: the stream of the CLI ended with no `result` event",
                      self.saved()[0][1])
        # a binary that prints no JSON at all is still told as that
        p = run([CLI, "-m", "haiku", "hello"], state=self.state,
                extra_env=dict(self.env, C_LEAN_CLAUDE_BIN=shutil.which("true") or "/usr/bin/true"))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("no JSON from claude (rc=0)", p.stderr)

    def test_a_cut_stream_that_quotes_the_wall_is_not_a_wall(self):
        p = self.call("cutwallwords")
        self.assertNotIn("LIMIT WALL", p.stderr,
                         "the model's words in the events of a stream decided the wall")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("ended with no `result` event", p.stderr)

    def test_the_cli_own_words_on_stderr_decide_when_the_stream_has_no_result(self):
        p = self.call("cutwallstderr")
        self.assertEqual(p.returncode, EXIT_WALL, "with no `result` event the CLI's own "
                         "sentence on stderr must still decide: rc %d %s"
                         % (p.returncode, p.stderr[-300:]))
        self.assertEqual(p.stdout, "")
        self.assertEqual(self.saved_blocks(), [["the start of an answer that never en"]],
                         "the text of a call that hit the wall must be saved")

    def test_default_timeout_depends_on_the_effort(self):
        """600 s killed an opus call at effort max that needed 1236 s: exit 124,
        no answer, the tokens spent. The numbers were then doubled: waiting
        longer is cheaper than losing an answer that is already paid for."""
        for effort, want in ((None, 1200), ("", 1200), ("low", 1200), ("medium", 1200),
                             ("high", 1200), ("xhigh", 3600), ("max", 6000)):
            self.assertEqual(B.default_timeout(effort), want,
                             "the default timeout for effort %r" % effort)

    def test_an_explicit_timeout_always_wins(self):
        self.assertEqual(B.timeout_plan(7.0, "max")[0], 7.0,
                         "an explicit --timeout must win over the default for the effort")
        self.assertEqual(B.timeout_plan(None, "max")[0], 6000)
        self.assertEqual(self.timeout_given_to_the_call("-m", "haiku", "-e", "max",
                                                        "--timeout", "7", "x"), 7.0,
                         "c-lean must give the call the --timeout it was given")

    def test_tool_gives_the_call_the_default_for_its_effort(self):
        for args, want in ((("-e", "xhigh"), 3600), (("-e", "max"), 6000), ((), 1200),
                           (("-e", "high"), 1200)):
            self.assertEqual(self.timeout_given_to_the_call("-m", "haiku", *args, "x"), want,
                             "c-lean must give the call the default timeout for %s"
                             % (" ".join(args) or "no -e"))

    def test_timeout_message_names_the_source_and_the_way_to_raise_it(self):
        msg = B.timeout_message(*B.timeout_plan(None, "max"), 0)
        for phrase in ("timeout after 6000s", "the default for -e max",
                       "raise it with --timeout SEC", "0 messages of the model had finished",
                       "is billed", "cannot be recovered",
                       "no text block had come whole before the kill, so there is nothing to keep"):
            self.assertIn(phrase, msg, "the timeout message must say: %s" % phrase)
        msg = B.timeout_message(*B.timeout_plan(5.0, "max"), 2, 3)
        for phrase in ("timeout after 5s", "set by --timeout", "2 messages of the model",
                       "3 text blocks had come whole before the kill and are kept"):
            self.assertIn(phrase, msg, "the timeout message must say: %s" % phrase)
        self.assertNotIn("nothing to keep", msg)
        self.assertIn("the default with no -e", B.timeout_message(*B.timeout_plan(None, None), 0))

    def test_a_timeout_in_the_middle_of_the_stream_gives_no_answer(self):
        """One message had come whole, the CLI had asked for the next one, and
        the timeout fell there. Nothing of it is an answer: stdout stays empty,
        and the text that had come whole is saved."""
        # 3 s, not less: the fake has to start and print its events before the
        # kill, and a loaded machine makes a short limit a coin
        p = self.call("conthang", "-e", "max", "--timeout", "3")
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertEqual(p.stdout, "", "a call killed by the timeout must print no answer, "
                         "whatever had come before the kill")
        self.assertIn("timeout after 3s (set by --timeout; raise it with --timeout SEC)",
                      p.stderr, "the timeout message must name where the limit came from")
        self.assertIn("1 message of the model had finished", p.stderr,
                      "the timeout message must count the messages that had finished")
        rec = self.jsonl(self.log)[0]
        self.assertEqual((rec["exit"], rec["tokens_unknown"]), (124, True))
        saved = self.saved()
        self.assertEqual([b for _n, _h, b, _w in saved], [["the whole first message"]],
                         "the text that had come whole before the timeout must be saved")
        self.assertIn("1 text block had come whole before the kill and is kept: see the next line",
                      p.stderr, "the timeout message must say that the finished text is kept")
        lines = p.stderr.splitlines()
        at = [i for i, l in enumerate(lines) if l.startswith("c-lean: timeout after 3s")][0]
        self.assertEqual(lines[at + 1], "c-lean: SAVED 23 characters in 1 text block to %s — the "
                         "text of an answer that did not finish or was not printed, NOT a result"
                         % os.path.join(self.partial, saved[0][0]),
                         "the line after the timeout message must give the full path of the file")
        self.assertIn("NOT a result: the call was killed by the timeout after 3s", saved[0][1])

    def test_a_message_still_being_written_is_not_counted_as_finished(self):
        """The events of a message come block by block, and nothing in them
        says the message is over: its thinking block alone is not a message.
        No text block had come, so there is nothing to save."""
        p = self.call("thinkhang", "--timeout", "3")
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertIn("0 messages of the model had finished", p.stderr,
                      "a message whose blocks are still coming must not be counted as finished")
        self.assertIn("no text block had come whole before the kill", p.stderr)
        self.assertFalse(os.path.exists(self.partial))
        self.assertEqual(self.saved_lines(p), [])


    def test_an_ordinary_call_is_untouched_by_the_stream(self):
        """One message, one text block: the output is what it was before the
        stream was read, the usage log says continued 0, and no file is made."""
        p = self.call("single")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "ECHO:hello\n")
        for word in ("CONTINUED", "NOTE", "WARN", "SAVED"):
            self.assertNotIn(word, p.stderr,
                             "an ordinary one-message call must not be marked %s" % word)
        # the trailer, and nothing but the trailer, exactly as it was
        self.assertRegex(p.stderr, r"\A\[c-lean\] haiku · tokens in 575 cw 0 cr 0 out 40 "
                         r"\(think 0\) · \d+\.\ds · exit 0 · bin \S+fake_claude\.py\n\Z")
        self.assertEqual(self.jsonl(self.log)[0].get("continued"), 0,
                         "the usage log must carry continued: 0 for an ordinary call")
        self.assertFalse(os.path.exists(self.partial), "an ordinary call must leave no file and "
                         "make no directory: %r" % self.saved())

    def script(self, events, *args, end="exit", rc=0, stderr="", tail_hex=""):
        """`c-lean` run against a stream written out in the test: the fake prints
        exactly these events (a dict is a JSON line, a string a line as it is)."""
        self.nscript = getattr(self, "nscript", 0) + 1
        path = self.path("script-%d.json" % self.nscript)
        with open(path, "w") as f:
            json.dump({"events": events, "end": end, "rc": rc, "stderr": stderr,
                       "tail_hex": tail_hex}, f)
        return run([CLI, "-m", "haiku", "--system", "S"] + list(args) + ["hello"],
                   mode="script", state=self.state,
                   extra_env=dict(self.env, C_LEAN_FAKE_SCRIPT=path))

    def user_event(self, text, **top):
        return dict(ev_ask(text), **top)

    def api_error_event(self, mid, **top):
        ev = ev_text(mid, "API Error: x")
        ev.update(top)
        return ev

    def test_a_single_block_with_outer_whitespace_is_the_result(self):
        """One text block that has a newline at its end is the ordinary call:
        `result` is that block with the outside whitespace cut off. It is
        printed, so there is nothing to keep."""
        p = self.script([ev_think("1"), ev_text("1", "ans\n"), ev_result("ans")])
        self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), p.stderr)
        self.assertEqual(self.saved_lines(p), [], "an ordinary call whose one block has outer "
                         "whitespace must not be reported as saved text")
        self.assertFalse(os.path.exists(self.partial), "an ordinary call must write no file")

    def test_wall_phrase_after_long_stderr_still_decides(self):
        p = self.script([], end="exit", rc=1,
                        stderr="x" * 5000 + "\nYou've hit your limit · resets 3pm\n")
        self.assertEqual(p.returncode, EXIT_WALL, "the sentence at the END of a long stderr "
                         "must still decide: rc %d %s" % (p.returncode, p.stderr[-200:]))
        self.assertIn("LIMIT WALL", p.stderr)

    def test_wall_phrase_after_long_plain_stdout_still_decides(self):
        p = self.script(["x" * 5000, "You've hit your limit · resets 3pm",
                         '{"type":"assistant","message":{"con'], end="exit", rc=1)
        self.assertEqual(p.returncode, EXIT_WALL, "the sentence at the END of a long plain "
                         "stdout must still decide: rc %d %s" % (p.returncode, p.stderr[-200:]))
        self.assertIn("LIMIT WALL", p.stderr)

    def test_save_blocks_writes_a_lone_surrogate_as_an_escape(self):
        d = self.path("sur")
        with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": d}):
            try:
                path = B.save_blocks(["a\ud800b"], "p", "r")
            except UnicodeError as e:
                self.fail("a lone surrogate must be written as its escape: %s" % e)
        with open(path, encoding="utf-8", newline="") as f:
            whole = f.read()
        self.assertIn("a\\ud800b\n", whole, "a lone surrogate must be written as its escape")
        self.assertEqual(os.listdir(d), [os.path.basename(path)])

    def test_third_file_in_the_same_second_gets_counter_3(self):
        d = self.path("same-second")
        now = datetime(2026, 10, 3, 10, 15, 0, tzinfo=timezone.utc)
        with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": d}):
            try:
                paths = [B.save_blocks(["t%d" % i], "same prompt", "r", now=now) for i in range(3)]
            except OSError as e:
                self.fail("a third same-second file must be saved: %s" % e)
        base = "20261003T101500Z-" + B.sha256("same prompt")[:8]
        self.assertEqual([os.path.basename(x) for x in paths],
                         [base + ".txt", base + "-2.txt", base + "-3.txt"],
                         "three files of one second and one prompt must be named with no "
                         "counter, -2 and -3")

    def test_names_exhausted_raises_oserror_and_costs_one_warn(self):
        d = self.partial
        os.makedirs(d, mode=0o700)
        base = "20261003T101500Z-" + B.sha256("hello")[:8]
        for n in range(1, 1001):
            open(os.path.join(d, base + ("" if n == 1 else "-%d" % n) + ".txt"), "w").close()
        now = datetime(2026, 10, 3, 10, 15, 0, tzinfo=timezone.utc)
        with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": d}):
            with self.assertRaises(OSError) as cm:
                B.save_blocks(["x"], "hello", "r", now=now)
        self.assertIn("1000 files named", str(cm.exception))
        self.assertIn("are already in", str(cm.exception))
        # the same through `c-lean`: one WARN, and the exit code and stdout stay as they were
        p = run(["-c", KEEP_PROBE, CLI, "-m", "haiku", "--system", "S", "hello"],
                mode="contfail", state=self.state, extra_env=dict(self.env, PROBE_NOW="1"))
        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr[-300:])
        lost = [l for l in self.warns(p) if "could not save" in l]
        self.assertEqual(len(lost), 1, "names used up must cost exactly one WARN: %r" % p.stderr)
        self.assertIn("1000 files named", lost[0])
        self.assertEqual(self.saved_lines(p), [])
        self.assertEqual(len(os.listdir(d)), 1000, "no file may be written over")

    def test_splice_seam_ignores_leading_whitespace(self):
        self.assertEqual(B.splice(["\n\nabc", "def"]), ("abcdef", [3]),
                         "a seam is placed in the printed answer, after its leading whitespace "
                         "was cut off")

    def test_unknown_request_before_the_first_text_message_does_not_block_the_join(self):
        events = [ev_think("0"), self.user_event("odd request"), ev_text("1", "A"),
                  ev_ask(CAP_ASK), ev_text("2", "B"), ev_result("B")]
        s = B.read_stream(stream_text(*events))
        self.assertEqual(B._pieces(s, "B"), (["A", "B"], None), "a request nobody knows that "
                         "stands before the first text message ties nothing that is printed")
        p = self.script(events)
        self.assertEqual((p.returncode, p.stdout), (0, "AB\n"), p.stderr)
        self.assertIn("CONTINUED x1", p.stderr)
        self.assertEqual(self.warns(p), [])

    def test_an_empty_text_block_is_not_a_block(self):
        events = [ev_text("1", "A"), ev_ask(CAP_ASK), ev_text("2", "B"), ev_text("2", ""),
                  ev_result("B")]
        s = B.read_stream(stream_text(*events))
        self.assertEqual(s.texts(), ["A", "B"], "an empty text block is not a block")
        self.assertEqual(B._pieces(s, "B"), (["A", "B"], None))

    def test_each_mark_of_the_cli_own_error_message_is_enough(self):
        """The CLI's own "API Error" message has three marks. Any one of them
        alone must keep it out of the answer (a test with all three at once
        cannot tell which one the code reads)."""
        marks = {
            "the top level `error` field": self.api_error_event("3", error="max_output_tokens"),
            "the model <synthetic>": self.api_error_event("3"),
            "the top level `is_api_error_message` field": self.api_error_event(
                "3", is_api_error_message=True),
        }
        marks["the model <synthetic>"]["message"]["model"] = "<synthetic>"
        for name, ev in marks.items():
            with self.subTest(mark=name):
                s = B.read_stream(stream_text(
                    ev_text("1", "A"), ev_ask(CAP_ASK), ev_text("2", "B"), ev, ev_result("B")))
                self.assertEqual(s.texts(), ["A", "B"], "a message of the CLI with only %s must "
                                 "be dropped from the answer" % name)
                self.assertEqual(B._pieces(s, "B"), (["A", "B"], None), name)

    def test_a_request_before_the_first_message_ties_nothing(self):
        events = [ev_ask(CAP_ASK), ev_text("1", "A"), ev_result("A")]
        s = B.read_stream(stream_text(*events))
        self.assertEqual((s.links, s.continued), ([None], 0),
                         "a request before the first message ties nothing")
        p = self.script(events)
        self.assertEqual((p.returncode, p.stdout), (0, "A\n"), p.stderr)
        self.assertNotIn("CONTINUED", p.stderr, "a request that no message precedes is no "
                         "continuation")
        self.assertEqual(self.jsonl(self.log)[-1]["continued"], 0)

    def test_issynthetic_must_be_literal_true(self):
        for value in ("true", 1, "false"):
            with self.subTest(isSynthetic=value):
                s = B.read_stream(stream_text(
                    ev_text("1", "A"), dict(ev_ask(CAP_ASK), isSynthetic=value),
                    ev_text("2", "B"), ev_result("B")))
                self.assertEqual(s.links, [None, None], "isSynthetic %r is not the literal true: "
                                 "the event is no request of the CLI" % (value,))
                pieces, why = B._pieces(s, "B")
                self.assertIsNone(pieces)
                self.assertIn("no continuation between them", why)

    def test_resumed_flag_must_be_literal_true(self):
        for value in ("true", 1, "false"):
            with self.subTest(resumed=value):
                s = B.read_stream(stream_text(
                    ev_text("1", "A"),
                    ev_text("2", "B", resumed_from_incomplete_thinking=value), ev_result("B")))
                self.assertEqual(s.links, [None, None], "resumed_from_incomplete_thinking %r is "
                                 "not the literal true: it ties nothing" % (value,))
                self.assertIsNone(B._pieces(s, "B")[0])
        s = B.read_stream(stream_text(
            ev_text("1", "A"), ev_text("2", "B", resumed_from_incomplete_thinking=True),
            ev_result("B")))
        self.assertEqual(B._pieces(s, "B"), (["A", "B"], None))

    def test_a_known_prefix_after_whitespace_is_not_known(self):
        events = [ev_text("1", "A"), ev_ask("  " + CAP_ASK), ev_text("2", "B"), ev_result("B")]
        s = B.read_stream(stream_text(*events))
        self.assertEqual(s.links[1][0], B.UNKNOWN, "a request is told by the START of its text: "
                         "whitespace in front makes it a request nobody knows")
        p = self.script(events)
        self.assertEqual((p.returncode, p.stdout), (0, "B\n"), p.stderr)
        self.assertIn('its request begins "  Output token limit hit', "\n".join(self.warns(p)))
        self.assertEqual(self.saved_blocks(), [["A", "B"]])

    def test_the_first_unknown_request_is_quoted(self):
        events = [ev_text("1", "A"), ev_ask("first odd"), ev_ask("second odd"),
                  ev_text("2", "B"), ev_result("B")]
        p = self.script(events)
        warn = "\n".join(self.warns(p))
        self.assertIn('begins "first odd"', warn, "the warning must quote the FIRST request "
                      "nobody knows")
        self.assertNotIn("second odd", warn)

    def test_the_last_known_request_names_the_kind(self):
        events = [ev_text("1", "A"), ev_ask(CAP_ASK), ev_ask(CUT_ASK), ev_text("2", "B"),
                  ev_result("B")]
        s = B.read_stream(stream_text(*events))
        self.assertEqual(s.joined_after(), ["a response cut off mid-stream"],
                         "the kind of a continuation is the LAST known request before the message")
        p = self.script(events)
        self.assertIn("after a response cut off mid-stream", p.stderr)
        self.assertNotIn("the output cap", p.stderr)

    def test_note_counts_pieces_as_seams_plus_one(self):
        p = self.call("cont3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("3 pieces are printed joined exactly as they came", p.stderr,
                      "the NOTE of an answer in three pieces (two seams) must say 3 pieces")

    def test_default_timeout_tolerates_case_and_spaces(self):
        for effort, secs in (("XHIGH", 3600), (" max ", 6000), ("Max", 6000)):
            with self.subTest(effort=effort):
                self.assertEqual(B.default_timeout(effort), secs,
                                 "default_timeout(%r) must be %d" % (effort, secs))
        self.assertEqual(B.timeout_plan(None, " xhigh ")[1], "the default for -e xhigh")

    def test_check_timeout_ignores_effort_flag(self):
        """The probe is haiku at effort low whatever -e says: 1200 s, not the
        6000 s of -e max. An explicit --timeout still wins."""
        self.assertEqual(self.timeout_given_to_the_call("--check", "-e", "max"), 1200,
                         "c-lean --check must take its limit from effort low, not from -e")
        self.assertEqual(self.timeout_given_to_the_call("--check", "-e", "max", "--timeout", "7"),
                         7.0)

    def test_a_failure_with_only_blank_blocks_saves_nothing(self):
        p = self.script([ev_text("1", "  "), ev_ask(CAP_ASK), ev_text("2", "\n"),
                         ev_result("API Error", is_error=True, subtype="error")], rc=1)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(self.saved_lines(p), [], "blocks of whitespace alone are not worth a file")
        self.assertFalse(os.path.exists(self.partial))

    def test_system_prompt_is_passed_byte_for_byte(self):
        p = run([CLI, "-m", "haiku", "--system", "  Rubric.\n", "hello"], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = self.received()
        self.assertEqual((got["system_chars"], got["system_sha256"]),
                         (10, hashlib.sha256(b"  Rubric.\n").hexdigest()),
                         "the system prompt must reach the CLI byte for byte")

    def test_the_last_result_event_is_the_document(self):
        s = B.read_stream(stream_text(ev_result("old"), ev_text("1", "A"), ev_result("new")))
        self.assertEqual(s.doc["result"], "new", "of several `result` events the LAST is the "
                         "document")

    # --- the unprinted text of a call that was cut short is counted too
    def test_a_timeout_after_real_continuations_says_continued(self):
        p = self.script([ev_think("1"), ev_text("1", "first part"), ev_ask(CAP_ASK),
                         ev_think("2"), ev_text("2", "second part")], "--timeout", "3",
                        end="hang")
        self.assertEqual(p.returncode, 124, p.stderr)
        trailer = [l for l in p.stderr.splitlines() if l.startswith("[c-lean]")]
        self.assertTrue(trailer and "CONTINUED x1" in trailer[0],
                        "the trailer of a call killed by the timeout must say CONTINUED x1: %r"
                        % trailer)
        self.assertEqual(self.jsonl(self.log)[-1]["continued"], 1,
                         "the usage log of a call killed by the timeout must count the "
                         "continuations that were read")

    def test_a_death_by_signal_after_real_continuations_says_continued(self):
        p = self.script([ev_think("1"), ev_text("1", "first part"), ev_ask(CAP_ASK),
                         ev_think("2"), ev_text("2", "second part")], end="kill")
        self.assertEqual(p.returncode, 137, p.stderr)
        trailer = [l for l in p.stderr.splitlines() if l.startswith("[c-lean]")]
        self.assertTrue(trailer and "CONTINUED x1" in trailer[0],
                        "the trailer of a call that died of a signal must say CONTINUED x1: %r"
                        % trailer)
        self.assertEqual(self.jsonl(self.log)[-1]["continued"], 1,
                         "the usage log of a call that died of a signal must count the "
                         "continuations that were read")

    # --- a place for saved text that cannot be told
    def test_a_directory_that_starts_with_a_tilde_is_refused(self):
        here = self.mktemp("c-lean-cwd-")
        was = os.getcwd()
        os.chdir(here)
        try:
            with mock.patch.object(os.path, "expanduser", lambda p: p):
                for value in ("", "~someone-unknown/x"):
                    with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": value}):
                        with self.assertRaises(OSError, msg="partial_dir %r" % value):
                            B.partial_dir()
                        with self.assertRaises(OSError, msg="save_blocks %r" % value):
                            B.save_blocks(["x"], "p", "r")
            self.assertEqual(os.listdir(here), [], "a directory named `~` must never be made "
                             "in the working directory")
        finally:
            os.chdir(was)

    def test_no_home_and_no_partial_dir_saves_nothing_and_costs_one_warn(self):
        here = self.mktemp("c-lean-cwd-")
        p = run(["-c", KEEP_PROBE, CLI, "-m", "haiku", "--system", "S", "hello"],
                mode="contfail", state=self.state, cwd=here,
                extra_env=dict(self.env, C_LEAN_PARTIAL_DIR="", PROBE_NO_HOME="1"))
        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr[-300:])
        lost = [l for l in self.warns(p) if "could not save" in l]
        self.assertEqual(len(lost), 1, "an unknown home must cost exactly one WARN: %r" % p.stderr)
        self.assertEqual(self.saved_lines(p), [])
        self.assertEqual(os.listdir(here), [], "a directory named `~` must never be made in "
                         "the working directory")

    def test_a_file_that_failed_half_way_is_removed(self):
        d = self.path("half")
        with mock.patch.dict(os.environ, {"C_LEAN_PARTIAL_DIR": d}):
            with self.assertRaises(Exception):
                B.save_blocks(["ok", 5], "p", "r")   # the second block is not a string
        self.assertEqual(os.listdir(d), [], "a file the write of which failed must not be left "
                         "behind: it would be empty or cut off")

    def test_help_describes_the_big_call(self):
        s = run([CLI, "-h"], state=self.state, extra_env=self.env).stdout
        for phrase in ("and empty when\n    neither is given", "never applies",
                       '--system-prompt "" is passed when there is none', "~0.6K input tokens",
                       # how the prompt and the system prompt reach the CLI
                       "the prompt is never on the command line of the CLI: it goes on stdin",
                       "with mode 0600 and outside the\n    cwd of the call, passed with "
                       "--system-prompt-file", "The file is removed\n    when the call ends",
                       "The JSON schema of --schema does go on the command\n    line",
                       "The trailer says\n    CONTINUED xN", "c_lean_pieces",
                       "the key is there only when the blocks were joined",
                       "A whitespace character can be\n    lost at a seam",
                       "at most 3 times", "With --schema nothing is joined",
                       # the kinds of continuation
                       "after a message that hit the output cap",
                       "after a response that was cut off mid-stream",
                       "after a response with no visible text",
                       "after thinking that was cut at the output cap",
                       # what is printed when nothing is joined: the `result`, not a message
                       "is joined: the `result` of the document is printed",
                       # what is saved and where
                       "no text the model wrote is dropped", "C_LEAN_PARTIAL_DIR",
                       "$XDG_STATE_HOME/c-lean/partial (made with mode 0700, the file with 0600)",
                       "===== c-lean: text block 2 of 3 =====",
                       "Thinking is never printed and never saved",
                       "a good call with --schema", "--check (a probe, not an answer)",
                       "A response stopped by a safety classifier is not joined",
                       "generous on purpose", "up to\n    4 messages: 6000 s covers",
                       # the rule of the wall, as the code has it
                       "a line whose first character after leading\n  whitespace is `{`",
                       # what CONTINUED counts, and what it does not
                       "It counts them even\n    when the text came in one block",
                       "gets a NOTE and no CONTINUED: nothing was continued",
                       "a half-written one is removed"):
            self.assertIn(phrase, s, "c-lean -h must say: %s" % phrase)
        for stale in ("~0.4K", "6.7K", "raw JSON", "only\n    the last message is printed",
                      "gives no answer at all", "no JSON from claude",
                      "--system-prompt is always passed"):
            self.assertNotIn(stale, s, "c-lean -h must not say %s any more" % stale)

    def check_blank_result_not_joined(self, events, blocks):
        s = B.read_stream(stream_text(*events))
        pieces, why = B._pieces(s, "")
        self.assertIsNone(pieces, "a blank `result` must not let the blocks be joined")
        self.assertIn("is empty although text blocks came", why)
        p = self.script(events)
        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
        self.assertIn("c-lean: empty answer", p.stderr)
        self.assertEqual(len(self.warns(p)), 1, p.stderr)
        self.assertIn("is empty although text blocks came", self.warns(p)[0])
        self.assertEqual(self.saved_blocks(), [blocks])
        shutil.rmtree(self.partial)
        p = self.script(events, "--allow-empty")
        self.assertEqual((p.returncode, p.stdout), (0, "\n"), p.stderr)
        self.assertEqual(self.saved_blocks(), [blocks])

    def test_a_blank_result_after_a_whitespace_block_is_not_joined(self):
        """Blocks "WITHDRAWN" and " " of one message, then a request nobody
        knows, then a `result` of "": the CLI says the last thing was not
        text. The old check (the last block, stripped, equals `result`)
        passed for the whitespace block and printed WITHDRAWN with exit 0."""
        self.check_blank_result_not_joined(
            [ev_text("1", "WITHDRAWN"), ev_text("1", " "), self.user_event("odd request"),
             ev_result("")], ["WITHDRAWN", " "])

    def test_a_blank_result_after_a_known_request_is_not_joined(self):
        self.check_blank_result_not_joined(
            [ev_text("1", "A"), ev_text("1", " "), ev_ask(CAP_ASK), ev_result("")], ["A", " "])

    def test_a_lone_surrogate_in_a_joined_answer_is_printed_as_an_escape(self):
        p = self.script([ev_text("1", "\ud800"), ev_text("1", "OK"), ev_result("OK")])
        self.assertEqual((p.returncode, p.stdout), (0, "\\ud800OK\n"), p.stderr[-300:])
        self.assertNotIn("Traceback", p.stderr)

    def test_a_lone_surrogate_in_an_ordinary_answer_is_printed_as_an_escape(self):
        p = self.script([ev_text("1", "\ud800"), ev_result("\ud800")])
        self.assertEqual((p.returncode, p.stdout), (0, "\\ud800\n"), p.stderr[-300:])
        self.assertNotIn("Traceback", p.stderr)

    def test_a_child_killed_inside_a_character_is_a_stop_not_a_traceback(self):
        """The stream is written as the call goes: SIGKILL can land between the
        two bytes of one character, and the strict decoder of communicate()
        raised a UnicodeDecodeError out of the tool."""
        p = self.script([ev_text("1", "first block")], end="kill", tail_hex="d0")
        self.assertEqual(p.returncode, 128 + signal.SIGKILL, p.stderr[-300:])
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("c-lean: stopped", p.stderr)
        self.assertEqual(self.saved_blocks(), [["first block"]])

    def test_a_timeout_inside_a_character_keeps_the_text(self):
        p = self.script([ev_text("1", "first block")], "--timeout", "2", end="hang",
                        tail_hex="d0")
        self.assertEqual(p.returncode, 124, p.stderr[-300:])
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.saved_blocks(), [["first block"]],
                         "a timeout inside a character must keep completed text blocks")

    def test_an_exception_out_of_communicate_does_not_leave_the_child_running(self):
        def boom(proc, *a, **kw):
            # the prompt comes on stdin from a thread of the tool, which closes
            # it: the fake starts its work once stdin ends
            path = os.path.join(self.state, "hang.pids")
            self.wait_for(lambda: os.path.exists(path) and len(open(path).read().split()) == 2,
                          what="the fake to start its grandchild")
            raise OverflowError("timestamp too large to convert to C PyTime_t")
        env = child_env("hang", self.state, self.env)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(B, "_communicate", boom):
            with self.assertRaises(OverflowError):
                B._call_claude("p", "S", "haiku", "low", None, 5)
        parent, kid = open(os.path.join(self.state, "hang.pids")).read().split()
        self.wait_for(lambda: not self.alive(parent) and not self.alive(kid), secs=5,
                      what="the child and its grandchild to be killed")

    def test_a_timeout_above_thirty_days_is_refused(self):
        for bad in ("1e100", "2592001"):
            s = run([CLI, "-m", "haiku", "--system", "S", "--timeout", bad, "x"],
                    mode="single", state=self.state, extra_env=self.env)
            self.assertEqual(s.returncode, 64, "c-lean %s: %s" % (bad, s.stderr))
            self.assertIn("2592000", s.stderr)
            self.assertNotIn("Traceback", s.stderr)
        self.assertEqual(self.calls(), 0, "no call may be spent on a refused timeout")
        s = run([CLI, "-m", "haiku", "--system", "S", "--timeout", "2592000", "x"],
                mode="single", state=self.state, extra_env=self.env)
        self.assertEqual(s.returncode, 0, "30 days exactly must be accepted: %s" % s.stderr)
        h = run([CLI, "-h"], state=self.state, extra_env=self.env).stdout
        self.assertIn("2592000", h, "-h must name the upper bound of --timeout")

    def test_check_looks_at_every_block_of_the_probe_answer(self):
        """The probe answered in two blocks, "Привет" and "OK": `c-lean` would
        print both, and --check looked at the last one only and said OK."""
        self.nscript = getattr(self, "nscript", 0) + 1
        path = self.path("script-%d.json" % self.nscript)
        with open(path, "w") as f:
            json.dump({"events": [ev_text("1", "Привет"), ev_text("1", "OK"), ev_result("OK")]}, f)
        p = run([CLI, "--check"], mode="script", state=self.state,
                extra_env=dict(self.env, C_LEAN_FAKE_SCRIPT=path))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("answer is not English", p.stderr)

    def test_help_says_when_no_partial_file_is_written(self):
        h = run([CLI, "-h"], state=self.state, extra_env=self.env).stdout
        self.assertIn("with no HOME and no entry for the user in the system", h,
                      "c-lean -h must say that the file is not written only when the home "
                      "directory cannot be told at all")
        self.assertNotIn("with no HOME and no C_LEAN_PARTIAL_DIR\n    nothing is written", h)

    def cap_in_child(self, *argv, **extra):
        """The CLAUDE_CODE_MAX_OUTPUT_TOKENS the child saw, or None when it had none."""
        p = run(list(argv), mode="envdump", state=self.state,
                extra_env=dict(self.env, **extra))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.load(open(os.path.join(self.state, "env.json"))).get(
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS")


    def test_the_child_gets_the_output_cap_of_the_tool(self):
        """A model whose default cap is below its upper bound writes up to the bound
        when the variable says 128000: the CLI cuts the number down, and says nothing."""
        self.assertEqual(B.OUTPUT_CAP, 128000)
        self.assertEqual(self.cap_in_child(CLI, "-m", "haiku", "--system", "S", "x"), "128000",
                         "c-lean must give the child CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000")


    def test_the_output_cap_of_the_caller_does_not_pass(self):
        """The caller's 300 would make every answer end after 300 tokens; neither the
        environment nor C_LEAN_PASS_ENV may carry it in. The child sees our number."""
        for pass_env in (None, "CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
            extra = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "300"}
            if pass_env:
                extra["C_LEAN_PASS_ENV"] = pass_env
            what = "C_LEAN_PASS_ENV=%s" % pass_env if pass_env else "the environment alone"
            self.assertEqual(self.cap_in_child(CLI, "-m", "haiku", "--system", "S", "x", **extra),
                             "128000", "c-lean: the caller's value must not pass (%s)" % what)

    def test_scrubbed_env_stays_a_pure_allow_list(self):
        """The cap is put in at the call, not in scrubbed_env(): the transport note of
        the trailer and anything else built on it must not see a name we added."""
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                         B.scrubbed_env({"PATH": "/bin", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "300",
                                         "C_LEAN_PASS_ENV": "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}))
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", B.scrubbed_env({"PATH": "/bin"}))

    def usage_warns(self, p):
        return [l for l in p.stderr.splitlines() if "WARN" in l]

    def test_a_model_held_below_its_default_cap_is_named(self):
        """A result that names a default cap above 128000 says that our number held
        the model down: one WARN with the model, its number and ours."""
        usage = {"claude-x": {"maxOutputTokens": 256000}}
        p = self.script([ev_text("1", "ans"), ev_result("ans", modelUsage=usage)])
        self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), p.stderr)
        warns = self.usage_warns(p)
        self.assertEqual(len(warns), 1, "exactly one WARN line is wanted: %r" % p.stderr)
        for want in ("256000", "128000", "claude-x", "OUTPUT_CAP"):
            self.assertIn(want, warns[0], "the WARN must name %s: %s" % (want, warns[0]))
        self.assertNotIn("Traceback", p.stderr)

    def test_two_models_held_down_still_make_one_warn(self):
        usage = {"claude-x": {"maxOutputTokens": 200000}, "claude-y": {"maxOutputTokens": 300000}}
        p = self.script([ev_text("1", "ans"), ev_result("ans", modelUsage=usage)])
        warns = self.usage_warns(p)
        self.assertEqual(len(warns), 1, p.stderr)
        self.assertIn("claude-y", warns[0], "the model with the highest number is the one named")

    def test_the_cap_guard_is_silent_when_nothing_is_held_down(self):
        """128000 and 64000 are within our number; no `modelUsage`, a `modelUsage` that
        is not a dict, an entry that is not a dict, no field, a value that is not an
        integer (a string, a float, a bool, null): no information, no WARN, no trace."""
        shapes = [None,
                  {"claude-x": {"maxOutputTokens": 128000}},
                  {"claude-x": {"maxOutputTokens": 64000}},
                  "claude-x", ["claude-x"], 256000, True,
                  {"claude-x": 256000}, {"claude-x": "256000"}, {"claude-x": [256000]},
                  {"claude-x": None}, {"claude-x": {}},
                  {"claude-x": {"maxOutputTokens": "256000"}},
                  {"claude-x": {"maxOutputTokens": 256000.5}},
                  {"claude-x": {"maxOutputTokens": 256000.0}},
                  {"claude-x": {"maxOutputTokens": True}},
                  {"claude-x": {"maxOutputTokens": False}},
                  {"claude-x": {"maxOutputTokens": None}},
                  {"claude-x": {"maxOutputTokens": [256000]}}]
        for shape in shapes:
            extra = {} if shape is None else {"modelUsage": shape}
            p = self.script([ev_text("1", "ans"), ev_result("ans", **extra)])
            self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), "%r: %s" % (shape, p.stderr))
            self.assertEqual(self.usage_warns(p), [], "no WARN is wanted for modelUsage %r" % (shape,))
            self.assertNotIn("Traceback", p.stderr, "modelUsage %r" % (shape,))


    def test_help_names_the_output_cap_and_its_examples(self):
        b = run([CLI, "-h"], state=self.state, extra_env=self.env).stdout
        flat = " ".join(b.split())
        for phrase in ("cannot ride in from the calling session",
                       "sets that one itself, to 128000",
                       "lowers the number to the upper bound of the model",
                       "haiku-4-5 gets 64000 instead of 32000",
                       "opus-5 gets 128000 instead of 64000",
                       "prints one WARN line", "raise OUTPUT_CAP in c_lean_core.py"):
            self.assertIn(phrase, flat, "c-lean -h must say: %s" % phrase)


    def state_env(self, xdg):
        return dict(self.env, HOME=self.dir, XDG_STATE_HOME=xdg,
                    C_LEAN_USAGE_LOG="", C_LEAN_PARTIAL_DIR="")

    def test_usage_log_uses_xdg_state_home(self):
        state = self.path("xdg/state")
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state,
                extra_env=self.state_env(state))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.jsonl(os.path.join(state, "c-lean", "usage.jsonl"))), 1,
                         "usage log must use XDG_STATE_HOME")

    def test_usage_log_falls_back_when_xdg_unset(self):
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state,
                extra_env=self.state_env(None))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.jsonl(self.path(".local/state/c-lean/usage.jsonl"))), 1,
                         "unset XDG_STATE_HOME must use the home usage log")

    def test_usage_log_falls_back_when_xdg_empty(self):
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state,
                extra_env=self.state_env(""))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.jsonl(self.path(".local/state/c-lean/usage.jsonl"))), 1,
                         "empty XDG_STATE_HOME must use the home usage log")

    def test_partial_uses_xdg_state_home(self):
        state = self.path("xdg/state")
        p = run([CLI, "-m", "haiku", "hello"], mode="contfail", state=self.state,
                extra_env=self.state_env(state))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(self.saved_blocks(os.path.join(state, "c-lean", "partial")),
                         [CUT_PIECES], "partial files must use XDG_STATE_HOME")

    def test_partial_falls_back_when_xdg_empty(self):
        p = run([CLI, "-m", "haiku", "hello"], mode="contfail", state=self.state,
                extra_env=self.state_env(""))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(self.saved_blocks(self.path(".local/state/c-lean/partial")),
                         [CUT_PIECES], "empty XDG_STATE_HOME must use the home partial directory")

    def test_usage_log_override_wins_over_xdg(self):
        state = self.path("unused-state")
        log = self.path("custom/log/usage.jsonl")
        env = dict(self.state_env(state), C_LEAN_USAGE_LOG=log)
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state, extra_env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.jsonl(log)), 1, "usage log override must win over XDG_STATE_HOME")
        self.assertFalse(os.path.exists(state), "the overridden default must not be created")

    def test_partial_override_wins_over_xdg(self):
        state = self.path("unused-state")
        partial = self.path("custom/saved/text")
        env = dict(self.state_env(state), C_LEAN_PARTIAL_DIR=partial,
                   C_LEAN_USAGE_LOG=self.log)
        p = run([CLI, "-m", "haiku", "hello"], mode="contfail", state=self.state, extra_env=env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(self.saved_blocks(partial), [CUT_PIECES],
                         "partial override must win over XDG_STATE_HOME")
        self.assertFalse(os.path.exists(state), "the overridden default must not be created")

    def test_usage_log_creates_private_parents(self):
        log = self.path("missing/state/log/usage.jsonl")
        mask = os.umask(0)
        try:
            B.log_usage({"prompt_sha256": B.sha256("p")}, path=log)
        finally:
            os.umask(mask)
        self.assertTrue(os.path.isfile(log), "usage log must be created")
        for parent in ("missing", "missing/state", "missing/state/log"):
            self.assertEqual(stat.S_IMODE(os.stat(self.path(parent)).st_mode), 0o700,
                             "usage log parent must have mode 0700")

    def test_usage_log_creates_private_file(self):
        mask = os.umask(0)
        try:
            B.log_usage({"prompt_sha256": B.sha256("p")}, path=self.log)
        finally:
            os.umask(mask)
        self.assertTrue(os.path.isfile(self.log), "usage log must be created")
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600,
                         "usage log file must have mode 0600")

    def test_usage_log_permissions_survive_restrictive_umask(self):
        mask = os.umask(0o777)
        try:
            B.log_usage({"prompt_sha256": B.sha256("p")}, path=self.log)
        finally:
            os.umask(mask)
        self.assertTrue(os.path.isfile(self.log), "usage log must be created")
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600,
                         "usage log file must retain mode 0600 under a restrictive umask")

    def test_pass_env_adds_requested_names(self):
        names = {"HTTPS_PROXY": "http://proxy.example:8080",
                 "NODE_EXTRA_CA_CERTS": self.path("ca.pem"), "EXTRA_CANARY": "wanted"}
        env = dict(self.env, C_LEAN_PASS_ENV=", ".join(names), **names)
        p = run([CLI, "-m", "haiku", "hello"], mode="envdump", state=self.state, extra_env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        child = json.load(open(os.path.join(self.state, "env.json")))
        for name, value in names.items():
            self.assertEqual(child.get(name), value, "pass-env must add requested " + name)
        self.assertIn("HTTPS_PROXY", p.stderr, "transport opt-in must be visible in the trailer")
        self.assertIn("NODE_EXTRA_CA_CERTS", p.stderr)

    def test_help_explains_prompt_hash_is_not_anonymization(self):
        p = run([CLI, "-h"], state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        help_text = " ".join(p.stdout.split())
        for phrase in ("sha256 hash of the prompt", "This is not anonymization:",
                       "anyone who holds a candidate prompt can confirm it against the hash"):
            self.assertIn(phrase, help_text, "help must explain prompt hash privacy: " + phrase)

    # --- neither the prompt nor the system prompt is on the command line
    def on_stdin(self, prompt, got=None):
        """The fake got `prompt` whole on stdin, and no argument holds it."""
        got = got or self.received()
        self.assertEqual(got.get("prompt_via"), "stdin",
                         "the prompt must reach the CLI on stdin, not on its command line")
        self.assertEqual((got["prompt_chars"], got["prompt_sha256"]),
                         (len(prompt), hashlib.sha256(prompt.encode("utf-8")).hexdigest()),
                         "the prompt must reach the CLI whole")
        self.assertEqual([a[:60] for a in got["argv"] if prompt[:200] in a], [],
                         "no argument of the command line may hold the prompt")

    def write(self, name, text):
        path = self.path(name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_a_prompt_that_begins_with_a_dash_reaches_the_cli_whole(self):
        """The prompt was the last argument of the command line, with no `--`
        before it, and the CLI read it as an option: `- item one` gave "error:
        unknown option" and exit 1, `--version` printed the version of the CLI
        instead of an answer."""
        for args, prompt in ((["- item one"], "- item one"), (["--", "--version"], "--version")):
            p = run([CLI, "-m", "haiku"] + args, mode="single", state=self.state,
                    extra_env=self.env)
            self.assertEqual((p.returncode, p.stdout), (0, "ECHO:%s\n" % prompt),
                             "%r: %s" % (prompt, p.stderr[-300:]))
            self.on_stdin(prompt)

    def test_a_prompt_too_long_for_one_argument_arrives_whole_on_stdin(self):
        """Prompts of up to 180,000 characters went on the command line, and
        Linux refuses any one argument of 131,072 bytes or more: exit 69,
        "Argument list too long". Any local user could read them there, too."""
        prompt = "".join("Item %06d: é — needs a verdict.\n" % n for n in range(6000))[:150000]
        self.assertEqual(len(prompt), 150000)
        self.assertGreater(len(prompt.encode("utf-8")), 131072)
        p = run([CLI, "-m", "haiku", "-f", self.write("long-prompt.txt", prompt)], mode="single",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, "the prompt must reach the CLI on stdin, whatever its "
                         "length: %s" % p.stderr[-300:])
        self.on_stdin(prompt)

    def test_a_nul_in_the_prompt_or_the_system_prompt_reaches_the_cli(self):
        """On the command line a NUL character ended the run in a ValueError
        traceback. stdin and a file carry it."""
        prompt, system = "before\0after", "rubric\0tail"
        p = run([CLI, "-m", "haiku", "--system-file", self.write("nul-rubric.md", system),
                 "-f", self.write("nul-prompt.txt", prompt)], mode="single", state=self.state,
                extra_env=self.env)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.on_stdin(prompt)
        self.assertEqual(self.received()["system_sha256"],
                         hashlib.sha256(system.encode("utf-8")).hexdigest())

    def test_a_big_system_prompt_goes_in_a_private_file_outside_the_cwd(self):
        """The system prompt was the value of --system-prompt: Linux refused it
        over 128 KiB, macOS over about 1 MiB, and the process list showed it to
        any local user. Now it is a file of mode 0600 outside the cwd of the
        call, which stays empty, and the file is gone when the call ends."""
        system = "".join("Rubric line %07d: é — keep to it.\n" % n for n in range(60000))
        self.assertGreater(len(system.encode("utf-8")), 2 << 20)
        # a temporary directory of the test's own: nothing may be left in the real one
        p = run([CLI, "-m", "haiku", "--system-file", self.write("rubric.md", system), "hello"],
                mode="single", state=self.state,
                extra_env=dict(self.env, TMPDIR=self.mktemp("c-lean-tmp-")))
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr[-300:])
        got = self.received()
        self.assertEqual(got["system_flags"], ["--system-prompt-file"],
                         "the system prompt must come in a file, not as --system-prompt")
        self.assertEqual([a[:60] for a in got["argv"] if system[:200] in a], [],
                         "no argument of the command line may hold the system prompt")
        self.assertEqual((got["system_chars"], got["system_sha256"]),
                         (len(system), hashlib.sha256(system.encode("utf-8")).hexdigest()),
                         "the system prompt must reach the CLI whole")
        self.assertEqual(got["system_file_mode"], 0o600,
                         "the file of the system prompt must be private: mode 0600")
        cwd, where = os.path.realpath(got["cwd"]), got["system_file_dir"]
        self.assertFalse(where == cwd or where.startswith(cwd + os.sep),
                         "the file of the system prompt must be outside the cwd of the call: "
                         "%s in %s" % (where, cwd))
        self.assertEqual(got["cwd_entries"], [], "the cwd of the call must stay empty")
        self.assertFalse(os.path.exists(got["system_file"]),
                         "the file of the system prompt must be removed when the call ends")
        self.on_stdin("hello", got)

    def test_the_system_file_is_removed_after_a_timeout(self):
        p = run([CLI, "-m", "haiku", "--system", "A rubric for a call that hangs.",
                 "--timeout", "2", "x"], mode="hang", state=self.state,
                extra_env=dict(self.env, TMPDIR=self.mktemp("c-lean-tmp-")))
        pids = os.path.join(self.state, "hang.pids")
        for pid in (open(pids).read().split() if os.path.exists(pids) else []):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(p.returncode, 124, p.stderr[-300:])
        got = self.received()
        self.assertEqual(got["system_flags"], ["--system-prompt-file"])
        self.assertFalse(os.path.exists(got["system_file"]),
                         "the file of the system prompt must be removed after a timeout")

    def test_the_system_file_is_removed_after_a_signal(self):
        self.env["TMPDIR"] = self.mktemp("c-lean-tmp-")
        proc = self.start_call([CLI, "-m", "haiku", "--system", "A rubric for a call that is "
                                "stopped.", "hello"], mode="sleeper")
        try:
            self.wait_for(lambda: len(self.sleeper_pids()) >= 1, what="the live child")
            got = self.received()
            self.assertTrue(os.path.exists(got["system_file"]),
                            "the file of the system prompt must be there while the call runs")
            os.kill(proc.pid, signal.SIGTERM)
            _o, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            self.kill_leftovers()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err[-300:])
        self.assertFalse(os.path.exists(got["system_file"]),
                         "the file of the system prompt must be removed after a stop signal")

    def test_a_schema_too_long_for_the_command_line_is_a_usage_error(self):
        """The schema is the one value of any size left on the command line,
        and one of 2 MiB is too long for it on Linux and on macOS alike. That
        is the operator's value, not a broken binary: exit 64, not 69."""
        schema = json.dumps({"type": "object", "properties": {"verdict": {"type": "string"}},
                             "description": "d" * (2 << 20)})
        tmp = self.mktemp("c-lean-tmp-")
        p = run([CLI, "-m", "haiku", "--system", "S", "--schema",
                 self.write("big-schema.json", schema), "hello"], mode="schema",
                state=self.state, extra_env=dict(self.env, TMPDIR=tmp))
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 64, "a schema too long for the command line must be exit "
                         "64: %s" % p.stderr[-400:])
        self.assertNotIn("cannot run the claude binary", p.stderr)
        self.assertIn("its command line is too long", p.stderr)
        self.assertIn("The biggest part of it is the JSON schema of --schema, %d bytes"
                      % len(schema), p.stderr)
        self.assertEqual(self.calls(), 0)
        self.assertEqual(os.listdir(tmp), [], "the run directory and the file of the system "
                         "prompt must be removed when the call cannot start")

    def test_a_nul_in_the_schema_is_a_usage_error(self):
        """A file given to --schema can hold a NUL character, and a command line
        cannot: exit 64 with a message, not a ValueError traceback."""
        tmp = self.mktemp("c-lean-tmp-")
        p = run([CLI, "-m", "haiku", "--system", "S", "--schema",
                 self.write("nul-schema.json", '{"type": "object", "description": "a\0b"}'),
                 "hello"], mode="schema", state=self.state, extra_env=dict(self.env, TMPDIR=tmp))
        self.assertNotIn("Traceback", p.stderr, "a NUL in the schema must not end in a traceback")
        self.assertEqual(p.returncode, 64, p.stderr[-300:])
        self.assertIn("the JSON schema of --schema holds a NUL character", p.stderr)
        self.assertEqual(self.calls(), 0)
        self.assertEqual(os.listdir(tmp), [])

    def test_a_missing_binary_is_still_69(self):
        """E2BIG is the one OSError of the start that comes from the operator's
        value; every other one is still 69, and nothing is left behind."""
        missing = self.path("no-such-claude")
        tmp = self.mktemp("c-lean-tmp-")
        p = run([CLI, "-m", "haiku", "--system", "S", "hello"], state=self.state,
                extra_env=dict(self.env, C_LEAN_CLAUDE_BIN=missing, TMPDIR=tmp))
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 69, "a missing binary must still be exit 69: %s"
                         % p.stderr[-300:])
        self.assertIn("cannot run the claude binary %s: No such file or directory" % missing,
                      p.stderr)
        self.assertEqual(os.listdir(tmp), [], "the run directory and the file of the system "
                         "prompt must be removed when the binary cannot be run")

    def test_a_cli_that_exits_before_it_reads_the_prompt_is_judged_by_its_exit_code(self):
        """A CLI too old for --system-prompt-file refuses the command line and
        exits 1 before it reads stdin, so a 2 MiB prompt meets a closed pipe
        (EPIPE). That is a plain failure in the CLI's own words."""
        p = run([CLI, "-m", "haiku", "--system", "S", "-f",
                 self.write("big-prompt.txt", "q" * (2 << 20))], mode="single",
                state=self.state, extra_env=dict(self.env, C_LEAN_FAKE_UNKNOWN="--system-prompt-file"))
        self.assertNotIn("Traceback", p.stderr, "a closed stdin pipe must not end in a traceback: "
                         "%s" % p.stderr[-500:])
        self.assertEqual(p.returncode, 1, p.stderr[-300:])
        self.assertIn("no JSON from claude (rc=1): error: unknown option '--system-prompt-file'",
                      p.stderr)
        argv = self.received()["argv"]
        self.assertFalse(os.path.exists(argv[argv.index("--system-prompt-file") + 1]))

    def test_the_fake_reads_its_command_line_as_the_cli_does(self):
        """The fake took its last argument for the prompt unless it began with
        "-": `--model haiku` made "haiku" the prompt of a call that came on
        stdin, and a prompt the CLI reads as an option went through. This one
        checks the FAKE, so that the tests above test what the CLI would do."""
        env = child_env("single", self.state, self.env)
        del env["C_LEAN_FAKE_ASSERT_ENV"]

        def fake(*argv, stdin=""):
            return subprocess.run([sys.executable, FAKE] + list(argv), input=stdin, env=env,
                                  capture_output=True, text=True, timeout=60)
        p = fake("-p", "--model", "haiku", "--output-format", "json", stdin="from stdin")
        self.assertEqual((p.returncode, json.loads(p.stdout or "{}").get("result")),
                         (0, "ECHO:from stdin"), p.stderr)
        self.assertEqual(self.received()["prompt_via"], "stdin")
        p = fake("-p", "--output-format=json", "--", "--version", stdin="never read")
        self.assertEqual(json.loads(p.stdout or "{}").get("result"), "ECHO:--version", p.stderr)
        self.assertEqual(self.received()["prompt_via"], "argv")
        for argv, said in ((["-p", "- item one"], "error: unknown option '- item one'\n"),
                           (["-p", "--verbose=yes"], "error: unknown option '--verbose=yes'\n"),
                           (["-p", "--model"], "error: option '--model <value>' argument missing\n")):
            p = fake(*argv, stdin="never read")
            self.assertEqual((p.returncode, p.stdout, p.stderr), (1, "", said),
                             "the fake must refuse %r as the CLI does" % argv)
        p = fake("-p", "--version", "--model", "haiku")
        self.assertEqual((p.returncode, p.stdout), (0, "0.0.0-fake (Claude Code)\n"))

    # --- the accounting: nothing in a usage document fails the call, a total
    # is exact only when every count in it is, and the trailer and the usage
    # log give the exit code of the process
    def trailer_line(self, p):
        lines = [l for l in p.stderr.splitlines() if l.startswith("[c-lean")]
        self.assertEqual(len(lines), 1, "one trailer is wanted: %r" % p.stderr[-300:])
        return lines[0]

    def trailer_exit(self, p):
        return int(re.search(r" · exit (\d+)", self.trailer_line(p)).group(1))

    def test_a_usage_document_that_holds_no_counts_cannot_fail_the_call(self):
        """`iterations` that was not a list raised a TypeError, and a count of
        1e999 (infinity) an OverflowError: exit 1, and the answer that had
        come was lost, with no usage-log line and no file. A value that is not
        a JSON integer of 0 or more is no count: the total is NOT COUNTED, and
        the answer is printed and its text saved as for any other call."""
        inf = "INFINITE-COUNT"  # written as 1e999 in the stream: JSON has no infinity
        shapes = [{"iterations": 5}, {"iterations": "many"},
                  {"iterations": {"input_tokens": 5, "output_tokens": 7}}, {"iterations": [5, None]},
                  {"input_tokens": inf, "output_tokens": 7}, {"input_tokens": 5, "output_tokens": True},
                  {"input_tokens": -5, "output_tokens": 7}, {"input_tokens": 5.5, "output_tokens": 7},
                  {"input_tokens": "5", "output_tokens": 7},
                  {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": inf},
                  {"input_tokens": 5, "output_tokens": 7, "cache_creation_input_tokens": -1},
                  {"input_tokens": 5, "output_tokens": 7,
                   "output_tokens_details": {"reasoning_tokens": inf}},
                  {"iterations": [{"input_tokens": inf, "output_tokens": 7}]}]
        for n, usage in enumerate(shapes, 1):
            shutil.rmtree(self.partial, ignore_errors=True)
            line = json.dumps(ev_result("FINAL two", usage=usage)).replace('"%s"' % inf, "1e999")
            # two messages with nothing between them are not joined: a file is saved
            p = self.script([ev_text("1", "DRAFT one"), ev_text("2", "FINAL two"), line])
            what = "usage %s: %s" % (json.dumps(usage), p.stderr[-300:])
            self.assertNotIn("Traceback", p.stderr, "a usage document must not fail the call: " + what)
            self.assertEqual((p.returncode, p.stdout), (0, "FINAL two\n"), what)
            self.assertIn("NOT COUNTED", self.trailer_line(p), "the trailer must say NOT COUNTED: "
                          + what)
            recs = self.jsonl(self.log)
            self.assertEqual(len(recs), n, "every call must leave its usage-log line: " + what)
            self.assertEqual((recs[-1]["exit"], recs[-1]["tokens_unknown"]), (0, True), what)
            for k in ("in", "cw", "cr", "out", "think"):
                v = recs[-1][k]
                self.assertTrue(type(v) is int and v >= 0, "%s must be a count: %r, %s" % (k, v, what))
            self.assertEqual(self.saved_blocks(), [["DRAFT one", "FINAL two"]],
                             "the text blocks must be saved as for any other answer: " + what)

    def test_a_total_over_messages_that_lack_a_counter_is_not_exact(self):
        """With no flat totals the counts are summed over `iterations`. When
        some entries lacked a counter, the others were summed and the sum was
        logged as exact. It is a lower bound: NOT COUNTED. A counter that no
        entry has is no gap (nothing was cached), and the sum stays exact."""
        both = {"input_tokens": 100, "output_tokens": 20}
        for entries, want, unknown in (
                ([both, {"input_tokens": 200}], (300, 20), True),
                ([both, {"output_tokens": 30}], (100, 50), True),
                ([dict(both, cache_read_input_tokens=50), both], (200, 40), True),
                ([dict(both, output_tokens_details={"reasoning_tokens": 5}), both], (200, 40), True),
                ([both, "an entry that is not a dict"], (100, 20), True),
                ([both, {"input_tokens": 200, "output_tokens": 30}], (300, 50), False)):
            p = self.script([ev_text("1", "ans"), ev_result("ans", usage={"iterations": entries})])
            what = "iterations %s: %s" % (json.dumps(entries), p.stderr[-300:])
            self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), what)
            rec = self.jsonl(self.log)[-1]
            self.assertEqual((rec["in"], rec["out"]), want, what)
            self.assertEqual(rec["tokens_unknown"], unknown, "a sum over messages that do not all "
                             "have the counter is a lower bound, not an exact total: " + what)
            self.assertEqual("NOT COUNTED" in self.trailer_line(p), unknown, what)

    def test_a_call_killed_by_a_signal_is_not_counted(self):
        """A call that died of a signal was logged as an exact zero, though the
        model may have spent tokens by then and no document says how many."""
        p = self.call("sigdeath")
        self.assertEqual(p.returncode, 137, p.stderr)
        trailer = self.trailer_line(p)
        self.assertIn("INTERRUPTED", trailer)
        self.assertIn("NOT COUNTED", trailer, "the trailer of a call killed by a signal must say "
                      "NOT COUNTED: %s" % trailer)
        rec = self.jsonl(self.log)[0]
        self.assertEqual((rec["exit"], rec["interrupted"], rec["tokens_unknown"]), (137, True, True),
                         "a call killed by a signal must not be logged as an exact zero")

    def test_the_trailer_and_the_log_give_the_exit_of_the_process(self):
        """An empty answer, and no structured_output under --schema, are
        decided after the call: the process exited 1 while the trailer and
        the usage-log line said exit 0."""
        for mode, args, want in (("emptysingle", (), 1), ("emptysingle", ("--json",), 1),
                                 ("single", ("--schema", self.SCHEMA), 1),
                                 ("emptysingle", ("--allow-empty",), 0), ("single", (), 0)):
            p = self.call(mode, *args)
            what = "%s %s: %s" % (mode, " ".join(args), p.stderr[-300:])
            self.assertEqual(p.returncode, want, what)
            self.assertEqual((self.trailer_exit(p), self.jsonl(self.log)[-1]["exit"]), (want, want),
                             "the trailer and the usage log must give the exit code of the "
                             "process: " + what)

    def test_check_gives_its_own_exit_in_the_trailer_and_the_log(self):
        """--check fails a probe whose call went through (counters that cannot
        be read, an empty answer) with exit 1: the trailer and the usage-log
        line said exit 0."""
        for mode, want in (("usagenone", 1), ("emptysingle", 1), ("usageinout", 0)):
            p = run([CLI, "--check"], mode=mode, state=self.state, extra_env=self.env)
            what = "%s: %s" % (mode, p.stderr[-300:])
            self.assertEqual(p.returncode, want, what)
            rec = self.jsonl(self.log)[-1]
            self.assertEqual((self.trailer_exit(p), rec["exit"], rec["tool"]),
                             (want, want, "c-lean --check"), "the trailer and the usage log of "
                             "--check must give the exit code of the process: " + what)

    # --- a CLI that does not exit after its `result` event
    def lingered(self, how):
        return ("c-lean: WARN the claude CLI printed its `result` event and had not exited %s: it "
                "was killed with its process tree, and the call is judged by that event" % how)

    def test_a_cli_that_does_not_exit_after_its_result_still_gives_the_answer(self):
        """The CLI printed its whole `result` event and then did not exit: the
        call ran into the timeout, exit 124, NOT COUNTED, and the answer that
        had come was lost. Now the CLI has RESULT_GRACE seconds to exit; then
        it is killed with its process tree, one WARN says so, and the call is
        judged by that event as if the CLI had exited."""
        t0 = time.monotonic()
        p = self.call("linger", "--timeout", "40")
        took = time.monotonic() - t0
        pids = os.path.join(self.state, "hang.pids")
        pids = open(pids).read().split() if os.path.exists(pids) else []
        try:
            self.wait_for(lambda: not any(self.alive(pid) for pid in pids), secs=10,
                          what="the CLI and its child to be killed")
        finally:
            for pid in pids:
                if self.alive(pid):
                    os.kill(int(pid), signal.SIGKILL)
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), "the answer of a CLI that "
                         "did not exit after its `result` must be printed: %s" % p.stderr[-300:])
        self.assertLess(took, 25, "the CLI must have %ss to exit after its `result`, not the whole "
                        "timeout: the call took %.1fs" % (B.RESULT_GRACE, took))
        self.assertEqual(self.warns(p), [self.lingered("%ss later" % B.RESULT_GRACE)],
                         "one WARN must say that the CLI was killed after its `result`")
        rec = self.jsonl(self.log)[0]
        self.assertEqual((rec["exit"], rec["tokens_unknown"], rec["in"], rec["out"]),
                         (0, False, 575, 40), "the call is judged by its `result` event")
        self.assertNotIn("NOT COUNTED", self.trailer_line(p))

    def test_a_timeout_after_the_result_event_still_gives_the_answer(self):
        """The timeout ran out after the CLI had printed its whole `result`
        event, before the CLI's time to exit was up. The answer is in hand:
        the call is judged by that event, not reported as exit 124."""
        p = self.script([ev_text("1", "ans"), ev_result("ans")], "--timeout", "3", end="hang")
        self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), "a call whose `result` event had "
                         "come must not be lost to the timeout: %s" % p.stderr[-300:])
        self.assertEqual(self.warns(p), [self.lingered("when the timeout of 3s ran out")])
        self.assertNotIn("timeout after", p.stderr)
        rec = self.jsonl(self.log)[0]
        self.assertEqual((rec["exit"], rec["tokens_unknown"], rec["in"], rec["out"]), (0, False, 5, 7))

    def test_the_pipes_of_the_child_are_utf8_whatever_the_locale(self):
        """The pipes to the child were opened in the encoding of the locale,
        and the CLI reads and writes UTF-8: under a locale that is not UTF-8
        a prompt with "é" reached it as "caf\\xe9", and what it printed was
        decoded in the locale too. Here the locale is C with Python's coercion
        and UTF-8 mode off, i.e. ASCII; the tool's own stdin and stdout stay
        UTF-8."""
        env = dict(self.env, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0",
                   PYTHONIOENCODING="utf-8")
        probe = subprocess.run([sys.executable, "-c", "import locale; "
                                "print(locale.getpreferredencoding(False))"], capture_output=True,
                               text=True, timeout=60, env=child_env("single", self.state, env))
        if probe.stdout.strip().lower().replace("-", "") in ("utf8", ""):
            self.skipTest("the C locale of this system is UTF-8: %r" % probe.stdout)
        prompt = "café — Привет"
        p = run([CLI, "-m", "haiku"], mode="single", state=self.state, extra_env=env, stdin=prompt)
        self.assertEqual(self.received()["prompt_sha256"],
                         hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                         "the prompt must reach the CLI as UTF-8 whatever the locale")
        # read in the locale, UTF-8 in a JSON string becomes escapes JSON does
        # not have, and no event parses: exit 1
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:%s\n" % prompt),
                         "what the CLI prints must be read as UTF-8 whatever the locale: %s"
                         % p.stderr[-300:])

    def test_help_describes_the_accounting_and_a_cli_that_does_not_exit(self):
        h = " ".join(run([CLI, "-h"], state=self.state, extra_env=self.env).stdout.split())
        for phrase in ("NOT COUNTED in the trailer, and tokens_unknown true in the usage log, mean "
                       "that the numbers are not exact: read them as a lower bound",
                       "a count that is not a whole number of 0 or more",
                       "an entry lacks the input or the output count, or an entry lacks a count "
                       "that another entry has",
                       "for every call the timeout or a signal killed",
                       "Nothing in the usage document fails a call",
                       "the exit code in the trailer and in the usage-log line is the exit code "
                       "of the process",
                       "If it has not exited 5 s after that, it is killed with its process tree",
                       "judged by that event as if the CLI had exited",
                       "124 timeout, before the CLI printed its `result` event",
                       "The usage-log line still gives the exit code c-lean had decided, not 120",
                       "The prompt goes to the CLI as UTF-8"):
            self.assertIn(phrase, h, "c-lean -h must say: %s" % phrase)

    def test_linux_proc_walk_parses_comm_and_never_runs_pgrep(self):
        procdir = self.mktemp("c-lean-proc-")
        for pid, parent in ((101, 1), (102, 101), (103, 102)):
            os.mkdir(os.path.join(procdir, str(pid)))
            with open(os.path.join(procdir, str(pid), "stat"), "w") as f:
                f.write("%d (a name (with) parentheses)) S %d 0 0\n" % (pid, parent))
        scan = os.scandir
        with mock.patch.object(B.sys, "platform", "linux"), \
                mock.patch.object(B.os, "scandir", lambda path: scan(procdir)), \
                mock.patch.object(B.subprocess, "run") as run_pgrep:
            self.assertEqual(B._descendants(101), [102, 103],
                             "/proc must parse ppid after the last closing parenthesis")
            self.assertEqual(run_pgrep.call_count, 0,
                             "Linux discovery must not start a subprocess")

    def test_an_unreadable_proc_warns_once_and_still_kills_the_child(self):
        import io
        err = io.StringIO()
        proc = mock.Mock(pid=101, c_lean_own_session=False, c_lean_killed_at=None)
        with mock.patch.object(B.sys, "platform", "linux"), \
                mock.patch.object(B.os, "scandir", side_effect=PermissionError("unreadable")), \
                mock.patch.object(B, "_TREE_WARNED", False), \
                mock.patch.object(B.sys, "stderr", err):
            B._kill_tree(proc)
            B._kill_tree(proc)
        self.assertEqual(proc.kill.call_count, 2, "unreadable /proc must still kill the child")
        self.assertEqual(err.getvalue().count("WARN could not list the processes of the call"), 1,
                         "unreadable /proc must warn once per run")
        self.assertIn("some may still run", err.getvalue())

    def test_pgrep_failure_warns_and_uses_the_absolute_fallback(self):
        import io
        pgrep = self.path("pgrep")
        with open(pgrep, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(pgrep, 0o700)
        real_run = subprocess.run
        commands = []

        def run_pgrep(cmd, **kw):
            commands.append(cmd)
            return real_run([pgrep] + cmd[1:], **kw)
        err = io.StringIO()
        proc = mock.Mock(pid=101, c_lean_own_session=False, c_lean_killed_at=None)
        with mock.patch.object(B.sys, "platform", "darwin"), \
                mock.patch.object(B.shutil, "which", return_value=None), \
                mock.patch.object(B.subprocess, "run", run_pgrep), \
                mock.patch.object(B, "_TREE_WARNED", False), \
                mock.patch.object(B.sys, "stderr", err):
            B._kill_tree(proc)
            B._kill_tree(proc)
        self.assertEqual(commands[0], ["/usr/bin/pgrep", "-P", "101"],
                         "pgrep missing from PATH must fall back to /usr/bin/pgrep")
        self.assertEqual(proc.kill.call_count, 2, "pgrep failure must still kill the child")
        self.assertEqual(err.getvalue().count("WARN could not list the processes of the call"), 1,
                         "pgrep failure must warn once per run")
        self.assertIn("pgrep exited 3", err.getvalue())

    def test_a_tree_warning_on_a_stderr_that_cannot_be_written_never_raises(self):
        """The WARN of a failed process listing is printed from the signal
        handler (interrupt -> _kill_tree -> _descendants). On a stderr that can
        no longer be written it raised out of the handler, and the run ended
        with exit 120 and no usage-log line."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        # line-buffered like sys.stderr: the write itself meets EPIPE
        no_reader = os.fdopen(write_end, "w", buffering=1)
        closed = open(os.devnull, "w")
        closed.close()

        class Reentrant:
            """A write that a signal handler starts in the middle of another one."""
            def write(self, text):
                raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")

            def flush(self):
                pass
        try:
            for name, stream in (("a pipe with no reader", no_reader), ("a closed file", closed),
                                 ("a write under way", Reentrant())):
                with self.subTest(stderr=name):
                    proc = mock.Mock(pid=101, c_lean_own_session=False, c_lean_killed_at=None)
                    with mock.patch.object(B.sys, "platform", "linux"), \
                            mock.patch.object(B.os, "scandir",
                                              side_effect=PermissionError("unreadable")), \
                            mock.patch.object(B, "_TREE_WARNED", False), \
                            mock.patch.object(B.sys, "stderr", stream):
                        try:
                            B._kill_tree(proc)
                        except (OSError, ValueError, RuntimeError) as e:
                            self.fail("a WARN on a stderr that cannot be written must not raise "
                                      "(%s): %r" % (name, e))
                    self.assertEqual(proc.kill.call_count, 1,
                                     "the child must be killed whatever stderr does")
        finally:
            try:
                no_reader.close()
            except OSError:
                pass

    def proc_listing(self, stats):
        """A /proc of these (name, bytes of stat) entries, listed in this order;
        bytes None is an entry whose stat cannot be opened (a directory)."""
        procdir = self.mktemp("c-lean-proc-")
        for name, data in stats:
            os.mkdir(os.path.join(procdir, name))
            if data is None:
                os.mkdir(os.path.join(procdir, name, "stat"))
            else:
                with open(os.path.join(procdir, name, "stat"), "wb") as f:
                    f.write(data)
        found = {e.name: e for e in os.scandir(procdir)}
        entries = [found[name] for name, _data in stats]
        return lambda path: contextlib.nullcontext(entries)

    def test_a_proc_entry_that_cannot_be_read_costs_that_entry_alone(self):
        """A process that exits between open() and read() of its stat gives an
        empty or cut read. That raised IndexError, which the per-entry handler
        did not catch: a WARN for a listing that worked, and every entry after
        it was lost, so their processes were not killed."""
        listing = self.proc_listing([
            ("999", b""),                       # exited before the read
            ("998", b"998 (cut sh"),            # cut inside comm
            ("997", b"997 (x) S"),              # cut before ppid
            ("996", b"996 (x) S nan 0 0\n"),    # no number where ppid stands
            ("995", None),                      # stat cannot be opened
            ("101", b"101 (fake) S 1 0 0\n"),
            ("102", b"102 (sleep) S 101 0 0\n")])
        import io
        err = io.StringIO()
        with mock.patch.object(B.sys, "platform", "linux"), \
                mock.patch.object(B.os, "scandir", listing), \
                mock.patch.object(B, "_TREE_WARNED", False), \
                mock.patch.object(B.sys, "stderr", err):
            found = B._descendants(101)
        self.assertEqual(found, [102], "an entry of /proc that cannot be read must cost that "
                         "entry alone, not the rest of the snapshot")
        self.assertEqual(err.getvalue(), "", "a /proc that could be listed needs no WARN")

    def test_a_proc_stat_in_no_encoding_is_still_parsed(self):
        """comm holds whatever bytes a process set. The stat file was read in
        the encoding of the locale: one such process anywhere on the machine
        raised UnicodeDecodeError, every snapshot got a WARN and lost the
        entries after it. Here the locale is UTF-8 for those reads, and the
        processes under the call have such names too: skipping them would lose
        their children."""
        listing = self.proc_listing([
            ("100", b"100 (caf\xe9) S 1 0 0\n"),          # elsewhere on the machine
            ("101", b"101 (fake) S 1 0 0\n"),
            ("102", b"102 (\xff\xfe) x)) S 101 0 0\n"),   # under the call
            ("103", b"103 (sleep) S 102 0 0\n")])
        real_open = open

        def utf8_locale(path, mode="r", *args, **kw):
            if "b" not in mode:
                kw.setdefault("encoding", "utf-8")
            return real_open(path, mode, *args, **kw)
        import io
        err = io.StringIO()
        with mock.patch.object(B.sys, "platform", "linux"), \
                mock.patch.object(B.os, "scandir", listing), \
                mock.patch.object(B, "open", utf8_locale, create=True), \
                mock.patch.object(B, "_TREE_WARNED", False), \
                mock.patch.object(B.sys, "stderr", err):
            found = B._descendants(101)
        self.assertEqual(found, [102, 103], "a stat whose comm is in no encoding must still give "
                         "its ppid")
        self.assertEqual(err.getvalue(), "", "a /proc that could be listed needs no WARN")

    def pgrep_walk(self, failing):
        """_descendants(101) off /proc, over the tree 101 -> 102, 103 and
        103 -> 104, with pgrep -P 102 ending in `failing`: (found, parents
        asked, what was printed on stderr)."""
        tree = {"101": "102\n103\n", "102": "", "103": "104\n", "104": ""}
        asked = []

        def run_pgrep(cmd, **kw):
            asked.append(cmd[2])
            if cmd[2] == "102":
                if isinstance(failing, BaseException):
                    raise failing
                return subprocess.CompletedProcess(cmd, failing, "", "")
            out = tree[cmd[2]]
            return subprocess.CompletedProcess(cmd, 0 if out else 1, out, "")
        import io
        err = io.StringIO()
        with mock.patch.object(B.sys, "platform", "darwin"), \
                mock.patch.object(B.shutil, "which", return_value="/usr/bin/pgrep"), \
                mock.patch.object(B.subprocess, "run", run_pgrep), \
                mock.patch.object(B, "_TREE_WARNED", False), \
                mock.patch.object(B.sys, "stderr", err):
            found = B._descendants(101)
        return found, asked, err.getvalue()

    def test_one_failed_pgrep_costs_the_subtree_of_its_parent_alone(self):
        """One pgrep -P that failed ended the whole walk: the subtrees of the
        parents still in the queue were never asked for and were left alive."""
        for name, failing in (("exit 3", 3), ("a signal", -9),
                              ("an OSError", OSError("Resource temporarily unavailable"))):
            with self.subTest(failure=name):
                found, asked, err = self.pgrep_walk(failing)
                self.assertEqual(found, [102, 103, 104], "a failed pgrep for one parent must "
                                 "not cost the subtrees of the parents after it")
                self.assertEqual(asked, ["101", "102", "103", "104"])
                self.assertEqual(err.count("WARN could not list the processes of the call"), 1,
                                 "a failed pgrep must warn once per run")

    def test_a_pgrep_that_times_out_ends_the_walk_with_what_was_found(self):
        """Each pgrep may take up to its timeout: after one that ran out, the
        walk stops, so that one kill waits for at most one timeout."""
        found, asked, err = self.pgrep_walk(subprocess.TimeoutExpired(["pgrep"], 5))
        self.assertEqual(asked, ["101", "102"], "after a pgrep that timed out no other may run")
        self.assertEqual(found, [102, 103], "what was found before the timeout must be kept")
        self.assertEqual(err.count("WARN could not list the processes of the call"), 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc discovery")
    def test_linux_timeout_kills_the_tree_without_pgrep(self):
        bindir = self.mktemp("c-lean-no-pgrep-")
        os.symlink(sys.executable, os.path.join(bindir, "python3"))
        # child_env can prepend the discovery fixture; this test needs an empty PATH.
        env = child_env("hang", self.state, self.env)
        env["PATH"] = bindir
        p = subprocess.run([sys.executable, CLI, "-m", "haiku", "--timeout", "2", "x"],
                           env=env, capture_output=True, text=True, timeout=15)
        pids = open(os.path.join(self.state, "hang.pids")).read().split()
        try:
            self.wait_for(lambda: not any(self.alive(pid) for pid in pids), secs=2,
                          what="/proc to kill the grandchild without pgrep")
        finally:
            for pid in pids:
                if self.alive(pid):
                    os.kill(int(pid), signal.SIGKILL)
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertNotIn("could not list", p.stderr)

    def test_killing_an_isolated_call_also_kills_its_group(self):
        events = []
        proc = mock.Mock(pid=101, c_lean_own_session=True, c_lean_killed_at=None)
        proc.kill.side_effect = lambda: events.append("child")
        with mock.patch.object(B, "_descendants", side_effect=lambda pid: events.append("list") or [102, 103]), \
                mock.patch.object(B.os, "kill", side_effect=lambda pid, sig: events.append(pid)), \
                mock.patch.object(B.os, "killpg", side_effect=lambda pid, sig: events.append((pid, sig))):
            B._kill_tree(proc)
        self.assertEqual(events, ["list", 103, 102, (101, signal.SIGKILL), "child"],
                         "collect the whole tree first, then kill descendants, group and child")

    def test_the_default_call_stays_in_the_job(self):
        proc = self.start_call([CLI, "-m", "haiku", "hello"], "cli", new_session=True)
        try:
            self.wait_for(lambda: self.sleeper_pids(), what="the call in the job")
            child = int(self.sleeper_pids()[0][0])
            self.assertEqual(os.getpgid(child), os.getpgid(proc.pid),
                             "with no inherited ignore the child must stay in the job")
        finally:
            os.kill(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=10)
            self.kill_leftovers()

    def test_each_inherited_ignore_survives_a_signal_to_the_job(self):
        for sig in STOP_SIGNALS:
            with self.subTest(signal=sig):
                self.state = self.mktemp("c-lean-ignored-")
                def inherited_ignore():
                    default_sighup()
                    signal.signal(sig, signal.SIG_IGN)
                env = child_env("cli", self.state, dict(self.env, C_LEAN_FAKE_SLEEP="1"))
                proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", "hello"],
                                        env=env, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True,
                                        preexec_fn=inherited_ignore)
                try:
                    self.wait_for(lambda: self.sleeper_pids(), what="the isolated call")
                    os.killpg(proc.pid, sig)
                    out, err = proc.communicate(timeout=10)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                    self.kill_leftovers()
                self.assertEqual((proc.returncode, out), (0, "ECHO:hello\n"),
                                 "each inherited ignore must survive a signal to the job: " + err)

    def test_cli_stops_have_the_caught_signal_exit_for_pid_and_job(self):
        for sig in STOP_SIGNALS:
            for whole_job in (False, True):
                with self.subTest(signal=sig, whole_job=whole_job):
                    self.state = self.mktemp("c-lean-stop-")
                    proc = self.start_call([CLI, "-m", "haiku", "hello"], "cli", new_session=True)
                    try:
                        self.wait_for(lambda: self.sleeper_pids(), what="the CLI handlers")
                        (os.killpg if whole_job else os.kill)(proc.pid, sig)
                        out, err = proc.communicate(timeout=10)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.communicate()
                        self.kill_leftovers()
                    self.assertEqual(proc.returncode, 128 + sig,
                                     "a caught stop must exit 128+signal regardless of the CLI: " + err)
                    self.assertEqual(out, "")
                    self.assertIn("INTERRUPTED", err)
                    self.assertIn("NOT COUNTED", err)
                    rec = self.jsonl(self.log)[-1]
                    self.assertEqual((rec["exit"], rec["interrupted"], rec["tokens_unknown"]),
                                     (128 + sig, True, True), "a caught stop must not count tokens")

    def test_a_stop_keeps_its_exit_when_the_cli_finishes_first(self):
        site = self.mktemp("c-lean-stop-race-")
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write("import sys, time\nsys.path.insert(0, %r)\nimport c_lean_core as B\n"
                    "real = B._kill_tree\n"
                    "def delayed(proc):\n    time.sleep(0.3)\n    real(proc)\n"
                    "B._kill_tree = delayed\n" % BIN)
        for sig in STOP_SIGNALS:
            self.state = self.mktemp("c-lean-cli-first-")
            env = child_env("cli", self.state, dict(self.env, PYTHONPATH=site, C_LEAN_FAKE_SLEEP="20"))
            proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", "hello"], env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, preexec_fn=default_sighup)
            try:
                self.wait_for(lambda: self.sleeper_pids(), what="the CLI signal handlers")
                os.killpg(proc.pid, sig)
                out, err = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                self.kill_leftovers()
            self.assertEqual(proc.returncode, 128 + sig,
                             "the caught stop must win even when the CLI finishes first: " + err)
            self.assertIn("INTERRUPTED", err)
            self.assertIn("NOT COUNTED", err)
            self.assertEqual(out, "")

    def test_a_good_result_stands_when_a_stop_arrives(self):
        for sig in STOP_SIGNALS:
            self.state = self.mktemp("c-lean-answer-stop-")
            proc = self.start_call([CLI, "-m", "haiku", "hello"], "linger", new_session=True)
            try:
                self.wait_for(lambda: os.path.exists(os.path.join(self.state, "hang.pids")),
                              what="the result already printed")
                os.kill(proc.pid, sig)
                out, err = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                pids = os.path.join(self.state, "hang.pids")
                for pid in open(pids).read().split() if os.path.exists(pids) else []:
                    if self.alive(pid):
                        os.kill(int(pid), signal.SIGKILL)
            self.assertEqual((proc.returncode, out), (0, "ECHO:hello\n"),
                             "a good result before a stop must stand: " + err)
            self.assertIn("came after the answer", err, "a stop after the answer needs a WARN")
            self.assertIn("the call was killed with its process tree", err,
                          "a CLI that c-lean killed after its answer must be said to be killed")
            self.assertNotIn("INTERRUPTED", err)
            self.assertFalse(self.jsonl(self.log)[-1]["tokens_unknown"])

    def site(self, body):
        """A directory for PYTHONPATH: its sitecustomize.py imports the core
        under test as B, then runs `body` in the process of c-lean."""
        site = self.mktemp("c-lean-site-")
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write("import os, signal, sys\nsys.path.insert(0, %r)\nimport c_lean_core as B\n%s"
                    % (BIN, body))
        return site

    def test_a_stop_after_the_cli_exited_does_not_say_it_was_killed(self):
        """A stop after a good `result` lets the answer stand. Its WARN said
        that the call was killed with its process tree even when the CLI had
        exited by itself and nothing was killed. Here the stop comes right
        after the read loop, from a CLI that exited by itself."""
        site = self.site("real = B._communicate\n"
                         "def read_then_stop(proc, timeout):\n"
                         "    got = real(proc, timeout)\n"
                         "    os.kill(os.getpid(), int(os.environ['STOP_WITH']))\n"
                         "    return got\n"
                         "B._communicate = read_then_stop\n")
        for sig in STOP_SIGNALS:
            with self.subTest(signal=sig):
                env = child_env("single", self.state,
                                dict(self.env, PYTHONPATH=site, STOP_WITH=str(int(sig))))
                p = subprocess.run([sys.executable, CLI, "-m", "haiku", "hello"], env=env,
                                   capture_output=True, text=True, timeout=60,
                                   preexec_fn=default_sighup)
                self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr)
                self.assertEqual(self.warns(p), ["c-lean: WARN the stop (signal %d) came after the "
                                                 "answer, and the answer stands" % sig],
                                 "a stop after a CLI that exited by itself must not say that "
                                 "the call was killed")

    def test_a_stop_interrupts_prompt_reads_without_eof(self):
        # Mark handler installation; stdin is kept open throughout communicate().
        site = self.mktemp("c-lean-prompt-probe-")
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write("import sys, os\nsys.path.insert(0, %r)\nimport c_lean_core as B\n"
                    "real = B.install_signal_handlers\n"
                    "def install():\n    real()\n    open(os.environ['PROMPT_READY'], 'w').close()\n"
                    "B.install_signal_handlers = install\n" % BIN)
        for source in ("stdin", "dash", "fifo", "fifo-open"):
            for sig in STOP_SIGNALS:
                with self.subTest(source=source, signal=sig):
                    ready = self.path("ready-%s-%d" % (source, sig))
                    read_end, write_end = os.pipe()
                    fifo_fd = None
                    args = []
                    if source.startswith("fifo"):
                        fifo = self.path("fifo-%s-%d" % (source, sig))
                        os.mkfifo(fifo)
                        if source == "fifo":
                            fifo_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
                            os.write(fifo_fd, b"partial prompt")
                        args = ["-f", fifo]
                    elif source == "dash":
                        args = ["-f", "-"]
                    env = child_env("ok", self.state, dict(self.env, PYTHONPATH=site, PROMPT_READY=ready))
                    proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku"] + args, env=env,
                                            stdin=read_end, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, preexec_fn=default_sighup)
                    os.close(read_end)
                    os.write(write_end, b"partial prompt")
                    try:
                        self.wait_for(lambda: os.path.exists(ready), what="prompt handlers")
                        time.sleep(0.05)
                        t0 = time.monotonic()
                        os.kill(proc.pid, sig)
                        try:
                            out, err = proc.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.fail("a stop must interrupt a prompt read without EOF within 2s")
                        self.assertLess(time.monotonic() - t0, 2)
                    finally:
                        os.close(write_end)
                        if fifo_fd is not None:
                            os.close(fifo_fd)
                        if proc.poll() is None:
                            proc.kill()
                            proc.communicate()
                    self.assertEqual(proc.returncode, 128 + sig, "prompt stop must exit 128+signal: " + err)
                    self.assertIn("stopped before the call (signal %d)" % sig, err)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("INTERRUPTED", err)
                    self.assertEqual(out, "")
                    rec = self.jsonl(self.log)[-1]
                    self.assertEqual((rec["exit"], rec["interrupted"]), (128 + sig, True))
                    self.assertEqual(self.calls(), 0)

    def test_a_stop_ends_a_read_of_the_system_file_or_the_schema(self):
        """--system-file and --schema were read after the prompt, outside the
        reads that a stop ends at once. A stop while c-lean waited on a FIFO
        given to either (`--system-file <(slow command)`) was recorded, and the
        read went on: with no writer c-lean waited until SIGKILL."""
        site = self.site("real = B.install_signal_handlers\n"
                         "def install():\n"
                         "    real()\n"
                         "    open(os.environ['PROMPT_READY'], 'w').close()\n"
                         "B.install_signal_handlers = install\n")
        for flag in ("--system-file", "--schema"):
            with self.subTest(flag=flag):
                fifo, ready = self.path("fifo" + flag), self.path("ready" + flag)
                os.mkfifo(fifo)  # nobody ever writes it
                env = child_env("ok", self.state, dict(self.env, PYTHONPATH=site, PROMPT_READY=ready))
                proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", flag, fifo, "hello"],
                                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, preexec_fn=default_sighup)
                try:
                    self.wait_for(lambda: os.path.exists(ready), what="the signal handlers")
                    # the prompt is an argument: c-lean now waits on the FIFO
                    time.sleep(0.3)
                    os.kill(proc.pid, signal.SIGINT)
                    try:
                        out, err = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.fail("a stop must end a read of %s on a FIFO with no writer at once"
                                  % flag)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                self.assertEqual(proc.returncode, 130, "a stop during the read of %s must exit "
                                 "128+signal: %s" % (flag, err))
                self.assertIn("stopped before the call (signal 2)", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")
                rec = self.jsonl(self.log)[-1]
                self.assertEqual((rec["exit"], rec["interrupted"]), (130, True))
                self.assertEqual(self.calls(), 0)

    def pipeholder(self, ending, sig=None, timeout="40", rc=0):
        env = child_env("pipeholder", self.state,
                        dict(self.env, C_LEAN_FAKE_HOLDER_END=ending, C_LEAN_FAKE_RC=str(rc)))
        proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", "--timeout", timeout, "hello"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, preexec_fn=default_sighup)
        t0 = time.monotonic()
        try:
            self.wait_for(lambda: os.path.exists(os.path.join(self.state, "orphan.pid")),
                          what="the orphan holding both pipes")
            if sig:
                self.wait_for(lambda: self.sleeper_pids(), what="the call with a pipe holder")
                os.kill(proc.pid, sig)
            try:
                out, err = proc.communicate(timeout=B.RESULT_GRACE + B.KILL_DRAIN + 5)
            except subprocess.TimeoutExpired:
                self.fail("a surviving pipe holder must not wait for the call timeout")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            path = os.path.join(self.state, "orphan.pid")
            if os.path.exists(path) and self.alive(open(path).read()):
                os.kill(int(open(path).read()), signal.SIGKILL)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), time.monotonic() - t0

    def test_an_exited_cli_with_a_result_and_open_pipes_keeps_the_answer(self):
        p, took = self.pipeholder("answer")
        self.assertLess(took, B.RESULT_GRACE + B.KILL_DRAIN + 5,
                        "open pipes after exit must have a bounded wait")
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr)
        self.assertIn("CLI exited but a process it started still held its output open 5s later", p.stderr)
        self.assertIn("its output open after 3s; it may still run", p.stderr)

    def test_an_exited_cli_without_a_result_and_open_pipes_fails(self):
        p, took = self.pipeholder("exit")
        self.assertLess(took, B.RESULT_GRACE + B.KILL_DRAIN + 5)
        self.assertEqual(p.returncode, 1, "exited CLI without a result must fail, not time out: " + p.stderr)
        self.assertIn("ended with no `result` event", p.stderr)
        self.assertIn("rc=0", p.stderr)
        self.assertIn("CLI exited but a process it started still held its output open 5s later", p.stderr)
        self.assertIn("may still run", p.stderr)

    def test_open_pipes_do_not_replace_the_childs_own_failure_exit(self):
        p, took = self.pipeholder("answer", rc=1)
        self.assertEqual(p.returncode, 1, "open pipes must preserve the child's own failure exit: " + p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("claude reported failure (rc=1", p.stderr)
        self.assertIn("CLI exited but a process it started still held its output open 5s later", p.stderr)

    def test_a_stop_with_a_surviving_pipe_holder_is_bounded(self):
        for sig in STOP_SIGNALS:
            self.state = self.mktemp("c-lean-stop-holder-")
            p, took = self.pipeholder("hang", sig)
            self.assertLess(took, B.KILL_DRAIN + 2, "a stop must drain for at most KILL_DRAIN")
            self.assertEqual(p.returncode, 128 + sig, "a stop with open pipes must keep its exit: " + p.stderr)
            self.assertIn("INTERRUPTED", p.stderr)
            self.assertIn("its output open after 3s; it may still run", p.stderr)

    def test_a_timeout_with_a_surviving_pipe_holder_is_bounded(self):
        p, took = self.pipeholder("hang", timeout="2")
        self.assertLess(took, 2 + B.KILL_DRAIN + 2, "a timeout must drain for at most KILL_DRAIN")
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertIn("its output open after 3s; it may still run", p.stderr)

    def kill_recorded(self):
        """Kill every process the fake recorded that is still alive."""
        pids = []
        for name in ("hang.pids", "pids.log", "orphan.pid"):
            path = os.path.join(self.state, name)
            if os.path.exists(path):
                with open(path) as f:
                    pids += f.read().split()
        for pid in pids:
            if self.alive(pid):
                os.kill(int(pid), signal.SIGKILL)

    def broken_stderr(self):
        """The write end of a pipe whose reader has exited: every write meets EPIPE."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        return write_end

    def test_the_drain_warning_on_a_broken_stderr_keeps_the_usage_line(self):
        """The WARN about a process that still holds the output after a kill
        is printed while the call runs. On a stderr that could no longer be
        written it raised out of the read loop: exit 120 and no usage-log
        line, where -h says that line still gives the exit code c-lean decided."""
        env = child_env("pipeholder", self.state, dict(self.env, C_LEAN_FAKE_HOLDER_END="hang"))
        stderr = self.broken_stderr()
        try:
            subprocess.run([sys.executable, CLI, "-m", "haiku", "--timeout", "2", "hello"],
                           env=env, stdout=subprocess.PIPE, stderr=stderr, text=True, timeout=60,
                           preexec_fn=default_sighup)
        finally:
            os.close(stderr)
            self.kill_recorded()
        self.assertTrue(os.path.exists(os.path.join(self.state, "orphan.pid")),
                        "the orphan must have held the output through the drain")
        self.assertEqual([r["exit"] for r in self.jsonl(self.log)], [124],
                         "the drain WARN on a broken stderr must not cost the usage-log line")

    def test_a_stop_whose_tree_warning_cannot_be_written_keeps_the_usage_line(self):
        """A stop kills the call from the signal handler, and a process listing
        that fails prints its WARN from there. On a stderr that could no longer
        be written that WARN raised out of the handler: exit 120 and no
        usage-log line. Here the listing is made to fail by the test."""
        site = self.site("real = B._descendants\n"
                         "def failing(pid):\n"
                         "    B._tree_warning('made to fail by the test')\n"
                         "    return real(pid)\n"
                         "B._descendants = failing\n")
        env = child_env("hang", self.state, dict(self.env, PYTHONPATH=site))
        stderr = self.broken_stderr()
        proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", "--timeout", "60", "hello"],
                                env=env, stdout=subprocess.PIPE, stderr=stderr, text=True,
                                preexec_fn=default_sighup)
        os.close(stderr)
        try:
            self.wait_for(lambda: os.path.exists(os.path.join(self.state, "hang.pids")),
                          what="the call")
            os.kill(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            self.kill_recorded()
        self.assertEqual([(r["exit"], r["interrupted"]) for r in self.jsonl(self.log)],
                         [(143, True)], "the tree WARN on a broken stderr must not cost the "
                         "usage-log line of the stop")

    def test_a_stop_during_the_drain_after_a_kill_keeps_no_warn_of_that_kill(self):
        """A stop that lands while c-lean drains the output after a kill of its
        own ends the call with 128+N, INTERRUPTED. The WARN of that kill stayed
        and said what was no longer true: that the call was judged by its
        `result` event (after a timeout with no `result` at all), or by what
        the CLI printed and its own exit code. Here the stop is sent right
        after the first kill of the call, i.e. at the start of its drain."""
        site = self.site("real = B._kill_tree\n"
                         "def kill_then_stop(proc):\n"
                         "    real(proc)\n"
                         "    if not B.interrupted():\n"
                         "        os.kill(os.getpid(), signal.SIGTERM)\n"
                         "B._kill_tree = kill_then_stop\n")
        script = self.path("error-then-no-exit.json")
        with open(script, "w") as f:
            json.dump({"events": [ev_result("", is_error=True, subtype="error_during_execution")],
                       "end": "hang"}, f)
        for reason, mode, args, extra in (
                ("timeout", "hang", ["--timeout", "1"], {}),
                ("result", "script", [], {"C_LEAN_FAKE_SCRIPT": script}),
                ("pipes", "pipeholder", [], {"C_LEAN_FAKE_HOLDER_END": "exit"})):
            with self.subTest(kill=reason):
                self.state = self.mktemp("c-lean-drain-stop-")
                env = child_env(mode, self.state, dict(self.env, PYTHONPATH=site, **extra))
                try:
                    p = subprocess.run([sys.executable, CLI, "-m", "haiku"] + args + ["hello"],
                                       env=env, capture_output=True, text=True, timeout=60,
                                       preexec_fn=default_sighup)
                finally:
                    self.kill_recorded()
                self.assertEqual(p.returncode, 143, p.stderr)
                self.assertIn("c-lean: stopped — call stopped by signal 15", p.stderr)
                self.assertEqual([w for w in self.warns(p) if "judged by" in w], [],
                                 "a stop after a %s kill must leave no WARN of that kill: %s"
                                 % (reason, p.stderr[-600:]))

    def test_open_pipes_after_a_death_by_signal_still_warn(self):
        """The CLI died of a signal that c-lean did not catch (SIGTERM to the
        CLI alone), and a process it started still held its output. After the
        exit grace its tree was killed and drained, exit 143, but no WARN said
        so: the return for a death by signal dropped the line every other
        return keeps."""
        proc = self.start_call([CLI, "-m", "haiku", "--timeout", "40", "hello"], "sleeper")
        try:
            self.wait_for(lambda: self.sleeper_pids(), what="the CLI and its child")
            os.kill(int(self.sleeper_pids()[0][0]), signal.SIGTERM)
            out, err = proc.communicate(timeout=B.RESULT_GRACE + B.KILL_DRAIN + 10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            self.kill_leftovers()
        self.assertEqual((proc.returncode, out), (143, ""), err)
        self.assertIn("c-lean: stopped — call killed by signal 15", err)
        self.assertIn("c-lean: WARN the claude CLI exited but a process it started still held "
                      "its output open %ss later: its process tree was killed, and the call is "
                      "judged by what it printed and its own exit code"
                      % B.seconds_shown(B.RESULT_GRACE), err.splitlines(),
                      "open pipes after a death by signal must still get their WARN")

    def test_a_result_without_a_newline_starts_the_exit_grace(self):
        t0 = time.monotonic()
        p = self.script([ev_text("1", "ans")], "--timeout", "30", end="hang",
                        tail_hex=json.dumps(ev_result("ans")).encode().hex())
        self.assertLess(time.monotonic() - t0, B.RESULT_GRACE + B.KILL_DRAIN + 5,
                        "a whole result without newline must start RESULT_GRACE")
        self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), p.stderr)
        self.assertEqual(self.warns(p), [self.lingered("5s later")])

    def test_a_result_followed_by_eof_starts_the_exit_grace(self):
        t0 = time.monotonic()
        p = self.script([ev_text("1", "ans"), ev_result("ans")], "--timeout", "30", end="closehang")
        self.assertLess(time.monotonic() - t0, B.RESULT_GRACE + B.KILL_DRAIN + 5,
                        "a result followed by EOF must start RESULT_GRACE")
        self.assertEqual((p.returncode, p.stdout), (0, "ans\n"), p.stderr)
        self.assertEqual(self.warns(p), [self.lingered("5s later")])

    def test_the_output_reader_waits_in_watch_slices(self):
        pipes = []
        for data in (b"hello", b""):
            read_end, write_end = os.pipe()
            os.write(write_end, data)
            os.close(write_end)
            pipes.append(os.fdopen(read_end, "r", encoding="utf-8", errors="backslashreplace"))
        proc = mock.Mock(stdout=pipes[0], stderr=pipes[1], c_lean_killed_at=None)
        proc.poll.side_effect = [None, 0, 0]
        selector = B.selectors.DefaultSelector()
        select, waits = selector.select, []
        def record(wait):
            waits.append(wait)
            return select(wait)
        selector.select = record
        with mock.patch.object(B.selectors, "DefaultSelector", return_value=selector):
            out, _err, _reason, _gone = B._communicate(proc, B.MAX_TIMEOUT)
        self.assertEqual(out, "hello")
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), B.WATCH_SLICE,
                             "each output wait must be bounded by WATCH_SLICE")

    def test_pipe_decoding_keeps_universal_newlines_and_bad_utf8(self):
        p = self.script([], tail_hex=(b"first\r\nsecond\rthird\n\xff" +
                        b'\n' + json.dumps(ev_result("ans")).encode()).hex())
        self.assertEqual(p.returncode, 0, p.stderr)
        # Inspect the loop directly: junk is deliberately outside JSON events.
        proc = subprocess.Popen([sys.executable, "-c", "import os; os.write(1, b'a\\r\\nb\\rc\\n\\xff')"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="backslashreplace")
        out, err, reason, gone = B._communicate(proc, 5)
        self.assertEqual(out, "a\nb\nc\n\\xff", "the read loop must preserve text-mode decoding")

    def test_help_describes_tree_discovery_stops_and_bounded_waits(self):
        h = " ".join(run([CLI, "-h"], state=self.state, extra_env=self.env).stdout.split())
        for phrase in ("Linux reads /proc", "pgrep from PATH, else /usr/bin/pgrep",
                       "could not list the processes of the call", "some may still run",
                       "at most 3 s", "5 s after its exit", "inherited as SIG_IGN stays ignored",
                       "came after the answer", "exit 128+signal", "SIGKILL sent to the job"):
            self.assertIn(phrase, h, "help must describe process and stop handling: " + phrase)

    # --- error paths: a clear line and the right exit code, never a traceback
    def write_bytes(self, name, data):
        path = self.path(name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_bytes(self, args, mode="single", stdin=b"", extra_env=None, **kw):
        """`c-lean args` with bytes on stdin and both streams kept as bytes."""
        env = child_env(mode, self.state, dict(self.env, **(extra_env or {})))
        return subprocess.run([sys.executable, CLI] + args, env=env, input=stdin,
                              capture_output=True, timeout=180, **kw)

    def test_a_file_or_stdin_that_is_not_utf8_is_a_usage_error(self):
        """A file given to -f, --system-file or --schema, and the prompt on
        stdin, were decoded in the encoding of the locale: bytes that are not
        UTF-8 ended in a UnicodeDecodeError traceback, exit 1."""
        bad = self.write_bytes("bad.txt", b"ok \xff tail")
        for args, stdin, source in (
                (["-f", bad], b"", "-f %s" % bad),
                (["--system-file", bad, "hello"], b"", "--system-file %s" % bad),
                (["--schema", bad, "hello"], b"", "--schema %s" % bad),
                ([], b"ok \xff tail", "the prompt on stdin"),
                (["-f", "-"], b"ok \xff tail", "-f - (stdin)")):
            with self.subTest(source=source):
                p = self.run_bytes(["-m", "haiku"] + args, stdin=stdin)
                err = p.stderr.decode("utf-8", "replace")
                self.assertNotIn("Traceback", err)
                self.assertEqual((p.returncode, err.splitlines()),
                                 (64, ["c-lean: %s: not UTF-8: the first bad byte is 0xff at "
                                       "byte offset 3" % source]),
                                 "bytes that are not UTF-8 must be one usage line that names "
                                 "the flag, the source and the offset: %s" % err[-300:])
        self.assertEqual(self.calls(), 0)

    def test_files_and_stdin_are_read_as_utf8_whatever_the_locale(self):
        """Under a locale that is not UTF-8 (C, with Python's coercion and
        UTF-8 mode off) "é" in a file or on stdin ended in a traceback."""
        env = {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
        probe = subprocess.run([sys.executable, "-c", "import locale; "
                                "print(locale.getpreferredencoding(False))"], capture_output=True,
                               text=True, timeout=60, env=child_env("single", self.state, env))
        if probe.stdout.strip().lower().replace("-", "") in ("utf8", ""):
            self.skipTest("the C locale of this system is UTF-8: %r" % probe.stdout)
        prompt, system = "café — Привет", "Rubric: é ü — Привет"
        schema = '{"type": "object", "description": "é — Привет"}'
        for args, stdin in ((["-f", self.write("utf8-prompt.txt", prompt),
                              "--system-file", self.write("utf8-system.txt", system)], b""),
                            (["--system-file", self.write("utf8-system-2.txt", system)],
                             prompt.encode("utf-8"))):
            with self.subTest(stdin=bool(stdin)):
                p = self.run_bytes(["-m", "haiku"] + args, stdin=stdin, extra_env=env)
                self.assertEqual((p.returncode, p.stdout.decode("utf-8")), (0, "ECHO:%s\n" % prompt),
                                 p.stderr.decode("utf-8", "replace")[-300:])
                got = self.received()
                self.assertEqual((got["prompt_sha256"], got["system_sha256"]),
                                 (hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                  hashlib.sha256(system.encode("utf-8")).hexdigest()),
                                 "the files and stdin must be read as UTF-8 whatever the locale")
        p = self.run_bytes(["-m", "haiku", "--schema", self.write("utf8-schema.json", schema),
                            "hello"], mode="schema", extra_env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-300:])
        self.assertEqual(self.flag("--json-schema"), schema)

    def no_stdin(self):
        """preexec_fn: c-lean starts with stdin closed, as `c-lean <&-` does."""
        os.close(0)

    def test_a_closed_stdin_is_an_empty_prompt(self):
        """With stdin closed (`c-lean <&-`) sys.stdin is None, and a prompt that
        had to come from it ended in an AttributeError traceback, exit 1."""
        for args in ([], ["-f", "-"]):
            with self.subTest(args=args):
                p = self.run_bytes(["-m", "haiku"] + args, stdin=None, preexec_fn=self.no_stdin)
                self.assertEqual((p.returncode, p.stderr.decode("utf-8", "replace")),
                                 (64, "c-lean: empty prompt (give text, -f FILE, or pipe stdin)\n"),
                                 "a closed stdin must be an empty prompt")
        p = self.run_bytes(["-m", "haiku", "-f", "-", "hello"], stdin=None,
                           preexec_fn=self.no_stdin)
        self.assertEqual((p.returncode, p.stdout), (0, b"ECHO:hello\n"),
                         "a closed stdin adds nothing to a prompt given otherwise")
        self.assertEqual(self.calls(), 1)

    def no_stderr(self):
        """preexec_fn: c-lean starts with stderr closed, as `c-lean 2>&-` does."""
        default_sighup()
        os.close(2)

    def test_a_usage_error_with_stderr_closed_leaves_stdout_empty(self):
        """With stderr closed (`c-lean 2>&-`) sys.stderr is None, and print()
        to None writes to stdout: every usage error, argparse's included,
        went to stdout, the channel of the answer."""
        system = self.write("system.md", "S")
        for args in (["--no-such-flag", "x"], ["-m"], ["-m", "haiku", "-e", "turbo", "x"],
                     ["-m", "haiku", "--schema", "", "x"], ["-m", "haiku"], ["x"],
                     ["-m", "haiku", "--timeout", "0", "x"],
                     ["-m", "haiku", "--timeout", "99999999999", "x"],
                     ["-m", "haiku", "--backend", "nope", "x"],
                     ["-m", "haiku", "--system", "S", "--system-file", system, "x"],
                     ["-m", "haiku", "-f", self.path("no-such-prompt.md")]):
            with self.subTest(args=args):
                p = self.run_bytes(args, stdin=b"", preexec_fn=self.no_stderr)
                self.assertEqual((p.returncode, p.stdout), (64, b""),
                                 "a usage error must not reach stdout when stderr is closed")
        self.assertEqual(self.calls(), 0)

    def test_a_warning_during_the_call_with_stderr_closed_keeps_the_answer(self):
        """The WARN of a usage log that cannot be written went to stdout when
        stderr was closed from the start, and then the run ended with 120
        and no answer. Such a stderr does not fail: its lines are dropped."""
        p = self.run_bytes(["-m", "haiku", "hello"], preexec_fn=self.no_stderr,
                           extra_env={"C_LEAN_USAGE_LOG": self.mktemp("c-lean-log-")})
        self.assertEqual((p.returncode, p.stdout), (0, b"ECHO:hello\n"),
                         "with stderr closed, stdout must hold the answer and nothing else")
        self.assertEqual(self.calls(), 1)

    def test_a_good_call_with_stderr_closed_prints_the_answer(self):
        """A stderr closed from the start (`c-lean 2>&-`) cost the answer of a
        good call, paid for, and exit 120: the trailer could not be written."""
        p = self.run_bytes(["-m", "haiku", "héllo"], preexec_fn=self.no_stderr)
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:héllo\n".encode("utf-8")),
                         "a stderr closed from the start must not cost the answer")
        self.assertEqual(self.jsonl(self.log)[-1]["exit"], 0)

    def test_check_with_stderr_closed_keeps_its_exit(self):
        """--check with stderr closed from the start exited 120 after a good
        probe. It prints nothing on stdout, then as now."""
        probe = self.path("probe.jsonl")
        env = child_env("usageinout", self.state, dict(self.env, C_LEAN_CHECK_PROBE_FILE=probe))
        p = subprocess.run([sys.executable, CLI, "--check"], env=env, capture_output=True,
                           timeout=120, preexec_fn=self.no_stderr)
        self.assertEqual((p.returncode, p.stdout), (0, b""),
                         "with stderr closed, --check must keep the exit it decided")
        self.assertEqual((self.jsonl(self.log)[-1]["exit"], self.jsonl(probe)[-1]["exit"]), (0, 0))

    def test_an_empty_schema_is_a_usage_error(self):
        """--schema "" switched the schema off without a word, and a value of
        spaces alone was looked for as a file named "   "."""
        for value in ("", "   "):
            with self.subTest(value=value):
                p = run([CLI, "-m", "haiku", "--schema", value, "hello"], mode="schema",
                        state=self.state, extra_env=self.env)
                self.assertEqual((p.returncode, p.stderr),
                                 (64, "c-lean: --schema takes inline JSON or a file, and its value "
                                      "is empty\n"), "an empty --schema must be a usage error")
        self.assertEqual(self.calls(), 0)

    def test_an_empty_schema_file_is_a_usage_error(self):
        """A --schema file with nothing in it, or only whitespace, switched
        the schema off without a word, as --schema "" did."""
        for n, content in enumerate(("", " \n\t\r\n")):
            with self.subTest(content=content):
                path = self.write("schema-%d.json" % n, content)
                p = run([CLI, "-m", "haiku", "--schema", path, "hello"], mode="schema",
                        state=self.state, extra_env=self.env)
                self.assertEqual((p.returncode, p.stderr),
                                 (64, "c-lean: --schema takes inline JSON or a file, and the file "
                                      "%s is empty\n" % path),
                                 "an empty --schema file must be a usage error")
        self.assertEqual(self.calls(), 0)

    def test_effort_takes_the_five_values_in_any_case(self):
        """-e was passed to the CLI as given, whatever it was."""
        for value, want in (("MAX", "max"), ("High", "high"), ("low", "low"), (None, None)):
            with self.subTest(effort=value):
                p = run([CLI, "-m", "haiku"] + (["-e", value] if value else []) + ["hello"],
                        mode="single", state=self.state, extra_env=self.env)
                self.assertEqual(p.returncode, 0, p.stderr[-300:])
                self.assertEqual(self.flag("--effort"), want,
                                 "-e %s must reach the CLI as --effort %s" % (value, want))
        for value in ("turbo", "", "max ", "x-high"):
            with self.subTest(effort=value):
                p = run([CLI, "-m", "haiku", "-e", value, "hello"], mode="single",
                        state=self.state, extra_env=self.env)
                self.assertEqual((p.returncode, p.stderr),
                                 (64, "c-lean: unknown -e %r (have: low, medium, high, xhigh, max, "
                                      "in any letter case)\n" % value),
                                 "an effort the CLI does not take must be a usage error")
        self.assertEqual(self.calls(), 4)

    def script_env(self, events, rc=0, extra_env=None):
        """The environment of a run against these events (see script)."""
        self.nscript = getattr(self, "nscript", 0) + 1
        path = self.path("script-%d.json" % self.nscript)
        with open(path, "w") as f:
            json.dump({"events": events, "rc": rc}, f)
        return child_env("script", self.state, dict(self.env, C_LEAN_FAKE_SCRIPT=path,
                                                    **(extra_env or {})))

    def test_a_reader_that_goes_away_mid_answer_ends_quietly_with_120(self):
        """`c-lean … | head -c 10` with an answer of about 300 KB: the write met
        a closed pipe and ended in a BrokenPipeError traceback, exit 1, where
        -h promises exit 120."""
        answer = "".join("line %06d of a long answer\n" % n for n in range(12000)).strip()
        self.assertGreater(len(answer), 300000)
        proc = subprocess.Popen([sys.executable, CLI, "-m", "haiku", "--system", "S", "hello"],
                                env=self.script_env([ev_text("1", answer), ev_result(answer)]),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0, preexec_fn=default_sighup)
        try:
            head = proc.stdout.read(10)
            proc.stdout.close()
            err = proc.stderr.read().decode("utf-8", "replace")
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(head, answer[:10].encode())
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Exception ignored", err)
        self.assertEqual(proc.returncode, 120, "a reader that went away must give exit 120: %s"
                         % err[-300:])
        self.assertIn(" · exit 0 · ", err, "the trailer was written before the answer")
        self.assertEqual([r["exit"] for r in self.jsonl(self.log)], [0])

    def test_a_stdout_with_no_reader_ends_without_a_second_error(self):
        """An answer small enough for the buffer of stdout failed only when the
        interpreter flushed it on the way out: a second error on stderr."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            p = subprocess.run([sys.executable, CLI, "-m", "haiku", "--system", "S", "hello"],
                               env=child_env("single", self.state, self.env), stdout=write_end,
                               stderr=subprocess.PIPE, timeout=60, preexec_fn=default_sighup)
        finally:
            os.close(write_end)
        err = p.stderr.decode("utf-8", "replace")
        self.assertNotIn("Exception ignored", err, "a broken stdout must not fail again at "
                         "interpreter shutdown")
        self.assertNotIn("Traceback", err)
        self.assertEqual(p.returncode, 120, err[-300:])
        self.assertEqual([r["exit"] for r in self.jsonl(self.log)], [0])

    def test_a_usage_log_warning_on_a_broken_stderr_still_saves_the_text(self):
        """The WARN of a usage log that cannot be written met a broken stderr and
        raised before the text blocks of the call were saved."""
        # a directory cannot be opened as the log: one WARN
        env = self.script_env([ev_text("1", "text that must be kept"),
                               ev_result("API Error", is_error=True, subtype="error")], rc=1,
                              extra_env={"C_LEAN_USAGE_LOG": self.dir})
        stderr = self.broken_stderr()
        try:
            p = subprocess.run([sys.executable, CLI, "-m", "haiku", "--system", "S", "hello"],
                               env=env, stdout=subprocess.PIPE, stderr=stderr, timeout=60,
                               preexec_fn=default_sighup)
        finally:
            os.close(stderr)
        self.assertEqual(self.saved_blocks(), [["text that must be kept"]],
                         "a broken stderr at the usage-log WARN must not cost the text blocks")
        self.assertEqual((p.returncode, p.stdout), (120, b""))

    def test_the_answer_is_utf8_whatever_the_encoding_of_stdout(self):
        """Under PYTHONIOENCODING=ascii a non-ASCII answer ended in a
        UnicodeEncodeError traceback and was lost."""
        env = {"PYTHONIOENCODING": "ascii"}
        prompt = "café — Привет"
        p = self.run_bytes(["-m", "haiku"], stdin=prompt.encode("utf-8"), extra_env=env)
        err = p.stderr.decode("ascii")  # stderr keeps its own encoding, with escapes
        self.assertNotIn("Traceback", err)
        self.assertEqual((p.returncode, p.stdout), (0, ("ECHO:%s\n" % prompt).encode("utf-8")),
                         "the answer must go to stdout as UTF-8: %s" % err[-300:])
        self.assertIn(" \\xb7 exit 0 \\xb7 ", err, "the trailer must not fail on the encoding")
        # a lone surrogate still goes out as its escape, and a NOTE of model
        # text on stderr in escapes
        env = self.script_env([ev_text("1", "\ud800é "), ev_ask(CAP_ASK), ev_text("2", "ü end"),
                               ev_result("ü end")], extra_env=env)
        p = subprocess.run([sys.executable, CLI, "-m", "haiku", "--system", "S", "hello"], env=env,
                           capture_output=True, timeout=120)
        err = p.stderr.decode("ascii")
        self.assertEqual((p.returncode, p.stdout), (0, "\\ud800é ü end\n".encode("utf-8")),
                         err[-300:])
        self.assertIn('"\\ud800\\xe9 " | "\\xfc end"', err)

    def check_with_probe(self, mode, probe, extra=None, umask=None):
        env = child_env(mode, self.state, dict(self.env, C_LEAN_CHECK_PROBE_FILE=probe,
                                               **(extra or {})))

        def start():
            default_sighup()
            if umask is not None:
                os.umask(umask)
        return subprocess.run([sys.executable, CLI, "--check"], env=env, capture_output=True,
                              text=True, timeout=120, preexec_fn=start)

    def test_check_keeps_the_exit_of_a_binary_that_cannot_run_and_of_a_timeout(self):
        """--check turned exit 69 (the binary cannot be run) and 124 (the
        timeout) into 1, so a script could not tell them from a bad probe."""
        probe = self.path("probe.jsonl")
        for bin_, mode, args, want in ((self.unusable_bin(), "single", [], 69),
                                       (self.path("no-such-claude"), "single", [], 69),
                                       (FAKE, "hang", ["--timeout", "2"], 124)):
            with self.subTest(want=want, bin=bin_):
                env = child_env(mode, self.state, dict(self.env, C_LEAN_CLAUDE_BIN=bin_,
                                                       C_LEAN_CHECK_PROBE_FILE=probe))
                try:
                    p = subprocess.run([sys.executable, CLI, "--check"] + args, env=env,
                                       capture_output=True, text=True, timeout=120,
                                       preexec_fn=default_sighup)
                finally:
                    self.kill_recorded()
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual((p.returncode, self.trailer_exit(p), self.jsonl(self.log)[-1]["exit"],
                                  self.jsonl(probe)[-1]["exit"]), (want, want, want, want),
                                 "--check must give the exit of the call, in the trailer, the "
                                 "usage log and the probe record too: %s" % p.stderr[-300:])

    def test_check_keeps_the_exit_71_of_a_call_that_cannot_start(self):
        """--check turned exit 71 (a resource the call needs could not be
        made) into 1, as it did 69 and 124."""
        for what in ("the working directory", "the system-prompt file"):
            with self.subTest(what=what):
                body, reason = self.CANNOT_MAKE[what]
                tmp, probe = self.mktemp("c-lean-tmp-"), self.path("probe-71.jsonl")
                p = self.check_with_probe("single", probe, extra=dict(
                    TMPDIR=tmp, PYTHONPATH=self.site("import errno\n" + body)))
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual((p.returncode, self.trailer_exit(p), self.jsonl(self.log)[-1]["exit"],
                                  self.jsonl(probe)[-1]["exit"]), (71, 71, 71, 71),
                                 "--check must give exit 71 of a call that cannot start, in the "
                                 "trailer, the usage log and the probe record too: %s"
                                 % p.stderr[-300:])
                self.assertIn("c-lean --check: FAIL call failed: cannot start the call: %s: %s"
                              % (what, reason % {"tmp": tmp}), p.stderr.splitlines())
                self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(self.calls(), 0)

    def test_the_probe_record_carries_the_exit_of_check_and_is_private(self):
        """The record of C_LEAN_CHECK_PROBE_FILE held the exit of the call (0),
        not the exit of --check (1), and a new file got the mode of the umask."""
        for umask in (0o022, 0o277):
            with self.subTest(umask=oct(umask)):
                probe = self.path("probe-%o.jsonl" % umask)
                p = self.check_with_probe("usagenone", probe, umask=umask)
                self.assertEqual(p.returncode, 1, p.stderr[-300:])
                self.assertEqual([r["exit"] for r in self.jsonl(probe)], [1],
                                 "the probe record must carry the exit that --check returns")
                self.assertEqual(stat.S_IMODE(os.stat(probe).st_mode), 0o600,
                                 "a probe file made by c-lean must have mode 0600")
        # a file that is there keeps its mode
        probe = self.path("probe-kept.jsonl")
        open(probe, "w").close()
        os.chmod(probe, 0o640)
        p = self.check_with_probe("usageinout", probe)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertEqual(([r["exit"] for r in self.jsonl(probe)],
                          stat.S_IMODE(os.stat(probe).st_mode)), ([0], 0o640),
                         "a probe file that is already there must keep its mode")

    def test_a_probe_record_that_cannot_be_written_costs_one_warn(self):
        """A probe file that could not be opened ended --check in a traceback."""
        for probe, why in ((self.dir, "Is a directory"),
                           (self.path("no-such-dir/probe.jsonl"), "No such file or directory")):
            with self.subTest(probe=probe):
                p = self.check_with_probe("usageinout", probe)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(self.warns(p) + [l for l in p.stderr.splitlines()
                                                  if l.startswith("c-lean --check: WARN")],
                                 ["c-lean --check: WARN could not write the probe record to %s: %s"
                                  % (probe, why)], p.stderr[-300:])
                self.assertEqual((p.returncode, self.trailer_exit(p),
                                  self.jsonl(self.log)[-1]["exit"]), (0, 0, 0),
                                 "a probe record that cannot be written must not change the exit")

    def test_help_describes_check_and_the_exit_codes_of_the_error_paths(self):
        h = " ".join(run([CLI, "-h"], state=self.state, extra_env=self.env).stdout.split())
        for phrase in ("--check makes one fixed Haiku probe",
                       "It ignores the prompt and every flag except --timeout and --backend",
                       "It exits 0 or 1 (a failed check), or 69, 71, 75, 124 or 128+N as a call "
                       "would",
                       "C_LEAN_CHECK_PROBE_FILE=PATH appends a JSON record of the probe to PATH "
                       "(a new file gets mode 0600) with the exit code --check gives",
                       "71 a local resource the call needs could not be made",
                       "c-lean: cannot start the call: <what>: <reason>",
                       "c-lean then prints nothing more and exits 120, without a traceback",
                       "A stderr closed from the start (`2>&-`) is not a stream that fails: its "
                       "lines are dropped, the answer is printed, and the exit stays what c-lean "
                       "decided",
                       "so do the record of --check",
                       "a prompt on stdin are read as UTF-8 too",
                       "The answer goes to stdout as UTF-8 whatever the encoding of stdout"):
            self.assertIn(phrase, h, "c-lean -h must say: %s" % phrase)
        self.assertNotIn("cannot flush its streams", h)

    # What the call needs, made by c-lean, made to fail by the test (a real
    # failure needs a full disk, a quota or an exhausted process table).
    CANNOT_MAKE = {
        "the working directory": (
            "def failing(*a, **k):\n"
            "    raise OSError(errno.ENOSPC, 'No space left on device')\n"
            "B.tempfile.mkdtemp = failing\n", "No space left on device"),
        "the system-prompt file": (
            "def failing(*a, **k):\n"
            "    raise PermissionError(errno.EACCES, 'Permission denied', os.environ['TMPDIR'])\n"
            "B.tempfile.mkstemp = failing\n", "Permission denied (%(tmp)s)"),
        "the stdin pipe": (
            "def failing():\n"
            "    raise OSError(errno.EMFILE, 'Too many open files')\n"
            "B.os.pipe = failing\n", "Too many open files"),
        # The child is up when the thread cannot start: its pid is written
        # down, and a walk of its tree waits until the child has started and
        # then 0.5 s more, so that a child whose stdin was closed before the
        # kill has the time to go on.
        "the feeder thread": (
            "import threading, time, types\n"
            "class NoThread(threading.Thread):\n"
            "    def start(self):\n"
            "        with open(os.environ['CHILD_PIDS'], 'w') as f:\n"
            "            f.write(' '.join(str(p.pid) for p in B._LIVE))\n"
            "        raise RuntimeError(\"can't start new thread\")\n"
            "B.threading = types.SimpleNamespace(Thread=NoThread)\n"
            "real = B._descendants\n"
            "def slow(pid):\n"
            "    count = os.path.join(os.environ['C_LEAN_FAKE_STATE'], 'calls.count')\n"
            "    t0 = time.monotonic()\n"
            "    while not os.path.exists(count) and time.monotonic() - t0 < 30:\n"
            "        time.sleep(0.01)\n"
            "    time.sleep(0.5)\n"
            "    return real(pid)\n"
            "B._descendants = slow\n", "can't start new thread"),
    }

    def kill_pids(self, path):
        """Kill every process whose pid is in the file at `path`, if any."""
        if os.path.exists(path):
            for pid in open(path).read().split():
                if self.alive(pid):
                    os.kill(int(pid), signal.SIGKILL)

    def test_a_resource_the_call_needs_that_cannot_be_made_is_exit_71(self):
        """mkdtemp, the file of the system prompt, os.pipe and the thread that
        writes the prompt each ended in a traceback, exit 1, with no trailer
        and no usage-log line, and what had been made stayed behind."""
        for n, (what, (body, reason)) in enumerate(sorted(self.CANNOT_MAKE.items())):
            with self.subTest(what=what):
                tmp, pids = self.mktemp("c-lean-tmp-"), self.path("child-%d.pids" % n)
                self.state = self.mktemp("c-lean-state-")  # a count of calls of its own
                env = child_env("hang", self.state, dict(
                    self.env, TMPDIR=tmp, CHILD_PIDS=pids,
                    PYTHONPATH=self.site("import errno\n" + body)))
                try:
                    p = subprocess.run([sys.executable, CLI, "-m", "haiku", "--system", "S",
                                        "hello"], env=env, capture_output=True, text=True,
                                       timeout=120, preexec_fn=default_sighup)
                finally:
                    self.kill_recorded()
                    self.kill_pids(pids)
                self.assertNotIn("Traceback", p.stderr)
                line = "c-lean: cannot start the call: %s: %s" % (what, reason % {"tmp": tmp})
                self.assertEqual((p.returncode, [l for l in p.stderr.splitlines()
                                                 if l.startswith("c-lean:")]), (71, [line]),
                                 p.stderr[-400:])
                self.assertEqual(self.trailer_exit(p), 71)
                rec = self.jsonl(self.log)[-1]
                self.assertEqual((rec["exit"], rec["tokens_unknown"]), (71, False))
                self.assertEqual(os.listdir(tmp), [], "the working directory and the file of "
                                 "the system prompt must be removed")
                started = what == "the feeder thread"
                self.assertEqual(self.calls(), int(started))
                if started:
                    pid = open(pids).read().split()[0]
                    self.wait_for(lambda: not self.alive(pid), secs=5, what="the child to be killed")
                    self.assertFalse(os.path.exists(os.path.join(self.state, "hang.pids")),
                                     "the child must be killed before its stdin is closed")

    def test_a_feeder_thread_that_cannot_start_leaves_no_descriptor_behind(self):
        seen = []

        class NoThread(threading.Thread):
            def start(self):
                seen.extend(p.pid for p in B._LIVE)
                raise RuntimeError("can't start new thread")
        tmp = self.mktemp("c-lean-tmp-")
        env = child_env("hang", self.state, self.env)
        with mock.patch.dict(os.environ, env), mock.patch.object(tempfile, "tempdir", tmp), \
                mock.patch.object(B, "threading", types.SimpleNamespace(Thread=NoThread)):
            before = len(os.listdir("/dev/fd"))
            try:
                r = B._call_claude("p", "S", "haiku", "low", None, 5)
            except RuntimeError as e:
                r = e
            finally:
                after = len(os.listdir("/dev/fd"))
                self.addCleanup(self.kill_recorded)
                for pid in seen:
                    self.addCleanup(lambda pid=pid: self.alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertEqual((getattr(r, "exit", r), getattr(r, "err", None)),
                         (71, "cannot start the call: the feeder thread: can't start new thread"))
        self.assertEqual(after, before, "every pipe end of the call must be closed")
        self.assertEqual(len(seen), 1)
        self.wait_for(lambda: not self.alive(seen[0]), secs=5, what="the child to be killed")
        self.assertEqual(os.listdir(tmp), [])

    def test_the_command_line_goes_to_popen_as_utf8_bytes(self):
        """Under a locale that is not UTF-8 (C on Linux, with Python's coercion
        and UTF-8 mode off) Popen encoded each argument in ASCII, the
        filesystem encoding there, and "é" in a --schema ended in a
        UnicodeEncodeError traceback. macOS never showed it: its filesystem
        encoding is UTF-8."""
        seen = []

        def popen(args, **kw):
            seen.append(args)
            raise FileNotFoundError(2, "No such file or directory")
        schema = '{"type": "object", "description": "é — Привет"}'
        # what Python makes of bytes on its command line that the locale cannot decode
        model = "haiku-\udcc3\udca9"
        tmp = self.mktemp("c-lean-tmp-")
        with mock.patch.dict(os.environ, child_env("single", self.state, self.env)), \
                mock.patch.object(tempfile, "tempdir", tmp), \
                mock.patch.object(B.subprocess, "Popen", popen):
            r = B._call_claude("p", "S", model, "low", schema, 5)
        self.assertEqual((r.exit, len(seen)), (69, 1), r.err)
        self.assertEqual([a for a in seen[0] if not isinstance(a, bytes)], [],
                         "every argument must reach Popen as bytes")
        self.assertIn(schema.encode("utf-8"), seen[0], "text must go out as UTF-8")
        self.assertIn(b"haiku-\xc3\xa9", seen[0],
                      "an argument with surrogate escapes must go out as the bytes it came as")
        self.assertEqual(os.listdir(tmp), [])


    # --- environment, temporary files and diagnostics
    def test_child_pwd_is_its_private_working_directory(self):
        for pwd, pass_env in ((self.dir, None), (None, None), (self.dir, "PWD")):
            with self.subTest(pwd=pwd, pass_env=pass_env):
                p = run([CLI, "-m", "haiku", "hello"], mode="envdump", state=self.state,
                        cwd=self.dir, extra_env=dict(self.env, PWD=pwd,
                                                     C_LEAN_PASS_ENV=pass_env))
                self.assertEqual(p.returncode, 0, p.stderr)
                with open(os.path.join(self.state, "env.json")) as f:
                    env = json.load(f)
                self.assertEqual(env.get("PWD"), self.received()["cwd"],
                                 "child PWD must name its private working directory")
                self.assertNotEqual(env.get("PWD"), self.dir)

    def test_call_durations_use_monotonic_time_when_wall_clock_steps(self):
        class SteppingDatetime:
            count = 0

            @classmethod
            def now(cls, tz=None):
                cls.count += 1
                return datetime(2026, 10, 29, 20 - cls.count, tzinfo=tz)

        def read_all(proc, timeout):
            out, err = proc.communicate(timeout=5)
            return out, err, None, None

        def timed_out(proc, timeout):
            proc.communicate(timeout=5)
            return "", "", "timeout", None

        tmp = self.mktemp("c-lean-clock-")
        for outcome, code in (("answer", 0), ("timeout", 124), ("setup", 71)):
            with self.subTest(outcome=outcome), contextlib.ExitStack() as stack:
                SteppingDatetime.count = 0
                stack.enter_context(mock.patch.dict(os.environ,
                    child_env("single", self.state, self.env)))
                stack.enter_context(mock.patch.object(B, "_dt", types.SimpleNamespace(
                    datetime=SteppingDatetime, timezone=timezone)))
                stack.enter_context(mock.patch.object(B, "time", types.SimpleNamespace(
                    monotonic=mock.Mock(side_effect=[1000.0, 1003.5]))))
                stack.enter_context(mock.patch.object(tempfile, "tempdir", tmp))
                stack.enter_context(mock.patch.object(B, "_communicate",
                    timed_out if outcome == "timeout" else read_all))
                if outcome == "setup":
                    stack.enter_context(mock.patch.object(tempfile, "mkdtemp",
                        side_effect=PermissionError("test directory denied")))
                r = B._call_claude("p", None, "haiku", None, None, 5)
                self.assertEqual(r.exit, code, r.err)
                self.assertEqual(r.secs, 3.5,
                                 "call duration must use monotonic time after a wall-clock step")
                self.assertIn(" · 3.5s · ", B.trailer(r))
                B.log_call(r, "p")
                rec = self.jsonl(self.log)[-1]
                self.assertEqual(rec["secs"], 3.5)
                self.assertTrue(rec["ts"].startswith("2026-10-29T"))

    def test_usage_log_to_dev_null_does_not_warn(self):
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state,
                extra_env=dict(self.env, C_LEAN_USAGE_LOG="/dev/null"))
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr)
        self.assertEqual(self.warns(p), [], "a usage log on /dev/null must not WARN")

    def test_usage_log_fifo_is_written_without_changing_its_mode(self):
        path = self.path("usage.fifo")
        os.mkfifo(path, 0o640)
        os.chmod(path, 0o640)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            B.log_usage({"probe": "fifo"}, path=path)
            data = os.read(fd, 4096)
        finally:
            os.close(fd)
        self.assertEqual(json.loads(data), {"probe": "fifo"})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640,
                         "a FIFO usage log must keep its existing mode")

    def test_an_existing_regular_usage_log_is_made_private(self):
        self.write("usage.jsonl", "")
        os.chmod(self.log, 0o666)
        B.log_usage({"probe": "regular"}, path=self.log)
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600,
                         "an existing regular usage log must become private")
        self.assertEqual(self.jsonl(self.log), [{"probe": "regular"}])

    @unittest.skipIf(sys.version_info < (3, 11), "PYTHONSAFEPATH needs Python 3.11+")
    def test_symlink_with_python_safe_path_imports_the_core(self):
        link = self.path("c-lean-link")
        os.symlink(CLI, link)
        p = run([link, "-m", "haiku", "hello"], mode="single", state=self.state,
                cwd=self.dir, extra_env=dict(self.env, PYTHONSAFEPATH="1", PYTHONPATH=None))
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"),
                         "a symlink under PYTHONSAFEPATH must import its core: " + p.stderr)

    def test_hyphenated_rate_limits_in_cli_text_are_walls(self):
        for text in ("rate-limit", "rate-limited"):
            for channel in ("stderr", "stdout"):
                with self.subTest(text=text, channel=channel):
                    p = self.script([text] if channel == "stdout" else [], rc=1,
                                    stderr=text if channel == "stderr" else "")
                    self.assertEqual(p.returncode, 75,
                                     "hyphenated rate limits in CLI text must be a wall")
                    self.assertIn("LIMIT WALL", p.stderr)

    def test_hyphenated_rate_limits_in_model_words_are_not_walls(self):
        for text in ("rate-limit", "rate-limited"):
            for kind, code in (("answer", 0), ("failure", 1), ("cut", 1)):
                with self.subTest(text=text, kind=kind):
                    events = [ev_text("1", text)]
                    if kind != "cut":
                        events.append(ev_result(text, is_error=kind == "failure"))
                    p = self.script(events)
                    self.assertNotIn("LIMIT WALL", p.stderr,
                                     "hyphenated rate limits in model words must not be walls")
                    self.assertEqual(p.returncode, code, p.stderr)

    def test_system_file_has_its_own_private_directory_and_both_are_removed(self):
        tmp = self.mktemp("c-lean-shared-tmp-")
        os.chmod(tmp, 0o777)  # shared TMPDIR without the sticky bit
        p = run([CLI, "-m", "haiku", "--system", "rubric", "hello"], mode="single",
                state=self.state, extra_env=dict(self.env, TMPDIR=tmp))
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr)
        got = self.received()
        directory = got["system_file_dir"]
        self.assertNotIn(directory, (os.path.realpath(tmp), os.path.realpath(got["cwd"])),
                         "the system file needs its own directory outside TMPDIR and the cwd")
        self.assertEqual(got["system_dir_mode"], 0o700,
                         "the directory of the system file must have mode 0700")
        self.assertEqual(got["system_file_mode"], 0o600)
        self.assertFalse(os.path.exists(got["system_file"]), "the system file must be removed")
        self.assertFalse(os.path.exists(directory), "the system directory must be removed")
        self.assertEqual(os.listdir(tmp), [])

    def test_system_directory_permissions_survive_restrictive_umask(self):
        tmp = self.mktemp("c-lean-umask-")
        mask = os.umask(0o777)
        try:
            with mock.patch.object(tempfile, "tempdir", tmp):
                path = B._system_file("rubric")
        finally:
            os.umask(mask)
        directory = os.path.dirname(path)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700,
                         "the system directory must retain mode 0700 under a restrictive umask")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_old_cli_system_file_error_explains_how_to_fix_it(self):
        rubric = self.write("rubric.md", "rubric")
        tmp = self.mktemp("c-lean-old-cli-")
        for args in (["-m", "haiku", "--system", "rubric", "hello"],
                     ["-m", "haiku", "--system-file", rubric, "hello"], ["--check"]):
            with self.subTest(args=args):
                p = run([CLI] + args, mode="single", state=self.state,
                        extra_env=dict(self.env, TMPDIR=tmp,
                                       C_LEAN_FAKE_UNKNOWN="--system-prompt-file"))
                self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
                self.assertIn("this claude CLI is too old for --system, --system-file and "
                              "--check; update Claude Code", p.stderr,
                              "an old CLI must name the affected flags and tell how to fix it")
                self.assertIn("unknown option '--system-prompt-file'", p.stderr)
                self.assertEqual(self.jsonl(self.log)[-1]["exit"], 1)
                self.assertEqual(os.listdir(tmp), [])

    def test_old_cli_without_a_system_prompt_still_works(self):
        p = run([CLI, "-m", "haiku", "hello"], mode="single", state=self.state,
                extra_env=dict(self.env, C_LEAN_FAKE_UNKNOWN="--system-prompt-file"))
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:hello\n"), p.stderr)
        self.assertEqual(self.received()["system_flags"], ["--system-prompt"])

    def test_no_system_prompt_gets_no_old_cli_hint(self):
        error = "error: unknown option '--system-prompt-file'"
        plan = self.write("unknown-system-file.json", json.dumps({
            "events": [], "rc": 1, "stderr": error + "\n"}))
        p = run([CLI, "-m", "haiku", "hello"], mode="script", state=self.state,
                extra_env=dict(self.env, C_LEAN_FAKE_SCRIPT=plan))
        self.assertEqual((p.returncode, p.stdout), (1, ""), p.stderr)
        got = self.received()
        self.assertEqual(got["system_flags"], ["--system-prompt"])
        self.assertEqual(got["argv"][got["argv"].index("--system-prompt") + 1], "")
        self.assertIn(error, p.stderr)
        self.assertNotIn("this claude CLI is too old", p.stderr,
                         "a call without --system-prompt-file must not get the old CLI hint")
        self.assertNotIn("update Claude Code", p.stderr)
        self.assertEqual((self.trailer_exit(p), self.jsonl(self.log)[-1]["exit"]), (1, 1))

    def test_timeout_after_cli_exit_warns_about_open_pipes(self):
        def exited_with_open_pipes(proc, timeout):
            out, err = proc.communicate(timeout=5)
            return out, err, "timeout", proc.returncode

        for code in (0, 1, -signal.SIGKILL):
            with self.subTest(code=code):
                plan = self.write("exited.json", json.dumps({
                    "events": [ev_text("1", "answer"), ev_result("answer")],
                    "rc": code if code >= 0 else 0,
                    "end": "exit" if code >= 0 else "kill"}))
                env = child_env("script", self.state, dict(self.env, C_LEAN_FAKE_SCRIPT=plan))
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(B, "_communicate", exited_with_open_pipes):
                    r = B._call_claude("p", None, "haiku", None, None, 2)
                self.assertEqual(r.linger,
                    "the claude CLI exited but a process it started still held its output open "
                    "when the timeout of 2s ran out: its process tree was killed, and the call "
                    "is judged by what it printed and its own exit code",
                    "a timeout after CLI exit must warn about open pipes and the CLI's own exit")
                self.assertEqual(r.exit, code if code >= 0 else 128 - code)
                self.assertEqual(r.ok, code == 0)

    # --- documented prompt, schema and check paths
    def test_good_prompt_from_file(self):
        prompt = "File prompt: é — line one\nline two"
        path = self.write("brief.md", prompt)
        p = run([CLI, "-m", "haiku", "-f", path], mode="single", state=self.state,
                extra_env=self.env)
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:" + prompt + "\n"),
                         "a good prompt from -f FILE must reach the CLI whole: " + p.stderr)
        self.on_stdin(prompt)

    def test_good_prompt_from_stdin(self):
        prompt = "Stdin prompt: é — line one\nline two"
        p = run([CLI, "-m", "haiku"], mode="single", state=self.state,
                extra_env=self.env, stdin=prompt)
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:" + prompt + "\n"),
                         "a good prompt from stdin must reach the CLI whole: " + p.stderr)
        self.on_stdin(prompt)

    def test_good_prompt_from_file_dash(self):
        prompt = "Dash prompt: é — line one\nline two"
        p = run([CLI, "-m", "haiku", "-f", "-"], mode="single", state=self.state,
                extra_env=self.env, stdin=prompt)
        self.assertEqual((p.returncode, p.stdout), (0, "ECHO:" + prompt + "\n"),
                         "a good prompt from -f - must reach the CLI whole: " + p.stderr)
        self.on_stdin(prompt)

    def test_schema_from_a_file_reaches_the_cli_as_json(self):
        schema = '{"type":"object","properties":{"verdict":{"type":"string"}}}'
        path = self.write("schema.json", schema)
        p = run([CLI, "-m", "haiku", "--schema", path, "hello"], mode="schema",
                state=self.state, extra_env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        argv = self.received()["argv"]
        self.assertEqual(argv[argv.index("--json-schema") + 1], schema,
                         "--schema FILE must pass the file's JSON to the CLI")
        self.assertEqual(json.loads(p.stdout), {"verdict": "yes"})

    def test_check_fails_above_its_input_token_limit(self):
        for cache_read, code in ((250, 0), (251, 1)):
            with self.subTest(cache_read=cache_read):
                event = ev_result("OK", usage={"input_tokens": 1000,
                    "cache_creation_input_tokens": 250, "cache_read_input_tokens": cache_read,
                    "output_tokens": 1})
                env = self.script_env([event])
                p = run([CLI, "--check"], state=self.state, extra_env=env)
                self.assertEqual(p.returncode, code,
                                 "--check must fail only above 1500 total input tokens")
                if code:
                    self.assertIn("prefix too fat: 1501 input tokens (in 1000 + cw 250 + "
                                  "cr 251) > 1500", p.stderr)
                    self.assertNotIn("--check: OK", p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertEqual((self.trailer_exit(p), self.jsonl(self.log)[-1]["exit"]),
                                 (code, code))


def tearDownModule():
    """The shared sandbox of saved files must be empty once every test has run:
    a test that forgot to point C_LEAN_PARTIAL_DIR at a directory of its own would
    have written there. Checked at the very end, not inside one test, so that it
    catches any of them."""
    left = sorted(os.listdir(PARTIAL_SANDBOX))
    assert left == [], ("a test left a file in the shared sandbox of saved text (it forgot its "
                        "own C_LEAN_PARTIAL_DIR): %r" % left)


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
