# picocode

minimal, single-file, openai-compatible coding assistant for the terminal. zero dependencies beyond python 3.10+ stdlib.

## features

- streaming chat with any openai-compatible api
- tools: read, write, edit, glob, grep, bash, web search, fetch
- autonomous subagents with parallel execution
- rate limiting with automatic 429 backoff
- rich terminal markdown rendering
- prompt history (~/.picocode_history)

## usage

```
export OPENAI_API_KEY=sk-...
python picocode.py
```

or create a `.env` file in the project directory:

```
OPENAI_API_KEY=sk-...
MODEL=gpt-4o
```

### env vars

| var | default | description |
|-----|---------|-------------|
| `OPENAI_API_KEY` | — | api key (required) |
| `OPENAI_API_URL` | `https://api.openai.com/v1/chat/completions` | endpoint url |
| `MODEL` | `gpt-4o` | model name |
| `RPM_LIMIT` | `40` | requests per minute cap |

### commands

- `/c` — clear conversation
- `/q` or `exit` — quit

## license

MIT
