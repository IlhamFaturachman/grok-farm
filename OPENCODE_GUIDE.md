# OpenCode + Grok Farm — max setup

## Connection

```text
Base URL : https://api.liamnevalackin.my.id/v1
API Key  : (your g2a_… key)
Model    : grok-4.5
```

Server already injects a **code-first polyglot** system prompt + sensible defaults
(`temperature≈0.2`, `max_tokens≈12288` if you omit them). You do **not** need a
huge custom system prompt in OpenCode for basic “dewa” behavior.

## What works well

| Use | How |
|-----|-----|
| Coding / agent | Default mode (code-first). Enable tools: files, shell, terminal. |
| General questions | Same endpoint — prompt is multi-capable. |
| Live facts / “search” | Use **OpenCode tools** (web search MCP / builtin), not native Grok web. |
| Vision | Works if OpenCode sends real `image_url` / `data:image…` parts. Clipboard-as-filename only = model cannot see the picture. |

## Optional mode header

If your client can set headers:

```http
X-Grok-Mode: code      # default
X-Grok-Mode: general
X-Grok-Mode: research
```

Most OpenCode setups can ignore this — default is already code-first polyglot.

## Tools (Phase 2)

1. Keep **agent tools** enabled (edit files, run commands).
2. Add a **web search** tool if you need live web (Tavily / Serper / Brave / etc.).
3. Server will **not** strip `tools` / `tool_choice` from requests.

Prompt policy on server: model should use tools for live facts and not invent URLs.

## Limits

- Context window is fixed by the **model upstream** (not settable to “1M” on our gateway).
- `max_tokens` only controls **output** length.
- Prefer focused files / compact context over dumping entire monorepos.

## Not available on free Build pool alone

- Native Grok.com web search UI
- Imagine image/video models (need Web credentials — Phase 3)
- Console-only models with `supportedAccounts: 0`

## Smoke test

```bash
curl https://api.liamnevalackin.my.id/v1/chat/completions \
  -H "Authorization: Bearer g2a_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Say BOOST_OK"}]}'
```

Higher `prompt_tokens` than a bare prompt means server boost is active.

## Vision (OpenCode)

1. In provider model config set modalities image true (you already did).
2. Attach/paste the image so the chat shows a real image preview — not only a `clipboard` / path chip with no pixels.
3. If the model says it cannot see the image, check the request: it must contain `image_url` or `data:image/...;base64,...`. Text-only "clipboard" means the client never sent the image.
4. Retry on `at capacity due to high demand` — free Build pool is busy; wait or retry (not a bad API key).
