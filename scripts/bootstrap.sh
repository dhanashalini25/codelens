#!/usr/bin/env bash
#
# CodeLens bootstrap - macOS / Linux / Git Bash / WSL
#
# Verifies locally, publishes to GitHub, configures the secret and workflow
# permissions, then opens a test pull request so you can watch the review run.
#
# Run from inside the extracted codelens folder:
#
#     bash scripts/bootstrap.sh
#
# Prerequisites (checked below):
#   - Python 3.10+          https://python.org
#   - git                   https://git-scm.com
#   - GitHub CLI, signed in https://cli.github.com   then:  gh auth login
#
# Your Groq key is piped straight into `gh secret set`. It goes from your
# keyboard to GitHub - never written to a file, never echoed to the screen.

set -euo pipefail

CYAN=$'\033[36m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
step() { printf "\n%s[%s] %s%s\n" "$CYAN" "$1" "$2" "$OFF"; }
ok()   { printf "    %sOK  %s%s\n" "$GREEN" "$1" "$OFF"; }
fail() { printf "    %s!!  %s%s\n" "$RED" "$1" "$OFF"; exit 1; }

PY=${PYTHON:-python3}

# ---------------------------------------------------------------- checks ----
step 1 "Checking prerequisites"

command -v "$PY" >/dev/null 2>&1 || fail "python3 not found. Install Python 3.10+."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || fail "Need Python 3.10 or newer. Found: $($PY --version)"
ok "$($PY --version)"

command -v git >/dev/null 2>&1 || fail "git not found."
ok "git present"

command -v gh >/dev/null 2>&1 \
  || fail "GitHub CLI not found. Install from https://cli.github.com, then: gh auth login"

AUTH_OUTPUT=$(gh auth status 2>&1) || fail "GitHub CLI is not signed in. Run:  gh auth login"
ok "GitHub CLI signed in"

# The workflow file can only be pushed by a token carrying the workflow scope.
if ! grep -q "workflow" <<<"$AUTH_OUTPUT"; then
    printf "    %sAdding the 'workflow' scope (needed to push .github/workflows)%s\n" "$YELLOW" "$OFF"
    gh auth refresh -h github.com -s workflow || fail "Could not add the workflow scope."
fi
ok "workflow scope present"

# ------------------------------------------------------- local verification --
step 2 "Installing dependencies and running the test suite"

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "dependencies installed"

python -m pytest tests -q || fail "Tests failed. Fix this before publishing - CI will fail the same way."
ok "41 tests passed"

step 3 "Running the demo so you can see it find real bugs"
DEMO="${TMPDIR:-/tmp}/codelens-demo-$$"
python demo/make_demo_repo.py "$DEMO" >/dev/null
python -m codelens.cli index "$DEMO" >/dev/null
python -m codelens.cli review "$DEMO"

# --------------------------------------------------------------- publish ----
step 4 "Publishing to GitHub"

read -r -p "    Repository name [codelens]: " REPO_NAME
REPO_NAME=${REPO_NAME:-codelens}

[ -f .env ] && printf "    %s.env found - it is gitignored and will NOT be pushed.%s\n" "$YELLOW" "$OFF"

if [ ! -d .git ]; then
    git init --quiet
    git add .
    git commit --quiet -m "CodeLens: repository intelligence and AI code review"
    git branch -M main
    ok "repository initialised"
else
    ok "existing git repository reused"
fi

# Refuse to continue if a key somehow got staged.
git ls-files | grep -qx '\.env' && fail ".env is tracked by git. Run: git rm --cached .env"

gh repo create "$REPO_NAME" --public --source=. --push \
  || fail "Repository creation failed. A repo with that name may already exist."

OWNER=$(gh api user --jq .login)
SLUG="$OWNER/$REPO_NAME"
ok "pushed to https://github.com/$SLUG"

# ------------------------------------------------------------ configure -----
step 5 "Granting the workflow permission to comment"
gh api -X PUT "/repos/$SLUG/actions/permissions/workflow" \
  -f default_workflow_permissions=write >/dev/null
ok "workflow permissions set to read and write"

step 6 "Adding your Groq API key as a repository secret"
echo "    Get a free key at https://console.groq.com  (API Keys -> Create)"
echo "    Paste it below, or press Enter to skip and use static analysis only."
read -r -s -p "    Groq API key: " GROQ_KEY
echo

if [ -z "$GROQ_KEY" ]; then
    printf "    %sSkipped. Reviews will run static analysis only.%s\n" "$YELLOW" "$OFF"
else
    printf '%s' "$GROQ_KEY" | gh secret set CODELENS_LLM_API_KEY --repo "$SLUG" \
      || fail "Could not set the secret."
    ok "secret CODELENS_LLM_API_KEY stored"
fi
unset GROQ_KEY

# ----------------------------------------------------------- test the PR ----
step 7 "Opening a pull request with deliberately flawed code"

git checkout -q -b test-review
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
git commit --quiet -m "Add scratch helper"
git push --quiet -u origin test-review

gh pr create --title "Test: CodeLens review" \
  --body "Deliberately flawed code to exercise the review workflow." >/dev/null
ok "pull request opened"

# ------------------------------------------------------------------ done ----
printf "\n%sDone.%s\n\n" "$GREEN" "$OFF"
echo "  Repository:   https://github.com/$SLUG"
echo "  Actions:      https://github.com/$SLUG/actions"
echo "  Pull request: https://github.com/$SLUG/pulls"
echo
echo "The review takes about 90 seconds. Then open the PR's Conversation tab."
echo "Expect a comment listing a critical finding for the committed API key,"
echo "plus shell=True, a mutable default argument, and a bare except."
echo
echo "The workflow will fail on purpose - that is --fail-on critical working."
echo "Screenshot that comment and put it at the top of your README."
echo
