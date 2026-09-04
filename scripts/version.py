#!/usr/bin/env python3
"""Derive runtime build identity from repository/build metadata.

Precedence:
  RELEASE/TAG supplied by the build system
  GIT_TAG
  GIT_COMMIT / SOURCE_COMMIT / RAILWAY_GIT_COMMIT_SHA
  git rev-parse (when .git is available)
  repository + branch
  runtime fallback

Secrets are never inspected.
"""
from __future__ import annotations
import os
import re
import subprocess

def first(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""

def clean(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._:@/+=-]+", "-", value).strip("-")
    return value[:160] or "runtime"

def git(args):
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
    except Exception:
        return ""

tag = first("GIT_TAG", "CI_COMMIT_TAG", "RAILWAY_GIT_TAG")
commit = first(
    "GIT_COMMIT", "SOURCE_COMMIT", "RAILWAY_GIT_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT", "COMMIT_SHA"
) or git(["rev-parse", "HEAD"])
branch = first("GIT_BRANCH", "RAILWAY_GIT_BRANCH", "RAILWAY_GIT_BRANCH_NAME") or git(["branch", "--show-current"])
repo = first("REPOSITORY", "REPOSITORY_NAME", "GITHUB_REPOSITORY", "RAILWAY_PROJECT_NAME")

if tag:
    identity = f"tag:{tag}"
elif commit:
    identity = f"repo:{commit[:12]}"
elif repo and branch:
    identity = f"{repo}@{branch}"
elif repo:
    identity = repo
elif branch:
    identity = branch
else:
    identity = "runtime"

print(clean(identity))
