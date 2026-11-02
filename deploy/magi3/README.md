# ForWin on magi3

Run the deploy and backup scripts from magi1's clean checkout at
`/Users/magi1/ForWin-source-github`. Configure the `magi3` SSH alias and key
authentication on magi1; connect once interactively to verify the host key.
The deploy script also runs from any other clean clone whose `origin` is GitHub,
on a machine with the same `magi3` alias (the operator Mac, when magi1 is away).

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

Docker Hub is unreachable from magi3, so give the Docker daemon registry mirrors
before the first build (section "Builds from China"): replace `docker: {}` in
`~/.colima/default/colima.yaml` with

```yaml
docker:
  registry-mirrors:
    - https://docker.m.daocloud.io
    - https://docker.1ms.run
```

and `env: {}` with

```yaml
env:
  http_proxy: ""
  https_proxy: ""
```

then restart Colima (`brew services restart colima` when it runs as a Homebrew
service). `docker info` lists the mirrors under `Registry Mirrors` and shows no
`HTTP Proxy`. The empty proxy entries matter on magi3: launchd hands Colima the
host proxy that cnproxy sets for GUI sessions (`127.0.0.1:17891`), Colima copies
it into the VM's `daemon.json` as `192.168.5.2:17891`, the VM cannot reach host
ports, and every pull fails with `proxyconnect tcp: ... i/o timeout`. Keys set in
`env:` take precedence over the host environment.

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

The image installs torch from PyTorch's CPU index (`TORCH_INDEX_URL`, default https://download.pytorch.org/whl/cpu) and every other package from `PIP_INDEX_URL`. PyPI's Linux torch wheels, arm64 included, are CUDA builds that add about 4 GB of `nvidia-*` and triton packages the service never uses: with them the image took 10 GB of Docker disk on magi3, with the CPU build 2.1 GB (measured 2026-11-02, torch 2.15.0). The build fails if a CUDA torch is installed anyway, for example when a package requires a torch newer than the CPU index has.

## publish-hub tunnel

ForWin's containers reach publish-hub (magi2:18930) through the `publish-hub-tunnel` service, an ssh client that logs in to the VPS as a forwarding-only user and listens on :18930 inside the compose network. Callers use `http://publish-hub-tunnel:18930`: `FORWIN_PUBLISH_HUB_URL` in `.env` and the extension's hub address. On the VPS that port is magi2's reverse tunnel, which ends at `127.0.0.1:18930` (publish-hub repo, `docs/ops/2026-11-publish-hub-fanqie-cutover.md`). The forward runs in a container because the colima VM cannot reach any port on the magi3 host, so `host.docker.internal` (colima's `192.168.5.2`) does not work here.

One-time set-up on magi3:

```sh
mkdir -m 700 ~/forwin/publish-hub-tunnel
ssh-keygen -t ed25519 -N '' -C magi3-forwin-publish-hub-tunnel -f ~/forwin/publish-hub-tunnel/id_ed25519
ssh-keygen -F <vps-address> -f ~/.ssh/cnrelay_known_hosts | grep ' ssh-ed25519 ' > ~/forwin/publish-hub-tunnel/known_hosts
```

On the VPS, the public key goes into the forwarding-only user's `authorized_keys` as `restrict,port-forwarding,permitopen="127.0.0.1:18930" ssh-ed25519 …`, and that user's `Match User` block in `sshd_config.d` allows local forwarding to `127.0.0.1:18930` only. Then set `FORWIN_PUBLISH_HUB_TUNNEL_TARGET=<user>@<vps-address>` in `deploy/magi3/.env` and run `dc up -d publish-hub-tunnel` (`dc` as defined above). The service is healthy once publish-hub's `/health` answers through it. Without the target or the key it idles, so restart it after setting them up.

## colima DNS on magi3

