"""Provision the concepts the MIMIC ETL needs but the demo dictionary lacks (#68).

The o3 demo dictionary has NO chest-x-ray procedure and NO numeric lab concepts, so an order/report
cannot be coded and labs cannot be stored. This bootstrap inserts, at STABLE UUID5s (so config can
reference fixed values across deployments):
  - "Chest radiograph"  -> the order + DiagnosticReport concept (set MIMIC_ORDER_CONCEPT_UUID to it)
  - "Serum creatinine", "Estimated GFR" -> Numeric lab concepts for the EHR packet (creatinine/eGFR),
    each carrying SAME-AS LOINC reference maps for every code in LAB_LOINC_TO_CONCEPT. The maps are
    not decoration: the EHR Assistant finds labs by `code=http://loinc.org|<code>`, so a lab concept
    with no LOINC coding is one whose observations the assistant can never see (#84).

Direct SQL, mirroring docker/openmrs/bootstrap_presign_concept.py: the OpenMRS REST concept endpoint
auto-assigns UUIDs and will not honour a caller-supplied one, and a stable UUID is exactly what makes
MIMIC_ORDER_CONCEPT_UUID a fixed config value. Idempotent: a concept already present at its UUID is
left untouched. Run once before load_cohort (as a one-shot container on the compose network).

Loader coupling: load_cohort imports LAB_LOINC_TO_CONCEPT from here to map the manifest's LOINC lab
codes onto these concept UUIDs.
"""
from __future__ import annotations
import argparse
import os
import sys
import uuid

# pymysql is imported lazily in main() so load_cohort can import the concept UUIDs / LOINC map
# below without pulling in the DB driver.

# --- stable UUID5s (regenerable from the same seeds) -------------------------
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "librehealth.org")


def _u(seed: str) -> str:
    return str(uuid.uuid5(_NS, seed))


CHEST_RADIOGRAPH_UUID = _u("lh-radiology.mimic.chest-radiograph.v1")
CREATININE_UUID = _u("lh-radiology.mimic.serum-creatinine.v1")
EGFR_UUID = _u("lh-radiology.mimic.egfr.v1")

# LOINC -> provisioned concept, for the loader's EHR labs. Every code here is also given a
# SAME-AS reference map on its concept by provision(), because the EHR Assistant searches
# `http://loinc.org|<code>` and a concept with no LOINC coding can never match (#84).
#
# Between this map and LAB_LOINC_DELIBERATELY_UNMAPPED below, every code in
# `agents/ehr-assistant/handler.py::_LAB_LOINCS` must be accounted for: a panel code that is
# neither mapped nor deliberately exempted is a lab the assistant silently never sees.
# test_lab_loinc_maps guards the lists against drift.
LAB_LOINC_TO_CONCEPT = {
    "2160-0": CREATININE_UUID,                 # Creatinine [Mass/volume] in Serum or Plasma
    # eGFR variants land on one concept: they differ only by the estimating equation, and the
    # assistant takes the freshest value across the panel whichever LOINC the lab reported.
    "33914-3": EGFR_UUID,   # MDRD (older, generic)
    "62238-1": EGFR_UUID,   # GFR/1.73 sq M by CKD-EPI
    "88293-6": EGFR_UUID,   # eGFR by CKD-EPI 2021 (race-free, current recommended)
    "98979-8": EGFR_UUID,   # eGFR by CKD-EPI 2021
}

# Panel codes we deliberately do NOT map onto our concepts, and why.
#
# fhir2 serialises one coding per Observation and `_lean_observation` takes the first LOINC it
# finds, so every code mapped onto a single concept is a label that concept's values might be
# reported under. Stacking the race-based MDRD pair onto the shared eGFR concept meant a value
# computed by any equation could surface labelled "eGFR by MDRD, African American" (observed
# live: a CKD-EPI value came back as 48643-1). Mislabelling a lab by a race-based code that is
# being deprecated for exactly that reason is not a cosmetic slip in a clinical demo.
#
# These stay in the assistant's `_LAB_LOINCS` on purpose. A deployment whose own dictionary
# codes eGFR this way carries one accurate coding on its own concept, and the panel should
# still find it. What we refuse is to put these codes on OUR concept, where they would be a
# guess about an equation we never ran.
LAB_LOINC_DELIBERATELY_UNMAPPED = frozenset({
    "48642-3",  # eGFR by MDRD, non-African American
    "48643-1",  # eGFR by MDRD, African American
})

