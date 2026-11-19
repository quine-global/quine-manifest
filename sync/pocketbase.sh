#!/usr/bin/env bash
# Pulls quine-global/pocketbase's three upstreams into it (README, "Syncing
# forks"): each mirrored onto its own branch, then all merged into `quine`, our
# branch, with the admin UI rebuilt into ui/dist when its source changed
# (ui/embed.go embeds it into everything that imports PocketBase).
#
#   upstream   ganigeorgiev.com/git/pocketbase/pocketbase master: PocketBase
#              itself (github.com/pocketbase/pocketbase is archived), and its tags
#   thommorais github.com/thommorais/pocketbase sync: a mirror of it, plus CI
#   felix      github.com/felix-schindler/pocketbase master: OAuth sign-in
#              for superusers
#
# A merge that conflicts stops here, with nothing pushed: resolve it by hand
# in a clone, push `quine`, and the next run carries on.
#
# FORK_URL: where to push (default: the forge over HTTPS with
# $FORGEJO_TOKEN). DRY_RUN=1: do everything but push.
set -euo pipefail

fork=${FORK_URL:-https://token:${FORGEJO_TOKEN:?FORGEJO_TOKEN or FORK_URL}@forge.in.quineglobal.com/quine-global/pocketbase.git}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

git init -q pocketbase && cd pocketbase
git config user.name "Quine fork sync"
git config user.email "fork-sync@quineglobal.invalid"
git remote add fork "$fork"
git remote add upstream https://ganigeorgiev.com/git/pocketbase/pocketbase
git remote add thommorais https://github.com/thommorais/pocketbase.git
git remote add felix https://github.com/felix-schindler/pocketbase.git

git fetch -q fork quine
git fetch -q --tags upstream master
git fetch -q --no-tags thommorais sync
git fetch -q --no-tags felix master
git checkout -q -B quine fork/quine
before=$(git rev-parse HEAD)

for ref in upstream/master thommorais/sync felix/master; do
  if ! git merge -q --no-edit -m "Merge $ref" "$ref"; then
    git merge --abort || true
    echo "::error::$ref conflicts with quine: merge it by hand, push quine, and the next run carries on" >&2
    exit 1
  fi
done

# The committed admin UI, rebuilt when its source changed in these merges.
if ! git diff --quiet "$before" HEAD -- ui/src ui/package.json ui/package-lock.json ui/index.html ui/vite.config.js 2>/dev/null; then
  echo "ui/ changed: rebuilding ui/dist"
  (cd ui && npm ci --no-audit --no-fund --loglevel=error && npm run build)
  rm -rf ui/node_modules
  git add -A ui/dist
  git diff --cached --quiet || git commit -q -m "ui/dist: rebuilt after merging the upstreams"
fi

git log --oneline "$before"..HEAD | head -20
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "DRY_RUN: not pushing ($(git rev-list --count "$before"..HEAD) new commits on quine)"
  exit 0
fi
git push -q --force fork upstream/master:refs/heads/upstream thommorais/sync:refs/heads/thommorais felix/master:refs/heads/felix
git push -q fork --tags
git push -q fork quine
echo "pushed: upstream, thommorais, felix, the tags, and quine at $(git rev-parse --short HEAD)"
