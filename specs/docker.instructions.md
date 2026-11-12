---
description: "Current production and development Compose topology, images, persistence, environment propagation, and container startup behavior."
applyTo: "docker-compose*.yml, .env.example, .env.dev, backend/Dockerfile, frontend/Dockerfile*, .github/workflows/docker-publish*.yml"
---

# Docker Runtime

## Production topology

`docker-compose.yml` defines:

- `postgres`: PostgreSQL 16 with authenticated health check.
- `redis`: Redis 7 with password and authenticated health check.
- `backend`: published FreeLingo image; waits for PostgreSQL and Redis, applies existing Alembic
  revisions, then starts Uvicorn.
- `frontend`: published FreeLingo image; exposes port 3000 and talks to backend through private
  `BACKEND_URL`.
- `kokoro`: local TTS GPU image, required only when `TTS_PROVIDER=local`.
- `whisper`: local STT GPU image, required only when `STT_PROVIDER=local`.

Ollama is not a Compose service. The default configuration expects it on the host through
`host.docker.internal:11434`. Production Compose does not declare an `extra_hosts` mapping. Linux
deployments using host-based Ollama must provide resolution for that hostname, for example through
`host.docker.internal:host-gateway` in the backend service's `extra_hosts`.

## Local source-built deployment

Merge `docker-compose.yml` with `docker-compose.local.yml` using Compose 2.24.4 or later. The overlay
uses the `freelingo-fork` project name, removes fixed container names, and uses persistent named
volumes. Both services build targets of the separate root `Dockerfile.local`.

The `patched-source` stage downloads the original repository at the immutable `UPSTREAM_REF` commit
and copies only the explicitly listed local replacement files into that source tree. The backend
target uses a digest-pinned upstream backend runtime, installs the pinned requirements (including
OAuth encryption support), then replaces application and migration files from the patched source.
The frontend target rebuilds the original frontend with the settings-page/catalog replacements;
compiled Next.js assets cannot be updated by copying TypeScript into a published runner image.

Keep the pinned source revision and `UPSTREAM_BACKEND_IMAGE` aligned when upgrading upstream. Builds
need network access for the original archive, images and packages. Images remain local; no fork GHCR
namespace or publication workflow is required. Upstream service Dockerfiles, workflows, production
Compose and development Compose remain original. Local builds do not invoke GitHub Actions; fork
owners who do not want automatic image publication or releases must leave Actions disabled.
Production Compose alone still selects the original upstream images without these local patches.
`gateway` binds only `127.0.0.1:3000`, routes API/OAuth and WebSockets to backend and normal pages to
frontend, and preserves long inference timeouts. Upstream names use Docker DNS re-resolution.
OAuth bootstrap/callback access logs are disabled, as are backend access logs in this local mode.
The internal port 9000 adapts Whisper's root redirect to the backend's readiness check. The local
speech configuration uses NVIDIA GPU images. Whisper inherits its image, model and engine settings
from upstream Compose without local device, quantization or model-cache overrides.

The overlay fixes local application, cookie/CORS and loopback callback URLs. ChatGPT sign-in targets
the machine running the gateway; it is not a remote multi-user OAuth deployment. The native panel
is served by Next.js and uses backend administrator APIs. See `services.instructions.md` for the
configuration and credential contract. Back up the encrypted AI volume together with `SECRET_KEY`,
in addition to application data. Changing/removing volumes is never part of stop.

`start.ps1` builds/starts this overlay from its own directory and reports container startup, not
provider/model readiness. Docker must already be available; selected local LLMs must be running.
`stop.ps1` stops this project's containers without deleting volumes or stopping shared Docker/Ollama.
Neither helper installs dependencies or modifies another deployment.

## Development topology

`docker-compose.dev.yml` defines PostgreSQL, Redis, a locally built backend with source mount and
Uvicorn reload, and a locally built frontend with source/message mounts and `npm run dev`.

