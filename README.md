# Voting App

A containerized, multi-service voting application ("Lemon Vote") with a full
CI/CD pipeline: every push to `main` is tested, built into Docker images, and
automatically deployed to AWS EC2.

**Live demo:** `http://<your-ec2-ip>:5000`

---

## Architecture

Four services, each in its own container:

| Service    | Tech              | Role                                               |
|------------|-------------------|-----------------------------------------------------|
| `web`      | Flask (Python)    | Login, registration, voting UI, admin results page  |
| `redis`    | Redis             | Queue — `web` pushes votes, `worker` consumes them   |
| `worker`   | Node.js           | Pulls votes off the queue, writes them to Postgres   |
| `postgres` | PostgreSQL        | Persistent storage for users, options, and votes     |

**Vote flow:** user submits a vote → Flask marks them as `has_voted` and
pushes a job (`{user_id, option_id}`) onto a Redis list called
`vote_queue` → the worker blocks on that list, validates the job, and
inserts the `Vote` row into Postgres → the admin `/results` page reads
directly from Postgres.

---

## Repo structure

```
voting-app/
├── app.py                      Flask app (routes, models, auth)
├── requirements.txt            Production Python dependencies
├── requirements-dev.txt        + pytest, fakeredis (for testing only)
├── templates/                  Jinja2 HTML templates
├── tests/
│   └── test_app.py             Flask test suite (pytest)
├── worker/
│   ├── worker.js               Node worker (Redis → Postgres)
│   ├── worker.test.js          Worker unit tests (node:test)
│   ├── package.json
│   └── Dockerfile
├── Dockerfile                  Flask image
├── docker-compose.yml          Full stack, built from source (local dev)
├── docker-compose.dev.yml      Redis + Postgres only (for running Flask/worker outside Docker)
├── docker-compose.prod.yml     Full stack, pulling prebuilt images from GHCR (EC2 deployment)
├── .env.example                Template for required environment variables
└── .github/workflows/ci.yml    CI/CD pipeline (test, build, push, deploy)
```

---

## Local setup (running everything in Docker)

Prerequisites: Docker Desktop (or Docker Engine + Compose plugin on Linux).

```bash
git clone https://github.com/emanex4real/voting-app.git
cd voting-app
cp .env.example .env            # see "Environment variables" below
cp worker/.env.example worker/.env

docker compose up --build -d
docker compose exec web flask --app app seed   # creates tables + admin user
```

Visit `http://localhost:5000`. Default seeded admin login: `admin` / `admin123`
(change this before deploying anywhere real).

To stop everything: `docker compose down` (add `-v` to also wipe the database volume).

### Running without Docker (for active development)

Useful when you're editing `app.py` or `worker.js` directly and want fast
reloads instead of rebuilding images each time.

```bash
# Start just Redis + Postgres in Docker:
docker compose -f docker-compose.dev.yml up -d

# Flask:
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
flask --app app seed
python app.py

# Worker (separate terminal):
cd worker
npm install
npm start
```

---

## Environment variables

