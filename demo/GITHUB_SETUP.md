# Private GitHub onboarding: prepared setup

> **Status: nothing has been created, pushed or installed.** No GitHub repository was created,
> no code was pushed, no GitHub App was registered or installed, no permission was changed and no
> token was minted. The only thing that ran is the dry-run plan of
> `demo/scripts/publish_consumer_repos.py`, plus local tests. Everything below that touches GitHub
> is a prepared step for the operating user, to run only after they authorize the destination
> account and the action.

## Read this first

- **Provider.** Stripe is the API provider in this demo. Stripe is not a Shockwave customer, has
  not endorsed Shockwave, and sent no invitation. The demo operator plays the provider role for
  the Stripe API project.
- **Simulated change.** The Stripe change analyzed after onboarding is **simulated**. Stripe has
  not proposed or announced it.
- **Consumers.** The three repositories are team-created demo applications for three fictional
  companies. They are not Stripe customers.
- **Dependencies.** They are a **fixture** until Noirit's scanner exists.
- **Verification.** It runs on the **local runner double**, which runs trusted local fixtures in a
  plain subprocess. It is not a sandbox and not Sai's runner.
- **Analysis.** It uses deterministic rules (no LLM).
- **Stripe provider source discovery.** Unavailable.
- **Single account.** A local simulation on one GitHub account does **not** prove cross-company
  identity isolation. In that setup one account owns all three consumer repositories and both
  private apps (see [Limits](#limits)).

## Files

| File | What it is |
|---|---|
| `demo/github/app-manifest.json` | Manifest for the **Shockwave Demo Reader** app: `metadata: read`, `contents: read`, the `repository` event, private (`public: false`) |
| `demo/github/app-manifest.pr-writer.json` | Manifest for the **Shockwave Demo PR Writer** app: `metadata: read`, `contents: write`, `pull_requests: write`, private |
| `demo/github/onboarding-flow.json` | The onboarding state machine, as data. It covers the steps, actors, owners, GitHub calls, stored data, provider visibility, revocation events and job guards |
| `demo/github/docs-verified.json` | Every GitHub fact used, with its URL, the check date (2026-10-17), the fact itself and a verified/unverified flag |
| `demo/github/register-app.html` | Local helper page that POSTs a pasted manifest to GitHub's manifest flow |
| `demo/scripts/publish_consumer_repos.py` | Creates the three **private** repositories and pushes the prepared snapshots. Dry run by default |
| `tests/demo/test_github_setup.py` | Offline checks of all of the above |

The manifests contain only documented manifest keys, so you can paste them unchanged. Every
`https://shockwave.example.invalid/...` URL is a placeholder (`.invalid` never resolves).

## Four separate authorizations

| Authorization | Where it lives | Who grants it | Grants | Does not grant | Revoked by |
|---|---|---|---|---|---|
| **Sign-in** | Ishan's auth. Sign in with GitHub uses the Reader app's user authorization (web flow, `state` + PKCE S256) | The person | Identity and a session. The GitHub user token is used only for `GET /user` and `GET /user/installations` | Repository access, consent, PR writes | Sign-out, expiry (8 h), or GitHub > Settings > Applications > Authorized GitHub Apps > Revoke (`github_app_authorization` webhook) |
| **Repository access** | GitHub: Reader installed with **Only select repositories**. Shockwave: one RepositoryConnection per repository the user explicitly selects | Account or org owner (install); signed-in user (selection) | Reader installation tokens limited to one private repository, with `contents: read` and `metadata: read` | Analysis, writes, anything visible to the provider | Uninstall, suspend, removing the repository from the installation, deselecting it, or the repository being deleted or transferred |
| **Analysis consent** | Shockwave only (Ishan) | A signed-in consumer org admin, after seeing the exact provider-visible fields | Scan, analysis, verification, migration proposals, and the restricted provider summary | Repository access, PR writes | Withdrawal in Shockwave |
| **PR-write authorization** | Both are required: the PR Writer installed on **only** the authorized repositories, and a per-repository Shockwave PR authorization | A repository admin or owner (install); a signed-in user (record) | Push a **new** branch and open a pull request, with tokens limited to one repository and `{contents: write, pull_requests: write}` | Merging, pushing to the default branch, force-pushing, provider visibility | Uninstalling or suspending the PR Writer, removing the repository from it, or withdrawing the PR authorization; revoking access or consent also revokes it |

Revoking one authorization never silently grants or removes another. Repository access,
analysis consent and PR-write authorization each stop **new processing and publication**. For PR
writes, that means new branches, pushes and pull requests. **Sign-in revocation is different.**
It ends sessions and deletes stored user tokens. It does not stop processing that the
organization has consented to. That policy is for Ishan to confirm; see the coordination notes.

## Why two GitHub Apps

**Decision: a separate PR Writer app, rather than a permission upgrade on the Reader plus a
Shockwave-side per-repository flag.** Each reason below is backed by a verified fact; the IDs
refer to entries in `docs-verified.json`.

1. **GitHub enforces the split.** An installation token "cannot be granted permissions that the
   app was not granted" (`installation-access-token`). So no Reader token can ever write,
   whatever Shockwave's database says.
2. **The installer chooses the write scope in GitHub.** Permissions apply to the whole
   installation, but each installation has its own repository selection: "All repositories" or
   "Only select repositories" (`install-selected-repositories`). The PR Writer is installed only on
   repositories where migration PRs are authorized. A single upgraded app would hold write access
   on every repository selected for analysis.
3. **An upgrade would ask everyone, and change nothing until they approve.** "Each account where
   the app is installed will need to approve the new permissions". Until then the installation
   keeps its old permissions (`permission-change-approval`, `approve-updated-permissions`). Analysis-only
   consumers would get a write request they never asked for.
4. **Revocation is native and observable.** The consumer uninstalls the PR Writer or removes a
   repository from it. That fires the `installation` and `installation_repositories` webhooks,
   which every app receives by default (`webhook-installation`, `webhook-installation-repositories`).
   Read access and analysis are not affected.

**Trade-offs we accept:**

- There are two app registrations: two private keys, two webhook secrets, and two install steps
  for the consumer.
- Installing the PR Writer needs the account owner, an organization owner, or a repository admin.
  A repository admin can install it only for repositories they administer, which works because
  neither app requests organization permissions (`install-selected-repositories`).
- `contents: write` covers every branch of an installed repository. "Never push to the default
  branch, never force-push, never merge" is Shockwave policy. Consumers can add default-branch
  protection, but that was not verified in this session (`default-branch-protection`).

Shockwave still keeps its own per-repository PR authorization and limits every PR token to one
repository. The PR Writer acts only when both GitHub and Shockwave allow it.

**Rejected: one app plus a flag.** It needs only one install and one key, but write capability
would exist on every selected repository. The restriction to particular repositories would live
only in Shockwave. Narrowing tokens with `repository_ids` and `permissions` helps, but Shockwave
alone enforces it.

## Step-by-step setup

### 0. Before anything touches GitHub

The operating user decides the destination account (a personal account or an organization) and
explicitly authorizes three actions:

- creating the three private repositories;
- pushing the prepared snapshots;
- registering and installing the apps.

Agents never do any of this.

### 1. Prepare the local snapshots (local only)

```sh
.venv/bin/python demo/scripts/demo.py prepare
```

This builds `demo/.work/repos/<repository>/`. Each one is a `git init` with one commit, a fixed
identity and fixed dates, no remote and no `node_modules`. `demo.py` belongs to the
demo-environment work, not to this setup.

### 2. Review the dry-run plan (no network)

```sh
.venv/bin/python demo/scripts/publish_consumer_repos.py --owner <login>                 # personal account
.venv/bin/python demo/scripts/publish_consumer_repos.py --owner <org> --owner-type org  # organization
.venv/bin/python demo/scripts/publish_consumer_repos.py --owner <login> --json          # machine-readable
```

The plan prints the owner, each repository (all `private`), the exact REST calls with bodies, and
the git commands. It ends with the manual browser steps. In dry-run mode the script makes no
network call, starts no subprocess, writes nothing and never reads the token value.

### 3. Publish the private repositories (operator only)

```sh
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
.venv/bin/python demo/scripts/publish_consumer_repos.py --owner <login> --execute --i-authorize-github-changes
unset GITHUB_TOKEN
```

**Execution requirements.** The script needs all of the following and refuses otherwise (exit 2):

- `--owner`, `--execute` and `--i-authorize-github-changes`;
- a token in `GITHUB_TOKEN`;
- prepared snapshots;
- visibility `private`;
- commits that contain no `node_modules`, caches, `.env` files, keys or databases.

**What it does, in order:**

1. `GET /user`. On a personal account, the token's login must equal `--owner`.
2. `GET /repos/{owner}/{name}` for all three repositories before creating anything. A `200` or
   `301` (the repository exists) refuses the whole run.
3. Per repository:
   1. `POST /user/repos`, or `POST /orgs/{org}/repos` for an organization, with
      `"private": true`. Organizations also get `"visibility": "private"`.
   2. A check that GitHub reports `private: true` before anything is pushed.
   3. `git push` of the snapshot commit to `refs/heads/main`.
   4. `GET /repos/{owner}/{name}/commits/main` to confirm that `main` points at that commit.

Every request carries `X-GitHub-Api-Version: 2026-03-10`.

**How the token is handled.** It is never printed and never placed on a command line. git gets it
from a credential helper that reads the environment at run time:
`-c credential.helper= -c 'credential.helper=!f() { ... }; f'`. The empty first entry resets any
configured helper, such as the macOS keychain, so nothing stores the token. git also runs with
`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_TERMINAL_PROMPT=0`, with hooks
disabled, and without `--force`. Redirects are never followed with the token.

**git must start natively.** On this machine the venv's Python could not start
`/usr/local/bin/git` ("Bad CPU type in executable"), while `/usr/bin/git` works. If the script
refuses with `cannot run git`, run it with `PATH=/usr/bin:$PATH`. The script refuses before any
GitHub call, because its local commit check runs git first.

**Token requirements.** The token must be able to create private repositories and push to them:

- A classic token or an OAuth token needs the `repo` scope (`create-repository-user`).
- For a fine-grained token, repository creation is listed under "Administration" or "Repository
  creation" write (`permissions-required-fine-grained-pat`). Pushing needs "Contents" write on
  the new repositories; this was not verified in this session for git over HTTPS with
  fine-grained tokens.

**Push mechanics, verified locally on 2026-10-17.** The exact push argv and environment were run
against a local `git http-backend` behind Basic auth, using a fake token. Results:

- The first request got a 401. The helper then supplied `login:token`, and the push created
  `main` at the snapshot commit.
- The token never appeared in the argv.
- A later non-fast-forward push was rejected.

This probe was not run against GitHub.

### 4. Register the Reader app from its manifest (browser)

1. Register under the account that will own the three repositories. A private app "can only be
   installed on the account that owns the app" (`private-vs-public-app`).
2. Optional: replace `https://shockwave.example.invalid` in the manifest with your Shockwave base
   URL (`SHOCKWAVE_PUBLIC_BASE_URL`). With the placeholders, GitHub cannot deliver webhooks or
   redirects.
3. Open `demo/github/register-app.html` in a browser. Choose the personal account or an
   organization, paste `demo/github/app-manifest.json`, and press **Continue to GitHub**.
   - The page checks the pasted manifest. It must use only documented keys, have
     `public: false`, a name of 34 characters or fewer, and exactly the Reader or PR Writer
     permission set.
   - It then POSTs the manifest to `https://github.com/settings/apps/new` (or
     `.../organizations/<org>/settings/apps/new`) with a random `state` (`manifest-flow`).
4. On GitHub, check the permissions (**Metadata: read, Contents: read**) and the `repository`
   event, then click **Create GitHub App**. App names must be unique across GitHub; if the name
   is taken, add a suffix.

### 5. Exchange the code and keep the secrets out of the repository

GitHub redirects to the manifest's `redirect_url` with `?code=...&state=...`. With the
placeholder URL the page does not load, but the address bar shows the code. Check that `state`
matches what the helper page displayed.

Within **one hour**, exchange the code with `POST https://api.github.com/app-manifests/{code}/conversions`
(`manifest-code-exchange`, `manifest-conversion-response`). The response contains `id`,
`client_id`, `client_secret`, `pem` and `webhook_secret`. Write it straight to a protected file
outside the repository, never to the terminal:

```sh
umask 077 && mkdir -p ~/.shockwave-secrets
curl -sS -X POST -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/app-manifests/<code>/conversions" -o ~/.shockwave-secrets/reader-app.json
```

Load the values into Shockwave's secret store, then delete the file. Never commit or paste the
values. In production, Ishan's backend performs this exchange at `redirect_url`.

### 6. Install the Reader on only the selected repositories (browser)

1. Open `https://github.com/apps/<reader-app-slug>/installations/new` and pick the account.
2. Choose **Only select repositories** and select exactly `shockwave-stripe-checkout`,
   `shockwave-stripe-fraud` and `shockwave-stripe-reporting`. Never choose "All repositories"
   (`install-selected-repositories`).
3. Check the installation:
   - personal account: Settings > Applications > Installed GitHub Apps > Configure;
   - organization: Settings > Third-party Access > GitHub Apps > Configure.

   Repository access must list only those repositories (`installed-app-management`).

In the full product, the setup URL then sends the user back to Shockwave. There the user
explicitly selects private repositories (step 4 of the flow) and grants analysis consent (step 5).

### 7. PR Writer: only when a consumer authorizes migration PRs

This step is not needed for the analysis demo.

1. Register `demo/github/app-manifest.pr-writer.json` the same way.
2. Install it with **Only select repositories** on just the repositories authorized for migration
   PRs. That is none by default.

### 8. Configure Shockwave (names only; values live in a secret store)

| Variable | Owner | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | operator | The publish script's `--execute` only; never stored |
| `SHOCKWAVE_PUBLIC_BASE_URL` | Ishan | Base URL that replaces `https://shockwave.example.invalid` in the callback, setup and webhook URLs |
| `SHOCKWAVE_GITHUB_API_VERSION` | Sai | `2026-03-10` |
| `SHOCKWAVE_GITHUB_READER_APP_ID` | Sai | Reader app ID |
| `SHOCKWAVE_GITHUB_READER_CLIENT_ID` | Ishan | Sign-in web flow; also the recommended JWT issuer for Sai |
| `SHOCKWAVE_GITHUB_READER_CLIENT_SECRET` | Ishan | Sign-in code exchange, server side only |
| `SHOCKWAVE_GITHUB_READER_PRIVATE_KEY_PATH` | Sai | Private key file for Reader JWTs, outside the repository |
| `SHOCKWAVE_GITHUB_READER_WEBHOOK_SECRET` | Ishan | `X-Hub-Signature-256` validation for Reader webhooks |
| `SHOCKWAVE_GITHUB_READER_APP_SLUG` | Ishan | Install link |
| `SHOCKWAVE_GITHUB_PR_WRITER_APP_ID` | Sai | PR Writer app ID |
| `SHOCKWAVE_GITHUB_PR_WRITER_CLIENT_ID` | Sai | PR Writer JWT issuer |
| `SHOCKWAVE_GITHUB_PR_WRITER_PRIVATE_KEY_PATH` | Sai | Private key file for PR Writer JWTs, outside the repository |
| `SHOCKWAVE_GITHUB_PR_WRITER_WEBHOOK_SECRET` | Ishan | `X-Hub-Signature-256` validation for PR Writer webhooks |
| `SHOCKWAVE_GITHUB_PR_WRITER_APP_SLUG` | Ishan | Install link |

The PR Writer has no sign-in callback, so its client secret is not used.

## Onboarding flow

The flow is defined in `demo/github/onboarding-flow.json`, which is the source of truth. Each
step's `github` entries reference verified `docs-verified.json` IDs.

| # | Step | Actor | Owner | GitHub API or webhook | Provider sees |
|---|---|---|---|---|---|
| 1 | Provider invitation | Provider admin (the operator, for the Stripe API project) | Ishan | none | invitation status, consumer display name |
| 2 | Authenticated consumer acceptance | Signed-in consumer user | Ishan | `https://github.com/login/oauth/authorize` (state, PKCE S256), `POST https://github.com/login/oauth/access_token`, `GET /user` | + enrollment status |
| 3 | GitHub App installation / selection | Consumer account or org owner | Sai (+ Ishan) | `/apps/<slug>/installations/new` with **Only select repositories**; setup URL `installation_id` is **untrusted** and checked with `GET /user/installations`; `installation.created` webhook | enrollment status |
| 4 | Explicit private-repository selection | Signed-in consumer user | Ishan (+ Sai) | `GET /user/installations/{id}/repositories`, `GET /installation/repositories`, `GET /repos/{owner}/{repo}` (must be `private`). Nothing is pre-selected; repositories added later are never auto-selected | enrollment status |
| 5 | Analysis consent | Consumer org admin | Ishan | none (Shockwave record) | consent status |
| 6 | Pinned snapshot fetch | Shockwave worker | Sai | `POST /app/installations/{id}/access_tokens` limited to one repository with `{contents: read, metadata: read}`; `GET /repos/{o}/{r}/commits/{ref}` gives the SHA; `GET /repos/{o}/{r}/tarball/{sha}`, or a git fetch with the installation token; `DELETE /installation/token` | nothing |
| 7 | Scan | Shockwave worker | Noirit | none (stored snapshot, no network) | nothing |
| 8 | Private consumer findings | Worker produces; consumer members view | Kanishk (+ Sai, Ishan) | none | nothing |
| 9 | Restricted provider summary | Shockwave publishes; provider views | Ishan (+ Kanishk) | none | allowlisted summary fields only |

**What is real in this demo.**

- Steps 1 to 5 are not executed.
- Step 6 is local only: `demo.py prepare` builds the snapshots.
- Step 7 is a **fixture** until Noirit's scanner exists.
- Step 8 uses deterministic rules (no LLM) and the **local runner double**.
- Step 9 is dashboard-compatible JSON produced locally; Ishan's dashboard is not integrated.

**Optional PR branch.** After findings, `pr_write_authorization` installs the PR Writer and
records a Shockwave authorization. `migration_pr_publication` then mints a token limited to one
repository with `{contents: write, pull_requests: write}`. It pushes a **new** branch, never the
default branch and never with `--force`, and calls `POST /repos/{owner}/{repo}/pulls`. Shockwave
never merges.

**Job guards.** A queued job re-checks authorization immediately before it fetches, scans,
publishes consumer findings, publishes the provider summary, pushes a PR branch or opens a PR.
The check at enqueue time is not enough. The guards ask GitHub (token minting) and Shockwave's
records directly, so lost or late webhooks cannot leak work through. A failed check cancels the
job without side effects.

## Provider visibility

The rule is default deny. The provider sees only an allowlist projection, built per consumer
organization and never per repository:

- invitation, enrollment and consent status;
- the consumer's display name;
- the provider's own change ID, endpoint and field;
- impact status and maximum severity;
- risk counts by category;
- verification-status counts;
- migration readiness;
- labels such as simulated, rules (no LLM), local runner double and fixture.

**Never visible to the provider:**

- consumer source;
- private paths;
- code snippets;
- logs;
- patch artifacts;
- repository names and URLs;
- commit SHAs;
- dependency evidence;
- test names and assertion output;
- pull request links;
- installation and account IDs;
- consumer user identities;
- snapshot archives.

After a revocation, the provider sees only `access_revoked` or `consent_withdrawn`.

## Testing revocation

These tests need a reachable webhook URL, so replace the placeholders first. Without webhooks,
the same outcomes show up on the next job because the guards ask GitHub directly. Every action
below changes GitHub state, so run them only when authorized.

| Test | Do this | Expected GitHub signal | Expected Shockwave behavior |
|---|---|---|---|
| Remove one repository | Reader > Configure > deselect `shockwave-stripe-fraud` > Save | `installation_repositories` / `removed` | Re-list `GET /installation/repositories` (the removed list is empty when switching all to selected). The connection becomes `access_revoked`, queued jobs for it cancel, snapshots are deleted, and the provider summary shows `access_revoked` only |
| Suspend | Reader > Configure > Suspend (then Unsuspend) | `installation` / `suspend` (`unsuspend`) | The enrollment is `paused`. Fetch, scan and publication stop. After unsuspend, work resumes only when a re-check passes; nothing is backfilled |
| Uninstall | Reader > Configure > Uninstall | `installation` / `deleted` | All of the installation's connections become `access_revoked`, and token minting for them fails (`installation-token-git-https`) |
| Revoke sign-in | Settings > Applications > Authorized GitHub Apps > Revoke | `github_app_authorization` (revoked) | Sessions end and user tokens are deleted. Consent and installations are unchanged |
| Withdraw consent | Shockwave consent screen (Ishan) | none (Shockwave event) | Processing and publication stop at once, and the provider summary shows `consent_withdrawn` |
| Revoke PR writes | Uninstall the PR Writer, remove a repository from it, or withdraw the PR authorization | PR Writer `installation` / `deleted` or `installation_repositories` / `removed`; or none | Queued PR jobs cancel. No new branch, push or PR. Existing PRs are left to the consumer |
| Repository made public | Repository settings > change visibility | `repository` / `publicized` | The connection is `paused` until the consumer re-confirms selection and consent |
| Queued job | Enqueue a fetch, revoke before it runs | the event above | The job fails its pre-fetch re-check: no token, no fetch, no publication, and the reason is recorded |

From the app side, `DELETE /app/installations/{id}` returns 202 and
`PUT /app/installations/{id}/suspended` returns 204 (`installation-delete-suspend`). Both
simulate the same events with the app JWT, and both are GitHub changes too.

## Coordination notes for Sai

GitHub transport, runner, migrations and PRs.

**App JWT.**

- `iss` is the client ID (recommended).
- `iat` is now minus 60 s; `exp` is at most 10 minutes later.
- Sign with RS256 and send it as `Authorization: Bearer` (`app-jwt`).

**Snapshot tokens.**

- Mint one Reader token per job with `repository_ids=[<repo id>]` and
  `permissions={"contents": "read", "metadata": "read"}` (`installation-access-token`).
- Tokens expire after 1 hour. Do not assume they are 40 characters long; a new token format is
  rolling out.
- Revoke each token with `DELETE /installation/token` after use.

**Pinning the snapshot.**

- Resolve the ref first: `GET /repos/{o}/{r}/commits/{ref}` gives the SHA (`get-commit-ref`).
- Then download `GET /repos/{o}/{r}/tarball/{sha}`. Follow the 302; for private repositories the
  link expires after five minutes (`tarball-snapshot`). Do not forward `Authorization` to the
  redirect host.
- The docs leave the tarball `ref` description blank (`tarball-ref-commit-sha`, unverified). So
  confirm that the archive matches the SHA, or `git fetch` the SHA over HTTPS with the
  installation token as the password (`installation-token-git-https`).
- Supply the token through a credential helper, as `publish_consumer_repos.py` does, not in the
  URL: a URL would put it in argv and possibly in `.git/config`.
- Store `RepositorySnapshot{commit_sha, fetched_at, content_hash}` (contract).

**Webhook adapter.**

- Verify `X-Hub-Signature-256` with a constant-time compare (`webhook-signature`).
- Deduplicate deliveries by `X-GitHub-Delivery` (`webhook-delivery-headers`).
- On `installation_repositories`, re-list the installation's repositories instead of trusting the
  delta (`webhook-repository-object-fields`, unverified).
- Keep separate endpoints and secrets per app:
  - `/github/webhooks/reader`
  - `/github/webhooks/pr-writer`

**PR publication.**

- Use only PR Writer tokens, limited to `repository_ids=[repo]` and `{contents: write, pull_requests: write}`.
- Push a **new** branch, for example `shockwave/migration/<scenario-id>`. Never push to the
  default branch and never force-push.
- Open the PR with `POST /repos/{o}/{r}/pulls` (`create-pull-request`); `base` is the default
  branch.
- Copy `interpret.PINNED_PATCH_CAVEAT` and the simulated-change label into the PR body.
- Keep the analyzer HANDOFF allowlist: patches never touch config files or `.github/workflows`.
  Editing workflows needs the Workflows permission, which the PR Writer does not have
  (`minimum-permissions`).
- Draft PRs in private repositories depend on the GitHub plan.

**API version and breaking changes.**

- Send `X-GitHub-Api-Version: 2026-03-10`.
- In that version, PR responses no longer include `merge_commit_sha`.
- Uninstalling returns 202.
- Repository creation returns 451 when trade controls block it
  (`rest-breaking-changes-2026-03-10`).

**Run the job guards** before each fetch, scan and publication.

## Coordination notes for Ishan

Auth, invitations, consent, backend and dashboards.

**Sign-in.**

- Use the web application flow with `state` and PKCE (`code_challenge_method=S256`). Abort on a
  `state` mismatch.
- Exchange the code server side.
- `GET /user` gives the identity (`user-access-token-web-flow`, `login-with-github-tutorial`).
- User tokens expire after 8 hours. Store them encrypted, or not at all.
- The Reader is installed with `request_oauth_on_install: false` so that sign-in and installation
  stay separate steps. With OAuth-on-install, the setup URL cannot be used
  (`registration-options`).

**Installation binding.**

- The setup URL's `installation_id` can be spoofed (`setup-url-spoofing`). Bind an installation
  to an enrollment only if `GET /user/installations` with the signed-in user's token lists it.
- PR Writer binding rule (a design decision): it must match a PR-marked repository through
  `GET /repos/{owner}/{repo}/installation` with the app JWT, the same account as the verified
  Reader installation, and the signature-verified webhook `sender` (see
  `apps.binding_checks.pr_writer` in the flow file).

**Repository selection.**

- Selectable repositories are the intersection of the user's and the installation's lists, and
  must be `private: true`.
- The user ticks each one. There is no select-all.
- `installation_repositories` / `added` never selects or consents automatically.

**Records.** These are proposals, not shared-schema changes; the schema decision is Ishan's:

- Invitation, Enrollment, GitHubInstallation, RepositoryConnection, AnalysisConsent,
  PullRequestAuthorization and ProviderSummary;
- the exact fields are in `onboarding-flow.json`, under `steps[].stores` and
  `authorizations.*.stored_as`.

**Consent.**

- The consent screen lists the selected repositories, the purposes and the exact
  provider-visible fields.
- `allow_model_processing` defaults to false; this demo is rules-only.

**Provider summary.** Build it as an allowlist projection, aggregated per consumer
organization. Never include any `never_visible` item. Revoked or withdrawn entries show only
their status.

**Policy to confirm.** Sign-in revocation ends sessions but does not stop consented processing.
The conservative alternative is to pause the enrollment until an admin re-confirms.

## Verified and unverified facts

All GitHub details were checked on **2026-10-17** against docs.github.com, and against
git-scm.com for the git credential mechanics. The source is `demo/github/docs-verified.json`:
each entry has a URL, `checked_at`, the fact used and a `verified` flag. Nothing was executed
against GitHub, so the facts come from the documentation only.

These facts are **unverified**, and the design does not depend on them:

| Unverified fact | How the design avoids depending on it |
|---|---|
| Whether Metadata read is automatic | Both manifests request `metadata: read` explicitly |
| Any setup URL parameter other than `installation_id` | Not used |
| Fields of repository objects in `installation_repositories` payloads | Handlers re-list the repositories instead |
| Whether `default_events` may list the events delivered by default | The manifests do not list them |
| Whether the tarball `ref` accepts a full commit SHA | Sai checks the archive or uses `git fetch` |
| Restricting an app with `contents: write` to non-default branches | Shockwave policy, plus optional consumer branch protection |

## Limits

- **Single-account simulation.** With `public: false`, each app "can only be installed on the
  account that owns the app". The local demo therefore puts all three fictional consumer
  companies' repositories under one GitHub account. That shows the flow and the provider-view
  restrictions, but it **does not prove cross-company identity isolation**. Proving that would
  take:
  - public apps (`public: true`), which is a separate, authorized change;
  - three independent GitHub accounts or organizations, each signing in, installing and
    consenting on its own;
  - tests by Ishan showing that no consumer or provider can read another tenant's records.
- **Placeholder URLs.** Until `https://shockwave.example.invalid` is replaced, no webhook,
  redirect or callback reaches Shockwave, so revocation is observed only by job guards.
- **No live GitHub interaction.** No repository, app, installation, token or webhook delivery has
  been exercised against GitHub.

**Nothing has been created, pushed or installed.**
