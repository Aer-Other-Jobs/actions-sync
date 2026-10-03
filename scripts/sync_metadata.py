import os
import re
import sys

import requests


GH_TOKEN = os.environ.get("GH_TOKEN")
SOURCE_REPO = os.environ["SOURCE_GITHUB_REPO"]  # owner/repo

GITEA_TOKEN = os.environ["GITEA_TOKEN"]
GITEA_OWNER = os.environ["GITEA_OWNER"]
GITEA_REPO = os.environ["GITEA_REPO"]
GITEA_API = os.environ.get(
    "GITEA_API",
    "https://gitea.com/api/v1",
).rstrip("/")

GH_API = "https://api.github.com"

MIRROR_RE = re.compile(
    r"<!-- mirror:github:(issue|pr):(\d+) -->"
)

COMMENT_MARKER_RE = re.compile(
    r"<!-- mirror:github:(issue|pr):(\d+):comment:(\d+) -->"
)

SOURCE_ATTRIBUTION_RE = re.compile(
    r"\n?Originally opened by @\S+ on GitHub: https?://\S+\s*"
)


# ---------------------------------------------------------------------------
# HTTP sessions
# ---------------------------------------------------------------------------

gh = requests.Session()
gh.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
)

if GH_TOKEN:
    gh.headers["Authorization"] = f"Bearer {GH_TOKEN}"


