# Deploying OutboundAI to Coolify on an AWS EC2 Ubuntu server

> This is the AWS equivalent of `DEPLOYMENT_COOLIFY.md`. The Coolify-side steps (env vars,
> port 8000, Supabase schema, trunk, verification) are identical — this doc focuses on the
> AWS-specific parts: launching the instance, sizing, swap, and firewall (security groups).

---

## 0. Sizing reality check (READ FIRST)

| Instance | vCPU | RAM | Free tier? | Verdict for this app |
|----------|------|-----|-----------|----------------------|
| `t2.micro` / `t3.micro` | 1 | 1 GB | ✅ (750 hrs/mo, 12 months) | Works **only** with 4 GB swap added; build is slow and tight. Testing only. |
| `t3.small` | 2 | 2 GB | ❌ (~$15/mo) | Realistic minimum. |
| `t3.medium` | 2 | 4 GB | ❌ (~$30/mo) | Comfortable. Recommended for real use. |

Coolify's own minimum is 2 GB RAM. Your Docker build installs large Python wheels
(`onnxruntime`, `av`, `livekit-*`, `google-*`), which need RAM. **On a 1 GB free-tier box you
MUST add swap (Step 4) or the build will be OOM-killed.**

Storage: free tier gives **30 GB EBS** — that's enough. This image + Coolify use ~8–12 GB.

---

## 1. Launch the EC2 instance

1. AWS Console → **EC2** → **Launch instance**.
2. **Name:** `outboundai-prod`.
3. **AMI:** Ubuntu Server **24.04 LTS** (free-tier eligible).
4. **Instance type:** `t2.micro`/`t3.micro` (free tier) — or `t3.small`+ if you can.
5. **Key pair:** create or select one (you'll use this `.pem` to SSH in). Download and keep it safe.
6. **Network settings → Firewall (security group):** create a new security group and add the
   inbound rules in Step 2 below.
7. **Storage:** change the root volume to **30 GiB** gp3 (max free tier).
8. Launch. Note the instance's **Public IPv4 address**.

> Tip: allocate an **Elastic IP** (EC2 → Elastic IPs → Allocate → Associate to the instance) so
> the public IP doesn't change on reboot. Free while attached to a running instance.

---

## 2. Security group (firewall) — inbound rules

Add these inbound rules to the instance's security group:

| Type | Protocol | Port | Source | Why |
|------|----------|------|--------|-----|
| SSH | TCP | 22 | **My IP** | SSH access (restrict to your IP, not 0.0.0.0/0) |
| HTTP | TCP | 80 | Anywhere (0.0.0.0/0) | App + Let's Encrypt HTTP challenge |
| HTTPS | TCP | 443 | Anywhere (0.0.0.0/0) | App over TLS |
| Custom TCP | TCP | 8000 | **My IP** | Coolify dashboard (restrict to your IP) |

Notes:
- **You do NOT need any UDP / SIP / RTP ports.** Media and SIP are handled by **LiveKit Cloud**;
  your server only makes **outbound** WebSocket connections to LiveKit and Supabase, which AWS
  allows by default. This is why the app works behind NAT with no inbound media ports.
- Outbound rules: leave the default "allow all outbound."
- Port 8000 is Coolify's **own dashboard**. Your app is reached via 80/443 through Coolify's
  proxy — not directly on 8000. Restrict 8000 to your IP for safety.

---

## 3. SSH in and update

```bash
# from your machine (adjust path/key name and IP)
chmod 400 your-key.pem               # Linux/macOS only
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>

# on the server
sudo apt update && sudo apt upgrade -y
```

> On Windows, use the `.pem` with `ssh -i your-key.pem ubuntu@<IP>` from PowerShell, or use PuTTY.
> The default Ubuntu AMI user is **`ubuntu`** (not `root`).

---

## 4. Add swap (MANDATORY on 1 GB free tier, harmless on bigger)

This prevents the Docker build from being killed for running out of memory.

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h        # confirm "Swap: 4.0Gi"
```

---

## 5. Install Coolify

```bash
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
```

When it finishes, open Coolify in your browser:

```
http://<EC2_PUBLIC_IP>:8000
```

Create the admin account. (If it doesn't load, re-check that port **8000** is open to your IP in
the security group, Step 2.)

---

## 6. Everything else is identical to the DigitalOcean guide

From here, follow **`DEPLOYMENT_COOLIFY.md`** starting at its Supabase / trunk / app-creation
steps. The key points, condensed:

1. **Supabase schema** — already done for your project (`wkrgbeczmfstmpfftxso`). If you switch
   projects, re-run `supabase_schema.sql`.
2. **LiveKit trunk** — already created (`OUTBOUND_TRUNK_ID=ST_SAmXmkBoFHXJ`). Reuse it.
3. **Create the app in Coolify:** New Resource → Application → connect GitHub repo
   `DreamFaang78/Calling-agent`, branch `main`, **Build Pack = Dockerfile**, **Base Directory =
   `/`**, **Ports Exposes = 8000**.
4. **Environment variables:** paste the same block you used on DigitalOcean (all the
   `LIVEKIT_*`, `GOOGLE_API_KEY`, `VOBIZ_*`, `OUTBOUND_TRUNK_ID`, `SUPABASE_*`, etc.). They are
   NOT in the Git repo, so they must be set in Coolify. Mark secrets as **runtime only** (uncheck
   "build variable") to avoid baking them into the image.
5. **Deploy.** Confirm the deploy log imports the latest commit (currently `7c92994` — the tool-
   list fix), then check the **Logs** tab for `✅ OutboundAI server started` and
   `[OK] Supabase connected`.
6. **Domain + HTTPS:** add a domain in Coolify → Domains for a free Let's Encrypt cert. If you
   have no domain, Coolify gives you a `sslip.io` URL automatically.

---

## 7. Verify

```bash
curl http://<your-app-url>/health
# → {"status":"ok", "livekit_url":"wss://...", "supabase":true}
```

Then open `<your-app-url>/ui`, go to Single Call, dial your own number, and watch the **Logs**
tab. The agent should speak (the realtime session crash is fixed in commit `7c92994`).

---

## 8. AWS-specific gotchas

| Symptom | Cause | Fix |
|---------|-------|-----|
| Docker build killed / "exit code 137" / build hangs at pip install | Out of RAM on 1 GB instance | Add swap (Step 4) or use `t3.small`+ |
| Can't open `:8000` Coolify dashboard | Security group missing port 8000 | Add inbound TCP 8000 from your IP |
| Coolify dashboard URL keeps changing after reboot | No Elastic IP | Allocate + associate an Elastic IP |
| HTTPS cert fails to issue | Ports 80/443 not open, or domain DNS not pointing to the EC2 IP | Open 80/443; point your domain's A record at the (Elastic) IP |
| SSH "permission denied" | Wrong user or key perms | Use `ubuntu@`, `chmod 400` the `.pem` |
| App builds but calls don't connect | Env vars not set in Coolify | Add all required env vars, redeploy |

---

## 9. Cost note

- The **free tier** (`t2.micro`, 30 GB, 750 hrs/mo) covers one always-on instance for 12 months.
  After 12 months (or above free-tier usage) you'll be billed.
- Data transfer out has a free allowance; AI voice calls route media through LiveKit Cloud, so
  your EC2 egress stays low (mostly signaling + API traffic).
- LiveKit, Gemini, Supabase, and Vobiz bill separately from AWS.
```