The frontend is exposed at `http://localhost:3000`. The backend publishes port 8000 only on
`127.0.0.1`. Server-side frontend requests use `BACKEND_URL=http://backend:8000`; browser voice
connections use `NEXT_PUBLIC_API_URL=http://localhost:8000`, producing
`ws://localhost:8000/ws/conversation`. Next.js HTTP rewrites do not forward this WebSocket route.
`.env.dev` supplies localhost application and billing return URLs for this topology.

It does not define Kokoro or Whisper. Development must therefore select external/cloud speech
providers or compose the missing local services separately. Its default local speech hostnames are not
services contained in that file.

`frontend/Dockerfile.dev` is used only by development Compose. Publishing workflows use the production
backend and frontend Dockerfiles.

## Images and publication

Backend images use Python 3.14.9 and pip 26.2.2 with `requirements.txt` plus `constraints.txt`.
Frontend production/development images use Node 26.13.0 LTS, explicitly install npm 11.20.0, and use the
committed lockfile through `npm ci`. These exact runtime and package-manager versions match the local
toolchain and PR CI. Production uses Next.js standalone output.

The frontend builder places the frontend in `/app` and copies translation catalogs to `/app/messages`.
Next.js checks types with `frontend/tsconfig.build.json`, which excludes `tests/`; test imports resolve
against the monorepo layout and are checked by the full `tsconfig.json` in PR CI.

Push workflows for `main` and `develop` build and publish separate Linux `amd64` and `arm64` image
names with `latest` and short-SHA tags. They publish images only; they do not deploy to a VPS.

Publication uses GitHub-hosted `ubuntu-24.04` runners for `amd64`, preparation, and manifest merging,
and `ubuntu-24.04-arm` for `arm64`. The changelog release workflow also uses `ubuntu-24.04`.
These labels select the Ubuntu 24.04 LTS series; GitHub continues updating the runner images and
their preinstalled tools. Container base images remain defined by the Dockerfiles.

## Persistence

The original production/development Compose files use bind mounts below `DATA_PATH`; they do not
declare named volumes. The local overlay replaces these mounts with named volumes, so `start.ps1`
does not use `DATA_PATH` for storage. Keep the template value for base-file variable interpolation.

- PostgreSQL: `${DATA_PATH}/postgres`.
- Redis: `${DATA_PATH}/redis`.
- Avatars: `${DATA_PATH}/avatars`.
- Generated audio: `${DATA_PATH}/audio`.
- TTS previews: `${DATA_PATH}/tts_previews`.

Avatar and media access remains controlled by backend endpoints; a host mount does not make files
public.

## Environment propagation

`.env.example` is the operator-facing deployment template. Compose explicitly forwards environment
values; a field present in backend `Settings` but absent from Compose is not configurable merely by
placing it in `.env`.

Both Compose files forward optional `OPENAI_REASONING_EFFORT` with an empty default. It controls
`reasoning.effort` on OpenAI Responses requests only; nonempty values must be supported by the selected
model. An empty value leaves reasoning to the provider. `LLM_PROVIDER=openai` requires a model and
endpoint supporting Responses, not only Chat Completions.

The original production/development Compose files forward their original provider fields. The local
overlay alone forwards additional ChatGPT, request-timeout and lesson-quality options.
It sets `LLM_CONFIG_PATH=/data/llm/settings.json` and persists that path in a named volume. The supported
fork deployment uses this overlay; development files retain upstream configuration. Saved administrator
options override environment defaults; provider and credential rules belong to `services.instructions.md`.

Both Compose files forward `EXERCISE_GENERATION_TIMEOUT_SECONDS` with a default of 600. This positive
integer limits the complete Listening/Reading background job, including LLM output, JSON correction,
audio synthesis where applicable, and persistence. `.env.example` documents it for operators using
slow local models. The frontend receives the generation deadline through the API rather than a
separate environment variable.

