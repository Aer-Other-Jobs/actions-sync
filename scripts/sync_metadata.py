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

MARKER_RE = re.compile(r"<!-- mirror:github:(issue|pr):(\d+) -->")
SOURCE_LINK_RE = re.compile(r"\n?Original GitHub (?:issue|PR): https?://\S+\s*")

gh = requests.Session()
gh.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
if GH_TOKEN:
    gh.headers["Authorization"] = f"Bearer {GH_TOKEN}"

gt = requests.Session()
gt.headers.update({
    "Authorization": f"token {GITEA_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
})


def get_all(session, url, params=None):
    """Fetch all pages from an API endpoint using its Link header."""
    results = []

    while url:
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()

        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError(f"Expected a list response from {url}")

        results.extend(page)
        url = response.links.get("next", {}).get("url")
        params = None

    return results


def gh_url(path):
    return f"{GH_API}/repos/{SOURCE_REPO}/{path}"


def gitea_url(path):
    return f"{GITEA_API}/repos/{GITEA_OWNER}/{GITEA_REPO}/{path}"


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


def marker_for(kind, number):
    return f"<!-- mirror:github:{kind}:{number} -->"


def mirrored_body(source, kind, number):
    """Build a stable body with a source link and mapping marker."""
    body = source.get("body") or ""

    # Strip any old marker/link before adding the current ones.
    body = MARKER_RE.sub("", body)
    body = SOURCE_LINK_RE.sub("\n", body).rstrip()

    item_type = "PR" if kind == "pr" else "issue"
    source_link = f"Original GitHub {item_type}: {source['html_url']}"

    parts = [part for part in (body, source_link, marker_for(kind, number)) if part]
    return "\n\n".join(parts)


def normalized(value):
    return (value or "").strip()


def get_gitea_items():
    return get_all(
        gt,
        gitea_url("issues"),
        params={"state": "all", "type": "all", "limit": 50},
    )


def get_mirror_map(items):
    """Map (kind, GitHub number) to its Gitea issue/PR object."""
    result = {}

    for item in items:
        match = MARKER_RE.search(item.get("body") or "")
        if match:
            kind, number = match.group(1), int(match.group(2))
            result[(kind, number)] = item

    return result


def update_existing_item(current, title, body, state, kind, source_number):
    """Patch a Gitea issue/PR only when a synced field changed."""
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
        print(f"Unchanged Gitea item for GitHub {kind} #{source_number}")


def create_gitea_issue(title, body, state, description):
    """Create an issue, then explicitly close it if the source is closed."""
    created = gitea_request(
        "POST",
        "issues",
        json={"title": title, "body": body},
    )

    if state == "closed":
        gitea_request(
            "PATCH",
            f"issues/{created['number']}",
            json={"state": "closed"},
        )

    print(f"Created Gitea issue #{created['number']} {description}")
    return created


def sync_github_item(source, existing):
    number = source["number"]
    kind = "pr" if "pull_request" in source else "issue"
    title = source.get("title") or "(untitled)"
    body = mirrored_body(source, kind, number)
    state = "closed" if source["state"] == "closed" else "open"

    current = existing.get((kind, number))
    if current:
        update_existing_item(
            current=current,
            title=title,
            body=body,
            state=state,
            kind=kind,
            source_number=number,
        )
        return

    if kind == "issue":
        create_gitea_issue(
            title,
            body,
            state,
            f"from GitHub issue #{number}",
        )
        return

    # A PR from a fork can't be created in Gitea unless its source branch
    # has first been made available there. Represent it as a regular issue.
    pr_data = source["pull_request"]
    head = source.get("head") or {}
    base = source.get("base") or {}
    head_repo = head.get("repo") or {}

    if head_repo.get("full_name", "").lower() != SOURCE_REPO.lower():
        create_gitea_issue(
            title,
            body,
            state,
            f"for fork-based GitHub PR #{number}",
        )
        return

    # For same-repository PRs, the head and base branches must exist in Gitea.
    payload = {
        "title": title,
        "body": body,
        "head": head.get("ref"),
        "base": base.get("ref"),
    }

    try:
        created = gitea_request("POST", "pulls", json=payload)

        # Explicitly set state after creation; this also handles already-closed PRs.
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

    except requests.HTTPError as exc:
        print(
            f"Could not create Gitea PR for GitHub PR #{number}. "
            f"Check that head branch '{head.get('ref')}' and base branch "
            f"'{base.get('ref')}' exist in Gitea. API error: {exc}",
            file=sys.stderr,
        )


def sync_issues_and_prs():
    existing = get_mirror_map(get_gitea_items())

    github_items = get_all(
        gh,
        gh_url("issues"),
        params={"state": "all", "per_page": 100},
    )

    for source in github_items:
        # GitHub's issues endpoint also returns PRs; pull_request identifies them.
        sync_github_item(source, existing)


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
        if source.get("draft"):
            continue

        tag = source.get("tag_name")
        if not tag:
            continue

        payload = {
            "tag_name": tag,
            "target_commitish": source.get("target_commitish") or "main",
            "name": source.get("name") or tag,
            "body": source.get("body") or "",
            "draft": False,
            "prerelease": bool(source.get("prerelease")),
        }

        current = releases_by_tag.get(tag)

        if current:
            changes = {}

            for field in ("name", "body", "prerelease"):
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
            gitea_request("POST", "releases", json=payload)
            print(f"Created Gitea release {tag}")


if __name__ == "__main__":
    try:
        sync_issues_and_prs()
        sync_releases()
    except requests.RequestException as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