# OpenMRS reference rows (present in every seeded install; verified on the o3 stack).
NA_DATATYPE = "8d4a4c94-c2cc-11de-8d13-0010c6dffd0f"
NUMERIC_DATATYPE = "8d4a4488-c2cc-11de-8d13-0010c6dffd0f"
RADIOLOGY_CLASS = "8caa332c-efe4-4025-8b18-3398328e1323"   # Radiology/Imaging Procedure
TEST_CLASS = "8d4907b2-c2cc-11de-8d13-0010c6dffd0f"        # Test
DIAGNOSIS_CLASS = "8d4918b0-c2cc-11de-8d13-0010c6dffd0f"   # Diagnosis (order reasons)
DRUG_CLASS = "8d490dfc-c2cc-11de-8d13-0010c6dffd0f"        # Drug (med concepts)
SAME_AS_MAP_TYPE = "35543629-7d8c-11e1-909d-c80aa9edcf4e"  # concept_map_type SAME-AS

# The ICD-10 source the #81 resolver matches on (any source whose normalised name starts
# "ICD10"; the live CIEL dictionary calls it "ICD-10-WHO"). ensure_order_reason get-or-creates a
# source by that same normalisation and only falls back to creating this one when none exists.
ICD10_SOURCE_UUID = _u("lh-radiology.mimic.icd10-source.v1")

# Same story for LOINC: the lab concepts' reference terms hang off whichever source the
# dictionary calls LOINC, and this UUID is only used when no such source exists at all.
LOINC_SOURCE_UUID = _u("lh-radiology.mimic.loinc-source.v1")


def lab_term_uuid(code: str) -> str:
    """Stable UUID for a LOINC reference term this bootstrap creates. Only used when the
    dictionary has no term for the code already; an existing term is always reused."""
    return _u(f"lh-radiology.mimic.loinc-term.{code}.v1")


def lab_map_uuid(concept_uuid: str, code: str) -> str:
    return _u(f"{concept_uuid}.loinc-map.{code}")


def reason_concept_uuid(codes: list[str]) -> str:
    """Stable UUID for the order-reason Concept carrying this ICD-10 code set. Sorted, so the
    same set always lands on the same concept regardless of manifest ordering."""
    return _u("lh-radiology.mimic.order-reason." + "+".join(sorted(codes)) + ".v1")


def reason_term_uuid(code: str) -> str:
    return _u(f"lh-radiology.mimic.icd10-term.{code}.v1")


def drug_concept_uuid(name: str) -> str:
    return _u(f"lh-radiology.mimic.drug-concept.{name.strip().lower()}.v1")


def drug_uuid(name: str) -> str:
    return _u(f"lh-radiology.mimic.drug.{name.strip().lower()}.v1")

CONCEPTS = [
    {"uuid": CHEST_RADIOGRAPH_UUID, "name": "Chest radiograph",
     "class": RADIOLOGY_CLASS, "datatype": NA_DATATYPE, "numeric": None},
    {"uuid": CREATININE_UUID, "name": "Serum creatinine",
     "class": TEST_CLASS, "datatype": NUMERIC_DATATYPE, "numeric": {"units": "mg/dL"}},
    {"uuid": EGFR_UUID, "name": "Estimated GFR",
     "class": TEST_CLASS, "datatype": NUMERIC_DATATYPE, "numeric": {"units": "mL/min/1.73m2"}},
]


