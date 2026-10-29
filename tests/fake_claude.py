#!/usr/bin/env python3
"""A fake subscription CLI for single-call tests; no model is called.

C_LEAN_FAKE_MODE selects an event stream or a failure scenario. The stream
matches `-p --output-format stream-json --verbose`: system/init first, one
assistant event per content block, result last. JSON mode returns the result
alone. Continuation, truncation, signals, timeouts, schema output and malformed
streams all use the same event writer, so the stream reader is always exercised.

The command line is read as the CLI reads it (see parse): a positional
argument is the prompt, and without one the prompt is read from stdin.

C_LEAN_FAKE_STATE holds calls.count, cwd.log, argv.jsonl, received.jsonl and
env.json. received.jsonl has one line per run for what the fake was given: the
argv, where the prompt came from, the system prompt (length and sha256), and
the mode bits and directory of the --system-prompt-file file (see note).
C_LEAN_FAKE_SCRIPT supplies exact events and optional stderr, tail_hex and
end (exit, hang or kill). The `linger` mode prints a whole answer and then
does not exit, with a child of its own. C_LEAN_FAKE_SLEEP controls signal scenarios;
C_LEAN_FAKE_RC and C_LEAN_FAKE_WALL_RC control selected exit codes.
C_LEAN_FAKE_ASSERT_ENV refuses leaked credentials or an unexpected output cap.
C_LEAN_FAKE_UNKNOWN="--a,--b" names flags this run does not know, as an older
CLI would not: it then exits 1 before it reads stdin.
"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time

MODE = os.environ.get("C_LEAN_FAKE_MODE", "ok")
STATE = os.environ["C_LEAN_FAKE_STATE"]
WALL_TEXT = "You've hit your weekly limit"
CREDITS_TEXT = "Out of usage credits — buy more below to keep going"
# The first three were the whole of the old deny list; the rest are the ways a
# call can still leave this subscription or come back cut short, plus one plain
# unrelated name that proves the allow list is an allow list.
BANNED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
          "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "AWS_BEARER_TOKEN_BEDROCK",
          "ANTHROPIC_API_HOST", "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_CUSTOM_HEADERS",
          "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_IDENTITY_TOKEN", "CLAUDE_CODE_API_BASE_URL",
          "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
          "CLAUDE_ENV_FILE", "MAX_THINKING_TOKENS", "LEAK_CANARY_TOKEN")


# Every flag c_lean_core._claude_cmd passes, as the CLI knows it: True when it
# takes a value, False when it does not.
FLAGS = {"-p": False, "--print": False, "--verbose": False, "--strict-mcp-config": False,
         "--disable-slash-commands": False, "--no-session-persistence": False,
         "--tools": True, "--setting-sources": True, "--output-format": True,
         "--system-prompt": True, "--system-prompt-file": True, "--model": True,
         "--effort": True, "--json-schema": True}
# Flags the CLI acts on as soon as it meets them: it prints, and exits 0.
VERSION = "0.0.0-fake (Claude Code)"
USAGE = "Usage: claude [options] [command] [prompt]"
AT_ONCE = {"-v": VERSION, "--version": VERSION, "-h": USAGE, "--help": USAGE}
# Set by main(): `--output-format stream-json` makes stdout one event a line.
STREAM = False
SESSION = "5e55104d-fa4e-4000-8000-00000000fa4e"
# What the CLI says to the model when it goes on with an answer by itself:
# constants of CLI 2.1.288, whole. `isSynthetic` marks the event as the CLI's
# own, and the beginning of the text is the only thing that says which request
# it is, so the tool does read these words (see c_lean_core.RESUME_KINDS).
RESUME_TEXT = ("Output token limit hit. Resume directly — no apology, no recap of what you "
               "were doing. Pick up mid-thought if that is where the cut happened. Break "
               "remaining work into smaller pieces.")
CUT_TEXT = ("Your response above was cut off mid-stream. Resume directly from where it stops "
            "— no apology, no recap. If none of it survived, answer the request from the start.")
NOVIS_TEXT = ("[Your previous response had no visible output. Please continue and produce a "
              "user-visible response.]")
# after this one nobody knows whether the model goes on or starts again
SAFETY_TEXT = ("Your response above was stopped by a safety classifier — this is not a tool or "
               "API error. The rest of it was withheld, and tool calls in it that had not "
               "finished did not run. Do not produce that content again, even reworded.")
API_ERROR_TEXT = ("API Error: Claude's response exceeded the 100 output token maximum. To "
                  "configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS "
                  "environment variable.")
# the three messages of the `cont3` answer
CONT_PIECES = ("alpha 1\nalpha 2\nalpha 3\nalpha 4\nalpha 5\nalpha 6",
               "beta 1\nbeta 2", "gamma 1\ngamma 2")
# the four messages of `contfail`, every one cut at the cap
CUT_PIECES = ("1\n2\n3", "\n4\n5 ", "6\n7", "8\n9")
# the two text blocks of the one message of `twoblocks`
TWO_BLOCKS = ("BLOCK one, then thinking, ", "then BLOCK two")
_SEQ = [0]
_HEAD = [False]


def uid(prefix=""):
    _SEQ[0] += 1
    return "%s%08d-fa4e-4000-8000-%012d" % (prefix, _SEQ[0], os.getpid())


def usage(n_in=575, out=40):
    return {"input_tokens": n_in, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": out}


def put(*events):
    for ev in events:
        line = ev if isinstance(ev, str) else json.dumps(ev, ensure_ascii=False)
        # a lone surrogate goes out as the escape JSON has for it, as the CLI's
        # own serialiser writes it; stdout cannot encode it as it is
        sys.stdout.write(line.encode("utf-8", "backslashreplace").decode("utf-8") + "\n")


def head():
    """`system/init`, which the CLI prints before it calls the model, and one
    event of a type the tool does not know. Once per run, stream mode only."""
    if not STREAM or _HEAD[0]:
        return
    _HEAD[0] = True
    put({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": SESSION,
         "tools": [], "mcp_servers": [], "model": "claude-fake-1", "permissionMode": "default",
         "apiKeySource": "none", "claude_code_version": "0.0.0-fake", "uuid": uid()},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50,
         "estimated_tokens_delta": 50, "session_id": SESSION, "uuid": uid()})
    sys.stdout.flush()


def thinking(text=""):
    return {"type": "thinking", "thinking": text, "signature": "c2lnLWZha2U="}


def text(t):
    return {"type": "text", "text": t}


def assistant(mid, block, n_in=575):
    """One content block of one model message: an event of its own, as live.
    `stop_reason` is null even in a message cut at the cap, and `usage` is a
    snapshot from the start of the message — neither is worth reading."""
    return {"type": "assistant",
            "message": {"model": "claude-fake-1", "id": mid, "type": "message",
                        "role": "assistant", "content": [block], "container": None,
                        "stop_reason": None, "stop_sequence": None,
                        "usage": dict(usage(n_in, 3), service_tier="standard"),
                        "context_management": None},
            "parent_tool_use_id": None, "session_id": SESSION, "uuid": uid(),
            "timestamp": "2026-10-03T08:15:03.723Z", "request_id": uid("req_")}


def resume(n=0, say=RESUME_TEXT):  # noqa: ARG001 — `n` is the place it stands at, see messages()
    """A request the CLI makes of the model by itself: by default the one to go
    on after a message that hit the output cap."""
    return {"type": "user", "message": {"role": "user", "content": [text(say)]},
            "parent_tool_use_id": None, "session_id": SESSION, "uuid": uid(),
            "timestamp": "2026-10-03T08:15:05.201Z", "isSynthetic": True}


def plain_user(body):
    """A `user` event the CLI did not make up as a request to go on (a tool
    result is one): no isSynthetic."""
    return {"type": "user", "message": {"role": "user", "content": [body]},
            "parent_tool_use_id": None, "session_id": SESSION, "uuid": uid(),
            "timestamp": "2026-10-03T08:17:27.102Z"}


def api_error(message, error):
    """The message the CLI writes itself when a call ends in an API error."""
    return {"type": "assistant",
            "message": {"id": uid(), "model": "<synthetic>", "role": "assistant",
                        "type": "message", "stop_reason": "stop_sequence", "stop_sequence": "",
                        "usage": usage(0, 0), "content": [text(message)],
                        "context_management": None},
            "parent_tool_use_id": None, "session_id": SESSION, "uuid": uid(),
            "timestamp": "2026-10-03T08:16:39.184Z",
            "error": error, "is_api_error_message": True, "api_error": error}


def messages(*msgs, between=resume, think="THINKING-NOT-THE-ANSWER"):
    """The events of several model messages. A message is a text, or None for
    one that is all thinking. `between(n)` makes the event that stands before
    message number n, counted from 0 (None: nothing stands between them)."""
    out = []
    for n, body in enumerate(msgs):
        if n and between is not None:
            out.append(between(n))
        mid = uid("msg_")
        # live haiku sends the thinking text empty; a model that shows it must
        # not get it into the answer either, hence the marker
        out.append(assistant(mid, thinking(("%s-%d" % (think, n + 1)) if think else "")))
        if body is not None:
            out.append(assistant(mid, text(body)))
    return out


def emit(result, extra=None, is_error=False, n_in=575, out=40, usage_doc=None, events=None,
         last="whole"):
    """Every mode ends here. `events` are the `assistant`/`user` events of the
    run; left out, they are made from the document: one message with the
    result as its text, or the CLI's own error message. `last` is what becomes
    of the `result` event: "whole", "cut" (half a line) or "none"."""
    doc = {"type": "result", "subtype": "success" if not is_error else "error",
           "is_error": is_error, "result": result, "api_error_status": None,
           "usage": usage(n_in, out) if usage_doc is None else usage_doc,
           "session_id": "fake", "num_turns": 1,
           "duration_ms": 1234, "total_cost_usd": 0.0017, "permission_denials": [],
           "uuid": uid()}
    if not is_error:
        doc.update(stop_reason="end_turn", terminal_reason="completed")
    if extra:
        doc.update(extra)
    if not STREAM:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False))
        return 0
    head()
    if events is None:
        if is_error:
            events = [api_error(result, doc.get("error") or doc.get("api_error") or "unknown")]
        else:
            events = messages(result if result else None, think="")
    put(*events)
    put({"type": "rate_limit_event", "session_id": SESSION, "uuid": uid(),
         "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                             "utilization": 0.52, "isUsingOverage": False}})
    line = json.dumps(doc, ensure_ascii=False)
    if last == "whole":
        put(line)
    elif last == "cut":
        sys.stdout.write(line[:len(line) // 2])
    return 0


def emit_wall(text, rc=0):
    """The shape a spent window really has, copied from a recorded one:
    `subtype` still says success, `is_error` is true and `api_error_status` is
    429. The process exit code has been seen as both 0 and 1, so it is a knob."""
    emit(text, extra={"subtype": "success", "api_error_status": 429}, is_error=True,
         n_in=0, out=0)
    return rc


def bump(name):
    path = os.path.join(STATE, name)
    try:
        with open(path) as f:
            n = int(f.read().strip() or 0)
    except Exception:
        n = 0
    n += 1
    with open(path, "w") as f:
        f.write(str(n))
    return n


def list_children():
    """A pgrep fixture for the fake's recorded process trees.

    Process listing can be unavailable in a sandbox. Only discovery is faked:
    the timeout and signal tests still have to kill real child processes.
    """
    if len(sys.argv) != 3 or sys.argv[1] != "-P":
        return 2
    children = []
    for name in ("hang.pids", "pids.log"):
        try:
            with open(os.path.join(STATE, name)) as f:
                for line in f:
                    pids = line.split()
                    if pids and pids[0] == sys.argv[2]:
                        children.extend(pids[1:])
        except FileNotFoundError:
            pass
    if children:
        print("\n".join(children))
    return 0 if children else 1


class Leave(Exception):
    """The CLI ends the run while it reads its command line, before it reads
    stdin: (text, exit code). An error goes to stderr with exit 1; --version
    and --help go to stdout with exit 0."""


def parse(argv, unknown=()):
    """(options, positional arguments) of argv, read as the CLI's parser
    (commander) reads it. A flag that takes a value takes the part after `=`
    or the next argument, whatever that is. `--` ends the options, and "-"
    alone is an argument. Anything else that begins with "-" and is not a
    known flag is an unknown option. `unknown` names flags to treat as unknown."""
    opts, words, rest = {}, [], list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            words += rest
            break
        if len(arg) < 2 or not arg.startswith("-"):
            words.append(arg)
            continue
        if arg in AT_ONCE:
            raise Leave(AT_ONCE[arg], 0)
        name, eq, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        takes = None if name in unknown else FLAGS.get(name)
        if takes is None or (eq and not takes):
            raise Leave("error: unknown option '%s'" % arg, 1)
        if not takes:
            opts[name] = True
        elif eq:
            opts[name] = value
        elif rest:
            opts[name] = rest.pop(0)
        else:
            raise Leave("error: option '%s <value>' argument missing" % name, 1)
    return opts, words


def note(seen):
    """Append what this run was given to received.jsonl."""
    with open(os.path.join(STATE, "received.jsonl"), "a") as f:
        f.write(json.dumps(seen) + "\n")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    global STREAM
    argv = sys.argv[1:]
    if os.environ.get("C_LEAN_FAKE_ASSERT_ENV"):
        for k in BANNED:
            if k in os.environ:
                sys.stderr.write("fake_claude: banned env var %s present\n" % k)
                return 90
        if os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") != "128000":
            sys.stderr.write("fake_claude: CLAUDE_CODE_MAX_OUTPUT_TOKENS is %r, not 128000\n"
                             % os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"))
            return 90
    if "--bare" in argv:
        sys.stderr.write("fake_claude: API-key-only mode must never be used\n")
        return 91
    bump("calls.count")
    with open(os.path.join(STATE, "cwd.log"), "a") as f:
        f.write(os.getcwd() + "\n")
    with open(os.path.join(STATE, "argv.jsonl"), "a") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")
    seen = {"argv": argv, "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir("."))}
    unknown = [n.strip() for n in os.environ.get("C_LEAN_FAKE_UNKNOWN", "").split(",") if n.strip()]
    try:
        opts, words = parse(argv, unknown)
    except Leave as e:
        said, code = e.args
        note(dict(seen, refused=said, exit=code))
        (sys.stderr if code else sys.stdout).write(said + "\n")
        return code
    STREAM = opts.get("--output-format") == "stream-json"
    if words:
        raw, seen["prompt_via"] = words[0].encode("utf-8", "surrogateescape"), "argv"
    else:
        raw, seen["prompt_via"] = sys.stdin.buffer.read(), "stdin"
    prompt = raw.decode("utf-8", "replace")
    seen.update(prompt_chars=len(prompt), prompt_sha256=digest(raw),
                system_flags=[n for n in ("--system-prompt", "--system-prompt-file") if n in opts])
    path = opts.get("--system-prompt-file")
    if path is not None:
        try:
            with open(path, "rb") as f:
                system = f.read()
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as e:
            note(dict(seen, refused=str(e), exit=1))
            sys.stderr.write("error: cannot read the system prompt file %s: %s\n" % (path, e))
            return 1
        seen.update(system_file=path, system_file_mode=mode,
                    system_file_dir=os.path.dirname(os.path.realpath(path)))
    else:
        system = opts.get("--system-prompt", "").encode("utf-8", "surrogateescape")
    seen.update(system_chars=len(system.decode("utf-8", "replace")), system_sha256=digest(system))
    note(seen)
    if MODE not in ("noise", "noisewall"):
        # the live CLI prints `system/init` before it calls the model: whatever
        # a mode does next, the stream has begun
        head()


    # ------------------------------------------------- answers over the cap
    if MODE == "cont3":
        return emit(CONT_PIECES[-1], events=messages(*CONT_PIECES), n_in=1725, out=120,
                    extra={"num_turns": 3},
                    # Totals cover all messages; iterations holds only the last.
                    usage_doc=dict(usage(1725, 120), iterations=[
                        dict(usage(575, 40), type="message")]))
    if MODE == "contcut":
        return emit("CUT two", extra={"num_turns": 2}, events=messages(
            "CUT one ", "CUT two", between=lambda n: resume(say=CUT_TEXT)))
    if MODE == "contnovis":
        return emit("NOVIS three", extra={"num_turns": 3}, events=messages(
            "NOVIS one ", None, "NOVIS three",
            between=lambda n: resume(say=NOVIS_TEXT if n == 2 else RESUME_TEXT)))
    if MODE == "contthink":
        whole = "FULL ANSWER, after a message that was all thinking"
        return emit(whole, events=messages(None, whole), extra={"num_turns": 2})
    if MODE == "thinkresume":
        # no `user` event: the cut message was one thinking block, and the CLI
        # asked for it again with the mark below on every event of the new request
        whole = "FULL ANSWER, after thinking that was resumed"
        first, second = uid("msg_"), uid("msg_")
        return emit(whole, extra={"num_turns": 1}, events=[
            assistant(first, thinking("THINKING-NOT-THE-ANSWER-1")),
            dict(assistant(second, thinking("THINKING-NOT-THE-ANSWER-2")),
                 resumed_from_incomplete_thinking=True),
            dict(assistant(second, text(whole)), resumed_from_incomplete_thinking=True)])
    if MODE == "twoblocks":
        mid = uid("msg_")
        return emit(TWO_BLOCKS[1], events=[
            assistant(mid, text(TWO_BLOCKS[0])),
            assistant(mid, thinking("THINKING-NOT-THE-ANSWER-1")),
            assistant(mid, text(TWO_BLOCKS[1]))])
    if MODE == "contempty":
        return emit("", events=messages("PART-ONE and nothing after it", None),
                    extra={"num_turns": 2})
    if MODE == "conttail":
        return emit("\n", events=messages("PART-ONE and a newline after it", "\n"),
                    extra={"num_turns": 2})
    if MODE in ("contfail", "contfailerr"):
        # the marker has two names in the CLI: a document may carry one of them
        marker = ({"error": "max_output_tokens"} if MODE == "contfailerr"
                  else {"api_error": "max_output_tokens"})
        emit(API_ERROR_TEXT,
             events=messages(*CUT_PIECES) + [api_error(API_ERROR_TEXT, "max_output_tokens")],
             is_error=True, n_in=2636, out=400,
             extra=dict({"subtype": "success", "stop_reason": "stop_sequence",
                         "terminal_reason": "api_error", "num_turns": 4}, **marker))
        return 1
    if MODE == "safety":
        return emit("SAFE two", extra={"num_turns": 2}, events=messages(
            "SAFE one ", "SAFE two", between=lambda n: resume(say=SAFETY_TEXT)))
    if MODE in ("supersedes", "retracted"):
        # every other condition of a join holds: the request to go on stands
        # between the two messages, and the last block is the `result`
        events = messages("TAKEN-BACK one ", "KEPT two")
        gone = events[1]["uuid"]
        if MODE == "supersedes":
            events[-1]["supersedes"] = [gone]
            events[-2]["supersedes"] = [gone]
        else:
            events.insert(2, {"type": "system", "subtype": "model_refusal_fallback",
                              "retracted_message_uuids": [gone], "session_id": SESSION,
                              "uuid": uid()})
        return emit("KEPT two", extra={"num_turns": 2}, events=events)
    if MODE == "twomsg":
        return emit("FINAL two", extra={"num_turns": 2}, events=messages(
            "DRAFT one", "FINAL two",
            between=lambda n: plain_user({"type": "tool_result", "tool_use_id": "toolu_fake",
                                          "content": "noted"})))
    if MODE == "contbroken":
        # a request to go on before the second message, a plain event before the third
        return emit("LINK three", extra={"num_turns": 3}, events=messages(
            "LINK one ", "LINK two ", "LINK three",
            between=lambda n: resume() if n == 1 else plain_user(
                {"type": "tool_result", "tool_use_id": "toolu_fake", "content": "noted"})))
    if MODE == "contmismatch":
        return emit("SOMETHING ELSE", events=messages("PART-A ", "PART-B"), extra={"num_turns": 2})
    if MODE == "noise":
        sys.stdout.write("(node:123) Warning: deprecated API\n")
        head()
        body = "ECHO:" + prompt.strip()[:200]
        mid = uid("msg_")
        return emit(body, out=40, events=[
            "{\"type\": \"assistant\", \"message\": {\"content\": [",   # a line that broke off
            assistant(mid, thinking()),
            "[1, 2, 3]", "\"a JSON string, not an object\"", "",
            {"type": "brand_new_event", "text": "an event type nobody has met yet",
             "session_id": SESSION, "uuid": uid()},
            assistant(mid, text(body))])
    if MODE == "linesep":
        return emit("first line second line\u0085third line end")
    if MODE in ("cutstream", "cutwallwords", "cutwallstderr"):
        if MODE == "cutwallstderr":
            sys.stderr.write(WALL_TEXT + "\n")
        emit("", last="none", events=messages(
            WALL_TEXT if MODE == "cutwallwords" else "the start of an answer that never en"))
        return 1
    if MODE in ("conthang", "thinkhang"):
        mid = uid("msg_")
        if MODE == "conthang":
            put(*(messages("the whole first message") + [resume()]))
        else:
            put(assistant(mid, thinking()))
        sys.stdout.flush()
        time.sleep(45)
        return 0
    if MODE == "script":
        with open(os.environ["C_LEAN_FAKE_SCRIPT"]) as f:
            plan = json.load(f)
        if plan.get("stderr"):
            sys.stderr.write(plan["stderr"])
            sys.stderr.flush()
        put(*plan.get("events", []))
        sys.stdout.flush()
        if plan.get("tail_hex"):
            # raw bytes after the last line, no newline: a character cut in two
            sys.stdout.buffer.write(bytes.fromhex(plan["tail_hex"]))
            sys.stdout.buffer.flush()
        if plan.get("end") == "hang":
            time.sleep(45)
        if plan.get("end") == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        return int(plan.get("rc", 0))
    if MODE == "sigdeath":
        put(*messages("text that had come before the process died"))
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    if MODE == "ratelimit_type":
        # the structural marker seen in real transcripts next to the wall text:
        # error type rate_limit — but this CLI reports no HTTP status
        emit("Nothing to do until the window resets.", is_error=True, n_in=0, out=0,
             extra={"error": "rate_limit", "subtype": "error"})
        return 1
    if MODE == "usageinout":
        return emit("OK", usage_doc={"input_tokens": 575, "output_tokens": 3})

    if MODE in ("hang", "linger"):
        if MODE == "linger":
            # the whole answer, `result` included, and then no exit
            emit("ECHO:" + prompt.strip()[:200])
            sys.stdout.flush()
        kid = subprocess.Popen(["/bin/sleep", "45"])
        with open(os.path.join(STATE, "hang.pids"), "w") as f:
            f.write("%d %d\n" % (os.getpid(), kid.pid))
        time.sleep(45)
        return 0
    if MODE == "sleeper":
        secs = float(os.environ.get("C_LEAN_FAKE_SLEEP", "20"))
        kid = subprocess.Popen(["/bin/sleep", str(secs)])
        with open(os.path.join(STATE, "pids.log"), "a") as f:
            f.write("%d %d\n" % (os.getpid(), kid.pid))
        time.sleep(secs)
        return emit("ECHO:" + prompt.strip()[:200])
    if MODE == "envdump":
        with open(os.path.join(STATE, "env.json"), "w") as f:
            json.dump(dict(os.environ), f, ensure_ascii=False)
        return emit("ECHO:ok")
    if MODE == "limit":
        return emit_wall(WALL_TEXT, rc=int(os.environ.get("C_LEAN_FAKE_WALL_RC", "0")))
    if MODE == "credits":
        # the same wall from a CLI that has no api_error_status field: the
        # failure is structural (is_error, rc 1) and only the wording identifies it
        emit(CREDITS_TEXT, is_error=True, n_in=0, out=0)
        return 1
    if MODE == "wall429":
        # tomorrow's wording, today's structure: the only thing we can rely on
        return emit_wall("Your plan is paused. Nothing to do until Tuesday.")
    if MODE == "wallwords":
        # a real answer about the wall: the data itself is billing support text
        emit(WALL_TEXT + " — that is the sentence to translate")
        return int(os.environ.get("C_LEAN_FAKE_RC", "0"))
    if MODE == "wallstderr":
        sys.stderr.write("noise\n" + WALL_TEXT + "\n")
        return 1
    if MODE == "noisewall":
        # one line of CLI noise before the stream (node does that), a `result`
        # event that broke off half way, and an answer that itself says `rate
        # limit`: there is no document, and the only wall words on stdout are
        # the model's — in the message of the stream and in the broken line
        sys.stdout.write("(node:123) Warning: deprecated API\n")
        emit("the vendor returned a rate limit error", last="cut")
        return 1
    if MODE == "wallstdout":
        # the CLI's own sentence on stdout, as a plain line AFTER the stream has
        # begun, then a `result` event it never finished
        sys.stdout.write(WALL_TEXT + "\n{\"type\": \"result\", \"is_err")
        return 1
    if MODE == "cutwalljson":
        # the sentence only inside lines that begin with `{`: an event that
        # broke off in the middle of the model's text, and a whole JSON object
        # of a type nobody knows. Neither is the CLI speaking for itself.
        sys.stdout.write("{\"type\": \"brand_new_event\", \"text\": \"%s\"}\n" % WALL_TEXT
                         + "  {\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": "
                           "\"text\", \"text\": \"%s" % WALL_TEXT)
        return 1
    if MODE == "ratewords":
        return emit("the log line rate_limit means we were throttled")
    if MODE == "fail":
        return emit("Execution error", is_error=True)
    if MODE in ("schema", "schemacont"):
        if "--json-schema" not in argv:
            return emit("no schema given", is_error=True)
        # As live: ONE message, a thinking block and a tool_use block, no text;
        # then the tool result, a `user` event that is no request to go on;
        # num_turns 2 and stop_reason tool_use without any continuation.
        verdict = {"verdict": "yes"}
        as_text = json.dumps(verdict)
        mid = uid("msg_")
        tool = {"type": "tool_use", "id": "toolu_fake", "name": "StructuredOutput",
                "input": verdict, "caller": {"type": "direct"}}
        done = plain_user({"type": "tool_result", "tool_use_id": "toolu_fake",
                           "content": "Structured output provided successfully"})
        if MODE == "schemacont":
            # never seen live: a first message cut at the cap, then the request
            # to go on, then a message with a text block next to the tool call
            first = uid("msg_")
            events = [assistant(first, thinking()), assistant(first, text("PART-A ")), resume(),
                      assistant(mid, text(as_text)), assistant(mid, tool), done]
        else:
            events = [assistant(mid, thinking()), assistant(mid, tool), done]
        return emit(as_text, events=events,
                    extra={"structured_output": verdict, "num_turns": 2, "stop_reason": "tool_use"})
    if MODE == "single":
        return emit("ECHO:" + prompt.strip()[:200])
    if MODE == "usage_iters":
        # no flat totals at all: only the per-message list (100+200 in, 20+30 out).
        # The live CLI's iterations carry the cache counters too, so the fake does.
        return emit("ECHO:" + prompt.strip()[:80], usage_doc={"iterations": [
            {"input_tokens": 100, "output_tokens": 20, "type": "message",
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            {"input_tokens": 200, "output_tokens": 30, "type": "message",
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]})
    if MODE == "usagepartial":
        # a rename or a partial document: `in` survived, `out` did not
        return emit("ECHO:" + prompt.strip()[:80], usage_doc={"input_tokens": 575})
    if MODE == "usagenone":
        # every counter under a name we do not know: nothing can be read
        return emit("OK", usage_doc={"prompt_tokens": 575, "completion_tokens": 40})
    if MODE == "terminal":
        return emit("a complete-looking answer",
                    extra={"stop_reason": "end_turn", "terminal_reason": "max_output_tokens"},
                    out=40)
    if MODE == "msgstop":
        return emit("a complete-looking answer",
                    extra={"message": {"stop_reason": "max_tokens"}}, out=40)
    if MODE == "emptysingle":
        return emit("")
    if MODE == "cli":
        def leave(code, secs, doc=None):
            def handler(signum, frame):  # noqa: ARG001
                time.sleep(secs)
                if doc is not None:
                    sys.stdout.write(json.dumps(doc) + "\n")
                    sys.stdout.flush()
                os._exit(code)
            return handler
        signal.signal(signal.SIGHUP, leave(129, 0.04))
        signal.signal(signal.SIGTERM, leave(143, 0.13))
        signal.signal(signal.SIGINT, leave(0, 0.1, {
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": "", "usage": usage(0, 0)}))
        with open(os.path.join(STATE, "pids.log"), "a") as f:
            f.write("%d\n" % os.getpid())
        time.sleep(float(os.environ.get("C_LEAN_FAKE_SLEEP", "0.5")))
        return emit("ECHO:" + prompt.strip()[:200])
    if MODE == "maxtokens":
        # a whole-looking answer that the CLI says was cut at the output cap
        return emit("a complete-looking answer", extra={"stop_reason": "max_tokens"})
    # default: ok
    return emit("ECHO:" + prompt.strip()[:200])


if __name__ == "__main__":
    sys.exit(list_children() if os.path.basename(sys.argv[0]) == "pgrep" else main())
