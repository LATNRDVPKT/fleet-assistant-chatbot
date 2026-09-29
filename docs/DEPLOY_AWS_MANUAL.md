# Deploying Fleet Copilot to AWS — manual, step by step

This guide deploys the whole system (FastAPI API + Streamlit UI + nginx) on **one EC2 instance with
Docker Compose**, using **Amazon Bedrock (Claude)** as the LLM and **CloudWatch Logs** for logs and metrics.
Every step is done by hand in the AWS Console, so you see and control each piece.
Section 12 shows how to grow it to two instances behind a load balancer for 1 lakh requests/day with high availability.

```
 Browser / app ──HTTP(S)──► EC2 instance (Docker Compose)
                              ├─ nginx  :80   /           → ui  (Streamlit :8501)
                              │                /v1 /health → api (FastAPI  :8000, N workers)
                              ├─ api ──► Amazon Bedrock (Claude)      via the instance IAM role
                              └─ all containers' logs ──► CloudWatch Logs  (/fleet-copilot/prod)
```

Time needed: about 45 minutes. Region used below: **ap-south-1 (Mumbai)**; any Bedrock region works.

---

## 1. Before you start

| Need | How to check |
| --- | --- |
| An AWS account with admin (or EC2 + IAM + Bedrock + CloudWatch) permissions | You can open the EC2, IAM and Bedrock consoles |
| The project zip `fleet-assist-copilot.zip` on your laptop | — |
| An SSH client (or use EC2 Instance Connect / Session Manager in the browser) | `ssh -V` |

## 2. Turn on Bedrock model access

1. Console → **Amazon Bedrock** → left menu **Model access** → **Modify model access**.
2. Tick **Anthropic → Claude Haiku** (fast and cheap; Sonnet is also fine) → **Next** → **Submit**.
   Status must become **Access granted** (usually a few minutes).
3. Bedrock → **Model catalog** → open the model → copy its **Model ID**.
   If the console shows an **inference profile** for your region (ids starting with `apac.` or `global.`),
   copy that id instead — some models can only be called through a profile. You will paste it into
   `BEDROCK_MODEL_ID` in step 7.
4. (For 1 lakh requests/day) Console → **Service Quotas** → **Amazon Bedrock** → search your model →
   check "requests per minute" and "tokens per minute". Request at least **600 requests/min** and
   **1,500,000 tokens/min** (see `docs/SCALING.md` for the maths).

## 3. Create the IAM role for the instance

The API calls Bedrock and writes logs using this role — no access keys are stored anywhere.

1. Console → **IAM** → **Policies** → **Create policy** → **JSON** tab.
2. Paste `deploy/aws/iam-policy.json`, replace `YOUR_ACCOUNT_ID` (top-right menu shows it, 12 digits).
   → **Next** → name `FleetCopilotPolicy` → **Create policy**.
3. IAM → **Roles** → **Create role** → Trusted entity **AWS service**, Use case **EC2** → **Next**.
4. Attach **FleetCopilotPolicy** and **AmazonSSMManagedInstanceCore** (lets you open a browser shell
   without SSH) → **Next** → name `FleetCopilotEC2Role` → **Create role**.

## 4. Create a security group

Console → **EC2** → **Security Groups** → **Create security group**:

| Setting | Value |
| --- | --- |
| Name | `fleet-copilot-sg` |
| Inbound rule 1 | HTTP, port 80, source **My IP** (or your office range; `0.0.0.0/0` only if it must be public) |
| Inbound rule 2 | SSH, port 22, source **My IP** (skip if you use Session Manager) |
| Outbound | Keep "All traffic" (needed for Bedrock, CloudWatch and package downloads) |

Ports 8000 and 8501 are **not** opened: only nginx on port 80 is public.

## 5. Launch the EC2 instance

EC2 → **Instances** → **Launch instances**:

| Setting | Value |
| --- | --- |
| Name | `fleet-copilot-1` |
| AMI | **Amazon Linux 2023** (x86_64) |
| Instance type | **c6i.2xlarge** (8 vCPU, 16 GB) for 1 lakh/day on one box · **t3.large** is enough for a demo |
| Key pair | Create or pick one (for SSH) |
| Network | Default VPC, a **public subnet**, **Auto-assign public IP = Enable** |
| Security group | `fleet-copilot-sg` |
| Storage | **30 GiB gp3** (Docker images with PyTorch are ~4 GB) |
| Advanced → IAM instance profile | `FleetCopilotEC2Role` |
| Advanced → User data | paste the whole of `deploy/aws/ec2-user-data.sh` |

Click **Launch**. Wait until **Status checks: 2/2 passed** (~3 minutes). The user-data script installs
Docker, Docker Compose, git and unzip; check `/var/log/cloud-init-output.log` ends with
`EC2 bootstrap complete ✓`.

## 6. Copy the project to the instance

From your laptop:

