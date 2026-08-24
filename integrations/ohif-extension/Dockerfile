# syntax=docker/dockerfile:1.6
#
# LH-Radiology OHIF build.
#
# ARCHITECTURE (per docs/ohif-integration-approach.md addendum):
# There is no viable "thin-app" path -- OHIF does NOT publish a consumable
# app-shell npm package (@ohif/app doesn't exist; platform/app is application
# source living only inside the Viewers monorepo). So this Dockerfile clones
# the OHIF Viewers repo at the pinned release, drops our single extension
# package into the workspace, registers it in pluginConfig.json, and runs
# OHIF's own build. We are COMPOSING OHIF plus our plugin, not maintaining a
# fork (per Saptarshi's #21 review): a version bump is a dependency-tag bump
# on line ~30 below, nothing to rebase.
#
# HOW /reading GETS ROUTED (design note, see R2 doc addendum):
# We do NOT ship a custom OHIF "mode" for the reading worklist. OHIF modes
# are study-viewer wrappers: every mode route is nested under DataSourceWrapper
# and gated on `?StudyInstanceUIDs=...`, so a worklist-screen mode with no
# study renders blank. Instead the extension's preRegistration hook injects
# `/reading -> WorkList` via customizationService's `customRoutes` global
# customization (see platform/app/src/routes/index.tsx line ~55, which
# spreads customRoutes.routes into allRoutes OUTSIDE DataSourceWrapper).
# That's the intended extension point for app-level routes; no OHIF source
# is patched here.
#
# Stage 1 (builder) is heavy: it clones ~600 MB, installs Cornerstone3D's
# native deps, and runs the OHIF webpack build. First `docker build` will
# take 10-20 minutes; subsequent rebuilds hit the Yarn cache and are ~3-5
# minutes if only our extension source changed.
#
# Stage 2 (nginx) serves the built dist/. Same nginx config we use today for
# the stock ohif/app image (SPA index fallback, gzip_static, /dicom-web
# proxy to Orthanc). One workaround from the stock image can now be dropped:
# the __filename WASM shim in app-config.js. Left in for this MR to avoid
# scope creep; noted as a follow-up in the R2 doc addendum.

# ---------- Stage 1: build the OHIF viewer with our extension ---------------

FROM node:20-bookworm-slim AS builder

# Native deps Cornerstone3D pulls in during install (via node-gyp). Trimmed
# from the OHIF Viewers Dockerfile -- see their platform/app/Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# Pin the OHIF source to an exact commit. There is NO v3.6.5 git tag in
# OHIF/Viewers (v-prefixed tags stop at v3.6.0), even though Docker Hub has an
# ohif/app:v3.6.5 image, so cloning a tag with a branch fallback would quietly
# build whatever release/3.6 pointed at that day. We fetch the commit itself
# and a bad ref FAILS the build instead of falling back. Bumping OHIF is still
# a one line change: point OHIF_REF at a new commit or any fetchable ref.
# 72ec0bf is the release/3.6 HEAD this image was validated against.
ARG OHIF_REF=72ec0bffb533f883aac3b76d0ed9559c70fe5838
RUN git init -q . \
    && git remote add origin https://github.com/OHIF/Viewers.git \
    && git fetch --depth 1 origin "${OHIF_REF}" \
    && git checkout -q FETCH_HEAD

# Drop our extension into the workspace. Copying into extensions/ means Yarn
# workspaces picks it up automatically as `@lhrad/extension-worklist`. This
# is a single-package drop-in: no separate modes/ package, because /reading
# is registered via customizationService (see architecture note above), not
# via OHIF's mode registry.
COPY integrations/ohif-extension/package.json      /tmp/lhrad-src/
COPY integrations/ohif-extension/tsconfig.json     /tmp/lhrad-src/
COPY integrations/ohif-extension/src               /tmp/lhrad-src/src

# Extension package -- everything except test files. tsconfig comes along so
# OHIF's TypeScript build picks up our compiler options.
RUN mkdir -p extensions/lhrad-extension-worklist/src \
    && cp -r /tmp/lhrad-src/src/. extensions/lhrad-extension-worklist/src/ \
    && rm -rf extensions/lhrad-extension-worklist/src/__tests__ \
    && cp /tmp/lhrad-src/tsconfig.json extensions/lhrad-extension-worklist/

# package.json for the workspace extension. Generated here rather than
# committed to the repo, because our local single-package layout doesn't need
# it -- it exists only inside the OHIF workspace.
RUN cat > extensions/lhrad-extension-worklist/package.json <<'JSON'
{
  "name": "@lhrad/extension-worklist",
  "version": "0.1.0",
  "private": true,
  "main": "src/index.ts",
  "peerDependencies": {
    "@ohif/core": "*",
    "@ohif/ui": "*",
    "react": "17.x",
    "react-dom": "17.x"
  }
}
JSON

# The thin LH reading mode (#73/#74): wraps @ohif/mode-longitudinal and mounts
# the findings/report panels the default mode never does. Same drop-in pattern
# as the extension above; the mode is one file, the package.json is generated.
COPY integrations/ohif-extension/mode /tmp/lhrad-mode
RUN mkdir -p modes/lhrad-mode-reading/src \
    && cp /tmp/lhrad-mode/index.js modes/lhrad-mode-reading/src/index.js

