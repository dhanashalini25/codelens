"""Create a throwaway git repository with a deliberately flawed commit.

Gives you something to review in ten seconds without pointing CodeLens at real
code. Every issue planted here is one the static rules are designed to catch,
so the demo output is verifiable rather than impressive-looking.

    python demo/make_demo_repo.py /tmp/demo-repo
    python -m codelens.cli index /tmp/demo-repo
    python -m codelens.cli review /tmp/demo-repo
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASELINE = '''"""User account helpers."""

from dataclasses import dataclass


@dataclass
class User:
    """A user account."""

    id: int
    email: str
    active: bool = True


def normalize_email(email):
    """Lowercase and strip an email address."""
    return email.strip().lower()


def find_user(users, email):
    """Return the user with this email, or None."""
    target = normalize_email(email)
    for user in users:
        if normalize_email(user.email) == target:
            return user
    return None
'''

FLAWED_CHANGE = '''"""User account helpers."""

import subprocess
from dataclasses import dataclass

SESSION_SECRET = "sk-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"


@dataclass
class User:
    """A user account."""

    id: int
    email: str
    active: bool = True


def normalize_email(email):
    """Lowercase and strip an email address."""
    return email.strip().lower()


def find_user(users, email):
    """Return the user with this email, or None."""
    target = normalize_email(email)
    for user in users:
        if normalize_email(user.email) == target:
            return user
    return None


def audit_login(user, log_path, history=[]):
    # TODO: move this off the request path
    history.append(user.id)
    if user.email == None:
        return False
    assert user.active, "inactive user"
    try:
        subprocess.run(f"echo {user.email} >> {log_path}", shell=True)
    except:
        pass
    return True


def classify(user, score, region, tier, flags):
    if score > 90:
        if region == "eu" and tier == "gold":
            return "priority"
        elif "beta" in flags:
            return "beta-priority"
        else:
            return "high"
    elif score > 70:
        if tier == "gold" or tier == "silver":
            return "medium-plus"
        elif region in ("eu", "uk") and user.active:
            return "medium-eu"
        else:
            return "medium"
    elif score > 40:
        if "trial" in flags and not user.active:
            return "lapsed-trial"
        elif region == "us":
            return "low-us"
        else:
            return "low"
    else:
        return "unranked"
'''


def run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codelens-demo")
    if destination.exists():
        raise SystemExit(f"{destination} already exists - delete it or pick another path.")

    (destination / "src").mkdir(parents=True)
    (destination / "src" / "accounts.py").write_text(BASELINE, encoding="utf-8")

    run(destination, "init", "-q")
    run(destination, "config", "user.email", "demo@example.com")
    run(destination, "config", "user.name", "CodeLens Demo")
    run(destination, "add", ".")
    run(destination, "commit", "-qm", "Add account helpers")

    (destination / "src" / "accounts.py").write_text(FLAWED_CHANGE, encoding="utf-8")
    run(destination, "add", ".")
    run(destination, "commit", "-qm", "Add login auditing and classification")

    print(f"Demo repository created at {destination}\n")
    print("Now run:")
    print(f"  python -m codelens.cli index {destination}")
    print(f"  python -m codelens.cli review {destination}")


if __name__ == "__main__":
    main()
