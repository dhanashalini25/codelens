# Setting up automated PR review — step by step

From a downloaded zip to a robot reviewing your pull requests. About 25 minutes,
most of it waiting for things to install.

Everything here is free on a public repository.

---

## Step 0 — Check what you have

```bash
python3 --version    # need 3.10 or newer
git --version        # any recent version
gh --version         # optional; makes step 3 faster
```

If `python3` is missing or older than 3.10, install it before continuing —
nothing below will work otherwise.

---

## Step 1 — Unpack and run it locally

Prove it works on your machine before involving GitHub. Debugging locally is
minutes; debugging inside CI is a push-and-wait loop each time.

```bash
unzip codelens.zip
cd codelens

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

`(.venv)` should now appear in your prompt. Run the tests:

```bash
python -m pytest tests -q
```

Expected: `41 passed`. If anything fails, stop here — a broken local checkout
will fail identically in CI, just more slowly.

Now watch it find real bugs:

```bash
make demo
```

That builds a small git repository with a deliberately flawed commit and
reviews it. You should see a critical finding for a committed API key, plus
`shell=True`, a mutable default argument, and a bare `except`.

**You have now confirmed the static half works. No API key was involved.**

---

## Step 2 — Get a Groq key (2 minutes, free, no card)

The static rules work without a model. The AI pass needs a key.

1. Go to **console.groq.com** and sign up.
2. Find **API Keys** in the left sidebar.
3. **Create API Key**, name it `codelens`.
4. **Copy it now.** It is shown once. It starts with `gsk_`.

Test it locally before it goes anywhere near GitHub:

```bash
export CODELENS_LLM_PROVIDER=groq
export CODELENS_LLM_API_KEY=gsk_your_key_here
export CODELENS_LLM_MODEL=openai/gpt-oss-120b

make demo
```

Look for `provider: groq` in the output instead of `provider: mock`. The AI
findings appear tagged `[llm]`.

A `401` means the key is wrong. A `404` usually means the model name has been
retired — check the current list in Groq's docs.

For everyday local use, put those three lines in `.env` instead of exporting
them. `.env` is gitignored; keep it that way.

---

## Step 3 — Push to GitHub

**Never commit the key.** Confirm first:

```bash
git status --porcelain | grep -i env    # should print nothing
```

Then:

```bash
git init
git add .
git commit -m "CodeLens: repository intelligence and AI code review"
git branch -M main
```

Create the repository — **public**, so Actions is free:

```bash
gh repo create codelens --public --source=. --push
```

No `gh`? Create it at github.com/new (public, no README/licence/gitignore —
you already have all three), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/codelens.git
git push -u origin main
```

Check the **Actions** tab. `ci.yml` should already be running — that is the
test suite, not the review job.

---

## Step 4 — Add the key as a repository secret

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `CODELENS_LLM_API_KEY`  ← must match exactly
- Secret: your `gsk_...` key

A typo in the name does not error. The workflow silently falls back to `mock`
and you get static findings only, wondering where the AI went.

---

## Step 5 — Allow the workflow to comment

**Settings → Actions → General → Workflow permissions**
→ select **Read and write permissions** → **Save**

Many accounts default to read-only. Symptom if you skip this: the review runs
fine, then the comment step fails with a 403.

---

## Step 6 — Open a pull request

```bash
git checkout -b test-review
```

Create a file with obvious problems:

```bash
mkdir -p src
cat > src/scratch.py <<'EOF'
import subprocess

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def run_it(command, seen=[]):
    seen.append(command)
    try:
        return subprocess.run(command, shell=True)
    except:
        pass
EOF

git add src/scratch.py
git commit -m "Add scratch helper"
git push -u origin test-review
```

Open the PR:

```bash
gh pr create --fill
```

Or use the "Compare & pull request" button GitHub shows on the repo page.

---

## Step 7 — Watch it work

Open the **Actions** tab. The `CodeLens review` workflow starts within seconds.

| Step | Time |
|---|---|
| Checkout + Python setup | ~15s |
| pip install | ~30s (faster after the first run, thanks to caching) |
| Index the repository | ~10s |
| Review | 20–40s |
| Post comment | ~2s |

About 90 seconds total. Then go to the PR's **Conversation** tab.

You should see a comment from **github-actions bot** listing a critical finding
for the API key, plus the `shell=True`, mutable default, and bare `except`.
Under **Files changed**, critical findings appear as red annotations on the
exact lines.

The workflow also fails deliberately — that is the `--fail-on critical` guard
doing its job. A build that goes red when someone commits a secret is the
feature, not a bug.

---

## Step 8 — Screenshot it

Take a screenshot of that PR comment and put it at the top of your README:

```markdown
## It reviews its own pull requests

![CodeLens reviewing a pull request](docs/pr-review.png)
```

This is the highest-value five minutes in the whole project. Anyone reading
your repository sees the tool working on real code, automatically, without
taking your word for it.

---

## Step 9 — Clean up

```bash
gh pr close test-review --delete-branch
```

Keep the screenshot. Everything else was scaffolding.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `unknown revision origin/main` | shallow clone | `fetch-depth: 0` on the checkout step |
| Comment step 403 | read-only token | Step 5 |
| Log says `provider: mock` | secret name mismatch | check the spelling in Step 4 |
| `401` from Groq | key revoked or wrong provider | regenerate; check `CODELENS_LLM_BASE_URL` |
| `400` with an empty body | model decommissioned | `gh variable set CODELENS_LLM_MODEL --body "<id>"`; list ids at `/v1/models` |
| Context-length error | diff too large | lower `CODELENS_MAX_DIFF_CHARS` |
| Fork PR gets no comment | fork tokens are read-only | expected — see the run summary instead |

The review step prints the provider it used. **Read that line first, always.**

---

## Using CodeLens on your other repositories

`pyproject.toml` makes CodeLens installable, so other repositories can pull it
in without vendoring the code.

Tag a release first:

```bash
git tag v0.1.0
git push --tags
```

Then in the target repository, copy `.github/workflows/pr-review.yml` and
change the install step:

```yaml
- name: Install CodeLens
  run: pip install git+https://github.com/YOUR_USERNAME/codelens.git@v0.1.0
```

**Pin the tag.** Tracking `main` means a push to CodeLens can break CI in every
repository that uses it, and you will debug the wrong project for an hour.

Because `pyproject.toml` declares `[project.scripts]`, the workflow can call
`codelens review .` directly instead of `python -m codelens.cli review .`.

Add the `CODELENS_LLM_API_KEY` secret to each repository too — secrets do not
cross repository boundaries. (An organisation-level secret shared with selected
repositories avoids repeating yourself.)
