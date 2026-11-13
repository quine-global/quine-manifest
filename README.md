# quine-manifest

Declares where every Quine repo lives (this repo: forge.in.quineglobal.com/quine-global/quine-manifest) on GitHub and on Forgejo
(forge.in.quineglobal.com), and a script that makes both match.

| Bucket   | GitHub org              | Forgejo org             | Notes                         |
|----------|-------------------------|-------------------------|-------------------------------|
| main     | `quine-global`          | `quine-global`          |                               |
| labs     | `quine-global-labs`     | `quine-global-labs`     |                               |
| boneyard | `quine-global-boneyard` | `quine-global-boneyard` | retired repos (not archived)  |

By default Forgejo is primary and push-mirrors to GitHub (`primary = "forgejo"`).
With `primary = "github"`, Forgejo holds a pull mirror instead.

## Use

Needs Python 3.11+ and a logged-in `gh`.

```sh
python3.14 quine-repos.py                 # plan: show drift, change nothing
python3.14 quine-repos.py --apply         # make it so
python3.14 quine-repos.py --apply ncdu    # only some repos
```

To move a repo, change its `bucket` and apply: it's transferred on both sides,
and its push mirror is repointed.

## Credentials

**From Forgejo Actions** (`.forgejo/workflows/apply.yml`): a push to `main`
that changes the manifest shows the plan; run "Apply manifest" by hand to apply
it. Its secrets, on this repo: `QUINE_REPOS_FORGEJO_TOKEN` (a Forgejo token of
an owner of every bucket org, with write:repository and write:organization),
`QUINE_REPOS_GH_TOKEN` (a classic GitHub token with `repo` and `read:org`, since
it spans orgs), and `MIRROR_TOKEN_MAIN`, `MIRROR_TOKEN_LABS`,
`MIRROR_TOKEN_BONEYARD` (the buckets' mirror tokens, below).

**Run by hand:**

- Forgejo admin token: `$FORGEJO_TOKEN` or `~/.config/forgejo/token`.
- GitHub admin actions (transfer, archive) and imports: your `gh` login.
- Push mirrors: each bucket's `mirror_token_file`, a GitHub fine-grained token
  whose resource owner is that bucket's GitHub org (Contents + Workflows
  read/write). Fine-grained tokens cover one org, so each bucket needs its own.
  The `main` token expires 2027-10-12.

Every repo in a bucket org (on either host) must be listed in `manifest.toml`;
the script exits with an error before doing anything if it finds one that isn't.

Upstream mirrors (`upstream = …`) have Forgejo Actions off unless they say
`actions = true`: they carry upstream's own CI, which would sit "waiting"
forever for runners we don't have.

## Renovate

[Renovate](https://docs.renovatebot.com/) opens dependency update PRs on the
forge (it runs on the forge VM: petersweb-infra's `nixos/renovate.nix` and
`docs/RENOVATE.md`). For every repo it covers, `--apply` gives the forge's
`renovate` account write access and adds the webhook that wakes it
(`[renovate]` in `manifest.toml`). Renovate then finds the repo itself and
opens a "Configure Renovate" PR adding a `renovate.json` that extends
[`renovate/default.json`](renovate/default.json), the shared defaults: the
schedule (Monday mornings), Forgejo workflows, and leaving our own forge
images to their deploy workflows. Nothing else happens in a repo until that
PR is merged; put anything repo-specific in its own `renovate.json`.

It covers our own repos in the main and labs buckets by default, not forks
(`forked_from`: their dependencies come with upstream merges) or mirrors. A
repo's `renovate = true` or `false` overrides that. Turning it off is only
reported: remove the collaborator and webhook by hand.

## What it won't do

- Delete repositories. A repo that's the wrong kind (a regular repo that should
  be a pull mirror) is reported for you to fix by hand.
- Create GitHub orgs (GitHub has no API for that).
- Full imports that outlast nginx's 60 s proxy timeout leave the repo stuck
  "migrating" — use `import = "code"` for repos with big release assets.
  Don't click "Cancel migration" on a stuck repo: it deletes the repo.