Two separate `.env` files are needed — one for Flask, one for the worker
(the worker doesn't need `SECRET_KEY`).

**`voting-app/.env`**
```
SECRET_KEY=<random value>
DATABASE_URL=postgresql+psycopg://voting:<password>@postgres:5432/voting
REDIS_URL=redis://redis:6379/0
```

**`voting-app/worker/.env`**
```
DATABASE_URL=postgresql://voting:<password>@postgres:5432/voting
REDIS_URL=redis://redis:6379/0
```

Generate a strong random value for `SECRET_KEY` (and for your Postgres
password) with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

> Note the two `DATABASE_URL` formats differ slightly: Flask's SQLAlchemy
> needs the `+psycopg` driver suffix; the worker's raw `pg` client doesn't.

---

## Testing

```bash
# Flask (from the main folder, venv active):
pip install -r requirements-dev.txt
python -m pytest -v          # must be run with "python -m", not bare "pytest" —
                              # see Troubleshooting below for why

# Worker (from worker/):
npm install
npm test
```

Flask tests run against a temporary SQLite file and a fake in-memory Redis
(`fakeredis`) — no real Postgres/Redis needed. This keeps them fast and
makes them suitable for CI.

---

## CI/CD pipeline

Defined in `.github/workflows/ci.yml`. Tests run on every push and PR; the
build and deploy jobs run only on pushes to `main`.

1. **`test-web`**: installs `requirements-dev.txt`, runs `python -m pytest`
2. **`test-worker`**: installs worker deps, runs `npm test`
3. **`build-and-push`**: runs only after both test jobs pass. Builds the
   `web` and `worker` Docker images and pushes them to GitHub Container
   Registry:
   - `ghcr.io/emanex4real/voting-app-web:latest`
   - `ghcr.io/emanex4real/voting-app-worker:latest`

   (Each image is also tagged with the commit SHA, so any build can be
   redeployed by hand for a rollback.)
4. **`deploy`**: runs only after `build-and-push` succeeds. See
   "Automated deployment" below.

If any test fails, nothing is built or deployed.

The registry push needs no extra secrets (it uses GitHub's built-in
`GITHUB_TOKEN`). The `deploy` job needs the secrets listed below.

### Required GitHub Actions secrets (for the `deploy` job)

Set under repo **Settings > Secrets and variables > Actions**:

| Secret                  | Value                                                        |
|-------------------------|--------------------------------------------------------------|
| `EC2_HOST`              | The EC2 instance's public IP address                         |
| `EC2_USER`              | `ubuntu`                                                     |
| `EC2_SSH_KEY`           | Full contents of the `.pem` private key, including header/footer lines |
| `AWS_ACCESS_KEY_ID`     | Access key of the limited IAM deploy user (see below)        |
| `AWS_SECRET_ACCESS_KEY` | Secret key for that same IAM user                            |
| `AWS_REGION`            | The instance's region, e.g. `eu-north-1` (not the availability zone like `eu-north-1b`) |
| `EC2_SG_ID`             | ID of the instance's security group (`sg-...`)               |

---

## Deployment (AWS EC2)

High-level steps (see commit history / this file's history for the exact
commands used):

1. Launch a `t3.micro` Ubuntu EC2 instance. Security group: SSH (22) open
   only to your own IP, port 5000 open to everyone.
2. Install Docker Engine + Compose plugin on the instance (same steps as
   any Ubuntu Docker install — see docker.com/docs).
3. Add your user to the `docker` group (`sudo usermod -aG docker $USER`),
   then **log out and back in** for it to apply.
4. Since GHCR images are private by default, authenticate once:
   ```bash
   docker login ghcr.io -u emanex4real
   # password: a GitHub PAT with the "read:packages" scope
   ```
5. Copy `docker-compose.prod.yml` onto the server, and create `.env` there
   with production secrets (see "Environment variables" above).
6. ```bash
   docker compose -f docker-compose.prod.yml pull
   docker compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.prod.yml exec web flask --app app seed
   ```
7. Visit `http://<ec2-public-ip>:5000`.

`docker-compose.prod.yml` differs from the main one in two ways: it pulls
prebuilt `image:` references from GHCR instead of `build:`-ing locally (much
faster on a small instance), and all services have `restart: unless-stopped`
so they survive a server reboot.

### Automated deployment

After the first manual setup above, deploys are automatic. On every push to
`main` that passes tests, the `deploy` job:

1. Logs into AWS as a limited IAM user
2. Looks up the GitHub runner's public IP
3. Opens port 22 in the security group for **that IP only** (`/32`)
4. SSHs into the server and runs `docker compose pull` and `up -d`
5. Closes the port 22 rule again (this step runs even if the deploy fails)

This is needed because SSH on the server is restricted to the owner's own IP.
GitHub's runners connect from different addresses, so without the temporary
rule the deploy times out. The rest of the time SSH stays locked down.

**IAM setup (one time):**
- Create a policy allowing only `ec2:AuthorizeSecurityGroupIngress` and
  `ec2:RevokeSecurityGroupIngress`, with `Resource` set to the ARN of the
  one security group (`arn:aws:ec2:<region>:<account-id>:security-group/<sg-id>`).
- Create an IAM user with no console access, attach that policy, and create
  an access key for it. Store the key as the `AWS_*` secrets above.

The deploy user can change firewall rules on that single group and nothing
else in the AWS account.

**Manual deploy (fallback):** SSH in and run the `pull` and `up -d` commands
from step 6 above.

### Rotating the Postgres password safely

Changing `POSTGRES_PASSWORD` in the compose file / `.env` does **not**
retroactively change the password of an already-initialized database —
Postgres only applies that variable on first init. To rotate it without
downtime or data loss:

```bash
docker compose -f docker-compose.prod.yml exec postgres psql -U voting -d voting
# at the prompt:
ALTER USER voting WITH PASSWORD '<new-password>';
\q
```

Then update `.env` to match, and `docker compose -f docker-compose.prod.yml up -d`
to recreate `web`/`worker` with the new value.

---

## Operations

**View logs:**
```bash
docker compose -f docker-compose.prod.yml logs --tail=50      # all services
docker compose -f docker-compose.prod.yml logs -f web         # live, one service
```
Log rotation is configured (10MB × 3 files per container) so logs can't
silently fill the server's disk.

**Health check:** `GET /health` checks both the Postgres and Redis
connections, not just that the Flask process is alive — used by Docker's
own `healthcheck:` for the `web` service.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'app'` in CI / pytest**
Run `python -m pytest`, not bare `pytest`. The `-m` form adds the current
directory to Python's import path; without it, `tests/test_app.py` can't
find `app.py` sitting one level up.

**`psycopg2` fails to build from source (`pg_config not found` or a
CPython-internal-API compile error)**
This project uses `psycopg[binary]` (psycopg **3**), not `psycopg2` —
psycopg2's last release predates prebuilt wheels for newer Python versions
(e.g. 3.14) and fails to compile against them. If you see this, check
`requirements.txt` still says `psycopg[binary]`, not `psycopg2-binary`.

**`password authentication failed for user "voting"` after editing `.env`**
See "Rotating the Postgres password safely" above — you likely changed
`POSTGRES_PASSWORD` without actually updating it inside the running
database via `ALTER USER`.

**Files lose their leading dot when downloaded/transferred**
`.env.example`, `.dockerignore`, etc. can end up saved as `env.example` /
`dockerignore` depending on the browser/OS. Check with `ls -la` (not plain
`ls`, which hides dotfiles) and rename with `mv oldname .newname` (or
`git mv` if already tracked).

**`permission denied ... docker.sock`**
Your user was added to the `docker` group but the current shell session
predates that change. Run `newgrp docker` for a quick fix in the current
terminal, or fully log out and back in for it to apply everywhere.

**`git push` rejected: "refusing to allow a Personal Access Token to
create or update workflow `.github/workflows/...` without `workflow` scope"**
Your PAT needs the `workflow` scope in addition to `repo` to push changes
to CI config files specifically. Edit the token on GitHub to add it.

**`deploy` job fails with `dial tcp ...:22: i/o timeout`**
GitHub's runner can't reach port 22 because the security group only allows
the owner's IP. Check that the "Open SSH for this runner" step ran and
succeeded, that the `AWS_*` and `EC2_SG_ID` secrets are set, and that the IAM
policy's `Resource` ARN matches the security group exactly. Also confirm
`AWS_REGION` is the region (`eu-north-1`), not the availability zone
(`eu-north-1b`).

**Can't SSH into the server from your own machine (connection timed out)**
Your public IP has probably changed (home and mobile connections rotate
addresses). In the security group, edit the SSH (port 22) rule and set the
source to **My IP** again.

**GitHub Actions job fails with "The job was not acquired by Runner...
Internal server error"**
A transient GitHub infrastructure issue, not a problem with your workflow.
Just re-run the job.

---

## Default credentials (change before real use)

Seeded by `flask --app app seed`:
- Admin: `admin` / `admin123`

**Do not use these in any environment accessible to the public without
changing them first.**
