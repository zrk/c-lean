---
name: lean-delegation
description: Use before delegating a bounded piece of work when start-up cost matters, such as many small judgements over text, one lookup in the code, or one narrow edit with a check. Picks between the c-lean command, the lean-scout and lean-worker subagents, and a full subagent, and says how to brief them.
---

# Delegate cheaply

Every delegated call pays for its start-up context before it reads your task: system prompt, tool definitions, CLAUDE.md. Pick the lightest option that can still do the job.

| Option | Start-up context | What it can do | Use it for |
|---|---|---|---|
| `c-lean` command, run with Bash | about 0.6K tokens | answer only: no tools, no files | judging, classifying, rewriting or extracting from text that you put into the prompt |
| `c-lean:lean-scout` subagent | about 3.8K | Read, Grep, Glob | one bounded lookup: where X lives, what a function does, facts with path:line |
| `c-lean:lean-worker` subagent | about 4.4K | Read, Edit, Write, Bash | one bounded edit together with the check that proves it |
| a general-purpose subagent | tens of thousands | everything the session has | open-ended work that needs MCP servers, skills or the project's instructions |

Inside a git repository every subagent except Explore and Plan also receives a git status snapshot, from about 0.2K tokens in a clean repository up to about 1.3K.

## c-lean

- The model sees only your prompt. Put all the material in it.
- Always choose the model with `-m` (`haiku`, `sonnet`, `opus` or a full model ID); there is no default. Choose effort with `-e`.
- Pass the prompt as an argument, with `-f FILE`, or on stdin. Give a rubric with `--system` or `--system-file`, and ask for JSON with `--schema`.
- stdout carries only the answer. stderr carries a trailer with token counts.
- It runs only on a Claude subscription login. It ignores API keys and cloud-provider settings on purpose, so a call can never be billed to an API account by accident.

```bash
c-lean -m haiku --system "Answer yes or no." "Does this log line report an error? <line>"
```

## lean-scout and lean-worker

- They start without CLAUDE.md and without the project's instructions. The brief is the whole task: name the paths, the question or the change, and the check to run.
- lean-scout runs on `sonnet` and lean-worker on the session's model by default. Pass `model` in the Agent call to choose for this task: a cheaper model for a plain lookup, a stronger one for a hard edit. The per-call value wins over the agent file. `effort` works the same way.
- One task per subagent. If the work turns out larger than the brief, the subagent stops and reports instead of widening it.

## When none of these fits

- The task needs this conversation's history: use a fork.
- The task needs MCP tools, skills or the project's CLAUDE.md: use a general-purpose subagent.
