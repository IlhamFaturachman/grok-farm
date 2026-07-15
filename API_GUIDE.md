# Grok API — Community Access Guide

OpenAI **and** Anthropic compatible endpoint powered by grok2api.

---

## Endpoint

| | |
|---|---|
| **Base URL** | `https://api.example.com` |
| **API Key** | _(ask the admin for your key)_ |
| **Models** | `grok-4.5`, `grok-composer-2.5-fast` |

The endpoint supports both the **OpenAI** and **Anthropic** API formats.
Use whichever your client or SDK already speaks — no special adapters needed.

---

## Quick test (curl)

```bash
curl https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role": "user", "content": "Say hi in one word"}]
  }'
```

---

## Using with the OpenAI SDK

### Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.example.com/v1",
    api_key="YOUR_API_KEY",
)

response = client.chat.completions.create(
    model="grok-4.5",
    messages=[{"role": "user", "content": "Hello!"}],
)

print(response.choices[0].message.content)
```

### JavaScript / TypeScript

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "https://api.example.com/v1",
  apiKey: "YOUR_API_KEY",
});

const response = await client.chat.completions.create({
  model: "grok-4.5",
  messages: [{ role: "user", content: "Hello!" }],
});

console.log(response.choices[0].message.content);
```

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="https://api.example.com/v1",
    api_key="YOUR_API_KEY",
    model="grok-4.5",
)

print(llm.invoke("Hello!").content)
```

---

## Using with the Anthropic SDK

### Python

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://api.example.com",
    api_key="YOUR_API_KEY",
)

response = client.messages.create(
    model="grok-4.5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)

print(response.content[0].text)
```

### JavaScript / TypeScript

```javascript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: "https://api.example.com",
  apiKey: "YOUR_API_KEY",
});

const response = await client.messages.create({
  model: "grok-4.5",
  max_tokens: 1024,
  messages: [{ role: "user", content: "Hello!" }],
});

console.log(response.content[0].text);
```

---

## Streaming

Both formats support streaming (Server-Sent Events).

### OpenAI streaming

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.example.com/v1",
    api_key="YOUR_API_KEY",
)

stream = client.chat.completions.create(
    model="grok-4.5",
    messages=[{"role": "user", "content": "Tell me a joke"}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Anthropic streaming

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://api.example.com",
    api_key="YOUR_API_KEY",
)

with client.messages.stream(
    model="grok-4.5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Tell me a joke"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

---

## Compatible clients

Because the endpoint speaks both protocols, it works out of the box with:

| Client | Format | How to configure |
|---|---|---|
| **LibreChat** | OpenAI | Set `OPENAI_API_BASE` + key |
| **Open WebUI** | OpenAI | Add as OpenAI-compatible connection |
| **Cursor** | OpenAI | Settings → Models → OpenAI API base |
| **Continue.dev** | OpenAI / Anthropic | Set `apiBase` + `apiKey` |
| **SillyTavern** | OpenAI / Anthropic | Chat Completion → Custom endpoint |
| **Anything** with OpenAI SDK | OpenAI | Point `base_url` here |
| **Anything** with Anthropic SDK | Anthropic | Point `base_url` here |

---

## Models

| Model | Description |
|---|---|
| `grok-4.5` | Latest Grok model — general purpose, best quality |
| `grok-composer-2.5-fast` | Faster, lighter model — lower latency |

Check available models anytime:

```bash
curl https://api.example.com/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## Test scripts

If you have the repo, you can verify your key with:

**Linux / macOS:**
```bash
GROK_API_KEY=your-key-here ./scripts/test-api.sh
```

**Windows (PowerShell):**
```powershell
$env:GROK_API_KEY="your-key-here"; .\scripts\test-api.ps1
```

This runs three checks: health → list models → chat completion.

---

## FAQ

**Is the connection secure?**
Yes — HTTPS via Cloudflare. The server IP is hidden behind Cloudflare's network.

**What are the rate limits?**
Per-key limits are set by the admin. If you hit `429 Too Many Requests`, slow down or ask for a limit increase.

**Does it support function/tool calling?**
Yes — standard OpenAI tool-calling format is supported.

**Does it support vision/image inputs?**
Depends on the model — `grok-4.5` supports image inputs in OpenAI format.

**Can I use this with `ollama` / `litellm` / other proxies?**
Yes — any proxy that supports custom OpenAI or Anthropic base URLs can point here.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `401 Unauthorized` | Missing or invalid API key | Check your key starts with `g2a_` |
| `404 Not Found` | Wrong path or hitting root domain | Use `/v1/chat/completions`, not just the domain |
| `429 Too Many Requests` | Rate limit exceeded | Slow down or request higher limit |
| `500 / 502` | Upstream model error | Retry after a moment |

If you're stuck, ask the admin for help.
