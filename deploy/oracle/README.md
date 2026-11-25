# psxdata-api on a single VM

Docker Compose bundle for running the API on one Linux host (built for an arm64 Oracle
Ampere A1 VM, works on amd64 too).

## Services

| Service | Image | Role |
|---|---|---|
| `api` | `mtauha/psxdata-api:${API_TAG}` | The FastAPI app, 4 uvicorn workers. Not published to the host. |
| `redis` | `redis:7-alpine` | Shared `/historical` cache, rate-limit counters and PSX cooldown. 512 MB, LRU eviction, no persistence. Not published to the host. |
| `caddy` | `caddy:2-alpine` | Terminates HTTPS (Let's Encrypt) on 80/443 and proxies to `api`. |

The Compose network uses the fixed subnet `172.28.0.0/24` and Caddy sits at `172.28.0.2`.
`api` trusts `X-Forwarded-For` only from that address (`FORWARDED_ALLOW_IPS`), so the rate
limiter sees the real client IP and callers cannot spoof it. If you change the subnet, change
both together.

## First-time bring-up

Prerequisites: Docker with the Compose plugin, ports 80 and 443 (tcp, and 443 udp) open in the
firewall and cloud security list, and a DNS record for `API_DOMAIN` pointing at the VM.

```bash
sudo mkdir -p /opt/psxdata-api && sudo chown "$USER" /opt/psxdata-api
cd /opt/psxdata-api
# copy compose.yaml, Caddyfile and .env.example here, then:
cp .env.example .env && nano .env      # set API_TAG, API_DOMAIN, ACME_EMAIL
docker compose pull
docker compose up -d
curl -fsS https://$API_DOMAIN/health
```

## Updating

Pushing a release tag publishes the image, and the `Deploy to Oracle` workflow then does this
for you. By hand:

```bash
cd /opt/psxdata-api
sed -i 's/^API_TAG=.*/API_TAG=0.8.1/' .env
docker compose pull && docker compose up -d
docker compose ps
docker compose logs --tail 50 api
```

Rolling back is the same with the previous tag.
