# MIMIC-CXR showcase ETL tooling (#68)

Loads a curated ~100-study MIMIC-CXR cohort into the demo stack. Design + write-path rationale:
`docs/mimic-cxr-mapping.md`. **No MIMIC data or manifests live in this repo (PhysioNet DUA)** --
only the tooling and a synthetic `sample_cohort.json`.

## Two roles

The cohort is curated + fetched from PhysioNet **once** by a curator, published to a shared
credentialed store, and everyone else **pulls** from there. Nobody re-downloads the 4.7 TB source.

- **Curator** (steps 1a-3 below, then `share_cohort.py publish`): needs PhysioNet AWS access.
- **Every other dev** (`share_cohort.py pull`, then load): needs only read access to the shared
  store. They must still be individually PhysioNet-credentialed for **both** MIMIC-CXR and MIMIC-IV;
  the store's ACLs must enforce that. See "Sharing" below.

## Prerequisites

- `pip install -r requirements.txt` (pydicom, boto3, httpx, pymysql).
- Curator only: credentialed PhysioNet AWS access for `fetch.py` (MIMIC-CXR **and** MIMIC-IV DUAs).
- The concepts the demo dictionary lacks: run `bootstrap_radiology_concept.py` (provisions the
  chest-x-ray order/report concept + numeric creatinine/eGFR lab concepts), then set
  `MIMIC_ORDER_CONCEPT_UUID` to the printed Chest-radiograph UUID.
- Run on the compose network (mariadb/openmrs reachable by service name), e.g. as a one-shot
  container; do NOT publish the DB port.

## Flow

```bash
# 0. provision the concepts the demo dictionary lacks (CXR order/report + numeric labs); prints the UUID
python bootstrap_radiology_concept.py           # -> set MIMIC_ORDER_CONCEPT_UUID to the printed value

# 1a. curate the ~100-study cohort manifest from the label CSVs + reports + MIMIC-IV slices
#     (composition: 30 normal / 25 pneumothorax / 20 effusion-consolidation-edema / 15 priors /
#      10 portable; concordant chexpert+negbio labels only; manifest stays off-repo, DUA)
python curate_cohort.py --cxr-root /secure/mimic-cxr --reports-root /secure/mimic-cxr-reports \
    --mimic-iv-root /secure/mimic-iv --out /secure/cohort/my_cohort.json

# 1b. fetch only the cohort's studies from PhysioNet S3 (off-repo dest, DUA). For the PhysioNet
#     S3 ACCESS POINTS, pass the ARN as the bucket and the project key prefix:
#       MIMIC_CXR_BUCKET=arn:aws:s3:us-east-1:724665945834:accesspoint/mimic-cxr-v2-1-0-01\
#       MIMIC_CXR_KEY_PREFIX=mimic-cxr/2.1.0/  AWS_PROFILE=physionet  python fetch.py ...
python fetch.py /secure/cohort/my_cohort.json /secure/mimic-dl

# 1c. CURATOR: publish the curated cohort to the shared credentialed store (see "Sharing"). Every
#     other dev pulls from there instead of running 1a/1b.
python share_cohort.py publish --manifest /secure/cohort/my_cohort.json \
    --dicom-root /secure/mimic-dl --share-root $MIMIC_SHARE_ROOT --name v1

# 2. FHIR first: patients, encounters, RadiologyOrders (with ICD-10-mapped order reasons + a real
#    referring-physician orderer, #76), EHR packet (labs, problems, presence-only drug orders),
#    seeded preliminary reports
python load_cohort.py my_cohort.json --concept $MIMIC_ORDER_CONCEPT_UUID

# 3. fix up + push DICOM (per study: accession = study_id), which starts each workflow
python dicom_fixup.py /secure/mimic-dl/.../s56699142/xxx.dcm s56699142   # then POST to Orthanc

# 4. export the loaded (modality, StudyDescription) corpus for the registry selection test (#64)
python registry_corpus.py --out registry_corpus.json

# 5. rehearsal sign-off cue (live demo: radiologists sign in the RIS instead)
python report_seeder.py finalize s56699142
```

## Referring physicians (#76)