```bash
scp -i your-key.pem fleet-assist-copilot.zip ec2-user@<PUBLIC_IP>:~
ssh -i your-key.pem ec2-user@<PUBLIC_IP>
unzip fleet-assist-copilot.zip && cd fleet-assist-copilot
```

(Using Session Manager instead? EC2 → instance → **Connect** → **Session Manager**, run `sudo su - ec2-user`,
and upload the zip to S3 first, then `aws s3 cp s3://your-bucket/fleet-assist-copilot.zip .`.)

## 7. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Set these values (leave the rest as they are):

```ini
ENVIRONMENT=prod
LOG_FORMAT=json                 # one JSON object per log line → easy CloudWatch queries
EMIT_EMF_METRICS=true           # CloudWatch turns the metric lines into real metrics
API_KEYS=<a-long-random-key>:your-company      # generate with: openssl rand -hex 24
UI_API_KEY=<the same key>                      # the UI container uses it to call the API
WORKERS=6                       # ≈ vCPUs − 2  (c6i.2xlarge → 6, t3.large → 2)
EMBEDDING_PROVIDER=local
RERANKER=cross-encoder
LLM_PROVIDER=bedrock
AWS_REGION=ap-south-1
BEDROCK_MODEL_ID=<the id or inference profile from step 2>
```

Never commit `.env`; it holds your API key.

## 8. Build and start

First start **without** the CloudWatch add-on, so you can watch the logs in the terminal:

```bash
docker compose up -d --build        # 5–10 min the first time (PyTorch + 2 small models are baked into the image)
docker compose logs -f api          # watch the numbered start-up steps; Ctrl+C to stop watching
```

Expected start-up log (text format shown for readability):

```text
start_api | [1/4] config: LLM_PROVIDER=bedrock EMBEDDING_PROVIDER=local RERANKER=cross-encoder WORKERS=6
start_api | [2/4] checking search index in /app/data/index
start_api |       no index yet → building it from config/manifest.yaml (first start takes ~1 minute)
ingestion.indexer | [1/6] manifest valid ✓ | documents=4
...
ingestion.indexer | INGESTION DONE ✓ | processed=4 total_chunks=160
start_api | [4/4] starting FastAPI on 0.0.0.0:8000 with 6 worker(s)
api | STARTUP COMPLETE ✓ service ready in 9.8 s        (once per worker)
```

When everything in step 9 works, restart with logs going to CloudWatch:

```bash
docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d
```

## 9. Verify

```bash
# on the instance
curl -s localhost/health
curl -s -X POST localhost/v1/chat -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  -d '{"question":"What is the harsh braking threshold?","vehicle_model":"EV-60"}'
```

A good answer has `"status":"answered"`, `"grounded":true` and citations with `"doc_id":"VM-EV60-OM"`,
`"page":"5"`. Then open **http://&lt;PUBLIC_IP&gt;/** in your browser for the UI and
**http://&lt;PUBLIC_IP&gt;/docs** for the interactive API documentation.

Run the production evaluation on the server (uses Bedrock, costs a few rupees):

```bash
docker compose exec api python -m evals.run_evals --profile production
docker compose exec api python -m evals.run_evals --profile production --judge   # + LLM-as-judge scores
```

## 10. Logs and monitoring in CloudWatch

**Logs.** CloudWatch → **Log groups** → `/fleet-copilot/prod` → one stream per container.
Every request shows its numbered steps with the same `request_id`.

**Logs Insights** (CloudWatch → Logs Insights → select the group) — useful saved queries:

```sql
-- whole story of one request
fields @timestamp, logger, msg | filter request_id = "7f3a9c2e1b44" | sort @timestamp asc

-- slowest answers
fields @timestamp, request_id, vehicle_model, status, latency_ms, retrieval_ms, llm_ms
| filter event = "chat_completed" | sort latency_ms desc | limit 20

-- questions we could not answer (content gaps)
filter event = "chat_completed" and status = "not_found"
| stats count(*) as n by vehicle_model, question | sort n desc

-- errors and fallbacks
fields @timestamp, request_id, level, msg | filter level in ["WARNING","ERROR","CRITICAL"] | sort @timestamp desc
```

**Metrics.** With `EMIT_EMF_METRICS=true`, CloudWatch → **Metrics** → **All metrics** → namespace
**FleetCopilot** shows Requests, Latency, RetrievalLatency, LLMLatency, Answered, NotFound, Blocked,
Ungrounded, InputTokens, OutputTokens, FeedbackUp/Down (per Environment and per VehicleModel).

**Alarms** (CloudWatch → Alarms → Create alarm → select metric → set threshold → notify an SNS email topic):

