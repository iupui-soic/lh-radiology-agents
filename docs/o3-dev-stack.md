# Running the OpenMRS o3 backend (dev stack)

The `openmrs` service in `docker-compose.yml` is the LibreHealth Radiology o3
backend (the OpenMRS reference application with the radiology omod baked in).
A few things about bringing it up locally.

## First boot is slow, and that is expected

A clean first boot runs Liquibase (~1800 changesets), imports the concept
dictionary (~4200 concepts), and starts every module. That takes:

- **~10 minutes on native arm64** (Apple Silicon).
- **~18 to 23 minutes under emulation** (an amd64 image on an arm64 host).

The `:o3` image is published multi-arch (amd64 + arm64), so `docker compose up`
pulls the native variant per host and avoids the emulation tax. If a stale
amd64 `:o3` is already cached locally, force the arm64 variant once:

```
docker pull --platform linux/arm64 registry.gitlab.com/librehealth/radiology/lh-radiology/o3:o3
```

`docker compose pull` will NOT replace a locally cached image, because the
service uses `pull_policy: missing`.

## Readiness: 302 means "still booting", 200 means "ready"

For the entire boot, OpenMRS 302-redirects every request to
`/openmrs/initialsetup`. That is normal and clears only when startup fully
completes. A plain `curl -f` treats that 302 as success, so the compose
healthcheck instead gates on the session endpoint returning **200**. That makes
`depends_on: service_healthy` wait for a usable server, not just a listening
one. To check by hand:

```
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/openmrs/ws/rest/v1/session
# 302 -> still booting;  200 -> ready
```

## Clean boot is the supported path; do not reuse the volume across restarts

Bring the stack up from empty volumes:

```
docker compose down -v        # dev machine only -- see the demo-host caveat below
docker compose up -d mariadb openmrs
```

On the **demo host**, `-v` wipes the comms-ledger audit trail and the ingress store
(see Demo host ops); use the selective path in the seed section instead.

Recreating the `openmrs` container against an **existing** `mariadb-data` volume
is not supported. The initializer and OpenConceptLab modules re-load reference
data, hit `already exists` validation errors, abort module startup, and leave
the server stuck at `/initialsetup`. If a stack gets wedged that way, clear
ONLY the OpenMRS database and reboot it from the seed (the volumes worth protecting
survive):

```
docker compose down
docker volume rm lh-radiology-agents_mariadb-data   # volume name = <compose project>_mariadb-data
docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d
```

No seed captured yet? Then there is nothing to overlay: remove the mariadb volume the
same way, `docker compose up -d mariadb openmrs`, pay the ~16-minute first boot once,
and capture the seed at the end of it (`scripts/dump_openmrs_seed.sh`) so the next
recovery is fast. A full `docker compose down -v` also clears the wedge, but it wipes
the comms-ledger audit trail and the ingress store with it -- on the demo host it is
the last resort.

Making the module reference-data load idempotent so a restart does not collide
would remove this constraint. That is an o3-image change (sibling `lh-radiology`
repo) and is tracked separately.

## Fast restore from a seed (skip the ~16 min first boot)

The wedge above is why you cannot just keep the volume. The supported shortcut is a
**seed snapshot**: capture the finished DB once, then reload it into a fresh volume
with Liquibase migration off, so a boot is the module-load pass only.

```
scripts/dump_openmrs_seed.sh          # once, from a healthy clean boot -> docker/openmrs/seed/*.sql.gz
docker compose down                                 # keeps every named volume
docker volume rm lh-radiology-agents_mariadb-data   # ONLY the OpenMRS DB (docker volume ls | grep mariadb)
docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d
```

The seed only needs the **mariadb volume** empty. On the demo host, do NOT pay for that
with `-v` (which also wipes the comms ledger and the ingress store — see Demo host ops):

```
docker compose down
docker volume rm <compose-project>_mariadb-data     # docker volume ls to find the prefix
docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d
```

