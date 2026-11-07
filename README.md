# c-lean

Cheap delegation for Claude Code. The plugin gives Claude two light ways to hand work to a model, and a skill that tells it which one to pick:

- `c-lean`, a command that makes one model call with no agent harness. It starts at about 0.6K tokens of context.
- `sub-scout` and `sub-coder`, two subagents that start without CLAUDE.md and with a narrow tool set. They start at about 3.8K and 4.4K tokens, a little more inside the desktop app.

A general-purpose subagent starts at tens of thousands of tokens: the full Claude Code system prompt, the definition of every tool the session has, and your CLAUDE.md files. For a quick lookup or a small judgement most of that is never used, and every call pays for it again.

## What's inside

| Path | What it is |
|---|---|
| `bin/c-lean` | One model call through `claude -p` with no harness. While the plugin is enabled, Claude Code puts `bin/` on the PATH of the Bash tool, so Claude runs it by name. |
| `agents/sub-scout.md` | Read-only subagent: Read, Grep, Glob. |
| `agents/sub-coder.md` | Subagent for one bounded edit: Read, Edit, Write, Bash. |
| `skills/lean-delegation/SKILL.md` | When to use which option, and how to brief it. |
| `tests/` | Tests against a fake `claude` binary. No model is called. |

## Install

From GitHub:

```bash
claude plugin marketplace add zrk/c-lean
claude plugin install c-lean@c-lean
```

Inside a session the same works as `/plugin marketplace add zrk/c-lean` and `/plugin install c-lean@c-lean`.

**PATH shadowing.** Claude Code 2.1.316 appends a plugin's `bin/` to the end of `PATH`, and only for the session's Bash tool, so any other `c-lean` earlier on `PATH` (a copy you installed by hand, a symlink in `~/.local/bin`) shadows the plugin's. Check from inside a session, not from your own terminal, where the plugin's copy never appears: ask Claude to run `type -a c-lean` through its Bash tool. The first line is the one that runs, and it should be the plugin's.

To try it without installing, load it for one session:

```bash
claude --plugin-dir /path/to/c-lean
```

Requirements:

- A POSIX system: Linux or macOS. Windows is not supported: `c-lean` registers `signal.SIGHUP`, which does not exist there, so it fails at import, and it relies on `os.killpg`, process groups and `selectors` on pipes. (Read off the code; not run on Windows.)
- Claude Code 2.1.271 or later, for `omitClaudeMd` in agent files. The numbers below were measured on 2.1.311, except where a row names another version.
- Python 3.10 or later, for `c-lean`.
- For `--system`, `--system-file` and `--check`, a CLI version that supports `--system-prompt-file`. Update Claude Code if it reports that option as unknown; calls without a system prompt do not need it.
- For `c-lean`: Claude Code signed in with a Claude subscription. The subagents work with whatever provider your session uses.

## c-lean

```bash
c-lean -m haiku "Classify this commit message as feat, fix or chore: ..."
c-lean -m sonnet -e high -f brief.md
cat brief.md | c-lean -m opus
c-lean -m haiku --system-file rubric.md -f item.txt
c-lean -m haiku --schema schema.json "..."
c-lean --check
```