def _ref_id(cur, table: str, id_col: str, uuid_val: str) -> int:
    cur.execute(f"select {id_col} from {table} where uuid=%s", (uuid_val,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"reference {table} {uuid_val} not found -- is the OpenMRS seed loaded?")
    return row[0]


def _loinc_source_id(cur) -> int:
    """The dictionary's LOINC source, matched on a normalised name (upper, no dashes/spaces)
    so an existing CIEL-style source wins over creating a second parallel one. Mirrors
    OmrsClient._icd10_source_id. Creates 'LOINC' only when the dictionary has none."""
    cur.execute("select concept_source_id, name from concept_reference_source where retired=0")
    for sid, name in cur.fetchall():
        if str(name or "").upper().replace("-", "").replace(" ", "").startswith("LOINC"):
            return sid
    cur.execute(
        "insert into concept_reference_source (name, description, creator, date_created, "
        "retired, uuid) values ('LOINC', 'LOINC (provisioned by the #68 MIMIC ETL; this "
        "dictionary shipped no LOINC source)', 1, NOW(), 0, %s)",
        (LOINC_SOURCE_UUID,))
    return cur.lastrowid


def _ensure_loinc_maps(cur, concept_id: int, concept_uuid: str, codes: list[str]) -> int:
    """Give a lab concept its LOINC codings as SAME-AS maps. Returns the number added.

    Without these the concept carries no LOINC coding at all, so `search_observations` --
    which queries `http://loinc.org|<code>` -- can never match an observation stored against
    it. The obs sit in the DB and the EHR Assistant's relevantLabs comes back empty, silently,
    because `_degrade` turns the empty search into an empty list rather than an error (#84).

    An existing reference term for the code is REUSED rather than duplicated: a term may map
    to several concepts, and the dictionary already ships terms for some of these codes (its
    2160-0 term maps BROADER-THAN onto the stock creatinine concept). fhir2 honours any map
    type for token search, so we add SAME-AS purely because it is the honest relation here.

    Idempotent, and deliberately run on the already-exists path too: stacks provisioned before
    this fix have the concepts with no codings, and need the backfill.
    """
    if not codes:
        return 0
    source_id = _loinc_source_id(cur)
    map_type_id = _ref_id(cur, "concept_map_type", "concept_map_type_id", SAME_AS_MAP_TYPE)
    added = 0
    for code in sorted(codes):
        cur.execute("select concept_reference_term_id from concept_reference_term "
                    "where concept_source_id=%s and code=%s and retired=0 limit 1",
                    (source_id, code))
        row = cur.fetchone()
        term_id = row[0] if row else None
        if not term_id:
            cur.execute("insert into concept_reference_term (concept_source_id, code, creator, "
                        "date_created, retired, uuid) values (%s, %s, 1, NOW(), 0, %s)",
                        (source_id, code, lab_term_uuid(code)))
            term_id = cur.lastrowid
        cur.execute("select concept_map_id from concept_reference_map "
                    "where concept_id=%s and concept_reference_term_id=%s limit 1",
                    (concept_id, term_id))
        if not cur.fetchone():
            cur.execute("insert into concept_reference_map (concept_reference_term_id, "
                        "concept_map_type_id, creator, date_created, concept_id, uuid) "
                        "values (%s, %s, 1, NOW(), %s, %s)",
                        (term_id, map_type_id, concept_id, lab_map_uuid(concept_uuid, code)))
            added += 1
    return added


def provision(conn, spec: dict) -> str:
    codes = [code for code, target in LAB_LOINC_TO_CONCEPT.items() if target == spec["uuid"]]
    with conn.cursor() as cur:
        cur.execute("select concept_id from concept where uuid=%s", (spec["uuid"],))
        row = cur.fetchone()
        if row:
            # The concept is here but its LOINC maps may not be (anything provisioned before
            # #84). Backfill instead of returning early, so a re-run repairs an existing stack.
            added = _ensure_loinc_maps(cur, row[0], spec["uuid"], codes)
            return "exists" if not added else f"exists, +{added} loinc map(s)"
        datatype_id = _ref_id(cur, "concept_datatype", "concept_datatype_id", spec["datatype"])
        class_id = _ref_id(cur, "concept_class", "concept_class_id", spec["class"])
        cur.execute("select user_id from users where system_id='admin' limit 1")
        admin = (cur.fetchone() or [1])[0]
        cur.execute(
            "insert into concept (retired, datatype_id, class_id, is_set, creator, date_created, uuid) "
            "values (0, %s, %s, 0, %s, NOW(), %s)", (datatype_id, class_id, admin, spec["uuid"]))
        cid = cur.lastrowid
        cur.execute(
            "insert into concept_name (concept_id, name, locale, locale_preferred, creator, "
            "date_created, concept_name_type, voided, uuid) "
            "values (%s, %s, 'en', 1, %s, NOW(), 'FULLY_SPECIFIED', 0, %s)",
            (cid, spec["name"], admin, _u(spec["uuid"] + ".name.en")))
        if spec["numeric"] is not None:
            # allow_decimal=1: lab values like creatinine 0.9 are decimals; fhir2 rejects a decimal
            # obs against a numeric concept whose allow_decimal is false (the column default).
            cur.execute("insert into concept_numeric (concept_id, units, allow_decimal) "
                        "values (%s, %s, 1)", (cid, spec["numeric"]["units"]))
        added = _ensure_loinc_maps(cur, cid, spec["uuid"], codes)
        return "created" if not added else f"created, +{added} loinc map(s)"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Provision MIMIC ETL concepts (#68).")
    p.add_argument("--host", default=os.environ.get("OMRS_DB_HOST", "mariadb"))
    p.add_argument("--database", default=os.environ.get("OMRS_DB_NAME", "openmrs"))
    p.add_argument("--user", default=os.environ.get("OMRS_DB_USER", "openmrs"))
    p.add_argument("--password", default=os.environ.get("OMRS_DB_PASS", "openmrs"))
    args = p.parse_args(argv)
    import pymysql
    conn = pymysql.connect(host=args.host, database=args.database, user=args.user,
                           password=args.password, autocommit=False)
    try:
        for spec in CONCEPTS:
            status = provision(conn, spec)
            print(f"{spec['name']:20} {spec['uuid']}  [{status}]")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"\nset MIMIC_ORDER_CONCEPT_UUID={CHEST_RADIOGRAPH_UUID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
