# Harness² on university models

This project runs **Harness²**, Google Research's method for improving an AI coding agent without retraining the model, on the university's own models (`sofie-code-0`, `sofie-code-1`, `sofie-think-0`). It extends the method so that the agent and the improver can run in four different coding tools: **OpenCode, Codex, Grok Build and Pi**.

The original project is [google-research/harness2](https://github.com/google-research/harness2), Apache 2.0. Its README is kept in [`docs/HARNESS2_ORIGINAL_README.md`](docs/HARNESS2_ORIGINAL_README.md).

## The idea in one paragraph

An AI agent works inside a **harness**: its instructions, rules, skills and helper scripts. Harness² keeps the model fixed and improves the harness. For each task it:

1. runs the agent once with the current harness (**before**);
2. asks an **improver** model to propose changes to the harness;
3. tests each proposed change once;
4. combines the changes into a final harness;
5. runs the agent again with the final harness (**after**).

The improver never sees the test results. It works only from what the agent did. A separate set of hidden tests grades every run.

## What we built

| Area | Change |
|---|---|
| University models | All three roles use the university endpoint, through a local translation bridge where a tool needs it. |
| Four runtimes | The agent can run in **OpenCode, Codex, Grok Build or Pi**, and so can the improver. Grok and Pi needed new runners (`third_party/wb_runner/agents/`). |
| Improver edits kept | Skill files the improver wrote without the required header were silently discarded. They are now repaired automatically. |
| Clearer improver role | The improver is told plainly to edit the harness, not to solve the task itself. |
| Hard time limits | Every agent run and every improver session has a hard time limit, so a stalled model call can't block a run for hours. |
| Safe parallel runs | Two experiments can run at the same time without overwriting each other's job files. |
| Optional test feedback | For tuning experiments, the improver can be shown the hidden-test output (`HARNESS2_FEEDBACK_TASKS`). It is off by default, as in the original method. |

## Results

All results use the WorkBuddy-Bench code tasks, graded by hidden tests. Each cell is one run, so differences of 1 to 2 tests can be chance. Full details are in [`reports/`](reports/).

### Which runtime works best? (task `window_aggregates`, 13 tests)

Agent `sofie-code-0`, improver `sofie-think-0`, both in the same runtime.

| Runtime | Before | After |
|---|---|---|
| OpenCode | 11 | **13** |
| Codex | **13** | **13** |
| Grok Build | 11 | 11 |
| Pi | 5 | 11 |

Codex gave the strongest agent: it passed every test before any improvement.

### Which improver should a Codex agent use? (2 tasks)

| Task | Codex improver | OpenCode improver |
|---|---|---|
| `format_errors` (14 tests) | 8 to **11** | 2 to 2 |
| `stream_release` (12 tests) | 8 to 8 | 9 to 9 |

### Recommended setup

**Codex as the agent and Codex as the improver**, with `sofie-code-0` for the agent and `sofie-think-0` for the improver. This is a provisional choice: it rests on a few tasks with one run each, and needs repeated runs to confirm.

### An example of the improver working

On `window_aggregates` with OpenCode, the agent first computed "hours since last event" with `int(...)`, which rounds down and failed 2 tests. The improver added a skill saying "use exact fractional hours, never `int()`". The next run followed it and passed all 13 tests. See [`reports/window-aggregates-report.pdf`](reports/window-aggregates-report.pdf).

## How to run

Requirements: Linux, Python 3.12 or 3.13 with [`uv`](https://docs.astral.sh/uv/), Docker, Node 20+, and keys for the university models. See [`docs/SETUP.md`](docs/SETUP.md) for details.

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[render,bridge]'      # install
cp .env.example .env                      # then fill in the SOFIE_* keys
harness2-setup wb --subset code --substrate cx
harness2-bridge --model sofie-code-0 --host 0.0.0.0     # keep running in a second terminal
harness2-check --bench wb --subset code --substrate cx
```

Run the recommended setup on one task:

```bash
HARNESS2_PROPOSER_CHANNEL=codex HARNESS2_CODEX_IMPROVER_MODEL=sofie-think-0 \
HARNESS2_RUNS_DIR=runs/codex \
harness2 --bench wb --subset code --substrate cx --k 1 --mode agg \
  --split train --split-file experiments/window_aggregates_task.json
```

To try another runtime, set `HARNESS2_CX_AGENT=grok` or `pi` (agent) and `HARNESS2_PROPOSER_CHANNEL=opencode`, `grok` or `pi` (improver). Pi must be installed in `pi/` (`npm install --prefix pi @mariozechner/pi-coding-agent@0.73.1`). Grok must be installed at `~/.grok/bin/grok`.

Results appear under the chosen `HARNESS2_RUNS_DIR`. Each run's hidden-test score is in `wb/code/rollouts/<run>/<time>/<trial>/verifier/reward.json`.

## Repository layout

| Path | Contents |
|---|---|
| `harness2/` | The method (original code plus the changes above) |
| `third_party/wb_runner/` | Runners for the benchmark, including the new Grok and Pi runners |
| `third_party/patches/` | Patches applied to the downloaded benchmark |
| `experiments/` | Task lists used in our experiments |
| `reports/` | Reports with full results (PDF and Markdown) |
| `tools/` | `run_rollouts.py` (single runs for screening), `keepawake.sh` (keeps a laptop awake during long runs) |
| `docs/` | Original setup and benchmark guides |

## Limits

- Few tasks, one run per setup. The scores need repeated runs before any choice is final.
- The improver can't see test results, by design, so its final choice sometimes discards a better candidate. This happened with Grok.
- The Codex, Grok and Pi improvers run inside Docker, because Codex's own sandbox cannot start on Ubuntu 24.04.

## License

Apache 2.0, as in the original Harness² project; see [`LICENSE`](LICENSE). The files under `harness2/` and `third_party/` that differ from the original have been modified for this project.
