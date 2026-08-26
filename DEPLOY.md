# Deploying CodeLens

CodeLens is not one deployable thing. It is a CLI, an API, and a CI job, and
which one you "deploy" depends on how you want it used.

Read this section first, because it determines everything below.

## Where CodeLens actually belongs

CodeLens reads a **git working tree on local disk**. That shapes deployment
more than any hosting choice:

- **In CI, next to the checkout** — natural fit. The runner already has the
  repository. Nothing to host, nothing to pay for. **This is the primary
  deployment and it is already written.**
- **As a long-running API** — the `path` field in `/review` is a *server-side*
  path. A hosted instance has no repositories on disk, so most endpoints are
  useless there unless you add cloning or send diffs to it. See Option 3.
- **On a developer machine** — `pip install -e .` and use the CLI. Not a
  deployment, but it is how most people would use it day to day.

The honest answer for a portfolio: **deploy Option 1 for real use, and Option 2
or 3 for something clickable to put in your README.**

---

## Option 1 — GitHub Actions (recommended, zero cost)

Already implemented in `.github/workflows/pr-review.yml`. It reviews every pull
request, posts findings as a comment, and fails the build on a critical finding.

### Enable it

1. Push the repository to GitHub with the workflow file included.
2. Add the API key as a repository secret:
   **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `CODELENS_LLM_API_KEY`
   - Value: your Groq (or other provider) key
3. Confirm workflow permissions:
   **Settings → Actions → General → Workflow permissions** →
   *Read and write permissions* (needed to post the PR comment).
4. Open a pull request. The comment appears within a minute or two.

Without the secret the job still runs and reports the static findings, so a
pull request from a fork (which cannot read secrets) still gets a review
instead of a failed job.

### Cost

Free on public repositories. On private repositories it consumes Actions
minutes from your plan's allowance — a review run is roughly 1–2 minutes.

### Using it on *other* repositories

Copy `pr-review.yml` into the target repository and add one step to install
CodeLens from your repo:

```yaml
      - name: Install CodeLens
        run: pip install git+https://github.com/YOUR_USERNAME/codelens.git
```

`pyproject.toml` is already included. For reference, it is:

```toml
[project]
name = "codelens"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.110", "uvicorn[standard]>=0.27", "typer>=0.12",
    "httpx>=0.27", "numpy>=1.26",
    "python-dotenv>=1.0", "pydantic>=2.6",
]

[project.scripts]
codelens = "codelens.cli:app"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

That also gives you a `codelens` command instead of `python -m codelens.cli`.

---

## Option 2 — Docker, anywhere

The `Dockerfile` is complete and includes git, which CodeLens needs.

```bash
docker build -t codelens .

docker run --rm -p 8000:8000 \
  -e CODELENS_LLM_PROVIDER=groq \
  -e CODELENS_LLM_API_KEY=gsk_... \
  -v codelens-data:/data \
  -v /path/to/your/repo:/repos/myrepo:ro \
  codelens
```

Mounting a repository read-only at `/repos/myrepo` is what makes the API
useful: you then call it with `{"path": "/repos/myrepo"}`.

The named volume `codelens-data` holds the SQLite database, so review history
and the code index survive restarts. Without it, every restart starts empty.

### Deploying that container

Any platform that runs a container works — Render, Railway, Fly.io, Google
Cloud Run, a VPS. Two things to get right:

- **Persistent disk.** The database lives at `/data`. On a platform with an
  ephemeral filesystem, attach a volume and point `CODELENS_DATA_DIR` at it, or
  accept that history resets on every deploy.
- **Free tiers sleep.** The first request after idle can take 30–60 seconds.
  Note it on the demo page so a visitor does not think it is broken.

Free-tier terms change often — check the current limits before committing to a
platform rather than trusting a comparison article.

---

## Option 3 — A hosted API (needs one change first)

A public instance has no repositories on disk, so `POST /review` with a
server-side `path` is meaningless there. **One endpoint already works remotely:**

```bash
curl -X POST https://your-instance/review/patch \
  -H 'content-type: application/json' \
  -d '{"path": ".", "diff": "<unified diff text>", "ref": "pr-42"}'
```

`/review/patch` takes the diff in the request body. The static rules that read
files from disk will find nothing (there is no working tree), so you get the
AI pass plus the rules that operate on the diff text itself. That is the
correct hostable surface, and it is how a webhook would call this.

To make a hosted instance genuinely useful, add **one** of these:

**a) Clone on demand.** Accept a repository URL, shallow-clone it to a temp
directory, index, review, delete. `repo.clone()` in `codelens/repo.py` already
does the cloning. Guard it: allow-list the hosts you will clone from, cap the
clone size, and enforce a timeout — an endpoint that clones arbitrary URLs is
an SSRF and a disk-fill waiting to happen.

**b) A GitHub webhook receiver.** Verify the `X-Hub-Signature-256` HMAC against
your webhook secret, fetch the PR diff from the GitHub API, run
`review_diff_text()`, and post the findings back as a comment. This is the
version that behaves like a real product.

Either way, put a token in front of the API before it is public. Every request
can trigger an LLM call, and an open endpoint is someone else's free inference.

---

## Environment variables in production

| Variable | Set it to |
|---|---|
| `CODELENS_LLM_PROVIDER` | `groq`, `gemini`, `openai`, … (or `mock` for static-only) |
| `CODELENS_LLM_API_KEY` | your key — from a secret store, never in the image |
| `CODELENS_LLM_MODEL` | e.g. `openai/gpt-oss-120b` |
| `CODELENS_DATA_DIR` | a path on persistent disk (`/data` in the container) |
| `CODELENS_MIN_CONFIDENCE` | raise toward `0.7` if the AI pass is noisy in practice |
| `CODELENS_MAX_DIFF_CHARS` | lower it if you hit provider token limits on large PRs |

Never bake the key into the image. `.env` is gitignored; keep it that way.

---

## Cost control

Every review is one LLM call whose prompt scales with the diff. Three levers:

- `CODELENS_MAX_DIFF_CHARS` caps the prompt directly.
- The static pass runs first and its findings are listed in the prompt, so the
  model does not spend output repeating them.
- On free tiers you will hit rate limits before you hit a bill. If reviews
  start failing under load, the failure is reported as an `info` finding and
  the static results still come through — it degrades rather than breaks.

---

## Recommended path

1. Push to GitHub, add the `CODELENS_LLM_API_KEY` secret, open a test PR.
   Screenshot the review comment — that screenshot is the best thing you can
   put at the top of the README.
2. If you want a live URL as well, deploy the container somewhere free and
   expose `/review/patch` behind a token.
3. Build the webhook receiver (3b) only if you want CodeLens to look like a
   product rather than a tool. It is the difference between "I built a code
   reviewer" and "it reviews my PRs, here is the comment it left."
