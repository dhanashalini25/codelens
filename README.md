# CodeLens — repository intelligence and AI code review

CodeLens reviews a code change twice: once with deterministic static rules, and
once with a language model that has been given the surrounding code as context.
The two result sets are merged, deduplicated and ranked, then stored so the
history is searchable.

The split matters. Static rules are exact, instant and free, and they catch the
high-frequency issues. The model catches what a rule cannot express — a wrong
assumption, a missing case, a name that says the opposite of what the code does.
Running the rules **first** also shrinks the prompt, because the model is told
what has already been found and does not spend its output repeating it.

```
$ python -m codelens.cli review /tmp/codelens-demo

  Add login auditing and classification
  ac9f16e7e5  CodeLens Demo

  1 file(s) changed, +42/-0. Findings: 1 critical, 3 high, 2 medium, 1 low, 3 info.

  CRITICAL  src/accounts.py:6   Possible OpenAI-style API key committed to the repository
            A value on this line matches the shape of a real credential. If it is
            genuine, it must be rotated — removing it from the working tree does
            not remove it from git history.
            fix: Move the value to an environment variable and rotate the key.

  HIGH      src/accounts.py:32  Mutable default argument in audit_login()
            The default object is created once at definition time and shared by
            every call, so mutations leak between calls.
            fix: Default to None and build the container inside the function.

  HIGH      src/accounts.py:39  subprocess called with shell=True
  HIGH      src/accounts.py:40  Bare except catches everything
  MEDIUM    src/accounts.py:37  assert used outside tests
  MEDIUM    src/accounts.py:45  classify() has cyclomatic complexity 14
  ...
```

---

## Quickstart

No API key needed. With the default `mock` provider CodeLens runs the static
half of the pipeline and reports exactly what it found — it never fabricates an
AI opinion to make the demo look better.

```bash
git clone https://github.com/YOUR_USERNAME/codelens.git
cd codelens
pip install -r requirements.txt

make demo     # builds a flawed demo repo and reviews it
```

Point it at real code:

```bash
python -m codelens.cli index /path/to/repo      # build the code index
python -m codelens.cli review /path/to/repo     # review the last commit
python -m codelens.cli review . --working       # review uncommitted changes
```

Turn on the AI pass by copying `.env.example` to `.env` and setting a provider.
Groq and Google Gemini both have free tiers that are sufficient:

```bash
CODELENS_LLM_PROVIDER=groq
CODELENS_LLM_API_KEY=gsk_...
CODELENS_LLM_MODEL=openai/gpt-oss-120b
```

---

## How it works

```
repository ──► walk ──► parse ──► chunk by symbol ──► embed ──► SQLite
                        (AST for Python,                          │
                         regex elsewhere)                         │
                                                                  │
git diff ──► hunks with real line numbers                         │
               │                                                  │
               ├──► static rules on CHANGED LINES ONLY ──┐        │
               │                                          │        │
               └──► related code pulled from the index ◄──┼────────┘
                              │                           │
                              ▼                           │
                    prompt: diff + context +              │
                    "already found: ..."                  │
                              │                           │
                              ▼                           │
                          LLM findings ───────────────────┤
                                                          ▼
                                            merge, dedupe, rank by severity
                                                          │
                                                          ▼
                                              SQLite: searchable history
```

### Design decisions worth defending

**Static rules run only on lines the change actually touched.** A review that
reports pre-existing issues across the whole file is noise, and noisy reviews
get ignored. There is a test for this (`test_review_ignores_untouched_code`).

**Diff line numbers are absolute, not offsets.** The parser tracks old and new
file positions through every hunk, so a finding points at `src/accounts.py:39`
— a line you can open. Getting this subtly wrong is the most common bug in
homemade diff tooling, so it has its own test.

**Code is chunked on symbol boundaries, not fixed line windows.** A function cut
in half retrieves badly and reads worse when handed to a model as context.

**Symbol names are weighted in the index.** A function's name is the densest
description of it that exists, so it is repeated in the embedded text. Measured
on `eval/search_eval.py`: top-1 goes from 4/10 to 6/10, top-5 from 7/10 to 9/10.

**No compiled dependencies.** Retrieval is hashed TF-IDF in pure numpy — no
scikit-learn, no scipy. This project was first run on a Windows machine where
an Application Control policy blocked scipy's DLL, which made the point better
than any argument could: a tool that will not import is worse than a tool with
a simpler algorithm.

**No tree-sitter.** Python gets a real parse through the standard library `ast`
module — exact boundaries, signatures, decorators, complexity. Other languages
get a regex pass that finds top-level declarations well enough to chunk on.
Tree-sitter would parse them properly, at the cost of a native dependency and a
grammar build step, for a small gain when the model reads the source text
anyway. That is a trade, and it is documented rather than hidden.

**Model output is never trusted as-is.** Severities are validated against an
allow-list, file paths are checked against the diff, confidence below the
threshold is dropped, and a malformed response degrades to zero AI findings
without losing the static ones.

**Offline means offline.** With the `mock` provider the review reports static
findings only. A test (`test_mock_provider_adds_no_llm_findings`) enforces it.

---

## Static rules

Each rule reports a line number, a severity, and a concrete fix.

