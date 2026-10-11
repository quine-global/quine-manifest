#!/usr/bin/env python3
"""Make GitHub and Forgejo match manifest.toml.

    python3.14 quine-repos.py                    # plan: report drift, change nothing
    python3.14 quine-repos.py --apply            # carry out the plan
    python3.14 quine-repos.py --apply ncdu ava   # only these repos

Credentials: the Forgejo admin token in $FORGEJO_TOKEN or ~/.config/forgejo/token;
GitHub admin calls go through the `gh` CLI's login; push mirrors use each
bucket's mirror_token_file. Never deletes a repository.
"""
import argparse, json, os, subprocess, sys, time, urllib.error, urllib.request

try:
    import tomllib
except ImportError:
    sys.exit("needs Python 3.11+ (try python3.14)")

HERE = os.path.dirname(os.path.abspath(__file__))
OK = (200, 201, 202, 204)


# ── Forgejo ──────────────────────────────────────────────────────────────────
class Forgejo:
    def __init__(self, url, token):
        self.api = url.rstrip("/") + "/api/v1"
        self.token = token

    def call(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(
            self.api + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"token {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode(errors="replace")[:300]
        return status, data

    def must(self, method, path, body=None, timeout=60):
        status, data = self.call(method, path, body, timeout)
        if status not in OK:
            raise RuntimeError(f"Forgejo {method} {path}: HTTP {status}: {data}")
        return data

    def org_repos(self, org):
        """{name: repo} for an org, or None if the org doesn't exist."""
        if self.call("GET", f"/orgs/{org}")[0] == 404:
            return None
        repos, page = {}, 1
        while batch := self.must("GET", f"/orgs/{org}/repos?limit=50&page={page}"):
            repos.update({r["name"]: r for r in batch})
            page += 1
        return repos

    def push_mirrors(self, owner, name):
        return self.must("GET", f"/repos/{owner}/{name}/push_mirrors") or []

    def sync_push_mirror(self, owner, name, url):
        """Trigger a push-mirror sync and wait for its result."""
        def current():
            return next(m for m in self.push_mirrors(owner, name) if same_url(m["remote_address"], url))
        before = current().get("last_update")
        self.must("POST", f"/repos/{owner}/{name}/push_mirrors-sync")
        for _ in range(40):
            time.sleep(6)
            m = current()
            if m.get("last_error"):
                raise RuntimeError(f"push mirror sync failed: {m['last_error'][:500]}")
            if m.get("last_update") != before and not m["last_update"].startswith("0001"):
                return
        raise RuntimeError("push mirror sync gave no result after 4 minutes")


# ── GitHub (via gh) ──────────────────────────────────────────────────────────
def gh(*args, check=True):
    p = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"gh api {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def gh_org_repos(org):
    """{name: repo} for an org, or None if the org doesn't exist."""
    if gh(f"orgs/{org}", check=False).returncode:
        return None
    out = gh("--paginate", f"orgs/{org}/repos?per_page=100&type=all",
             "--jq", ".[] | {name, private, archived, fork, description}").stdout
    return {r["name"]: r for r in map(json.loads, out.splitlines())}


def gh_token():
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


# ── helpers ──────────────────────────────────────────────────────────────────
def same_url(a, b):
    norm = lambda u: u.lower().rstrip("/").removesuffix(".git")
    return norm(a) == norm(b)


def gh_url(org, name):
    return f"https://github.com/{org}/{name}.git"


def read_token(path):
    path = os.path.expanduser(path)
    return open(path).read().strip() if os.path.exists(path) else None


class Plan:
    def __init__(self, name):
        self.name, self.steps, self.notes = name, [], []

    def step(self, desc, fn):
        self.steps.append((desc, fn))

    def note(self, msg):
        self.notes.append(msg)


# ── planning ─────────────────────────────────────────────────────────────────
class World:
    """Current state of every bucket org on both hosts."""

    def __init__(self, manifest, fj):
        self.buckets = manifest["buckets"]
        self.fj = fj
        self.gh = {b["github"]: gh_org_repos(b["github"]) for b in self.buckets.values()}
        self.fjo = {b["forgejo"]: fj.org_repos(b["forgejo"]) for b in self.buckets.values()}
        self.missing_fj_orgs = set()

    @staticmethod
    def _find(orgs, name):
        hits = [(org, repos[name]) for org, repos in orgs.items() if repos and name in repos]
        return hits

    def find_gh(self, name):
        return self._find(self.gh, name)

    def find_fj(self, name):
        return self._find(self.fjo, name)


def plan_repo(name, spec, world, fj, cfg):
    p = Plan(name)
    bucket = world.buckets[spec["bucket"]]
    primary = spec.get("primary", "forgejo")
    upstream = spec.get("upstream")  # Forgejo pull-mirrors this, then push-mirrors to GitHub
    on_github = spec.get("github", True)
    explicit_archived = spec.get("archived", bucket.get("archived"))  # None = not stated
    archived = bool(explicit_archived)
    gh_org, fj_org = bucket["github"], bucket["forgejo"]
    want_url = gh_url(gh_org, name)

    # GitHub: location and archive state ------------------------------------
    gh_repo = None
    if on_github:
        hits = world.find_gh(name)
        if len(hits) > 1:
            p.note(f"on GitHub in several bucket orgs: {', '.join(o for o, _ in hits)}; fix by hand")
            return p
        if not hits:
            p.note(f"not found on GitHub in any bucket org (create it there, or set github = false)")
            return p
        cur_org, gh_repo = hits[0]
        if cur_org != gh_org and world.gh[gh_org] is None:
            p.note(f"GitHub org {gh_org} doesn't exist; create it at https://github.com/organizations/plan")
            return p
        if gh_repo["archived"] and explicit_archived is None:
            # Archived on GitHub but not in the manifest: don't undo it, ask for the manifest to catch up.
            p.note(f"archived on GitHub but the manifest doesn't say; set archived = true to keep it, or false to unarchive")
            return p
        if gh_repo["archived"] and (cur_org != gh_org or not archived):
            # archived repos can't be changed much; unarchive first, re-archive at the end if wanted
            p.step(f"GitHub: unarchive {cur_org}/{name}",
                   lambda o=cur_org: gh("-X", "PATCH", f"repos/{o}/{name}", "-F", "archived=false"))
            gh_repo = {**gh_repo, "archived": False}
        if cur_org != gh_org:
            p.step(f"GitHub: transfer {cur_org}/{name} → {gh_org}/{name}",
                   lambda o=cur_org: (gh("-X", "POST", f"repos/{o}/{name}/transfer", "-f", f"new_owner={gh_org}"),
                                      wait_gh(gh_org, name)))

    # Forgejo: location, existence, kind -------------------------------------
    hits = world.find_fj(name)
    if len(hits) > 1:
        p.note(f"on Forgejo in several bucket orgs: {', '.join(o for o, _ in hits)}; fix by hand")
        return p
    fj_repo = hits[0][1] if hits else None
    if world.fjo[fj_org] is None:
        world.missing_fj_orgs.add(fj_org)  # created once, before any repo steps
    if fj_repo is None:
        if not on_github:
            p.note("Forgejo-only repo is missing from Forgejo; nothing to import from")
            return p
        how = f"pull mirror of {upstream}" if upstream else \
            f"from GitHub ({'pull mirror' if primary == 'github' else spec.get('import', 'full')})"
        p.step(f"Forgejo: import {fj_org}/{name} {how}",
               lambda: import_repo(fj, name, spec, primary, gh_org, fj_org, gh_repo, cfg))
        fj_repo = {"mirror": primary == "github" or bool(upstream), "archived": False,
                   "private": gh_repo["private"], "_new": True}
    else:
        cur = fj_repo["owner"]["login"]
        if fj_repo["archived"] and (cur.lower() != fj_org.lower() or not archived):
            p.step(f"Forgejo: unarchive {cur}/{name}",
                   lambda c=cur: fj.must("PATCH", f"/repos/{c}/{name}", {"archived": False}))
        if cur.lower() != fj_org.lower():
            p.step(f"Forgejo: transfer {cur}/{name} → {fj_org}/{name}",
                   lambda c=cur: fj.must("POST", f"/repos/{c}/{name}/transfer", {"new_owner": fj_org}))
        if primary == "github" and not fj_repo["mirror"]:
            p.note("should be a pull mirror of GitHub but is a regular repo; recreate it by hand (not automated: needs a delete)")
        if upstream and not fj_repo["mirror"]:
            p.note(f"should be a pull mirror of {upstream} but is a regular repo; recreate it by hand (not automated: needs a delete)")
        if upstream and fj_repo["mirror"] and not same_url(fj_repo.get("original_url", ""), upstream):
            p.note(f"pull mirror points at {fj_repo.get('original_url')}; change it to {upstream} in Settings → Mirror")
        if primary == "forgejo" and not upstream and fj_repo["mirror"]:
            p.note("is a pull mirror but Forgejo should be primary; use Settings → Convert to regular repository")
        if primary == "github" and fj_repo["mirror"] and on_github and not same_url(fj_repo.get("original_url", ""), want_url):
            p.note(f"pull mirror points at {fj_repo.get('original_url')}; change it to {want_url} in Settings → Mirror")
        if on_github and fj_repo["private"] != gh_repo["private"]:
            p.step(f"Forgejo: make {'private' if gh_repo['private'] else 'public'} to match GitHub",
                   lambda: fj.must("PATCH", f"/repos/{fj_org}/{name}", {"private": gh_repo["private"]}))

    # Fork origin note in the description ------------------------------------
    if src := spec.get("forked_from"):
        note_url = src if "://" in src else f"https://github.com/{src}"
        if on_github and needs_fork_note(gh_repo.get("description"), note_url):
            p.step(f"GitHub: note 'forked from {note_url}' in description",
                   lambda: gh("-X", "PATCH", f"repos/{gh_org}/{name}", "-f",
                              f"description={with_fork_note(gh_repo.get('description'), note_url)}"))
        if fj_repo.get("_new") or needs_fork_note(fj_repo.get("description"), note_url):
            p.step(f"Forgejo: note 'forked from {note_url}' in description",
                   lambda: set_fj_fork_note(fj, fj_org, name, note_url))

    # Push mirrors (Forgejo primary) -----------------------------------------
    if on_github and primary == "forgejo":
        mirrors = [] if fj_repo.get("_new") or (fj_repo["mirror"] and not upstream) else \
            fj.push_mirrors(fj_repo["owner"]["login"], name)
        keep = [m for m in mirrors if same_url(m["remote_address"], want_url)]
        stale = [m for m in mirrors if "github.com" in m["remote_address"] and m not in keep]
        if archived and keep and not gh_repo["archived"]:
            p.step("Forgejo: final push-mirror sync before archiving",
                   lambda: fj.sync_push_mirror(fj_org, name, want_url))
        for m in stale + (keep if archived else []):
            p.step(f"Forgejo: remove push mirror → {m['remote_address']}",
                   lambda m=m: fj.must("DELETE", f"/repos/{fj_org}/{name}/push_mirrors/{m['remote_name']}"))
        if not archived and not keep and (upstream or not fj_repo["mirror"]):
            token_file = bucket.get("mirror_token_file")
            if not token_file or not read_token(token_file):
                p.note(f"no GitHub mirror token for bucket {spec['bucket']} ({token_file or 'mirror_token_file unset'}); can't add push mirror")
            else:
                p.step(f"Forgejo: add push mirror → {want_url} (sync on commit + every {cfg['interval']}) and sync",
                       lambda: add_push_mirror(fj, fj_org, name, want_url, read_token(token_file), cfg["interval"], gh_org))

    # Archive state -----------------------------------------------------------
    if on_github and archived and not gh_repo["archived"]:
        p.step(f"GitHub: archive {gh_org}/{name}",
               lambda: gh("-X", "PATCH", f"repos/{gh_org}/{name}", "-F", "archived=true"))
    if archived and not fj_repo["archived"]:
        p.step(f"Forgejo: archive {fj_org}/{name}",
               lambda: fj.must("PATCH", f"/repos/{fj_org}/{name}", {"archived": True}))
    return p


# ── actions ──────────────────────────────────────────────────────────────────
def needs_fork_note(desc, url):
    return url.lower().removesuffix(".git") not in (desc or "").lower()


def with_fork_note(desc, url):
    desc = (desc or "").strip()
    return f"{desc} (forked from {url})" if desc else f"Forked from {url}"


def set_fj_fork_note(fj, org, name, url):
    desc = fj.must("GET", f"/repos/{org}/{name}").get("description")
    if needs_fork_note(desc, url):
        fj.must("PATCH", f"/repos/{org}/{name}", {"description": with_fork_note(desc, url)})


def wait_gh(org, name):
    for _ in range(20):
        if not gh(f"repos/{org}/{name}", check=False).returncode:
            return
        time.sleep(3)
    raise RuntimeError(f"GitHub transfer to {org}/{name} didn't complete within a minute")


def import_repo(fj, name, spec, primary, gh_org, fj_org, gh_repo, cfg):
    url = gh_url(gh_org, name)
    body = {"clone_addr": url, "repo_owner": fj_org, "repo_name": name, "private": gh_repo["private"]}
    if spec.get("upstream"):
        body |= {"clone_addr": spec["upstream"], "service": "git", "mirror": True, "mirror_interval": cfg["interval"]}
    elif primary == "github":
        body |= {"service": "git", "mirror": True, "mirror_interval": cfg["interval"]}
        if gh_repo["private"]:  # stored for future syncs, so use the bucket's narrow token
            token = read_token(cfg["bucket"].get("mirror_token_file", ""))
            if not token:
                raise RuntimeError("private pull mirror needs the bucket's mirror_token_file")
            body |= {"auth_username": "x-access-token", "auth_password": token}
    elif spec.get("import", "full") == "code":
        body |= {"service": "git", "mirror": False, "auth_username": "x-access-token", "auth_password": gh_token()}
    else:
        body |= {"service": "github", "mirror": False, "auth_token": gh_token(),
                 "issues": True, "pull_requests": True, "releases": True,
                 "labels": True, "milestones": True, "wiki": True}
    status, data = fj.call("POST", "/repos/migrate", body, timeout=1800)
    if status == 504:
        raise RuntimeError("import hit the proxy timeout; Forgejo leaves the repo stuck 'migrating'. "
                           "Delete it in Forgejo, set import = \"code\" (or import via the web UI), and re-run")
    if status not in OK:
        raise RuntimeError(f"import failed: HTTP {status}: {data}")


def check_github_not_ahead(fj, org, name, gh_org):
    """Refuse to mirror over GitHub commits that Forgejo doesn't have (pushed after the import)."""
    p = gh("--paginate", f"repos/{gh_org}/{name}/git/matching-refs/", "--jq",
           '.[] | select(.ref | startswith("refs/heads/") or startswith("refs/tags/")) | "\\(.ref) \\(.object.sha)"',
           check=False)
    if p.returncode:
        if "is empty" in p.stdout + p.stderr:  # empty GitHub repo: nothing to lose
            return
        raise RuntimeError(f"couldn't list GitHub refs: {(p.stderr or p.stdout).strip()}")
    out = p.stdout
    missing = [ref for ref, sha in (line.split() for line in out.splitlines())
               if fj.call("GET", f"/repos/{org}/{name}/git/commits/{sha}?stat=false")[0] == 404
               and fj.call("GET", f"/repos/{org}/{name}/git/tags/{sha}")[0] == 404]
    if missing:
        raise RuntimeError(
            f"GitHub has commits Forgejo lacks on {', '.join(missing[:5])}; the first mirror sync would "
            f"overwrite them. Pull them into Forgejo first (fetch from GitHub, push to Forgejo), then re-run")


def add_push_mirror(fj, org, name, url, token, interval, gh_org):
    check_github_not_ahead(fj, org, name, gh_org)
    fj.must("POST", f"/repos/{org}/{name}/push_mirrors", {
        "remote_address": url, "remote_username": "oauth2", "remote_password": token,
        "interval": interval, "sync_on_commit": True})
    fj.sync_push_mirror(org, name, url)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="*", help="limit to these repos")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: plan only)")
    ap.add_argument("--manifest", default=os.path.join(HERE, "manifest.toml"))
    args = ap.parse_args()

    with open(args.manifest, "rb") as f:
        manifest = tomllib.load(f)
    repos = manifest["repos"]
    for name, spec in repos.items():
        if spec.get("bucket") not in manifest["buckets"]:
            sys.exit(f"{name}: unknown bucket {spec.get('bucket')!r}")
    unknown = [r for r in args.repos if r not in repos]
    if unknown:
        sys.exit(f"not in manifest: {', '.join(unknown)}")

    token = os.environ.get("FORGEJO_TOKEN") or read_token("~/.config/forgejo/token")
    if not token:
        sys.exit("no Forgejo token ($FORGEJO_TOKEN or ~/.config/forgejo/token)")
    fj = Forgejo(manifest["forgejo_url"], token)
    world = World(manifest, fj)

    # Every repo in a bucket org must be in the manifest; refuse to touch anything otherwise.
    unaccounted = []
    for host, orgs in (("GitHub", world.gh), ("Forgejo", world.fjo)):
        for org, found in orgs.items():
            for extra in sorted(set(found or {}) - set(repos)):
                hint = " (archived)" if found[extra]["archived"] else ""
                unaccounted.append(f"{host} {org}/{extra}{hint}")
    if unaccounted:
        print("\n".join(["✗ REPOS NOT IN THE MANIFEST — add them to manifest.toml before running:",
                         *(f"    {u}" for u in unaccounted)]), file=sys.stderr)
        sys.exit(2)
    interval = manifest.get("mirror_interval", "8h0m0s")

    plans = []
    for name in args.repos or repos:
        spec = repos[name]
        cfg = {"interval": interval, "bucket": manifest["buckets"][spec["bucket"]]}
        plans.append(plan_repo(name, spec, world, fj, cfg))

    changed = failed = 0
    blocked_orgs = set()
    for org in sorted(world.missing_fj_orgs):
        desc = f"Forgejo: create org {org}"
        if not args.apply:
            print(f"+ {desc}")
            continue
        try:
            fj.must("POST", "/orgs", {"username": org, "visibility": "public"})
            print(f"✓ {desc}")
            changed += 1
        except Exception as e:
            print(f"✗ {desc}\n  {e}")
            failed += 1
            blocked_orgs.add(org)

    for p in plans:
        if blocked_orgs and repos[p.name]["bucket"] in {
                b for b, v in manifest["buckets"].items() if v["forgejo"] in blocked_orgs}:
            print(f"✗ {p.name}: skipped, its Forgejo org couldn't be created")
            continue
        if not p.steps and not p.notes:
            print(f"✓ {p.name}")
            continue
        print(f"• {p.name}")
        for msg in p.notes:
            print(f"    ! {msg}")
        for desc, fn in p.steps:
            if not args.apply:
                print(f"    + {desc}")
                continue
            try:
                fn()
                print(f"    ✓ {desc}", flush=True)
                changed += 1
            except Exception as e:  # stop this repo, carry on with the rest
                print(f"    ✗ {desc}\n      {e}", flush=True)
                failed += 1
                break

    for host, orgs in (("GitHub", world.gh), ("Forgejo", world.fjo)):
        for org, found in orgs.items():
            if found is None:
                print(f"? {host} org {org} doesn't exist")

    pending = sum(len(p.steps) for p in plans) + len(world.missing_fj_orgs)
    if args.apply:
        print(f"\n{changed} change(s) made, {failed} failed.")
    else:
        print(f"\n{pending} change(s) planned. Run with --apply to make them.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
