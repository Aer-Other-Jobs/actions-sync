import os
import re
import sys
import requests

GH_TOKEN = os.environ["GH_TOKEN"]
SOURCE_REPO = os.environ["SOURCE_GITHUB_REPO"]  # owner/repo

GITEA_TOKEN = os.environ["GITEA_TOKEN"]
GITEA_OWNER = os.environ["GITEA_OWNER"]
GITEA_REPO = os.environ["GITEA_REPO"]
GITEA_API = os.environ.get(
    "GITEA_API", "https://gitea.com/api/v1"
).rstrip("/")

GH_API = "https://api.github.com"
MARKER_RE = re.compile(r"<!-- mirror:github:(issue|pr):(\d+) -->")

gh = requests.Session()
gh.headers.update({
    # "Authorization": f"Bearer {GH_TOKEN}",
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
    """Fetch all pages using the API's Link header."""
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


def body_with_marker(body, kind, number):
    body = body or ""
    body = MARKER_RE.sub("", body).rstrip()
    return f"{body}\n\n{marker_for(kind, number)}".strip()


def normalized(value):
    return (value or "").strip()


def get_gitea_items():
    return get_all(
        gt,
        gitea_url("issues"),
        params={"state": "all", "type": "all", "limit": 50},
    )


def get_item_map(items):
    result = {}
    for item in items:
        match = MARKER_RE.search(item.get("body") or "")
        if match:
            kind, number = match.group(1), int(match.group(2))
            result[(kind, number)] = item
    return result


def sync_issue_or_pr(source, existing):
    number = source["number"]
    kind = "pr" if "pull_request" in source else "issue"
    title = source.get("title") or "(untitled)"
    body = body_with_marker(source.get("body"), kind, number)
    state = "closed" if source["state"] == "closed" else "open"

    current = existing.get((kind, number))

    if current:
        changes = {}

        if normalized(current.get("title")) != normalized(title):
            changes["title"] = title

        if normalized(current.get("body")) != normalized(body):
            changes["body"] = body

        if (current.get("state") or "").lower() != state:
            changes["state"] = state

        if changes:
            gitea_request(
                "PATCH",
                f"issues/{current['number']}",
                json=changes,
            )
            print(f"Updated Gitea {kind} #{current['number']}")
        else:
            print(f"Unchanged Gitea {kind} for GitHub #{number}")

        return

    if kind == "issue":
        created = gitea_request(
            "POST",
            "issues",
            json={"title": title, "body": body, "state": state},
        )
        print(f"Created Gitea issue #{created['number']} from GitHub #{number}")
        return

    # Creating a Gitea PR requires both branches to exist in Gitea.
    pr_data = source["pull_request"]
    head = source.get("head") or {}
    base = source.get("base") or {}
    head_repo = head.get("repo") or {}

    if head_repo.get("full_name", "").lower() != SOURCE_REPO.lower():
        print(
            f"Skipping GitHub PR #{number}: its head branch is in a fork",
            file=sys.stderr,
        )
        return

    payload = {
        "title": title,
        "body": body,
        "head": head.get("ref"),
        "base": base.get("ref"),
    }

    try:
        created = gitea_request("POST", "pulls", json=payload)
        print(f"Created Gitea PR #{created['number']} from GitHub #{number}")

        if state == "closed":
            gitea_request(
                "PATCH",
                f"issues/{created['number']}",
                json={"state": "closed"},
            )

    except requests.HTTPError as exc:
        print(
            f"Could not create Gitea PR for GitHub #{number}. "
            f"Check that its head and base branches exist in Gitea. "
            f"API error: {exc}",
            file=sys.stderr,
        )


def sync_issues_and_prs():
    current_items = get_item_map(get_gitea_items())

    github_items = get_all(
        gh,
        gh_url("issues"),
        params={"state": "all", "per_page": 100},
    )

    for source in github_items:
        # GitHub's issues endpoint includes PRs; the PR field distinguishes them.
        sync_issue_or_pr(source, current_items)


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
