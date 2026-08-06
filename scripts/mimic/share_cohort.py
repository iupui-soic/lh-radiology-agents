"""Publish/pull the curated MIMIC showcase cohort to a shared credentialed location (#68).

The point: curate + fetch from PhysioNet ONCE, land the result on an access-controlled shared
store, and let every other credentialed dev `pull` from there instead of re-downloading the 4.7 TB
source. The manifest is the small reproducible recipe; the DICOMs are the bulk; both are shared.

DUA: MIMIC report text (in the manifest) and the DICOMs are credentialed data. The shared root MUST
be readable only by team members who are individually PhysioNet-credentialed for BOTH MIMIC-CXR and
MIMIC-IV. This tool refuses to publish into the git repo, and prints the DUA boundary on publish; it
cannot enforce the store's ACLs -- that is the operator's responsibility.

Shared root: a mounted path on the credentialed store (IU Slate-Project / Geode / RED, an NFS/SMB
mount, or a Globus-synced dir). Pass --share-root or set MIMIC_SHARE_ROOT. rsync is used when present
(resumable, checksummed); otherwise a checksum-verified copy.

Layout under <share-root>/<cohort-name>/:
  manifest.json          the cohort manifest (the recipe)
  dicom/files/pXX/...     the fetched, fixed-up DICOMs (same layout fetch.py writes)
  inputs/                 optional: curation-input CSVs, so a dev can re-curate without PhysioNet
  openmrs-seed.sql.gz     optional: a loaded OpenMRS DB, so a dev restores in minutes instead of
                          booting clean and running the ETL (build it with a clean boot + load_cohort,
                          verify with cohort_audit.py, dump with scripts/dump_openmrs_seed.sh)
  SHA256SUMS             integrity manifest over every shared file
  SHARE.json            provenance: cohort name, counts, tool version, source notes
  README.txt            the DUA boundary, in the share itself

Publish (curator, once):
  python share_cohort.py publish --manifest /secure/cohort/showcase_cohort.json \
      --dicom-root /secure/mimic-dl --share-root /geode2/projects/<proj>/mimic-showcase --name v1

Pull (each credentialed dev):
  python share_cohort.py pull --share-root /geode2/projects/<proj>/mimic-showcase --name v1 \
      --dest ~/mimic-secure
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

DUA_NOTICE = (
    "PhysioNet credentialed data (MIMIC-CXR + MIMIC-IV). Access is restricted to individually\n"
    "credentialed team members. Do NOT copy outside this access-controlled store, and do NOT\n"
    "share with anyone not credentialed for BOTH projects. Redistribution breaches the DUA."
)


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _inside_repo(path: str) -> bool:
    return os.path.abspath(path).startswith(_repo_root() + os.sep)


def _have_rsync() -> bool:
    return shutil.which("rsync") is not None


def _sync_tree(src: str, dst: str, cloud: bool = False) -> None:
    """Mirror src/ -> dst/ (resumable). rsync when available, else a plain recursive copy.

    cloud=True targets a sync-client folder (OneDrive/SharePoint via ~/Library/CloudStorage, or a
    Dropbox/Box mount): the CloudStorage filesystem rejects the POSIX perms/owner/symlink metadata
    that `rsync -a` sets, so drop those and copy contents only (-rt). DICOM/JSON carry no symlinks,
    so nothing is lost."""
    os.makedirs(dst, exist_ok=True)
    if _have_rsync():
        flags = ["-rt", "--partial"] if cloud else ["-a", "--partial"]
        if cloud:
            flags += ["--no-perms", "--no-owner", "--no-group", "--no-links"]
        subprocess.run(["rsync", *flags, src.rstrip("/") + "/", dst.rstrip("/") + "/"], check=True)
    else:
        shutil.copytree(src, dst, dirs_exist_ok=True)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root)


def _write_sumfile(root: str, sumpath: str) -> int:
    n = 0
    with open(sumpath, "w", encoding="utf-8") as out:
        for full, rel in _walk_files(root):
            if os.path.abspath(full) == os.path.abspath(sumpath):
                continue
            out.write(f"{_sha256(full)}  {rel}\n")
            n += 1
    return n


def _verify_sumfile(root: str, sumpath: str) -> tuple:
    ok, bad, missing = 0, [], []
    with open(sumpath, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, rel = line.split("  ", 1)
            full = os.path.join(root, rel)
            if not os.path.exists(full):
                missing.append(rel)
            elif _sha256(full) == digest:
                ok += 1
            else:
                bad.append(rel)
    return ok, bad, missing


def _dest_check(path: str, p) -> str:
    if _inside_repo(path):
        p.error(f"{path} is inside the repo; MIMIC content is DUA-restricted, keep it off the repo")
    return os.path.abspath(path)


def publish(args, p) -> int:
    share_root = _dest_check(args.share_root, p)
    if not os.path.exists(args.manifest):
        p.error(f"manifest not found: {args.manifest}")
    studies = json.load(open(args.manifest)).get("studies", [])
    target = os.path.join(share_root, args.name)
    os.makedirs(target, exist_ok=True)

    shutil.copy2(args.manifest, os.path.join(target, "manifest.json"))
    if args.dicom_root and os.path.isdir(args.dicom_root):
        _sync_tree(args.dicom_root, os.path.join(target, "dicom"), cloud=args.cloud)
    if args.inputs_root and os.path.isdir(args.inputs_root):
        _sync_tree(args.inputs_root, os.path.join(target, "inputs"), cloud=args.cloud)
    # The seed is the recipe's OUTPUT, shared beside the recipe rather than instead of it: a clean
    # boot plus the ETL is ~23 minutes, restoring this is ~1. It carries MIMIC narratives, so it is
    # credentialed data like everything else here and never goes in the repo.
    if args.seed and os.path.isfile(args.seed):
        shutil.copy2(args.seed, os.path.join(target, "openmrs-seed.sql.gz"))

    provenance = {
        "name": args.name,
        "studies": len(studies),
        "with_reason_codes": sum(1 for s in studies if s.get("reason_codes")),
        "with_meds": sum(1 for s in studies if s.get("meds")),
        "dicom_included": bool(args.dicom_root and os.path.isdir(args.dicom_root)),
        "inputs_included": bool(args.inputs_root and os.path.isdir(args.inputs_root)),
        "openmrs_seed_included": bool(args.seed and os.path.isfile(args.seed)),
        "source": "PhysioNet MIMIC-CXR v2.1.0 + MIMIC-IV v3.1 (curated by scripts/mimic/#68)",
    }
    with open(os.path.join(target, "SHARE.json"), "w", encoding="utf-8") as f:
        json.dump(provenance, f, indent=2)
    with open(os.path.join(target, "README.txt"), "w", encoding="utf-8") as f:
        f.write(DUA_NOTICE + "\n")

    count = _write_sumfile(target, os.path.join(target, "SHA256SUMS"))
    print(f"published '{args.name}' to {target}")
    print(f"  studies: {len(studies)} | files checksummed: {count}")
    print(f"  dicom: {'yes' if provenance['dicom_included'] else 'no'} | "
          f"inputs: {'yes' if provenance['inputs_included'] else 'no'} | "
          f"openmrs seed: {'yes' if provenance['openmrs_seed_included'] else 'no'}")
    if args.cloud:
        print("  cloud mode: wait for the sync client to finish UPLOADING before anyone pulls.")
    print("\n" + DUA_NOTICE)
    print(f"\nEnsure {share_root} is readable ONLY by credentialed team members (chmod/ACLs).")
    return 0


def pull(args, p) -> int:
    share_root = os.path.abspath(args.share_root)
    dest = _dest_check(args.dest, p)
    source = os.path.join(share_root, args.name)
    if not os.path.isdir(source):
        p.error(f"cohort '{args.name}' not found under {share_root}")
    local = os.path.join(dest, "cohort", args.name)
    _sync_tree(source, local, cloud=args.cloud)

    sumpath = os.path.join(local, "SHA256SUMS")
    if os.path.exists(sumpath):
        ok, bad, missing = _verify_sumfile(local, sumpath)
        status = "OK" if not bad and not missing else "MISMATCH"
        print(f"pulled '{args.name}' to {local}")
        print(f"  integrity: {status} ({ok} verified, {len(bad)} changed, {len(missing)} missing)")
        if bad or missing:
            for rel in (bad + missing)[:10]:
                print(f"    ! {rel}")
            return 1
    else:
        print(f"pulled '{args.name}' to {local} (no SHA256SUMS to verify)")
    manifest = os.path.join(local, "manifest.json")
    if os.path.exists(manifest):
        n = len(json.load(open(manifest)).get("studies", []))
        print(f"  manifest: {n} studies -> load with scripts/mimic/load_cohort.py")
    seed = os.path.join(local, "openmrs-seed.sql.gz")
    if os.path.exists(seed):
        # Say it plainly, because the fast path is the whole reason the seed is in the share: a
        # clean boot plus the ETL took ~23 minutes when this was built, a restore takes about one.
        print(f"  openmrs seed: {seed}")
        print("    restore instead of booting clean + loading:")
        print("      docker compose down && docker volume rm <project>_mariadb-data")
        print("      cp <seed> docker/openmrs/seed/openmrs-seed.sql.gz")
        print("      docker compose -f docker-compose.yml -f docker-compose.seed.yml up -d")
        print("    then verify: scripts/mimic/cohort_audit.py --manifest manifest.json")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Publish/pull the shared MIMIC showcase cohort (#68).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="curator: push manifest + DICOMs to the shared store")
    pub.add_argument("--manifest", required=True)
    pub.add_argument("--dicom-root", default="", help="fetched DICOM tree (fetch.py dest)")
    pub.add_argument("--inputs-root", default="", help="optional: curation-input CSVs")
    pub.add_argument("--seed", default="",
                     help="optional: a dumped OpenMRS DB (openmrs-seed.sql.gz) with the cohort "
                          "already loaded, for restore-in-minutes instead of a clean boot + ETL")
    pub.add_argument("--share-root", default=os.environ.get("MIMIC_SHARE_ROOT", ""))
    pub.add_argument("--name", default="v1", help="cohort version name under the share root")
    pub.add_argument("--cloud", action="store_true",
                     help="share root is a sync-client folder (OneDrive/SharePoint/Box): skip "
                          "POSIX perms/symlinks the CloudStorage filesystem rejects")

    pl = sub.add_parser("pull", help="dev: sync the shared cohort to a local dir")
    pl.add_argument("--share-root", default=os.environ.get("MIMIC_SHARE_ROOT", ""))
    pl.add_argument("--name", default="v1")
    pl.add_argument("--dest", default=os.path.expanduser("~/mimic-secure"))
    pl.add_argument("--cloud", action="store_true",
                    help="share root is a sync-client folder (see publish --cloud)")

    args = p.parse_args(argv)
    if not args.share_root:
        p.error("no shared location: pass --share-root or set MIMIC_SHARE_ROOT")
    return publish(args, p) if args.cmd == "publish" else pull(args, p)


if __name__ == "__main__":
    raise SystemExit(main())