- With `--json`, stdout carries the CLI's `result` document whenever the call received one, whatever the outcome (a cut-off answer, an error, a limit wall, a stop after the `result`), whole unless a stop comes after the call, before it is all printed (see the stop signals below); the exit code and stderr are unchanged (except exit 120 when stdout cannot be written, as for any answer). A call that ended before any `result` has no document and prints nothing.
- There is no default model. Choose model and effort for each task.
- The prompt is assembled in a fixed order: the positional words first (they may stand between options, so `c-lean -m haiku first -e low second` sends "first second"; after `--` every argument is a word), then each `-f FILE` in the order given, whatever the order on the command line. `-f -` reads stdin at that place: `cat data | c-lean -m haiku "Summarize:" -f -` sends the words and then the data. Stdin is read without `-f -` only when it is the only input. `-f /dev/stdin` is not reported as unread. When a prompt comes from words or `-f` and stdin holds data, `c-lean` does not read it and prints one WARN on stderr that says so; add `-f -` to include it, or redirect `</dev/null` to silence the WARN. What counts as data depends on the kind of stdin, and nothing is read or waited for to tell: a pipe that is not at EOF and empty (a producer that is still running counts even before its first byte, so a slow `curl … | c-lean "x"` is not dropped silently), a socket with bytes already waiting, or a file with bytes left after its offset. A terminal, `/dev/null`, any other character device, a closed stdin, a write-only fd and a listening socket give no WARN. The gap: an idle socket is silent because Claude Code's Bash tool leaves one open on a command with a `<` in its text, so a socket whose first bytes come later is not warned about (use `-f -`), and neither are a file that reports size 0 (`/proc`, cgroup v1 files), a datagram socket whose first datagram is empty, or a named FIFO that gets a new writer after EOF; on a platform where that tool gives a FIFO instead (macOS under Bun is plausible, unverified) such a command would get the WARN. In a loop, `while read -r x; do c-lean … "$x"; done < list` or `cat list | …`, stdin is the list and every pass but the last gives the WARN: give `</dev/null` to `c-lean` there.
- stdout carries only the answer. stderr carries a trailer with token counts and the exit code of the process. `NOT COUNTED` in the trailer means the counts are not exact, so read them as a lower bound: the usage report of `claude -p` was missing or malformed, or the call was stopped by the timeout or a signal. A malformed usage report never costs the answer.
- Each call has a timeout: `--timeout SEC` overrides the effort's default of 1200 s (no `-e`, low, medium or high), 3600 s at `-e xhigh` and 6000 s at `-e max`. If `claude -p` has printed its result and does not exit within 5 seconds, `c-lean` stops it, prints the answer and a warning.
- SIGINT/SIGTERM/SIGHUP stop the call with its process tree and exit 128+signal (130/143/129), `INTERRUPTED` and `NOT COUNTED`, whatever the CLI does after the signal. They also interrupt reads of the prompt, `--system-file` and `--schema` immediately (stdin, `-f -`, a FIFO), without making a call. A good whole `result` already received stands: the answer is judged by that event and a WARN says the stop came after the answer. A stop after the call, until the trailer and the answer are printed, still exits 128+signal, even when the reader does not read: at once, or about 1 s later when the stop lands in a write to stderr that blocks, and stderr is given at most 1 s more for the lines that say so. A wait for the usage log or the `--check` record ends at once too. stdout may then hold part of the answer, the text blocks of the call (if any) are saved to a file, and the trailer and the usage log keep the exit `c-lean` had decided. Lines written before the call ends (a WARN about `C_LEAN_PASS_ENV` or stdin, or one of the call) go to stderr as they come: a reader that does not read can hold them, and a stop then waits for it. So it holds the end of the run after a stop before or during the call, until a second stop ends it.
- Every stop signal inherited as `SIG_IGN` stays ignored. If any one was ignored at start (SIGHUP under `nohup`, SIGINT in some background shell jobs), the CLI runs in a session of its own so job signals cannot override that ignore through the CLI's own handlers. Every kill of such a call also sends SIGKILL to its process group. Otherwise the CLI stays in the job, so signals sent to the whole job reach it too, including a supervisor's SIGKILL. A SIGKILL sent to the job cannot reach a call in its own session. SIGKILL, SIGQUIT and other fatal signals that `c-lean` does not catch end it on the spot, without a trailer or usage-log line; a call they did not reach may continue.
- Linux finds the process tree through `/proc`, without a subprocess. Other systems walk `pgrep -P`, using `pgrep` from PATH, else `/usr/bin/pgrep`. The whole tree is collected before killing it. If discovery fails, the direct child is still killed and one line per run says `c-lean: WARN could not list the processes of the call (<reason>); some may still run`. A process already reparented away from the call cannot be found by that walk.
- After any kill, `c-lean` drains output for at most 3 seconds, closes its pipes and warns if a process still held them open and may still run. If the CLI exits but its pipes stay open for 5 seconds after its exit, the tree is killed and drained in the same way, with a WARN; the CLI's output and its own exit code decide the call. A whole final `result` needs no trailing newline; it starts the 5-second exit grace even if the CLI closes its output but keeps running.
- Each call runs `claude -p` in a fresh private empty directory with an empty system prompt, no tools, no settings files, no MCP servers and no slash commands.
- The prompt goes to `claude -p` on stdin as UTF-8, never on its command line. A system prompt from `--system` or `--system-file` goes in a temporary file with mode 0600, in its own directory with mode 0700 under TMPDIR, outside the call's cwd; both are removed when the call ends. So a prompt that begins with `-` is not read as an option, other local users cannot read either text in the process list, and the command-line length limit does not apply to them. A `--schema` still goes on the command line; one too long for it is a usage error (exit 64).
- `C_LEAN_CLAUDE_BIN` selects a CLI binary or wrapper. A wrapper that sandboxes the file system must let the CLI read the system-prompt file in TMPDIR.
- The child process gets an allow-listed environment. Every `ANTHROPIC_*`, `CLAUDE_*` and cloud-provider variable is dropped (bar the four opt-outs below), so an API key or a provider setting in the calling shell cannot move the call off your subscription. A proxy or an extra CA certificate passes only when you name it in `C_LEAN_PASS_ENV`. The opt-outs `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY`, `DO_NOT_TRACK` and `DISABLE_ERROR_REPORTING` pass when you export them in the shell; c-lean never sets them. The `env` block of settings.json is not read, so an opt-out configured there is lost. A refused name in `C_LEAN_PASS_ENV` costs one WARN that names it, never its value; so does an entry that is not a variable name (letters, digits, `_`), and that entry is not passed.
- `c-lean` sets the child's `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to 128000 (`OUTPUT_CAP` in `bin/c_lean_core.py`); the CLI clamps it to the model's upper bound. If a result names a default cap above it, a WARN says which model was held down; raise `OUTPUT_CAP` for that model.
- When the model's answer is cut and continued, the pieces are joined. Text that could not be printed as an answer is saved under `$XDG_STATE_HOME/c-lean/partial`, by default `~/.local/state/c-lean/partial`.
- Each call appends one line of counters to `$XDG_STATE_HOME/c-lean/usage.jsonl`, with the same exit code as the trailer, and with `tokens_unknown: true` when the trailer says `NOT COUNTED`. The line stores a sha256 hash of the prompt, not the prompt. That is not anonymization: anyone who holds a candidate prompt can confirm it against the hash.
- Data kept on disk. The usage log is `$XDG_STATE_HOME/c-lean/usage.jsonl` (`~/.local/state/c-lean/usage.jsonl` when `XDG_STATE_HOME` is unset or empty); `C_LEAN_USAGE_LOG` overrides the path. Text of an answer that was not printed goes to one file per call in `$XDG_STATE_HOME/c-lean/partial/` (files 0600; the directory is 0700 only when `c-lean` creates it, an existing directory keeps its mode); `C_LEAN_PARTIAL_DIR` overrides the directory. `partial/` holds model text, so treat it like the answers themselves. Nothing is ever pruned: both grow without bound. To prune, delete old files, for example `find ~/.local/state/c-lean/partial -type f -mtime +30 -delete`, and truncate or remove `usage.jsonl` (`(umask 077; f=~/.local/state/c-lean/usage.jsonl; tail -n 1000 "$f" > "$f.new") && mv ~/.local/state/c-lean/usage.jsonl.new ~/.local/state/c-lean/usage.jsonl` keeps the last 1 000 lines; run it when no `c-lean` call is in flight, or a concurrent append can be lost).
- `c-lean --check` makes one fixed live call to Haiku and reports the size of the start-up context. It ignores the prompt and every flag except `--timeout` and `--backend`. With `C_LEAN_CHECK_PROBE_FILE=PATH` it appends a JSON record of the probe to PATH, with the exit code `--check` returns; a file it creates there gets mode 0600, and a record that cannot be written costs one warning.
- Files given to `-f`, `--system-file` and `--schema`, and a prompt on stdin, are read as UTF-8 whatever the locale; bytes that are not UTF-8 are a usage error. The answer goes to stdout as UTF-8 whatever the encoding of stdout.
- `c-lean -h` lists every flag, exit code and environment variable.

