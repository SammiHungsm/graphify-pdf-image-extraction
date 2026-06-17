# graphify reference: MiniMax backend setup

MiniMax is the **preferred extraction backend** for graphify when running under host models that paraphrase prompts (e.g. opencode session models, smaller Claude variants). Routing extraction through MiniMax's OpenAI-compatible API gives you:

- The **full 25-relation SFC schema** delivered verbatim from `llm.py:_EXTRACTION_SYSTEM` (no host-model paraphrase risk).
- Predictable cost (MiniMax is roughly 3-5x cheaper than Claude Sonnet for this workload).
- Deterministic dispatch — no `@agent` / subagent flakiness, no "read-only Explore agent wrote nothing to disk" failures.

The trade-off: you supply your own `MINIMAX_API_KEY`. The token cost is paid to MiniMax, not your opencode/Claude session.

## One-time setup

### 1. Get a MiniMax API key

- China account: <https://www.minimaxi.com/> → Console → API Keys
- International account: <https://www.minimax.io/> → Console → API Keys

The two regions use different base URLs and are **not** cross-compatible:

| Region        | Base URL                          |
|---------------|-----------------------------------|
| China         | `https://api.minimaxi.com/v1`     |
| International | `https://api.minimax.io/v1`       |

### 2. Register the provider in `~/.graphify/providers.json`

The graphify pipeline auto-loads custom providers from this file at import time. Create it once:

```json
{
  "minimax": {
    "base_url": "https://api.minimaxi.com/v1",
    "default_model": "MiniMax-M2",
    "env_key": "MINIMAX_API_KEY",
    "model_env_key": "GRAPHIFY_MINIMAX_MODEL",
    "pricing": {"input": 0.30, "output": 1.20},
    "temperature": 0,
    "max_tokens": 16384,
    "vision": false
  }
}
```

Notes:
- Switch `base_url` to `https://api.minimax.io/v1` if you have an international account.
- `pricing` is in USD per 1M tokens. Update to your actual MiniMax tier — the values above are an estimate for `MiniMax-M2`. The number is only used for the cost report; it does not affect billing.
- `vision: false` — MiniMax models in this config do not receive image inputs. graphify extracts the **text layer** of PDFs via pypdf before sending; embedded charts/images are not described. This matches the extraction subagent default behaviour and is fine for SFC-style annual reports where the financial substance is in the text.
- Do **not** put this file in a project-local `./.graphify/providers.json` unless you fully trust the repo: that path is only loaded when `GRAPHIFY_ALLOW_LOCAL_PROVIDERS=1` is set, because a custom provider receives your full corpus and API key.

### 3. Set the API key in your shell profile

PowerShell (`$PROFILE`):

```powershell
$env:MINIMAX_API_KEY = "<your key>"
# Optional override of model:
# $env:GRAPHIFY_MINIMAX_MODEL = "abab6.5-chat"
```

Bash / zsh (`~/.bashrc` / `~/.zshrc`):

```bash
export MINIMAX_API_KEY="<your key>"
# Optional:
# export GRAPHIFY_MINIMAX_MODEL="abab6.5-chat"
```

### 4. Verify the backend is registered

```powershell
python -c "from graphify.llm import BACKENDS; print('minimax registered:', 'minimax' in BACKENDS)"
```

Expected output: `minimax registered: True`. If `False`, the JSON failed to parse or `provider_base_url_ok` rejected the URL — check that `base_url` is `https://...` and the JSON is well-formed.

### 5. (Optional) Smoke-test connectivity

```powershell
$env:MINIMAX_API_KEY = "<your key>"
python -c "
from openai import OpenAI
c = OpenAI(api_key='$env:MINIMAX_API_KEY', base_url='https://api.minimaxi.com/v1')
r = c.chat.completions.create(model='MiniMax-M2', messages=[{'role':'user','content':'ping'}], max_tokens=10)
print(r.choices[0].message.content)
"
```

If you get a single response back, the endpoint and key are good. Any 401 means the key is wrong / not provisioned for chat completions; any 404 means the base URL or model name is wrong for your region.

## How the skill picks MiniMax

`SKILL.md` Step 3 Part B applies this priority on every run:

1. `MINIMAX_API_KEY` set → `backend="minimax"` (this file's flow). **Preferred.**
2. `GEMINI_API_KEY` / `GOOGLE_API_KEY` set → `backend="gemini"`. Legacy.
3. Neither set → fall back to host-model `@agent` / subagent dispatch. **Quality unstable** — host model may paraphrase the schema.

Once `MINIMAX_API_KEY` is in your environment, you do not need to pass any flag. The skill will route through MiniMax automatically.

## Model selection

| Model                | Notes                                                    |
|----------------------|----------------------------------------------------------|
| `MiniMax-M2`         | Default. Newest reasoning model. Best for SFC extraction. |
| `abab6.5-chat`       | Older but cheaper. Adequate for shorter docs.            |
| `MiniMax-Text-01`    | Long-context variant if your chunks exceed 128k tokens.  |

Override with `GRAPHIFY_MINIMAX_MODEL=<name>` in the shell, or pass `--model` when calling graphify in headless flows.

## Troubleshooting

### "ignoring project-local …/providers.json"

You created the JSON in `./.graphify/providers.json` instead of `~/.graphify/providers.json`. Either move it, or set `GRAPHIFY_ALLOW_LOCAL_PROVIDERS=1` if you intentionally want a project-local config (do **not** do this in a shared/cloned repo).

### "provider 'minimax' has an unparseable base_url"

JSON syntax error or wrong key name. Run the verify command in Step 4 — it will show which fields actually loaded.

### Output JSON only has 3 relations (`references | cites | conceptually_related_to`)

This is the symptom that originally motivated this setup. It means extraction ran through host-model `@agent` dispatch, not MiniMax. Cause is usually:

- `MINIMAX_API_KEY` not exported in the shell that ran graphify.
- `~/.graphify/providers.json` typo'd the env_key (must be exactly `"MINIMAX_API_KEY"`).
- Key was set but graphify was launched from a parent shell that did not inherit the new variable. Open a fresh terminal.

After fixing, clear the stale extraction so the next run re-extracts:

```powershell
Remove-Item graphify-out\.graphify_chunk_*.json -ErrorAction SilentlyContinue
Remove-Item graphify-out\.graphify_semantic.json -ErrorAction SilentlyContinue
Remove-Item .graphify\semantic_cache\*.json -ErrorAction SilentlyContinue
```

Then re-run graphify and grep the new chunks for SFC relations:

```powershell
Select-String -Path graphify-out\.graphify_chunk_*.json -Pattern 'owns_pct|controls|audited_by|director_of'
```

If hits appear, MiniMax extraction succeeded.
