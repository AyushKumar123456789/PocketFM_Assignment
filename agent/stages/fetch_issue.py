"""Fetch a GitHub issue (and its comments, optionally) via the REST API."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import requests

from agent.config import RepoConfig


GITHUB_API = "https://api.github.com"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    url: str = ""
    comments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "title": self.title, "body": self.body,
            "labels": self.labels, "state": self.state, "url": self.url,
            "comments": self.comments,
        }

    @property
    def text_blob(self) -> str:
        """Single string suitable for keyword extraction and prompting."""
        parts = [self.title, "", self.body or ""]
        for c in self.comments:
            user = c.get("user", {}).get("login", "?")
            parts.append(f"\n--- comment by @{user} ---\n{c.get('body','')}")
        return "\n".join(parts)


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_issue(cfg: RepoConfig, number: int, *, include_comments: bool = True) -> Issue:
    base = f"{GITHUB_API}/repos/{cfg.owner}/{cfg.repo}/issues/{number}"
    r = requests.get(base, headers=_headers(), timeout=30)
    if r.status_code == 404:
        raise FileNotFoundError(f"Issue {cfg.slug}#{number} not found.")
    r.raise_for_status()
    data = r.json()

    comments: list[dict[str, Any]] = []
    if include_comments and data.get("comments", 0) > 0:
        cr = requests.get(base + "/comments", headers=_headers(), timeout=30)
        cr.raise_for_status()
        comments = cr.json()

    return Issue(
        number=number,
        title=data.get("title", ""),
        body=data.get("body") or "",
        labels=[lb["name"] for lb in data.get("labels", [])],
        state=data.get("state", "open"),
        url=data.get("html_url", ""),
        comments=comments,
    )
