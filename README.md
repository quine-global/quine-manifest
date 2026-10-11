# quine-manifest

Declares where every Quine repo lives on GitHub and on Forgejo
(forge.quinefoundation.com), and a script that makes both match.

| Bucket   | GitHub org              | Forgejo org             | Notes                         |
|----------|-------------------------|-------------------------|-------------------------------|
| main     | `quine-global`          | `Quine`                 |                               |
| labs     | `quine-global-labs`     | `quine-global-labs`     |                               |
| boneyard | `quine-global-boneyard` | `quine-global-boneyard` | archived on both sides        |

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
its push mirror is repointed, and moving into the boneyard archives it.

## Credentials

- Forgejo admin token: `$FORGEJO_TOKEN` or `~/.config/forgejo/token`.
- GitHub admin actions (transfer, archive) and imports: your `gh` login.
- Push mirrors: each bucket's `mirror_token_file`, a GitHub fine-grained token
  whose resource owner is that bucket's GitHub org (Contents + Workflows
  read/write). Fine-grained tokens cover one org, so each bucket needs its own.
  The `main` token expires 2027-10-12.

## What it won't do

- Delete repositories. A repo that's the wrong kind (a regular repo that should
  be a pull mirror) is reported for you to fix by hand.
- Create GitHub orgs (GitHub has no API for that).
- Full imports that outlast nginx's 60 s proxy timeout leave the repo stuck
  "migrating" — use `import = "code"` for repos with big release assets.
  Don't click "Cancel migration" on a stuck repo: it deletes the repo.
