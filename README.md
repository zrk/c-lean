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

To try it without installing, load it for one session:

```bash
claude --plugin-dir /path/to/c-lean
```

Requirements:

- Claude Code 2.1.271 or later, for `omitClaudeMd` in agent files. The numbers below were measured on 2.1.311.
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

- There is no default model. Choose model and effort for each task.
- stdout carries only the answer. stderr carries a trailer with token counts and the exit code of the process. `NOT COUNTED` in the trailer means the counts are not exact, so read them as a lower bound: the usage report of `claude -p` was missing or malformed, or the call was stopped by the timeout or a signal. A malformed usage report never costs the answer.
- Each call has a timeout: `--timeout SEC` overrides the effort's default of 1200 s (no `-e`, low, medium or high), 3600 s at `-e xhigh` and 6000 s at `-e max`. If `claude -p` has printed its result and does not exit within 5 seconds, `c-lean` stops it, prints the answer and a warning.
- SIGINT/SIGTERM/SIGHUP stop the call with its process tree and exit 128+signal (130/143/129), `INTERRUPTED` and `NOT COUNTED`, whatever the CLI does after the signal. They also interrupt reads of the prompt, `--system-file` and `--schema` immediately (stdin, `-f -`, a FIFO), without making a call. A good whole `result` already received stands: the answer is judged by that event and a WARN says the stop came after the answer.
- Every stop signal inherited as `SIG_IGN` stays ignored. If any one was ignored at start (SIGHUP under `nohup`, SIGINT in some background shell jobs), the CLI runs in a session of its own so job signals cannot override that ignore through the CLI's own handlers. Every kill of such a call also sends SIGKILL to its process group. Otherwise the CLI stays in the job, so signals sent to the whole job reach it too, including a supervisor's SIGKILL. A SIGKILL sent to the job cannot reach a call in its own session. SIGKILL, SIGQUIT and other fatal signals that `c-lean` does not catch end it on the spot, without a trailer or usage-log line; a call they did not reach may continue.
- Linux finds the process tree through `/proc`, without a subprocess. Other systems walk `pgrep -P`, using `pgrep` from PATH, else `/usr/bin/pgrep`. The whole tree is collected before killing it. If discovery fails, the direct child is still killed and one line per run says `c-lean: WARN could not list the processes of the call (<reason>); some may still run`. A process already reparented away from the call cannot be found by that walk.
- After any kill, `c-lean` drains output for at most 3 seconds, closes its pipes and warns if a process still held them open and may still run. If the CLI exits but its pipes stay open for 5 seconds after its exit, the tree is killed and drained in the same way, with a WARN; the CLI's output and its own exit code decide the call. A whole final `result` needs no trailing newline; it starts the 5-second exit grace even if the CLI closes its output but keeps running.
- Each call runs `claude -p` in a fresh private empty directory with an empty system prompt, no tools, no settings files, no MCP servers and no slash commands.
- The prompt goes to `claude -p` on stdin as UTF-8, never on its command line. A system prompt from `--system` or `--system-file` goes in a temporary file with mode 0600, in its own directory with mode 0700 under TMPDIR, outside the call's cwd; both are removed when the call ends. So a prompt that begins with `-` is not read as an option, other local users cannot read either text in the process list, and the command-line length limit does not apply to them. A `--schema` still goes on the command line; one too long for it is a usage error (exit 64).
- `C_LEAN_CLAUDE_BIN` selects a CLI binary or wrapper. A wrapper that sandboxes the file system must let the CLI read the system-prompt file in TMPDIR.
- The child process gets an allow-listed environment. Every `ANTHROPIC_*`, `CLAUDE_*` and cloud-provider variable is dropped, so an API key or a provider setting in the calling shell cannot move the call off your subscription. A proxy or an extra CA certificate passes only when you name it in `C_LEAN_PASS_ENV`.
- `c-lean` sets the child's `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to 128000 (`OUTPUT_CAP` in `bin/c_lean_core.py`); the CLI clamps it to the model's upper bound. If a result names a default cap above it, a WARN says which model was held down; raise `OUTPUT_CAP` for that model.
- When the model's answer is cut and continued, the pieces are joined. Text that could not be printed as an answer is saved under `$XDG_STATE_HOME/c-lean/partial`, by default `~/.local/state/c-lean/partial`.
- Each call appends one line of counters to `$XDG_STATE_HOME/c-lean/usage.jsonl`, with the same exit code as the trailer, and with `tokens_unknown: true` when the trailer says `NOT COUNTED`. The line stores a sha256 hash of the prompt, not the prompt. That is not anonymization: anyone who holds a candidate prompt can confirm it against the hash.
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
| 128+N | stopped by signal N (130, 143, 129), or 137 when `claude -p` died of SIGKILL |

