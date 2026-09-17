# Wengu · Historical Guess

[简体中文](README.md)

One historical figure. A trail of clues. Find the name by asking yes-or-no questions.

Wengu is a Chinese-language history guessing game that runs locally. A model selects a person, answers questions, and judges guesses; the browser provides the interface, and SQLite keeps the games. The frontend needs no build step and loads no external fonts, scripts, or images.

![Wengu interface](docs/screenshot.png)

## Quick start

You need **Python 3.10+, Bash, and a modern browser**. Use Linux, macOS, or WSL on Windows. The launcher creates `.venv` and installs dependencies on the first run, using `uv` to create the environment when available.

```bash
git clone https://github.com/THUQiXuan/historical-guess.git
cd historical-guess
```

Choose either referee backend below.

### Option A: Local Codex

Install a Codex CLI version that supports `codex app-server` ([official installation guide](https://learn.chatgpt.com/docs/codex/cli)), then authenticate under the same system user:

```bash
codex login
```

Start everything with one command:

```bash
bash start.sh
```

Open **http://127.0.0.1:7992**. The command starts the web server and a persistent background Codex process. `Ctrl+C` stops both. This mode uses your existing local Codex authentication, so the project needs no API key. It communicates over the [Codex App Server](https://learn.chatgpt.com/docs/app-server) stdio protocol. Settings apply only to the child process; your global Codex configuration is unchanged.

The default model is `gpt-5.6-sol`, with `low` reasoning for questions and guesses, `medium` for character selection, and the `fast` service tier. To customize it, copy the template and edit your local file:

```bash
cp .env.example .env
```

| Variable | Default / purpose |
| --- | --- |
| `AGENT_PROVIDER` | `codex` |
| `CODEX_MODEL` | `gpt-5.6-sol` when unset; an explicitly empty value inherits your local Codex model |
| `CODEX_EFFORT` | `low`, for questions and guesses |
| `CODEX_SELECT_EFFORT` | `medium`, for filtering and selecting characters |
| `CODEX_SERVICE_TIER` | `fast`; use `default` if unavailable |
| `CODEX_AGENT_TIMEOUT` | `180`, timeout in seconds for a referee request |

Fast availability and credit use depend on the model and account; see [Codex Speed](https://learn.chatgpt.com/docs/agent-configuration/speed). In the development environment, App Server reported the effective tier as `priority` when `fast` was requested. One small local sample took approximately 13.5 seconds to start the backend, 6.0 seconds to select a person, 4.9 seconds to answer, and 4.2 seconds to judge a guess. These are observations, not performance guarantees; installing dependencies and evaluating complex scopes can take longer.

### Option B: Your own OpenAI-compatible API

This mode requires no Codex installation or login. Copy `.env.example` to `.env` and fill in your local settings:

```dotenv
AGENT_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=your-model-id
OPENAI_API_KEY=your-api-key
OPENAI_JSON_MODE=schema
```

Then run:

```bash
bash start.sh
```

The service must support [Chat Completions](https://platform.openai.com/docs/api-reference/chat/create). Set `OPENAI_BASE_URL` to the API base address, such as a URL ending in `/v1`; the server appends `/chat/completions`. Remote APIs require HTTPS; localhost APIs may use HTTP.

| Variable | Purpose |
| --- | --- |
| `OPENAI_JSON_MODE=schema` | Request strict JSON Schema output from a model that supports it |
| `OPENAI_JSON_MODE=object` | Use JSON object mode for other services; the server still validates results |
| `OPENAI_REASONING_EFFORT` | Optional `reasoning_effort` for questions and guesses |
| `OPENAI_SELECT_EFFORT` | Optional `reasoning_effort` for selection |
| `OPENAI_SERVICE_TIER` | Optional provider-supported `service_tier` |
| `OPENAI_TIMEOUT` | Request timeout in seconds; default `120` |

Leave reasoning and service-tier values empty if your provider does not support those fields. Restart after editing `.env`. Git ignores `.env`; enter credentials only in your own local file.

## Rules and historical scope

1. Choose a preset, optionally narrowing it in natural language, such as “people who served Shu Han.” The referee filters the existing corpus.
2. Set question and guess limits. **Empty means unlimited**; otherwise use an integer from 1 to 10000.
3. Start a game and ask a yes-or-no question. Valid questions receive only **是 / 否** (“yes / no”).
4. Use the separate guess form to submit a name or an unambiguous alias. Guesses receive only **正确 / 错误** (“correct / incorrect”).
5. A correct guess, exhausted guesses, or giving up reveals the person and source links. Browse past games or resume an unfinished one from the history view.

The **broad Three Kingdoms** preset covers **184–316 CE, inclusive**, from the Yellow Turban Rebellion through the fall of Western Jin. A person's lifetime needs only to overlap this interval: they may have been born before 184 or survived beyond 316. The corpus includes late Eastern Han and Western Jin figures.

The **Records of the Three Kingdoms** preset contains corpus entries with name evidence in the main text or Pei Songzhi's annotations. It is a curated subset, not an exhaustive index of the book. See [data/SOURCES.md](data/SOURCES.md) for scope, collection methods, sources, and data terms. Current counts appear in the app.

**无法回答** (“cannot answer”) is reserved for invalid questions, including non-binary questions, unresolved ambiguity, paradoxes, and attempts to change the rules. These **do not consume a question**. When a valid historical claim cannot be verified, the app returns a retryable service error instead of disguising uncertainty as “cannot answer” or “no.” Model judgments may still be wrong; use the revealed sources to check them.

You can keep guessing after questions run out. The game ends when guesses run out. The optional timer includes model processing, waiting, and time while the page is closed.

## Storage, access, and privacy

Games are stored in `var/games.sqlite3`, including a character snapshot, rules, questions, guesses, and timestamps. Games survive refreshes and server restarts. A browser cookie identifies its history; clearing it or switching browsers does not automatically restore access to earlier games. For a simple backup, stop the server before copying `var/`.

Keep `.env`, `var/`, databases, and logs out of version control. Credentials stay on the server. Game inputs and relevant character data are sent to your selected model service. The app has no account system; browser sessions are not a complete user authentication system.

The default listener is `127.0.0.1:7992`. Set launcher variables in your shell to use another port:

```bash
PORT=9000 bash start.sh
```

`HOST` and `PORT` are read by the launcher, so set them in the shell. To run on a remote server, keep the default listener and forward the port from your own computer:

```bash
ssh -N -L 7992:127.0.0.1:7992 YOUR_SSH_ALIAS
```

Replace `YOUR_SSH_ALIAS` with the SSH alias you normally use for that server. Keep this terminal open, then visit http://127.0.0.1:7992 in your local browser.

With VS Code / Cursor Remote SSH, opening the project root as your workspace loads this project's `.vscode/settings.json`, which configures automatic forwarding of `7992` and opening the browser. An existing parent or multi-root workspace may not load that setting. You can also manually add and forward `7992` in the remote window's **Ports** panel, then open its local address. If the tool assigns a different local port, use the panel's address.

Public deployment needs HTTPS and access authentication at a reverse proxy, with controls for model usage. Keep a single web worker; current game operation locks are held within the process.

## Development and contributions

```text
static/                 Plain HTML / CSS / JavaScript interface
server/app.py           FastAPI routes, game rules, browser sessions
server/agent.py         Codex process, referee protocol, result validation
server/openai_agent.py  OpenAI-compatible transport
server/db.py            SQLite storage
data/characters.json   Historical character corpus
data/SOURCES.md         Sources, scope, and data terms
scripts/build_corpus.py Corpus validation tool
tests/                  Automated tests
```

Install development dependencies and run the checks:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/build_corpus.py
```

Tests use mock referees and protocol responses, consuming no real model credits. Live availability, latency, and historical judgments require separate integration checks. After editing the frontend, you can also run `node --check static/app.js`.

Contributions of historical figures, factual corrections, and features are welcome. New entries need reliable sources, evidence that the person was alive during 184–316 CE, and clear aliases. Entries marked as appearing in *Records of the Three Kingdoms* need a quotation and a volume link. Run the tests and corpus validator before submitting, and exclude personal configuration, credentials, and game records from your PR.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Referee unavailable | Check the Codex CLI and login, or the API base URL, model, and key in `.env`; restart and retry |
| Model requires a newer Codex version | Update the CLI or select a `CODEX_MODEL` supported by your CLI and account |
| Fast unavailable | Set `CODEX_SERVICE_TIER=default` and restart |
| Compatible API rejects parameters | Try `OPENAI_JSON_MODE=object` and leave unsupported reasoning or service-tier fields empty |
| Scope has no matching characters | Broaden the scope or inspect the character library |
| Timeout, unverified fact, or malformed response | No attempt is charged; retry later. Network retries of the same question submission are deduplicated |
| Earlier games missing | Keep the original database and use the same browser and site address that created the games |

Code is licensed under the [MIT License](LICENSE). Historical excerpts and source materials follow the [data source notes](data/SOURCES.md); the code license does not relicense all third-party material.