Verified: a seeded boot reaches a usable server (200) in ~6 min vs ~16 min clean, and does
NOT hit the initializer wedge (the seed carries the initializer/OCL tracking rows, so their
loaders find everything already present). The seed blob is data, not code, and is gitignored
(it will carry MIMIC/PHI once the #68 cohort is loaded). See `docker-compose.seed.yml`. The
durable fix is still the idempotent reference-data load noted above (#72).

## Demo host ops (#72)

For the hosted showcase the stack must survive restarts without losing clinical
state, so the compose file was hardened (#72):

- **Durable state lives on named volumes — do NOT `down -v` casually on the demo
  host.** `-v` deletes volumes, which now include:
  - `ingress-store-data` — the orchestrator's report→workflow join index, poll
    cursor, and dead letters (`INGRESS_STORE_PATH`). Losing it mid-read-gate means
    a radiologist signs, the poller matches nothing, and the study waits forever.
  - `comms-ledger-data` — the comms ledger's file-based H2, the
    clinical-communication audit trail.
  - `mariadb-data`, `orthanc-db`, `temporal-pg-data`, `worklist-api-db` as before.
  A plain `docker compose down` (no `-v`) keeps all of these volumes, so the DURABLE
  services (orthanc, the ledger, temporal, the ingress store) resume where they left
  off — but `openmrs` does NOT: `up` recreates its container against the populated
  `mariadb-data` volume, which is exactly the wedge above. After a full `down`, bring
  OpenMRS back wedge-aware **without sacrificing the other volumes**: remove ONLY the
  mariadb volume, then boot the seed overlay (the selective path in the seed section
  above). `down -v` also works but wipes the comms ledger and ingress store — the
  clinical state this hardening exists to protect — so on the demo host it is the
  last resort, not the routine.

- **The OpenMRS/mariadb wedge still applies** (see the section above): never
  recreate `openmrs` against a reused `mariadb-data` volume. When you deliberately
  want a WHOLE-STACK clean slate, `down -v` and boot clean — knowing that clears the
  ingress store and comms ledger too. For an OpenMRS-only reset, remove just
  `mariadb-data` and use the seed overlay.

- **Restart policy.** The long-lived app services (`orthanc`, `ohif`, `orchestrator`,
  `comms-ledger`, `worklist-api`) run `restart: unless-stopped`, so the demo host
  recovers them across a crash or a host reboot without intervention. **`openmrs` and
  `mariadb` deliberately carry NO restart policy**: an automatic restart re-runs the
  OpenMRS module startup against the populated volume, which is the wedge described
  above — after a host reboot, bring them back deliberately (clean boot, or the seed
  fast-restore) rather than letting Docker loop them into a wedged state. The
  one-shots (`presign-concept-bootstrap`, `comms-ledger-init`) are `restart: "no"` on
  purpose — each runs to completion every `up` and is idempotent. The Temporal trio
  (`temporal`, `temporal-postgresql`, `temporal-ui`) and the five A2A agents also have
  no restart policy: after a reboot they stay down until `docker compose up`, which the
  reboot already requires for openmrs/mariadb — Temporal durability resumes every
  workflow once they return, so nothing is lost, only paused.

- **Images are pinned to digests** (`name:tag@sha256:...`) so the demo host cannot
  drift under a re-pushed tag. Bump a digest deliberately when adopting a new build;
  `jaeger` (opt-in `otel` profile) keeps an explicit version tag. **Exception: the
  `openmrs` (o3) image is pinned by an immutable per-commit TAG**, not a digest.
  o3:o3 is a moving tag whose superseded manifests GitLab's registry garbage-collects,
  so a digest pin there 404s on any fresh host after the next upstream push. lh-radiology
  CI publishes `o3:<branch>-<short-sha>` alongside the moving tag and never re-pushes it,
  so that is what `docker-compose.yml` rides (see the comment there for why a `@sha256`
  suffix cannot be used on a service that also has a `build:` block).
