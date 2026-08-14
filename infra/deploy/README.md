# Deploying a change

Three ways in, one script underneath. Everything routes through
`infra/deploy/deploy.sh` on the server, so a deploy never depends on GitHub being
reachable — if Actions is down, or the workflow is misconfigured, you can still ship by
running one command over SSH.

```
git push  ──►  CI (lint, tests, layering, secrets)  ──►  Deploy workflow  ──┐
                                                                            │
             workflow_dispatch (a tag, or a manual re-run)  ────────────────┼──►  deploy.sh
                                                                            │      on the
             ssh server && ./infra/deploy/deploy.sh  ──────────────────────┘       server
```

---

## What a deploy does

1. **Refuses a dirty working tree.** Uncommitted changes on the server mean somebody
   edited it by hand. Building over that would either discard their work or bake an image
   from a state that exists in no commit.
2. **Shows the commits being deployed**, and flags separately if the release adds
   migrations.
3. **Tags the running images `:rollback`** — before touching git, so a build failure
   leaves the images and the working tree consistent with each other.
4. **Fast-forwards** to the target (or checks it out detached if it is a tag).
5. **Builds.** A build failure stops here, and the old stack is still serving — nothing
   was replaced.
6. **Applies migrations**, only if the release actually added any.
7. **Starts the new containers.**
8. **Waits for health** — every container healthy *and* the site answering HTTP 200
   through Caddy. `docker compose up -d` exiting 0 only means Docker accepted the
   containers; it says nothing about whether the app works.
9. **Rolls back automatically** if step 8 does not pass within the timeout: retags the
   previous images, returns git to the old commit, restarts, and prints the API and web
   logs that explain why.

```bash
./infra/deploy/deploy.sh --check      # what would change, no changes made
./infra/deploy/deploy.sh              # deploy branch tip
./infra/deploy/deploy.sh --ref v1.2.0 # deploy a tag
./infra/deploy/deploy.sh --rollback   # go back to the previous images
```

A record of every deploy lands in `/var/log/jobplatform-deploy.log`.

---

## The limit worth knowing before you need it

**Rollback restores containers. It does not restore the database.**

Migrations are one-way here by policy. Verifying that a migration reverses by running it
against a populated production database once dropped two columns and wiped 182,766
classifications (`docs/06-role-filtering.md` §6.5). So the script never attempts a
downgrade, and says so in its output when a release it is rolling back had applied one.

A release whose *code* is wrong: `--rollback` fixes it in under a minute.
A release whose *migration* is wrong: you need the backup. Which is the real argument for
having one — see `docs/07-deployment.md` §5.

Verify reversibility on a throwaway database, never on production.

---

## Enabling the GitHub Actions path

Add these under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `SSH_HOST` | the server's public IP |
| `SSH_USER` | `ubuntu` on Oracle's images |
| `SSH_PRIVATE_KEY` | the private half of the key the instance was created with |
| `SSH_PORT` | optional, defaults to 22 |

Optionally add a repository **variable** `SITE_URL` so the deployment shows a link.

The workflow triggers on **CI completing successfully** on the default branch, not on a
bare push — a commit that fails lint, tests, the layering guard or the secret scan never
reaches the server. `concurrency` allows one deploy at a time, because two overlapping
runs would race over the same working tree and leave the `:rollback` tags pointing at the
wrong release.

**A note on the deploy key.** It is a real credential for your server, held by GitHub. If
that trade is not one you want, drop `deploy.yml` and deploy over SSH by hand — the script
is the same either way, and nothing else in the repo depends on the workflow existing.
