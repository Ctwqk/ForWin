# ForWin on magi3

Run the deploy and backup scripts from magi1's clean checkout at
`/Users/magi1/ForWin-source-github`. Configure the `magi3` SSH alias and key
authentication on magi1; connect once interactively to verify the host key.

## First-time setup

On magi3, install Homebrew first if needed, then run:

```sh
brew install colima docker docker-compose docker-buildx
colima start --vm-type vz --cpu 8 --memory 12 --disk 100
docker context use colima
mkdir -p ~/.docker
```

Merge this setting into `~/.docker/config.json`, preserving existing settings.
The path below assumes Apple Silicon Homebrew at `/opt/homebrew`:

```json
{
  "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
}
```

This is the plugin discovery setting documented by Homebrew for
[Compose](https://formulae.brew.sh/formula/docker-compose) and
[Buildx](https://formulae.brew.sh/formula/docker-buildx).
Verify `docker compose version` and `docker buildx version`. Start Colima again
after a host reboot before deploying.

On magi1, run `./scripts/deploy_magi3.sh` once. It seeds `~/forwin/repo.git` and
`~/forwin/src` on magi3, then stops because the host-only `.env` is missing.
On magi3, create it from the checked-out template:

```sh
cd ~/forwin/src
install -m 600 deploy/magi3/env.example deploy/magi3/.env
chmod 600 deploy/magi3/.env
vi deploy/magi3/.env
```

Set the PostgreSQL password, configured LLM API keys,
publisher extension key and session secret. Keep the session secret and preferred
publisher client ID when restoring an existing installation. Use a long URL-safe
PostgreSQL password (for example, random hex); Compose embeds it in the database URL.
Optional entries can stay empty; Compose supplies defaults. Keep this file only
on magi3 and never print it or a resolved Compose config containing secrets.
When moving existing secrets, stream directly over SSH into a mode-600 file.

## Deploy, rollback, and service operations

On magi1:

```sh
cd /Users/magi1/ForWin-source-github
./scripts/deploy_magi3.sh
./scripts/deploy_magi3.sh --data-stores-only
./scripts/deploy_magi3.sh --ref <previous-commit-sha>
```

Deploy fetches `origin/master`, rejects any dirty tree, and accepts only commits
in that branch's history. It pushes to magi3, checks out the exact SHA, builds,
runs the `tools` migration, starts services, and checks both health endpoints for
up to 180 seconds. A successful full deploy records `~/forwin/src/.deploy-commit`.
The data-stores-only option builds and starts only PostgreSQL and Qdrant;
it skips migrations, app health polling, and the deployment marker. Use it before
an initial data import. Roll back by deploying a previous SHA with `--ref`;
database migrations remain applied, so check schema compatibility first.

On magi3, use this shell helper for routine operations:

```sh
export PATH="/opt/homebrew/bin:$PATH"
cd ~/forwin/src
dc() { docker compose -p forwin -f deploy/magi3/docker-compose.yml --env-file deploy/magi3/.env "$@"; }
dc ps
dc logs --tail 100 -f app mcp generation-worker
dc stop
dc up -d --remove-orphans
# Stop and remove containers while retaining named data volumes:
dc down
```

## Backup and restore

On magi1, `./scripts/backup_magi3.sh` streams a custom-format PostgreSQL dump to
`~/backups/forwin-magi3/forwin-<UTC timestamp>.dump`. Dumps below 1024 bytes fail
and partial files are removed. Sundays (magi1 local time), or with `--with-data`,
it also streams `forwin-data-<timestamp>.tgz`. Files are private and completed
backups older than 14 days are deleted after a successful run. These backups
cover PostgreSQL and `/app/data`; arrange a separate Qdrant backup. Artifacts are files under `/app/data/artifacts`, so the data backup covers them; magi3 runs no object store.
Pause writers for a consistent application-data archive when needed.

To restore, start the data stores and stop app services on magi3:

```sh
export PATH="/opt/homebrew/bin:$PATH"
cd ~/forwin/src
dc() { docker compose -p forwin -f deploy/magi3/docker-compose.yml --env-file deploy/magi3/.env "$@"; }
dc up -d postgres qdrant
dc stop app mcp generation-worker outbox-worker publisher-worker publisher-browser
```

Then stream the selected backups from magi1 (replace the timestamps):

```sh
cat ~/backups/forwin-magi3/forwin-<timestamp>.dump | ssh magi3 'export PATH="/opt/homebrew/bin:$PATH"; docker compose -p forwin -f ~/forwin/src/deploy/magi3/docker-compose.yml --env-file ~/forwin/src/deploy/magi3/.env exec -T postgres pg_restore -U forwin -d forwin --no-owner --clean --if-exists'
# Restore application files into an empty forwin_forwin-data volume:
cat ~/backups/forwin-magi3/forwin-data-<timestamp>.tgz | ssh magi3 'export PATH="/opt/homebrew/bin:$PATH"; docker run --rm -i -v forwin_forwin-data:/data alpine tar xzf - -C /data'
```

Restore any separately backed-up Qdrant data, then deploy the compatible
SHA from magi1 to migrate, restart, and health-check the stack.

## Tunnel and scheduled backup on magi1

Install both LaunchAgents on magi1:

```sh
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cp deploy/magi3/com.forwin.magi3-*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.forwin.magi3-tunnel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.forwin.magi3-backup.plist
```

The tunnel starts at login and reconnects automatically. Backups run daily at
03:17 local time and log to `~/Library/Logs/forwin-magi3-backup.log`.
To unload an agent, use `launchctl bootout gui/$(id -u) <installed-plist-path>`.
For a foreground tunnel:

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 28899:127.0.0.1:8899 -L 28896:127.0.0.1:8896 magi3
```

Access the app at `http://127.0.0.1:28899` and MCP at
`http://127.0.0.1:28896/mcp` on magi1. Health endpoints are `/health` on both
ports. All published ports on magi3 bind its loopback interface.

## Embeddings

The `embedding` service (deploy/magi3/embedding-gateway, copied from Constructure) serves all-MiniLM-L6-v2 at 384 dimensions on CPU, the same model infra-150 served on GPU. The model is baked into the image at build time through `HF_ENDPOINT` (default https://hf-mirror.com), so the service starts without network access.
