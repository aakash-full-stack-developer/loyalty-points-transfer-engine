# Loyalty Points Transfer Engine

A microservice for bi-directional point transfers between credit-card reward programs and
travel loyalty programs. Built with Python 3.12, FastAPI, PostgreSQL and Redis.

> Work in progress. The full README (architecture, API reference, design decisions) is
> written once the implementation is complete. The design brief is in
> [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md).

## Quick start

Requirements: Docker with Docker Compose v2, and `make`.

```bash
cp .env.example .env
make build
make up          # starts postgres, redis, partner-simulator, api, worker and waits until healthy
make ps
```

Check that the services are running:

```bash
curl localhost:8000/health/ready
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}

curl localhost:8001/health
# {"status":"ok","service":"partner-simulator"}
```

Interactive API docs: http://localhost:8000/docs

Run `make` to list every available command.