RUN cat > modes/lhrad-mode-reading/package.json <<'JSON'
{
  "name": "@lhrad/mode-reading",
  "version": "0.1.0",
  "private": true,
  "main": "src/index.js",
  "peerDependencies": {
    "@ohif/core": "*",
    "@ohif/mode-longitudinal": "*"
  }
}
JSON

# Register our extension AND mode in pluginConfig.json. We rewrite the file
# with node rather than sed so we survive any whitespace variations in the
# upstream OHIF file. If OHIF changes the plugin config shape in a future
# release, this is where debugging starts.
#
# The /reading WORKLIST route stays a customizationService route (not a mode:
# mode routes gate on StudyInstanceUIDs). The /read VIEWER route is the mode.
RUN node -e ' \
  const fs = require("fs"); \
  const path = "platform/app/pluginConfig.json"; \
  const cfg = JSON.parse(fs.readFileSync(path, "utf8")); \
  cfg.extensions = cfg.extensions || []; \
  if (!cfg.extensions.some(e => (e.packageName||e) === "@lhrad/extension-worklist")) { \
    cfg.extensions.push({ packageName: "@lhrad/extension-worklist", version: "0.1.0" }); \
  } \
  cfg.modes = cfg.modes || []; \
  if (!cfg.modes.some(m => (m.packageName||m) === "@lhrad/mode-reading")) { \
    cfg.modes.push({ packageName: "@lhrad/mode-reading", version: "0.1.0" }); \
  } \
  fs.writeFileSync(path, JSON.stringify(cfg, null, 2)); \
  console.log("registered extensions:", cfg.extensions.map(e => e.packageName||e)); \
  console.log("registered modes:", cfg.modes.map(m => m.packageName||m)); \
'

# Install workspace deps. --frozen-lockfile is the OHIF-documented flag for
# reproducible builds against their pinned lockfile. Our new workspace package
# is private and its deps resolve internally -- the lockfile doesn't need to
# regenerate.
RUN yarn config set workspaces-experimental true \
    && yarn install --frozen-lockfile --network-timeout 600000

# Build the OHIF app. `yarn build` in the monorepo root runs the platform/app
# webpack production build; output lands in platform/app/dist. Our extension
# gets compiled in via pluginConfig.json.
ENV PUBLIC_URL=/
ENV APP_CONFIG=config/default.js
RUN yarn build

# ---------- Stage 1b: fetch the radiology module's vendor assets (#115) ------
#
# The lh-radiology o3 omod ships WITHOUT its bundled vendor assets (jquery,
# jquery-migrate, moment, tinymce, datatables, font-awesome, date-range-picker),
# so every /openmrs/moduleResources/radiology/vendor/* URL 404s under a plain
# `docker compose up -d` and every RIS dashboard and report form dies on
# "jQuery is not defined". The real fix is the omod build itself (sibling
# lh-radiology repo, filed as #115) shipping `src/main/webapp/resources/vendor/**`
# in the WAR; this stage bakes the pinned vendor set into our own nginx layer
# so the plain stack works too, not just the #75 hosted TLS overlay that
# fronts OpenMRS with Caddy.
#
# Reuses docker/caddy/fetch-radiology-vendor.sh verbatim so the plain-stack
# workaround and the overlay workaround pin the same versions. One place to
# bump when the omod finally ships them again -- and one place to delete when
# the sibling fix lands.

FROM alpine:3.20 AS radiology-vendor-fetch
RUN apk add --no-cache bash curl ca-certificates
WORKDIR /fetch
COPY docker/caddy/fetch-radiology-vendor.sh /fetch/fetch-radiology-vendor.sh
# Strip CRLF before executing: a Windows-checkout of the repo (git's autocrlf) ships this
# script with CRLF line endings, and bash on Alpine treats the trailing \r as part of the
# option name, so `set -euo pipefail\r` fails with "invalid option name". `tr` normalises
# in place so the script runs identically whether the repo was checked out on Windows or
# Linux/macOS. Idempotent for LF-only files.
RUN sed -i 's/\r$//' /fetch/fetch-radiology-vendor.sh \
    && chmod +x /fetch/fetch-radiology-vendor.sh \
    && bash /fetch/fetch-radiology-vendor.sh
# The fetch script writes to `radiology-vendor/` in its own working directory,
# so the assets end up at /fetch/radiology-vendor/{jquery,moment,tinymce,...}.

# ---------- Stage 2: serve the built dist ------------------------------------

FROM nginx:1.27-alpine

# Same nginx config we use today with the stock image -- SPA index fallback,
# gzip_static (the OHIF build ships pre-compressed assets), and the /dicom-web
# and /wado proxies to Orthanc. Kept as a bind mount in docker-compose so we
# can edit routing without rebuilding the image.
COPY --from=builder /work/platform/app/dist /usr/share/nginx/html
# app-config.js is bind-mounted at runtime by docker-compose. Leave the built
# copy as a fallback so a docker run without the bind mount is still usable.

# #115: the radiology module's vendor assets, served locally by the /openmrs/
# moduleResources/radiology/vendor/ location block in default.conf. See the
# radiology-vendor-fetch stage above for the rationale; the location block
# rewrites the URL path so a mismatched destination directory (e.g. under
# /usr/share/nginx/html/vendor) would 404 every asset silently.
COPY --from=radiology-vendor-fetch /fetch/radiology-vendor /usr/share/nginx/html/radiology-vendor

EXPOSE 80