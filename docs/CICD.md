# CI/CD — GitHub Actions → AWS Lightsail

Push to `main` → CI runs → the backend is deployed to Lightsail and reloaded
under pm2 → `/api/health` is polled until it reports the new commit. If it
doesn't, the previous commit is put back automatically.

```
push to main ──▶ CI (.github/workflows/ci.yml)
                  ├─ backend  : pip install · compileall · import app · paper-trade smoke tests
                  ├─ frontend : npm ci · eslint · vite build
                  └─ guard    : no committed .env · deploy scripts parse · pm2 config valid
                        │  all green
                        ▼
              Deploy (.github/workflows/deploy.yml)
                  ├─ render .env from repo secrets  (never on a command line)
                  ├─ scp deploy/*.sh + .env  →  ~/.trading-deploy/
                  └─ ssh  bash ~/.trading-deploy/deploy.sh <sha>
                        ├─ git fetch + hard checkout of <sha>
                        ├─ backend/.venv  ←  pip install -r requirements.txt
                        ├─ write backend/local_data/RELEASE  (commit + timestamp)
                        ├─ pm2 startOrReload backend/ecosystem.config.js
                        └─ healthcheck.sh <sha>
                              ok   → done
                              fail → dump pm2 logs, redeploy the previous sha, fail the job
```

## One-time setup

### 1. On the server (once)

```bash
ssh ubuntu@<lightsail-ip>
curl -fsSL https://raw.githubusercontent.com/Immanueldavidckp/trading/main/deploy/server-setup.sh | bash
```

It installs `python3-venv`, `pm2` (if missing), registers the pm2 systemd unit
so processes survive a reboot, and creates `~/.trading-deploy/`.

**Python 3.12+ is required** — `requirements.txt` pins `upstox-totp`, which
publishes no wheel for older versions. Ubuntu 24.04 ships 3.12; on 22.04 add
the deadsnakes PPA and install `python3.12-venv`.

### 2. Deploy key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/trading_deploy -N ''
ssh-copy-id -i ~/.ssh/trading_deploy.pub ubuntu@<lightsail-ip>
```

### 3. GitHub secrets — Settings → Secrets and variables → Actions

| Secret | What |
|---|---|
| `SSH_PRIVATE_KEY` | contents of `~/.ssh/trading_deploy` (the private half) |
| `SERVER_HOST` | Lightsail public IP or hostname |
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET` | Upstox app credentials |
| `UPSTOX_USERNAME`, `UPSTOX_PIN_CODE`, `UPSTOX_TOTP_SECRET` | daily autologin |
| `SHOONYA_USER_ID`, `SHOONYA_PASSWORD`, `SHOONYA_VENDOR_CODE`, `SHOONYA_API_KEY`, `SHOONYA_TOTP_SECRET` | Shoonya |
| `GEMINI_API_KEY` | optional, AI analysis |
| `AUTH_SECRET` | session-cookie signing key; set it so logins survive redeploys |

Optional **variables** (not secrets) on the same page:

| Variable | Default | Use |
|---|---|---|
| `MOCK_MODE` | `true` | `false` switches the server to **live money**. Deliberate, and never a code change. |
| `PUBLIC_BASE_URL` | `http://<SERVER_HOST>:8000` | must match the Upstox app's redirect URL, e.g. `http://api.mtandtiot.in:8000` |
| `SERVER_USER` | `ubuntu` | ssh user |
| `SSH_PORT` | `22` | ssh port |
| `SHOONYA_IMEI` | `abc1234` | Shoonya device id |

### 4. Protect the deploy (recommended)

Create a `production` environment (Settings → Environments) and add yourself as
a required reviewer. The deploy job already targets it, so every push to `main`
then waits for your approval before it touches the trading server.

## Everyday use

- **Ship** — merge to `main`. Actions does the rest.
- **Watch** — the run's summary shows the ref, `MOCK_MODE`, and the health result.
- **Roll back / redeploy** — Actions → *Deploy Trading Backend* → **Run
  workflow**, and put a commit SHA (or tag) in the `ref` box. Same path as a
  normal deploy, health check included.
- **Check the live release**

  ```bash
  curl -s http://<host>:8000/api/health
  # {"ok":true,"commit":"<sha>","deployed_at":"…","uptime_s":123.4,"mock_mode":true}
  ```

  `/api/health` is the only route outside the login wall, and it returns no
  account data.

## Notes on the design

- **The server is a deploy target, not a workspace.** `deploy.sh` uses
  `git checkout --force --detach`, so a locally edited tracked file (e.g.
  `mock_portfolio.json` rewritten by a paper trade) can never block a deploy.
  Untracked paths — `backend/.env`, `backend/local_data/`, `backend/.venv/` —
  are never touched, so the SQLite data, watchlist and auth secret survive.
- **Secrets never hit a command line.** The `.env` is rendered on the runner
  from environment variables and `scp`-ed with mode 600, then deleted from the
  staging directory after the deploy. The old workflow interpolated every
  secret into the SSH command, where `ps` on the box could read them.
- **The health check compares commits.** `/api/health` reports the SHA read at
  process start, so a worker that survives a reload and keeps serving the old
  code fails the deploy instead of passing it.
- **pm2 no longer hot-loops.** `ecosystem.config.js` sets `min_uptime`,
  `max_restarts: 10` and exponential backoff — the 89 restarts previously
  visible in `pm2 status` were a crash loop retried flat-out. A broken release
  now fails fast, gets rolled back, and leaves its reason in
  `backend/logs/backend-error.log`.
- **The venv is the interpreter of record.** pm2 launches
  `backend/.venv/bin/python`, the same one the deploy installs requirements
  into, so a dependency bump can't leave the process on stale packages.

## Not deployed by this pipeline

- `frontend/` (Vite/React) is built and linted in CI but not shipped — the live
  UI is the hand-written HTML in `backend/static/`, which is served straight
  from the checkout. Nothing to build.
- `android/` APKs are built by `android/build-apk.sh` on demand.
- The two other pm2 processes on the box (`client_tm`, `server_tm`) belong to a
  different project. This pipeline only touches `trading-backend`.
