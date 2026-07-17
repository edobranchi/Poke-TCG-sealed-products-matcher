"""Upload the built sealed_prices.db + meta.json to a GitHub release.

The release uses the fixed tag "latest" so the app always downloads from the
same URL regardless of version. We delete the old assets and upload the new
ones - GitHub releases don't support in-place update.

Called automatically by collector_app.py after a successful run + validation.
Can also be run manually:

  python3 publish.py --out-dir out --repo edobranchi/Poke-TCG-sealed-products-matcher

Required env var: GH_TOKEN (fine-grained PAT, Contents read+write on the repo).
"""

import hashlib
import json
import logging
import os
import sys

import requests

TAG = "latest"
log = logging.getLogger("publish")


def publish(out_dir="out", repo=None, token=None):
    repo = repo or os.environ.get("DATA_REPO")
    token = token or os.environ.get("GH_TOKEN")
    if not repo or not token:
        raise RuntimeError("DATA_REPO and GH_TOKEN env vars are required")

    db_path = os.path.join(out_dir, "sealed_prices.db")
    meta_path = os.path.join(out_dir, "meta.json")
    for path in (db_path, meta_path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"missing: {path}")

    with open(meta_path) as f:
        meta = json.load(f)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    api = f"https://api.github.com/repos/{repo}"

    # verify the release and tag exist
    r = session.get(f"{api}/releases/tags/{TAG}", timeout=30)
    if r.status_code == 404:
        raise RuntimeError(
            f"no release with tag '{TAG}' found in {repo} — "
            "create it on GitHub first (Releases → Create new release, tag: latest)")
    r.raise_for_status()
    release = r.json()
    release_id = release["id"]

    # guard: don't publish an older version than what's already live
    existing_meta_url = next(
        (a["browser_download_url"] for a in release.get("assets", [])
         if a["name"] == "meta.json"), None)
    if existing_meta_url:
        try:
            existing = session.get(existing_meta_url, timeout=15).json()
            if int(existing.get("version", 0)) >= int(meta["version"]):
                log.info("already published version %s >= %s, skipping",
                         existing["version"], meta["version"])
                return False
        except Exception:
            pass  # if we can't read the existing meta, proceed with upload

    # verify sha256 matches what's in meta.json
    actual_sha = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    if actual_sha != meta.get("sha256"):
        raise RuntimeError(
            f"sha256 mismatch: meta.json says {meta['sha256']}, file is {actual_sha}")

    # delete old assets so we can upload fresh ones
    for asset in release.get("assets", []):
        if asset["name"] in ("sealed_prices.db", "meta.json"):
            r = session.delete(f"{api}/releases/assets/{asset['id']}", timeout=30)
            r.raise_for_status()
            log.info("deleted old asset: %s", asset["name"])

    upload_base = release["upload_url"].replace("{?name,label}", "")

    def upload(path, name, content_type):
        log.info("uploading %s (%s)...", name, _human_size(os.path.getsize(path)))
        with open(path, "rb") as f:
            r = session.post(f"{upload_base}?name={name}",
                             headers={"Content-Type": content_type},
                             data=f, timeout=120)
        r.raise_for_status()
        log.info("uploaded %s → %s", name, r.json()["browser_download_url"])

    upload(db_path, "sealed_prices.db", "application/octet-stream")
    upload(meta_path, "meta.json", "application/json")

    log.info("published version %s to %s/releases/tag/%s", meta["version"], repo, TAG)
    return True


def _human_size(n):
    return f"{n / 1024:.0f} KB" if n < 1_000_000 else f"{n / 1_000_000:.1f} MB"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--repo", default=None, help="owner/repo (or set DATA_REPO env var)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        published = publish(args.out_dir, args.repo)
        sys.exit(0 if published else 0)
    except Exception as e:
        log.error("publish failed: %s", e)
        sys.exit(1)