| Alarm | Metric | Condition |
| --- | --- | --- |
| Slow answers | FleetCopilot / Latency, statistic p95 | > 6000 ms for 3 × 5 min |
| Ungrounded answers | FleetCopilot / Ungrounded, Sum | > 5 in 1 hour |
| Instance down | EC2 / StatusCheckFailed | ≥ 1 for 2 × 1 min |
| CPU saturated | EC2 / CPUUtilization | > 80% for 3 × 5 min |
| Errors | Logs metric filter on `level = "ERROR"` | > 10 in 5 min |

## 11. HTTPS

Pick one:

* **Recommended — Application Load Balancer + ACM** (also needed for section 12): request a free
  certificate in **Certificate Manager** for your domain, create an ALB with an HTTPS:443 listener using it,
  forward to a target group containing the instance on port 80 with health check path `/health`, and change
  the security group so port 80 accepts traffic only from the ALB's security group.
* **Single instance — Let's Encrypt**: point a DNS name at the instance's Elastic IP and run certbot for nginx.

## 12. Scaling to 1 lakh requests/day with high availability

1 lakh/day ≈ 1.2 requests/s on average, **~12 requests/s at peak** (see `docs/SCALING.md`).

| Option | Setup | Handles | Availability |
| --- | --- | --- | --- |
| A — one big box | 1 × c6i.2xlarge, `WORKERS=6` | ~15–18 requests/s (CPU-bound on the reranker) | single instance: restart = short outage |
| **B — recommended** | 2 × c6i.xlarge (`WORKERS=3` each) in 2 Availability Zones behind the ALB from step 11 | same capacity, survives one instance or AZ failure | high |

Steps for option B:
1. Stop sending traffic changes for a moment; EC2 → your instance → **Actions → Image and templates → Create image**.
2. Launch a second instance from that image in **another Availability Zone** (same role, SG, user data not needed).
   SSH in, `cd fleet-assist-copilot && docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d`.
3. Add both instances to the ALB target group; the ALB health check (`/health`) removes a sick instance automatically.
4. Load-test from your laptop (raise the limit for a test key first: `RATE_LIMIT_PER_MINUTE=100000` in `.env`
   on both instances and restart):

```bash
pip install httpx
python -m evals.load_test --url https://<your-domain> --api-key <key> --rps 12 --duration 600   # peak
python -m evals.load_test --url https://<your-domain> --api-key <key> --rps 25 --duration 300   # 2× peak
```

Pass = p95 ≤ 6 s and error rate ≤ 0.5%. The report also prints the daily volume the measured rate supports.

Notes:
* The per-key rate limit is kept in each worker's memory, so the effective limit is
  `RATE_LIMIT_PER_MINUTE × workers × instances`. Set it with that in mind.
* The Bedrock quota (step 2.4) is usually the first real limit, not the instances.

## 13. Updating documents or code

```bash
# new or changed manual: copy the file, add/edit its entry in config/manifest.yaml, then
docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d --build api
```

On start-up the API re-ingests only changed documents (the logs show `changed:` / `unchanged, skip:`).
Run `docker compose exec api python -m evals.run_evals --profile production` before announcing the change.

**Rollback:** keep the previous project folder (e.g. `fleet-assist-copilot-2026-09-28`), `cd` into it and run the
same `up -d --build` command.

## 14. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/health` 503 with `FC-5001` | index missing or unreadable | `docker compose logs api` — look at step [2/4]; check `data/documents` and `config/manifest.yaml` |
| `/health` 503 with `FC-5002` | index built with another embedding model | `docker compose exec api python -m app.ingestion.cli ingest --rebuild`, then restart |
| Answers have `"degraded":"extractive_fallback"` and logs show `AccessDeniedException` | Bedrock model access not granted, or IAM policy/region wrong | Step 2 and 3; check `AWS_REGION` |
| Logs show `ValidationException ... on-demand throughput isn't supported` | model must be called through an inference profile | put the `apac.` / `global.` inference-profile id in `BEDROCK_MODEL_ID` |
| Many `FC-3002` warnings | Bedrock throttling | request a higher quota (step 2.4) |
| Clients get HTTP 503 `FC-5004` | a worker has 32 requests in flight | add workers/instances, or raise `MAX_INFLIGHT_REQUESTS` carefully |
| Clients get HTTP 429 `FC-1004` | per-key rate limit | raise `RATE_LIMIT_PER_MINUTE` or give each client its own key |
| UI says "Cannot reach the API" | api container not healthy yet | wait for `STARTUP COMPLETE ✓`; `docker compose ps` |
| Container restarts, `Killed` in logs | out of memory | fewer `WORKERS` or a bigger instance (≈ 1 GB per worker) |

## 15. Cost control and clean-up

* Stop the instance when not in use (EC2 → Instance state → **Stop**); you pay only for storage.
* Set CloudWatch log retention: Log groups → `/fleet-copilot/prod` → **Edit retention** → 30 or 90 days.
* To remove everything: terminate the instance(s), delete the ALB and target group, the log group, the
  security group, the IAM role and policy.