`load_cohort.py` seeds a small roster of demo referring physicians (`referrers.py`) and stamps each
study's **order with a real ordering provider** rather than the ETL admin account. fhir2 surfaces
that as `ServiceRequest.requester`, so `comms.resolve_ordering_provider` names a real physician and
the critical-result notification (`ehr-inbox` channel, #76) lands on the ordering patient's chart for
them to acknowledge (`docs/ehr-inbox-notification.md`). Assignment is deterministic per `subject_id`,
so a patient's studies share one referrer and re-runs stay stable.

Each referrer also gets an OpenMRS login (best-effort) so the physician can sign in at the ack
surface (#86); a login-provisioning failure never costs the requester seeding — the order still
carries a real requester, only the in-EHR login for that physician degrades (logged). Config:

- `MIMIC_REFERRER_PASSWORD` — demo login password (default clears the OpenMRS policy; override per
  deployment; never a real secret).
- `MIMIC_REFERRER_ROLES` — comma-separated OpenMRS roles for the login (default `Provider`); set to a
  role that grants patient-chart view if the notification must be visible after the physician logs in.

The referrer is **not** written into the comms ledger — the ledger holds only the on-call directory
(`docker/comms-ledger/seed_oncall.py`); the ordering physician's reference is carried verbatim from
fhir2 onto the ledger's `Communication.recipient` / `Task.owner`.

## Sharing (other devs pull, never re-download)

The curated cohort lives once on a shared **credentialed** store (IU Slate-Project / Geode / RED, or
another access-controlled mount). Point `MIMIC_SHARE_ROOT` at it. Every dev with read access pulls
the manifest + DICOMs and loads locally:

```bash
export MIMIC_SHARE_ROOT=/geode2/projects/<proj>/mimic-showcase   # the shared mount
python share_cohort.py pull --name v1 --dest ~/mimic-secure       # rsync + SHA256 verify
python load_cohort.py ~/mimic-secure/cohort/v1/manifest.json --concept $MIMIC_ORDER_CONCEPT_UUID
# DICOMs are under ~/mimic-secure/cohort/v1/dicom/files/... -> dicom_fixup + push to Orthanc
```

### Faster: restore the loaded DB instead of loading it

The share can carry `openmrs-seed.sql.gz`, an OpenMRS database with the cohort already loaded.
Measured when it was built: a clean boot plus `load_cohort` is ~23 minutes (the boot is nearly all
of it); restoring the seed is about one. Use it for a new test server or a throwaway stack; use the
manifest path when you need to change what gets loaded.

```bash
docker compose down && docker volume rm <project>_mariadb-data      # the seed loads into an EMPTY volume
cp ~/mimic-secure/cohort/v1/openmrs-seed.sql.gz docker/openmrs/seed/openmrs-seed.sql.gz
docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d
python cohort_audit.py --manifest ~/mimic-secure/cohort/v1/manifest.json   # 82 OK / 18 TRUNCATED
```

The audit is not optional ceremony: it is how we found, on the demo host, 10 studies whose
narrative had been replaced by rehearsal text and 3 that had lost their IMPRESSION section. The 18
TRUNCATED are expected -- those narratives exceed fhir2's 1024-char `conclusion` cap and the ETL
clamps them (#105).

Building a fresh seed (curator): clean boot with NO seed overlay, `bootstrap_radiology_concept.py`,
`load_cohort.py`, `cohort_audit.py` to verify, then `scripts/dump_openmrs_seed.sh <out>`. Since #101
a clean boot generates no reference demo patients, so the dump holds the cohort and nothing else --
the one built on 2026-08-07 was 3.0 MB against 6.6 MB for a drifted host DB.

```bash
```

`pull` uses rsync (resumable) and verifies every file against the published `SHA256SUMS`. **DUA:**
the shared root must be readable ONLY by team members individually credentialed for BOTH MIMIC-CXR
and MIMIC-IV; the store's ACLs enforce that, the tool cannot. Nothing MIMIC ever lands in the repo.

### OneDrive / SharePoint (IU Secure M365)

Works via the OneDrive sync client: point `--share-root` at a folder inside the local mount
(macOS: `~/Library/CloudStorage/OneDrive-IndianaUniversity/<library>/mimic-showcase`) and add
`--cloud`, which drops the POSIX perms/symlink metadata the CloudStorage filesystem rejects:

```bash
python share_cohort.py publish --cloud --name v1 \
    --manifest /secure/cohort/showcase_cohort.json --dicom-root /secure/mimic-dl \
    --share-root ~/Library/CloudStorage/OneDrive-IndianaUniversity/mimic-showcase
# then WAIT for the sync client to finish uploading before anyone pulls
python share_cohort.py pull --cloud --name v1 \
    --share-root ~/Library/CloudStorage/OneDrive-IndianaUniversity/mimic-showcase --dest ~/mimic-secure
```

Notes: files may arrive as Files-On-Demand placeholders; the `pull` checksum pass forces full
hydration, so let sync finish first. Keep the share name short (SharePoint path-length limits vs the
deep `files/pXX/...` DICOM nesting). **Confirm with IU's data steward / UISO that Secure M365 is an
approved location for PhysioNet credentialed data before using it** -- the tool cannot certify that.

## Proven

The join-critical path is verified end to end on the o3 stack: loader-created RadiologyOrder ->
DICOM -> orchestrator resolves it (triage URGENT from a `stat` order) -> read gate -> `finalize`
flips the seeded report to final -> poller releases the gate. See `docs/mimic-cxr-mapping.md`.

Tests (no stack, no data): `python -m pytest scripts/mimic/tests -q`.
