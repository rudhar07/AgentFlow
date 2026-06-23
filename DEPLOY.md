# Deploying AgentFlow to Hugging Face Spaces

The dashboard ([web/server.py](web/server.py)) is containerized via the
[Dockerfile](Dockerfile) and runs on port **7860** — the Hugging Face Spaces
default. These steps take it from local to a public live URL.

> **Run it locally first** to confirm it works:
>
> ```bash
> pip install -r requirements.txt
> python -m playwright install chromium
> python dashboard.py          # open http://localhost:7860
> ```

---

## Deploy (Docker Space)

You need a free [Hugging Face account](https://huggingface.co/join). The final
push uses a **write token** tied to your account — only you can run it.

### 1. Create the Space

Go to **huggingface.co → New → Space**:

- **SDK:** Docker → **Blank**
- **Space hardware:** CPU basic (free) is enough
- Give it a name, e.g. `automationagent`

### 2. Make the Space's `README.md` start with this front-matter

Hugging Face reads the Space configuration from YAML front-matter at the **top**
of `README.md`. Ensure the Space repo's `README.md` begins with exactly:

```yaml
---
title: AgentFlow
emoji: 🤖
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---
```

(Everything below the closing `---` can be your normal project README.)

### 3. Push the code to the Space

A Space is its own git repo on `huggingface.co`. From your project folder:

```bash
# one-time: add the Space as a second remote (replace <user>/<space>)
git remote add space https://huggingface.co/spaces/<user>/<space>

# push (you'll be prompted for your HF username + a WRITE token as the password —
# create one at huggingface.co/settings/tokens)
git push space main
```

The Space builds the Docker image automatically and goes live at:

```
https://<user>-<space>.hf.space
```

### 4. (Optional) Enable LLM-vision mode

The deterministic agent needs no key. For `--mode llm` in the deployed app, add
your key as a **Space secret**: Space → **Settings → Variables and secrets →
New secret** → name `ANTHROPIC_API_KEY`. The container picks it up automatically;
no rebuild of your code is required (the Space restarts).

---

## Notes & gotchas

- **Headless only.** A server has no display, so the deployed agent always runs
  headless and streams **screenshots** to the dashboard — that is the whole
  point of the web UI. (A *local* `python main.py --keep-open` is still the more
  convincing live demo because you watch the real browser move.)
- **Free tier sleeps.** Spaces idle out and cold-start on the next visit; the
  first run after a sleep is slower. Fine for a demo, not for production.
- **One run at a time.** The server serializes runs (a second request returns
  409 while one is in progress) — a single shared browser instance.
- **Don't commit secrets.** `.env` is git-ignored; the live key lives only in
  the Space secret. The image excludes `.env`, `logs/`, `screenshots/`,
  `docs/`, and `VIVA.md` via [.dockerignore](.dockerignore).
- **GitHub vs. Hugging Face are separate remotes.** Pushing to your public
  GitHub repo (`origin`) and to the Space (`space`) are independent; do both.
