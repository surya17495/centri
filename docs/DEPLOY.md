# Deploy CENTRI on an Oracle Cloud Always Free VM (Docker)

Target: an **Oracle Cloud Always Free** Ampere A1 instance (arm64, 4 OCPU /
24 GB, Ubuntu 22.04/24.04). The same `docker compose` flow works on any amd64
box too — the images use a plain `python:3.11-slim` base with no arch-specific
binaries.

> For the non-Docker, systemd + Caddy install path, see `deploy/install.sh`.
> This doc is the minimal **container** boot.

## 1. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
# optional: run docker without sudo (log out/in after)
sudo usermod -aG docker "$USER"
```

## 2. Clone and configure (BYOK)

```bash
git clone https://github.com/surya17495/centri.git
cd centri
cp .env.example .env
nano .env   # fill in your model gateway / provider keys + CENTRI_AUTH_TOKEN
```

At minimum set a model gateway (BYOK) and an auth token:

- `LITELLM_BASE_URL` + `LITELLM_API_KEY` (or `OPENAI_API_KEY` / `NEBIUS_API_KEY`)
- `CENTRI_AUTH_TOKEN` — a long random string; required once the port is reachable
  from outside the VM. Generate one with `openssl rand -hex 32`.

Secrets stay in `.env` (gitignored) and are read at container runtime — they are
never baked into the images.

## 3. Boot

```bash
docker compose up -d
docker compose ps
```

- `server` — the FastAPI core on host port **8760**, state persisted in the
  `centri-data` named volume (the memory spine / `state.db`).
- `shell` — the React UI (static, nginx) on host port **8761**.

## 4. Open the firewall

Two layers on Oracle Cloud:

1. **Security List / NSG** (Oracle console → your VCN → Security Lists): add
   ingress rules allowing TCP **8760** and **8761** from your source (your IP,
   or `0.0.0.0/0` if you accept public exposure — only with `CENTRI_AUTH_TOKEN`
   set).
2. **Host firewall** (Ubuntu images ship with iptables rules):

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8760 -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8761 -j ACCEPT
   sudo netfilter-persistent save
   ```

## 5. Verify

```bash
# from the VM:
curl -fsS http://127.0.0.1:8760/health
# -> {"status":"ok","version":"0.1.0"}

# from your laptop (replace with the VM public IP):
curl -fsS http://<VM_PUBLIC_IP>:8760/health
```

Then open `http://<VM_PUBLIC_IP>:8761` in a browser. In the shell's
**Settings**, set the **Backend URL** to `http://<VM_PUBLIC_IP>:8760` and paste
your **Auth token** (the `CENTRI_AUTH_TOKEN` you set in `.env`).

## Operations

```bash
docker compose logs -f server   # follow core logs
docker compose restart server   # restart after editing .env
docker compose pull && docker compose up -d --build   # rebuild after git pull
docker compose down             # stop (named volume + memory persist)
```

The `centri-data` volume survives `down`; remove it explicitly with
`docker volume rm centri_centri-data` only if you want to wipe memory.