gt = requests.Session()
gt.headers.update(
    {
        "Authorization": f"token {GITEA_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_all(session, url, params=None):
    """Fetch all pages from an API endpoint using its Link header."""
    results = []

    while url:
        response = session.get(
            url,
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        page = response.json()

        if not isinstance(page, list):
            raise RuntimeError(
                f"Expected a list response from {url}"
            )

        results.extend(page)

        url = response.links.get("next", {}).get("url")
        params = None

    return results


def gh_url(path):
    return f"{GH_API}/repos/{SOURCE_REPO}/{path}"


def gitea_url(path):
    return (
        f"{GITEA_API}/repos/"
        f"{GITEA_OWNER}/{GITEA_REPO}/{path}"
    )


def gitea_request(method, path, **kwargs):
    response = gt.request(
        method,
        gitea_url(path),
        timeout=30,
        **kwargs,
    )

    response.raise_for_status()

    if response.status_code == 204 or not response.content:
        return None

    return response.json()


# ---------------------------------------------------------------------------
# Body and marker helpers
# ---------------------------------------------------------------------------

def marker_for(kind, number):
    return f"<!-- mirror:github:{kind}:{number} -->"


def comment_marker_for(kind, number, comment_id):
    return (
        f"<!-- mirror:github:{kind}:{number}:"
        f"comment:{comment_id} -->"
    )


def mirrored_body(source, kind, number):
    """
    Add original GitHub author attribution and a stable mapping marker.
    """
    body = source.get("body") or ""

    # Remove previous mirror marker and attribution before rebuilding.
    body = MIRROR_RE.sub("", body)
    body = SOURCE_ATTRIBUTION_RE.sub("\n", body).rstrip()

    author = (source.get("user") or {}).get("login") or "unknown"

    attribution = (
        f"Originally opened by @{author} on GitHub: "
        f"{source['html_url']}"
    )

    parts = [
        part
        for part in (
            body,
            attribution,
            marker_for(kind, number),
        )
        if part
    ]

    return "\n\n".join(parts)


def mirrored_comment_body(comment, kind, source_number):
    """
    Add GitHub attribution and a stable comment ID marker.
    """
    body = comment.get("body") or ""

    author = (comment.get("user") or {}).get("login") or "unknown"
    comment_url = comment.get("html_url") or ""
    comment_id = comment["id"]

    attribution = (
        f"Originally commented by @{author} on GitHub: "
        f"{comment_url}"
    )

    marker = comment_marker_for(
        kind,
        source_number,
        comment_id,
    )

    return "\n\n".join(
        part
        for part in (
            body,
            attribution,
            marker,
        )
        if part
    )


def normalized(value):
    return (value or "").strip()


# ---------------------------------------------------------------------------
# Gitea issue, PR, and comment helpers
# ---------------------------------------------------------------------------

def get_gitea_items():
    return get_all(
        gt,
        gitea_url("issues"),
        params={
            "state": "all",
            "type": "all",
            "limit": 50,
        },
    )


def get_mirror_map(items):
    """
    Map (kind, GitHub number) to its Gitea issue or PR object.
    """
    result = {}

    for item in items:
        body = item.get("body") or ""
        match = MIRROR_RE.search(body)

        if match:
            kind = match.group(1)
            number = int(match.group(2))
            result[(kind, number)] = item

    return result


def get_gitea_comments(issue_number):
    return get_all(
        gt,
        gitea_url(f"issues/{issue_number}/comments"),
        params={"limit": 50},
    )


def sync_comments(source, gitea_issue_number, kind):
    """
    Synchronize GitHub issue/PR conversation comments into Gitea.

    GitHub's /issues/{number}/comments endpoint also contains the
    regular conversation comments for pull requests.
    """
    source_number = source["number"]

    github_comments = get_all(
        gh,
        gh_url(f"issues/{source_number}/comments"),
        params={"per_page": 100},
    )

    gitea_comments = get_gitea_comments(gitea_issue_number)

    existing_comment_ids = set()

    for comment in gitea_comments:
        body = comment.get("body") or ""
        match = COMMENT_MARKER_RE.search(body)

        if match:
            existing_comment_ids.add(int(match.group(3)))

    for comment in github_comments:
        comment_id = comment["id"]

        if comment_id in existing_comment_ids:
            continue

        body = mirrored_comment_body(
            comment=comment,
            kind=kind,
            source_number=source_number,
        )

        gitea_request(
            "POST",
            f"issues/{gitea_issue_number}/comments",
            json={"body": body},
        )

        print(
            f"Created Gitea comment for GitHub "
            f"{kind} #{source_number}, "
            f"comment {comment_id}"
        )


def create_gitea_issue(
    title,
    body,
    state,
    description,
    source=None,
    kind=None,
):
    """
    Create a Gitea issue, then close it if the GitHub item is closed.
    """
    created = gitea_request(
        "POST",
        "issues",
        json={
            "title": title,
            "body": body,
        },
    )

    if state == "closed":
        gitea_request(
            "PATCH",
            f"issues/{created['number']}",
            json={"state": "closed"},
        )

    print(
        f"Created Gitea issue #{created['number']} "
        f"{description}"
    )

    if source is not None and kind is not None:
        sync_comments(
            source=source,
            gitea_issue_number=created["number"],
            kind=kind,
        )

    return created


def update_existing_item(
    current,
    title,
    body,
    state,
    kind,
    source_number,
    source,
):
    """
    Update an existing Gitea issue/PR only when fields changed.
    Then synchronize its comments.
    """
    changes = {}

    if normalized(current.get("title")) != normalized(title):
        changes["title"] = title

    if normalized(current.get("body")) != normalized(body):
        changes["body"] = body

    current_state = (current.get("state") or "").lower()

    if current_state != state:
        changes["state"] = state

    if changes:
        gitea_request(
            "PATCH",
            f"issues/{current['number']}",
            json=changes,
        )

        print(
            f"Updated Gitea item #{current['number']} "
            f"from GitHub {kind} #{source_number}: "
            f"{', '.join(changes.keys())}"
        )
    else:
        print(
            f"Unchanged Gitea item for GitHub "
            f"{kind} #{source_number}"
        )

    sync_comments(
        source=source,
        gitea_issue_number=current["number"],
        kind=kind,
    )


# ---------------------------------------------------------------------------
# GitHub issue and pull request synchronization
# ---------------------------------------------------------------------------

def sync_github_item(source, existing):
    number = source["number"]

    # GitHub's issues endpoint also returns pull requests.
    kind = "pr" if "pull_request" in source else "issue"

    title = source.get("title") or "(untitled)"
    body = mirrored_body(source, kind, number)

    state = (
        "closed"
        if source.get("state") == "closed"
        else "open"
    )

    current = existing.get((kind, number))

    if current:
        update_existing_item(
            current=current,
            title=title,
            body=body,
            state=state,
            kind=kind,
            source_number=number,
            source=source,
        )
        return

    # Normal GitHub issue.
    if kind == "issue":
        create_gitea_issue(
            title=title,
            body=body,
            state=state,
            description=f"from GitHub issue #{number}",
            source=source,
            kind=kind,
        )
        return

    # Pull requests from forks require the fork branch to exist in Gitea.
    # Since the refs mirror only mirrors the main repository, represent
    # those PRs as regular issues.
    head = source.get("head") or {}
    base = source.get("base") or {}

    head_repo = head.get("repo") or {}
    head_repo_name = head_repo.get("full_name", "")

    is_same_repository = (
        head_repo_name.lower() == SOURCE_REPO.lower()
    )

    if not is_same_repository:
        create_gitea_issue(
            title=title,
            body=body,
            state=state,
            description=(
                f"for fork-based GitHub PR #{number}"
            ),
            source=source,
            kind=kind,
        )
        return

    # Same-repository pull requests can be created as actual Gitea PRs,
    # provided the head and base branches were mirrored.
    head_branch = head.get("ref")
    base_branch = base.get("ref")

    if not head_branch or not base_branch:
        print(
            f"Skipping GitHub PR #{number}: "
            "missing head or base branch",
            file=sys.stderr,
        )
        return

    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }

    try:
        created = gitea_request(
            "POST",
            "pulls",
            json=payload,
        )

        # Explicitly close PRs that are already closed on GitHub.
        if state == "closed":
            gitea_request(
                "PATCH",
                f"issues/{created['number']}",
                json={"state": "closed"},
            )

        print(
            f"Created Gitea PR #{created['number']} "
            f"from GitHub PR #{number}"
        )

        sync_comments(
            source=source,
            gitea_issue_number=created["number"],
            kind=kind,
        )

    except requests.HTTPError as exc:
        print(
            f"Could not create Gitea PR for GitHub PR "
            f"#{number}. Check that head branch "
            f"'{head_branch}' and base branch "
            f"'{base_branch}' exist in Gitea. "
            f"API error: {exc}",
            file=sys.stderr,
        )


def sync_issues_and_prs():
    existing_items = get_gitea_items()
    existing = get_mirror_map(existing_items)

    github_items = get_all(
        gh,
        gh_url("issues"),
        params={
            "state": "all",
            "per_page": 100,
        },
    )

    for source in github_items:
        sync_github_item(
            source=source,
            existing=existing,
        )


# ---------------------------------------------------------------------------
# Release synchronization
# ---------------------------------------------------------------------------

def sync_releases():
    github_releases = get_all(
        gh,
        gh_url("releases"),
        params={"per_page": 100},
    )

    gitea_releases = get_all(
        gt,
        gitea_url("releases"),
        params={"limit": 50},
    )

    releases_by_tag = {
        release.get("tag_name"): release
        for release in gitea_releases
        if release.get("tag_name")
    }

    for source in github_releases:
        # Do not mirror draft releases.
        if source.get("draft"):
            continue

        tag = source.get("tag_name")

        if not tag:
            continue

        payload = {
            "tag_name": tag,
            "target_commitish": (
                source.get("target_commitish") or "main"
            ),
            "name": source.get("name") or tag,
            "body": source.get("body") or "",
            "draft": False,
            "prerelease": bool(source.get("prerelease")),
        }

        current = releases_by_tag.get(tag)

        if current:
            changes = {}

            for field in (
                "name",
                "body",
                "prerelease",
            ):
                if current.get(field) != payload[field]:
                    changes[field] = payload[field]

            if changes:
                gitea_request(
                    "PATCH",
                    f"releases/{current['id']}",
                    json=changes,
                )
                print(f"Updated Gitea release {tag}")
            else:
                print(f"Unchanged Gitea release {tag}")

        else:
            gitea_request(
                "POST",
                "releases",
                json=payload,
            )
            print(f"Created Gitea release {tag}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        sync_issues_and_prs()
        sync_releases()

    except requests.RequestException as exc:
        print(
            f"Sync failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