| Exit | Meaning |
|---|---|
| 0 | the answer was printed |
| 1 | the call failed, an empty answer included |
| 64 | usage: a bad flag or value, a file that cannot be read or is not UTF-8, a `--schema` the command line cannot carry |
| 69 | the `claude` binary cannot be run |
| 71 | a local resource the call needs could not be made: its working directory, the system-prompt file, the stdin pipe or the thread that writes the prompt |
| 75 | the limit wall, not an ordinary failure |
| 120 | stdout or stderr could no longer be written. A stderr closed from the start (`2>&-`) is not a stream that fails: its lines are dropped, the answer is printed, and the exit stays what `c-lean` decided |
| 124 | timeout, before `claude -p` printed its result |
| 128+N | stopped by signal N (130, 143, 129), also after the call until the end of the run is printed, or 137 when `claude -p` died of SIGKILL |

## sub-scout and sub-coder

- Both start without CLAUDE.md (`omitClaudeMd: true`), with their own short prompt in place of the Claude Code system prompt, and with a narrow tool list. Give them a self-contained brief: the paths, the question or the change, and the check to run.
- The caller names `model` and `effort` in each Agent call to `c-lean:sub-scout` or `c-lean:sub-coder`, as with `-m` and `-e` of `c-lean`.
- Both agent files name the model `pass-model-and-effort-in-the-call`, on purpose. No such model exists, so an Agent call without `model` fails with a model-not-found error instead of running on another model (checked on Claude Code 2.1.319).
- The files cannot enforce effort: Claude Code ignores an `effort` value it does not know. So they name no effort, and a call without `effort` runs at the session's effort.
- Claude Code picks a subagent's model in this order: the `model` passed in that Agent call, the `model` in the agent file, the `CLAUDE_CODE_SUBAGENT_MODEL` environment variable, the session's model. In these two agent files the second step is the placeholder, which fails. A per-call `effort` works the same way. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` pins every subagent to one model.

## Measurements

Start-up context is `input + cache_creation + cache_read` of the first model request. For subagents it was read from the usage that the Agent tool reports. Conditions: `claude -p` on Claude Code 2.1.311, subagents on `claude-sonnet-5-5`, `c-lean` on `haiku`, a one-line task, a working directory outside git, hooks off.

| What starts | Tokens |
|---|---|
| `c-lean`: empty system prompt, no tools (CLI 2.1.311) | 579 |
| `c-lean --check`: haiku at effort low, with a short system prompt, no tools (CLI 2.1.316, 2026-11-01) | 412 |
| custom subagent, `tools: []`, `omitClaudeMd: true` | 960 |
| custom subagent, Read and Grep, `omitClaudeMd: true` | 3 327 |
| custom subagent, Read, Grep and Bash, `omitClaudeMd: true` | 3 383 |
| the same subagent without `omitClaudeMd` | 3 383 plus the size of your CLAUDE.md files (11 098 in the author's setup) |
| `c-lean:sub-scout`, loaded from this plugin | 3 791 |
| `c-lean:sub-coder`, loaded from this plugin | 4 354 |
| general-purpose subagent | 75K to 98K in the author's setup, with 9 MCP servers and a large CLAUDE.md |

- A parent session with 9 MCP servers and 83 tools gave the same subagent numbers, within one token, as a parent with no settings files and no MCP servers. The one exception was the Bash tool, whose description was 212 tokens longer under the parent without settings files. A narrowed subagent does not inherit the parent's MCP tools or skills.
- Inside a git repository, every subagent except Explore and Plan also receives a git status snapshot: 165 more tokens in a clean repository, up to 1 347 more when the status reaches its length cap.
- Inside a Claude desktop app session a subagent starts higher: the app adds a tool of its own and messages with parts of the session's environment. In the author's runs that added about 0.5K to 0.7K tokens.
- The two `c-lean` rows are not one setup: 579 is an empty system prompt on 2.1.311, 412 is the `--check` probe, which adds a short system prompt of its own, on 2.1.316, so they differ in CLI version and system prompt, and possibly in effort (nothing records the effort used for 579), and are not comparable. The CLI version may account for part of the difference; that is unverified. The 412 is the input-token count that `--check` reported (4 output tokens, 2.9 s); it is the same quantity (`input + cache_creation + cache_read` of the single request; the probe record shows 412/0/0), measured with a different prompt setup and on a different CLI version.
- The general-purpose figure was measured in September 2026 on an older Claude Code. Yours depends on your MCP servers, skills and CLAUDE.md.

## A lean worker outside a session

The subagents run inside a Claude Code session. For a script or a scheduled job, the same idea works with `claude -p` directly:

```bash
env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
  ${USER:+USER="$USER"} ${LOGNAME:+LOGNAME="$LOGNAME"} \
  ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
  ${LANG:+LANG="$LANG"} \
  ${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:+CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"} \
  ${DISABLE_TELEMETRY:+DISABLE_TELEMETRY="$DISABLE_TELEMETRY"} \
  ${DO_NOT_TRACK:+DO_NOT_TRACK="$DO_NOT_TRACK"} \
  ${DISABLE_ERROR_REPORTING:+DISABLE_ERROR_REPORTING="$DISABLE_ERROR_REPORTING"} \
  claude -p --model sonnet --effort high --no-session-persistence \
    --setting-sources "" --strict-mcp-config --disable-slash-commands \
    --tools "Read,Grep,Glob" --allowedTools "Read,Grep,Glob" \
    --system-prompt-file worker-prompt.md \
    --output-format stream-json --verbose \
    < brief.md > run.jsonl
```

- `--setting-sources ""` loads no settings files and no CLAUDE.md. `--strict-mcp-config` with no `--mcp-config` starts no MCP servers.
- `--system-prompt-file` replaces the Claude Code system prompt with the text of the file. Passed as `--system-prompt "$(cat ...)"`, the text would be on the command line, where Linux refuses an argument of 128 KiB or more and other local users can read it.
- `--tools` limits the tool set. `--allowedTools` pre-approves the same tools, so the run never stops at a permission prompt.
- `env -i` starts from an empty environment and passes only what `c-lean` itself passes (`PATH`, `HOME`, `USER`, `LOGNAME`, `TMPDIR`, `XDG_RUNTIME_DIR`, `LANG`; its list also has `SHELL`, `LC_*`, `TZ`, `TERM` and a few more that do not touch authentication). `USER` and `LOGNAME` are kept because the CLI's credential lookup may depend on them (for example the macOS Keychain account), and `XDG_RUNTIME_DIR` because a Linux keyring service may be found through it; neither is verified here. `XDG_CONFIG_HOME` is not in `c-lean`'s list, so it is not passed. A deny-list such as `env -u ANTHROPIC_API_KEY` is not enough: `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR` would still route the call elsewhere. The four telemetry opt-outs are optional pass-throughs; `${VAR:+...}` adds one only when it is set, and works the same in bash and zsh. Add `HTTPS_PROXY` and a CA file the same way only if your machine needs them.
- `--no-session-persistence` keeps the brief and the answer out of the transcripts the CLI would otherwise leave on disk.
- The brief comes on stdin, so its length is not limited by the command line.
- With a slightly broader tool set, workers like this started at 5.5K to 8K tokens in the author's runs.

## Known limits

- `c-lean` is built for a Claude subscription login: it gives the CLI an allow-listed environment and configuration, so API keys and cloud-provider settings (Amazon Bedrock, Google Cloud, Microsoft Foundry) from your session are not forwarded. That is a guarantee about what the call is given, not about how the CLI authenticates or bills on your machine.
- The Claude Code documentation says that `--bare` "will become the default for `-p` in a future release", and bare mode never reads a subscription login. When that happens, `c-lean` and the recipe above stop authenticating until an opt-out exists. API keys and cloud providers keep working in bare mode. The subagents run inside an authenticated session and are not affected.
- The subagents do not see your CLAUDE.md, so project rules reach them only through the brief.

## Tests

```bash
python3 tests/test_c_lean.py
python3 tests/redphase.py
```

`test_c_lean.py` runs the tool against a fake `claude` binary; no model is called. `redphase.py` breaks a throwaway copy of the tool one way at a time and requires the matching test to fail on its own assert, so a test that cannot fail is caught.

The timeout and signal tests find child processes through `/proc` on Linux and the real `pgrep` elsewhere. In a sandbox that forbids listing processes, set `C_LEAN_TEST_FAKE_PGREP=1` to use a fixture for that step; the tests still kill real processes. On macOS there is no `/proc`, so the tests also need `ps` to tell a live process from an unreaped zombie; a sandbox that refuses `ps` there makes `test_a_zombie_is_not_alive` fail, so allow `ps` in the sandbox.

## License

MIT