| Rule | Severity | Why it matters |
|---|---|---|
| Hardcoded credentials (AWS, GitHub, OpenAI-style, private keys) | critical | Removing a secret from the working tree does not remove it from git history |
| `eval()` / `exec()` | critical | Arbitrary code execution if any input is user-reachable |
| `subprocess(..., shell=True)` | high | Any interpolated value becomes injectable |
| Mutable default arguments | high | The default is shared across every call |
| Bare `except:` | high | Also catches `KeyboardInterrupt` and `SystemExit` |
| `except: pass` | medium | The failure leaves no trace at all |
| `assert` outside tests | medium | Stripped under `python -O`, so validation silently stops |
| Cyclomatic complexity ≥ 11 | medium/high | Each branch is a path that needs its own test |
| Functions over 80 lines | low | Usually more than one responsibility |
| `== None` / `!= None` | low | `is None` is the correct identity test |
| Missing docstrings on public API | info | |
| TODO / FIXME / HACK markers | info | |
| Lines over 120 characters | low | |

The secret patterns are deliberately narrow. A noisy secret scanner gets muted,
and a muted scanner catches nothing.

---

## Commands

```bash
python -m codelens.cli index <repo>              # build the code index
python -m codelens.cli review <repo>             # review HEAD~1...HEAD
python -m codelens.cli review <repo> --working   # review uncommitted changes
python -m codelens.cli review <repo> --staged    # review staged changes
python -m codelens.cli review <repo> --fail-on high   # non-zero exit for CI

python -m codelens.cli search "auth token refresh"    # semantic code search
python -m codelens.cli structure src/app.py           # symbols, sizes, complexity
python -m codelens.cli explain src/app.py -s handler  # explain a symbol
python -m codelens.cli document src/app.py            # propose docstrings
python -m codelens.cli tests src/app.py               # recommend tests

python -m codelens.cli reviews                   # past reviews
python -m codelens.cli show 3                    # one review in full
python -m codelens.cli find --severity high      # search all findings ever
python -m codelens.cli stats                     # hotspots and category counts
```

`structure` and the complexity ranking inside `tests` are computed locally, so
they work with no provider configured.

---

## API

```bash
uvicorn codelens.api:app --reload    # http://localhost:8000/docs
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/index` | index a repository |
| `POST` | `/review` | review a ref range or the working tree |
| `POST` | `/review/patch` | review a diff supplied directly — how CI calls this |
| `GET` | `/search?q=` | semantic code search |
| `POST` | `/structure` | symbols, complexity, undocumented public API |
| `POST` | `/explain` | explain a file or symbol |
| `POST` | `/document` | propose docstrings |
| `POST` | `/tests` | recommend tests |
| `GET` | `/reviews`, `/reviews/{id}` | review history |
| `GET` | `/findings` | search findings across every review |
| `GET` | `/stats` | aggregate counts and file hotspots |

---

## CI integration

`.github/workflows/pr-review.yml` runs CodeLens on every pull request, posts the
findings as a comment, and fails the build on a critical finding. It works
without a secret configured — it falls back to the static pass rather than
erroring, so a fork's pull request still gets a review.

---

## Search benchmark

Ten natural-language questions with a known correct symbol, run against
CodeLens's own source.

```bash
python -m eval.search_eval            # score current settings
python -m eval.search_eval --sweep    # compare configurations
```

Two settings in this repository were decided by it, and the second is the more
useful story:

| Change | top-1 | top-5 |
|---|---|---|
| No symbol weighting | 4/10 | 7/10 |
| Symbol weighting (shipped) | 6/10 | 9/10 |
| Pivoted length normalization, b=0.75 | 4/10 | 8/10 |

Length normalization was an obvious-sounding fix for short chunks outranking
long ones. It measured *worse*, so it ships disabled with the number recorded
in the code, so nobody restores it on intuition. Ten queries is a small
benchmark — one point is noise; only the shape of the curve is trustworthy.

## Testing

```bash
python -m pytest tests -q
```

35 tests. The ones that matter most:

- `test_added_line_numbers_are_absolute_not_relative` — the diff-parsing bug that would make every finding point at the wrong line
- `test_only_lines_restricts_scope` — reviews stay scoped to the change
- `test_review_ignores_untouched_code` — end-to-end version of the same guarantee
- `test_ignores_clean_code` — the rules do not fire on correct code
- `test_mock_provider_adds_no_llm_findings` — offline output is honest

The end-to-end test builds a real git repository in a temporary directory,
commits a deliberately flawed change, and asserts the review catches it.

---

## Project layout

```
codelens/
  repo.py      repository walking, language detection, exclusion rules
  parsing.py   AST symbols and complexity for Python; regex fallback elsewhere
  diff.py      unified diff parsing with absolute line numbers; git plumbing
  rules.py     the deterministic static checks
  index.py     symbol-boundary chunking, symbol weighting, code search
  review.py    the pipeline: static pass, AI pass, merge, persist
  explain.py   explanation, docstring generation, test recommendations
  history.py   SQLite: repositories, reviews, findings, aggregate stats
  llm.py       provider-agnostic client with defensive JSON extraction
  api.py       FastAPI
  cli.py       typer CLI
eval/          search benchmark
demo/          builds a flawed repository to review
tests/         35 tests
```

---

## Roadmap

- [ ] Tree-sitter parsing for full multi-language symbol extraction
- [ ] Inline review comments on the exact diff line, not one PR comment
- [ ] Track findings across reviews — flag regressions, mark resolved
- [ ] Call-graph context instead of embedding similarity
- [ ] Learn from dismissed findings to suppress repeat false positives
- [ ] Web dashboard over the history and hotspot data

---

## Licence

MIT.