The colima VM image has no systemd-resolved, so its /etc/resolv.conf symlink dangles and containers cannot resolve external names. `~/.colima/default/colima.yaml` on magi3 carries a `provision` script that writes `nameserver 192.168.10.1` and `nameserver 223.5.5.5` on every boot; keep it, and the `docker:` registry mirrors below, when recreating the VM.

## Builds from China

From magi3, `registry-1.docker.io` times out, and pypi.org (about 0.3 MB/s), deb.debian.org, registry.npmjs.org and Playwright's CDN (`cdn.playwright.dev`) crawl or drop connections. Builds therefore fetch through mirrors (measured from magi3 on 2026-11-02):

| What | Mirror | Set in |
| --- | --- | --- |
| Docker Hub base images (`python`, `node`, `postgres`, `qdrant`, `alpine`) | `docker.m.daocloud.io`, then `docker.1ms.run` | `docker: registry-mirrors` in `~/.colima/default/colima.yaml` |
| Python packages | `https://pypi.tuna.tsinghua.edu.cn/simple` | `PIP_INDEX_URL` |
| Debian packages | `mirrors.tuna.tsinghua.edu.cn` | `DEBIAN_MIRROR` |
| npm packages (World Studio) | `https://registry.npmmirror.com` | `NPM_REGISTRY` |
| Playwright browsers | `https://registry.npmmirror.com/-/binary/playwright` | `PLAYWRIGHT_DOWNLOAD_HOST` |
| Alpine packages (publish-hub-tunnel) | `mirrors.aliyun.com` | `ALPINE_MIRROR` |
| torch, CPU build (embedding) | `https://download.pytorch.org/whl/cpu` (upstream, about 17 MB/s) | `TORCH_INDEX_URL` |
| Hugging Face model (embedding) | `https://hf-mirror.com` | `HF_ENDPOINT` |

The defaults live in `docker-compose.yml`, so `.env` needs none of them; set one there only to use another mirror (or the upstream address to bypass it). The root `Dockerfile` keeps upstream when these build arguments are empty, so other hosts build as before.

The Docker daemon takes its mirrors only for Docker Hub images, and only when an image is missing locally: the build uses the local `python:3.13-slim`, `python:3.12-slim` and `node:22-slim` as long as they exist, so keep them (no `docker image prune -a` / `docker system prune -a`). If they are gone, the next build pulls them through the mirrors. To check the mirrors, run `docker info | grep -A2 'Registry Mirrors'` and `docker pull python:3.13-slim`.

The mirrors are public services and drop a download now and then (for example a DaoCloud redirect to Cloudflare timing out). A failed pull or package download fails `compose build` before anything is restarted; run the deploy again, finished layers are reused. If one mirror stays broken, swap it: for Docker Hub edit `colima.yaml`, for packages set the variable in `.env`.

After editing `colima.yaml`, restart Colima for the setting to take effect. That stops every container; `restart: unless-stopped` brings them back, or stop them cleanly first and start them afterwards:

```sh
dc stop                      # dc as defined above
brew services restart colima
dc up -d
```

The VM's outbound connections go through lima's user-mode network, whose forwarder on the host admits only 10 connects in progress. From China, connects to blocked hosts (Google, Docker Hub) hang until they time out, so a few such callers stall every new outbound connection of the VM, builds included: `lsof -nP -a -p $(pgrep -f 'limactl usernet') -iTCP -sTCP:SYN_SENT` then shows 10 entries to the blocked addresses. Chromium in publisher-browser did that with its background calls to Google, so the compose file sets `FORWIN_EXTENSION_HOST_RESOLVER_RULES` to fail Google's domain names at once. If a build stalls again, check that count and find the caller (`/proc/net/tcp` state `02` inside each container).

If `colima ssh` hangs, or a container with a bind mount from the magi3 home directory (publish-hub-tunnel) cannot start, the VM's virtiofs mount of `/Users/taiwei` may be stuck (processes wait in `fuse_simple_request`); restarting Colima as above clears it.