Error Detective, Sentence Order, and Vocabulary Pairs share `EXERCISE_GENERATION_TIMEOUT_SECONDS`
for generation and semantic review.
Both Compose files forward `FREEMIUM_GAMES_DAILY` (default 3), also documented in `.env.example`.
Zero prevents new free games; subscriptions, active trials and Stripe-disabled deployments bypass it.
Game sessions, creation-request identities, and global admission reservations are persisted in
PostgreSQL. Games needs no extra service or volume.

Operators must review database/data path, Redis password, JWT secret, CORS/cookie security,
registration, email, available languages, LLM/speech providers, quotas, Stripe/freemium, and logging.

Both Compose files forward the optional RevenueCat settings exclusively to the backend:
`REVENUECAT_ENABLED` (default false), `REVENUECAT_API_KEY`, `REVENUECAT_WEBHOOK_AUTHORIZATION`,
`REVENUECAT_ENTITLEMENT_ID` (default `premium`), `REVENUECAT_ENVIRONMENT` (default `PRODUCTION`), and
`REVENUECAT_STRIPE_PUBLIC_API_KEY`. The integration only applies with `STRIPE_ENABLED=true`; it requires
no additional container or frontend build configuration. The backend checks required credentials at
startup. Provider connections, environment isolation, receipt imports, and the authority switch are
specified in `subscriptions-freemium.instructions.md`.

`BACKEND_URL` is the frontend's private backend-connectivity variable.

The production and development Compose files currently inject an `AVAILABLE_TARGET_LANGUAGES`
fallback containing only `en-US` and `en-GB` when the variable is absent, while `Settings` and
`.env.example` default to the complete supported set. Operators should provide the explicit value from
`.env.example`; the injected Compose value takes precedence.

`ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `RATE_LIMIT_ENABLED`, and
`AUDIO_STORAGE_PATH` exist in backend Settings but are not forwarded by the current Compose contract.
Their in-code defaults therefore apply in containers.

## Startup and migrations

Schema changes include their Alembic revision files in `backend/alembic/versions/`, linked to the
previous revision through `down_revision` and committed with the corresponding model changes.
The backend image includes these files. On deployment, the backend startup command automatically
applies pending revisions with `alembic upgrade head` before starting Uvicorn. Startup does not
generate revisions; preparing the versioned files is part of development, not a manual deployment step.

Offline SQL checks do not replace verification of an actual PostgreSQL upgrade; tests using
`metadata.create_all` bypass Alembic.

PR checks targeting `develop` apply the migration chain to an empty ephemeral PostgreSQL 16 database
before running backend tests. CI overrides Alembic's Docker-specific script location for that step;
the deployment configuration remains `/app/alembic`. The CI check covers fresh installation, not
data-dependent upgrades of existing deployments. See `testing.instructions.md`.

## GPU and provider selection

Production Compose declares NVIDIA reservations for Kokoro and Whisper. CPU-only operation requires
an appropriate upstream CPU image and removal of the GPU reservation; exact upstream tags are not a
stable FreeLingo contract.

When TTS or STT uses OpenAI, the corresponding local speech service is unnecessary. LLM, TTS, STT,
recognition-language, and provider HTTP contracts belong to `services.instructions.md` and
`speech-services.instructions.md`.

## Host requirements

- Set `vm.overcommit_memory=1` for reliable Redis background persistence.
- Production voice conversation requires HTTPS and a reverse proxy that forwards `/ws/*` to backend.
- Keep provider API keys and `CHANGE_ME_*` secrets out of version control.
- Keep backend/frontend runtime and package-manager versions aligned with their PR/publish workflows
  when intentionally upgrading them.

## Related specifications

- `.env.example`: deployable value reference.
- `architecture.instructions.md`: runtime boundaries.
- `speech-services.instructions.md`: provider contracts.
- `subscriptions-freemium.instructions.md`: billing/access configuration.
- `testing.instructions.md`: CI and local validation workflows.
