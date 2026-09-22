"""The few GitLab REST calls the platform makes, in one place.

Reading a file off a branch (baseline sync, the diff base of a merge request),
committing one file onto a new branch, opening / reading / closing a merge
request. Stable v4 API, PRIVATE-TOKEN auth, one platform-level token — the
merge request is authored by the platform's account and names the requesting
user in its description.

Errors are typed so callers can say what went wrong: `GitLabUnavailable` is
"not configured or not reachable" (never succeeds until someone fixes it),
`GitLabError` is "the server said no" with the status and the first line of
the body.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings, get_settings


class GitLabError(RuntimeError):
    """GitLab answered with an error status."""


class GitLabUnavailable(GitLabError):
    """No base URL / token, or the host could not be reached."""


class GitLabClient:
    def __init__(self, settings: Settings | None = None, *, timeout: float = 30.0):
        self.settings = settings or get_settings()
        self.timeout = timeout

    # -- configuration -------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.settings.gitlab_base_url and self.settings.gitlab_token)

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("gitlab_base_url", self.settings.gitlab_base_url),
                ("gitlab_token", self.settings.gitlab_token),
            )
            if not value
        ]
        if missing:
            raise GitLabUnavailable(
                "GitLab is not configured: set "
                + ", ".join(f"AUTOTUNE_{m.upper()}" for m in missing)
            )

    @staticmethod
    def project_ref(project: str) -> str:
        """A project as the API path wants it: a numeric id as is, a
        `group/sub/name` path URL-encoded once."""
        project = (project or "").strip().strip("/")
        return project if project.isdigit() else quote(project, safe="")

    def project_url(self, project: str) -> str:
        """The project's web URL, for a human-facing link."""
        base = self.settings.gitlab_base_url.rstrip("/")
        return f"{base}/{project.strip('/')}" if project and not project.isdigit() else base

    # -- transport -----------------------------------------------------------

    def _request(self, method: str, project: str, path: str, **kwargs: Any) -> Any:
        self.require()
        base = self.settings.gitlab_base_url.rstrip("/")
        url = f"{base}/api/v4/projects/{self.project_ref(project)}{path}"
        headers = {"PRIVATE-TOKEN": self.settings.gitlab_token}
        try:
            response = httpx.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise GitLabUnavailable(f"gitlab {method} {path}: {exc}") from exc
        if response.status_code >= 300:
            body = response.text.strip().splitlines()
            raise GitLabError(
                f"gitlab {method} {path} -> {response.status_code}: {body[0][:300] if body else ''}"
            )
        return response.json() if response.content else {}

    # -- repository files ----------------------------------------------------

    def get_file(self, project: str, path: str, ref: str) -> dict[str, Any]:
        """A file at `ref`: {"content": text, "commit": last commit sha,
        "blob_id": ..., "ref": ref}."""
        encoded = quote(path.strip("/"), safe="")
        data = self._request("GET", project, f"/repository/files/{encoded}", params={"ref": ref})
        raw = data.get("content", "")
        text = (
            base64.b64decode(raw).decode("utf-8")
            if data.get("encoding", "base64") == "base64"
            else raw
        )
        return {
            "content": text,
            "commit": data.get("last_commit_id", "") or data.get("commit_id", ""),
            "blob_id": data.get("blob_id", ""),
            "ref": data.get("ref", ref),
        }

    def commit_file(
        self,
        project: str,
        *,
        branch: str,
        start_branch: str,
        path: str,
        content: str,
        message: str,
        author_name: str = "",
        author_email: str = "",
    ) -> dict[str, Any]:
        """One commit on a NEW branch (created from `start_branch`) that
        updates `path`. The branch must not already exist."""
        payload: dict[str, Any] = {
            "branch": branch,
            "start_branch": start_branch,
            "commit_message": message,
            "actions": [{"action": "update", "file_path": path, "content": content}],
        }
        if author_name:
            payload["author_name"] = author_name
        if author_email:
            payload["author_email"] = author_email
        return self._request("POST", project, "/repository/commits", json=payload)

    def list_branches(self, project: str, search: str = "", per_page: int = 100) -> list[dict]:
        """The repo's branches, as GitLab returns them.

        The deploy repo keeps one release branch per (model x card x engine),
        so "which branch does this go on" is a real choice — the UI offers
        this list rather than asking someone to type it. `search` is GitLab's
        own filter (a plain substring, or `^release/` for a prefix).
        """
        params: dict[str, Any] = {"per_page": min(per_page, 100)}
        if search:
            params["search"] = search
        data = self._request("GET", project, "/repository/branches", params=params)
        return [
            {
                "name": b.get("name", ""),
                "default": bool(b.get("default")),
                "protected": bool(b.get("protected")),
                "commit": ((b.get("commit") or {}).get("id") or "")[:10],
                "committed_at": (b.get("commit") or {}).get("committed_date", ""),
            }
            for b in (data if isinstance(data, list) else [])
        ]

    def branch_exists(self, project: str, branch: str) -> bool:
        try:
            self._request("GET", project, f"/repository/branches/{quote(branch, safe='')}")
            return True
        except GitLabError as exc:
            if "-> 404" in str(exc):
                return False
            raise

    # -- merge requests ------------------------------------------------------

    def create_merge_request(
        self,
        project: str,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        remove_source_branch: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": remove_source_branch,
        }
        if labels:
            payload["labels"] = ",".join(labels)
        return self._request("POST", project, "/merge_requests", json=payload)

    def get_merge_request(self, project: str, iid: int | str) -> dict[str, Any]:
        return self._request("GET", project, f"/merge_requests/{iid}")

    def close_merge_request(self, project: str, iid: int | str) -> dict[str, Any]:
        return self._request(
            "PUT", project, f"/merge_requests/{iid}", json={"state_event": "close"}
        )

    def trigger_pipeline(self, project: str, ref: str) -> dict[str, Any]:
        return self._request("POST", project, "/pipeline", json={"ref": ref})