## sub-scout and sub-coder

- Both start without CLAUDE.md (`omitClaudeMd: true`), with their own short prompt in place of the Claude Code system prompt, and with a narrow tool list. Give them a self-contained brief: the paths, the question or the change, and the check to run.
- Default models: sub-scout runs on `sonnet`, sub-coder on the session's model (`inherit`). Neither file sets an effort, so both use the session's effort.
- Claude Code picks a subagent's model in this order: the `model` passed in that Agent call, the `model` in the agent file, the `CLAUDE_CODE_SUBAGENT_MODEL` environment variable, the session's model. So Claude, or you through Claude, can run either agent on another model for one task. A per-call `effort` works the same way. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` pins every subagent to one model.
- To change a default for good, copy the agent file to `~/.claude/agents/` and edit it. Your copy runs as `sub-scout`, the plugin's as `c-lean:sub-scout`.

## Measurements

Start-up context is `input + cache_creation + cache_read` of the first model request. For subagents it was read from the usage that the Agent tool reports. Conditions: `claude -p` on Claude Code 2.1.311, subagents on `claude-sonnet-5-5`, `c-lean` on `haiku`, a one-line task, a working directory outside git, hooks off.

| What starts | Tokens |
|---|---|
| `c-lean`: empty system prompt, no tools | 579 |
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
- The general-purpose figure was measured in September 2026 on an older Claude Code. Yours depends on your MCP servers, skills and CLAUDE.md.

## A lean worker outside a session

The subagents run inside a Claude Code session. For a script or a scheduled job, the same idea works with `claude -p` directly:

```bash
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
  claude -p --model sonnet --effort high \
    --setting-sources "" --strict-mcp-config --disable-slash-commands \
    --tools "Read,Grep,Glob" --allowedTools "Read,Grep,Glob" \
    --system-prompt-file worker-prompt.md \
    --output-format stream-json --verbose \
    < brief.md > run.jsonl
```

- `--setting-sources ""` loads no settings files and no CLAUDE.md. `--strict-mcp-config` with no `--mcp-config` starts no MCP servers.
- `--system-prompt-file` replaces the Claude Code system prompt with the text of the file. Passed as `--system-prompt "$(cat ...)"`, the text would be on the command line, where Linux refuses an argument of 128 KiB or more and other local users can read it.
- `--tools` limits the tool set. `--allowedTools` pre-approves the same tools, so the run never stops at a permission prompt.
- `env -u` keeps a stray API key from moving the run off your subscription.
- The brief comes on stdin, so its length is not limited by the command line.
- With a slightly broader tool set, workers like this started at 5.5K to 8K tokens in the author's runs.

## Known limits

- `c-lean` works only on a Claude subscription login, on purpose. It does not use API keys, Amazon Bedrock, Google Cloud or Microsoft Foundry.
- The Claude Code documentation says that `--bare` "will become the default for `-p` in a future release", and bare mode never reads a subscription login. When that happens, `c-lean` and the recipe above stop authenticating until an opt-out exists. API keys and cloud providers keep working in bare mode. The subagents run inside an authenticated session and are not affected.
- The subagents do not see your CLAUDE.md, so project rules reach them only through the brief.

## Tests

```bash
python3 tests/test_c_lean.py
python3 tests/redphase.py
```

`test_c_lean.py` runs the tool against a fake `claude` binary; no model is called. `redphase.py` breaks a throwaway copy of the tool one way at a time and requires the matching test to fail on its own assert, so a test that cannot fail is caught.

The timeout and signal tests find child processes through `/proc` on Linux and the real `pgrep` elsewhere. In a sandbox that forbids listing processes, set `C_LEAN_TEST_FAKE_PGREP=1` to use a fixture for that step; the tests still kill real processes.

## License

MIT
