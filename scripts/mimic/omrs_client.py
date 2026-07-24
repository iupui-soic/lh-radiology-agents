"""Write client for the MIMIC ETL (#68): the mixed loader the probes forced.

fhir2 cannot create the resources #68 needs most (ServiceRequest 400, MedicationRequest 400,
Encounter 400, Condition 500), so the ETL is a MIX, chosen per resource by what actually works on
the deployed stack:
  - fhir2 create: Patient? no (422 preferred-id). Observation yes. DiagnosticReport yes.
  - OpenMRS REST (webservices.rest): Patient, Encounter, Condition.
  - direct SQL: the RadiologyOrder (module has no REST create; proven in the #70 E2E), drug
    orders (#68 gap 3), and load-time dictionary rows (order reasons, drugs).

Connection config (env), defaulting to the in-compose-network names so the ETL can run as a one-shot
container beside the stack; override to localhost for host-run tooling:
  FHIR2_BASE_URL (default http://openmrs:8080/openmrs/ws/fhir2/R4), FHIR2_BASIC_USER/PASS
  OMRS_REST_BASE_URL (derived from FHIR2_BASE_URL if unset)
  OMRS_DB_HOST (mariadb) / OMRS_DB_PORT (3306) / OMRS_DB_USER (openmrs) / OMRS_DB_PASS (openmrs) / OMRS_DB_NAME (openmrs)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import logging
import os
import re
from urllib.parse import urlparse
import httpx

import bootstrap_radiology_concept as dictionary  # stable UUIDs + reference-row constants

_log = logging.getLogger("mimic.omrs_client")


def fhir_instant(s: str) -> str:
    """Normalize a datetime to a FHIR-valid instant. Two source shapes need fixing:

    - this fhir2 rejects a `+0000` offset, it wants `+00:00`;
    - MIMIC-IV timestamps are SQL-style (`2180-08-07 06:15:00`), and every FHIR parser requires
      the `T` date/time separator -- HAPI refuses the space with
      `HAPI-1821 ... Expected character 'T' at index 10`, so a whole cohort's labs 400 without this.

    Leaves already-valid values (or `Z`) untouched.
    """
    s = (s or "").strip()
    # SQL-style separator -> ISO 8601. Anchored so only a real date+time pair is rewritten.
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s):
        s = s[:10] + "T" + s[11:]
    m = re.search(r"([+-]\d{2})(\d{2})$", s)
    return s[: m.start()] + m.group(1) + ":" + m.group(2) if m else s

# Stable references discovered on the o3 demo stack (overridable via env for another deployment).
RADIOLOGY_ORDER_TYPE_UUID = os.environ.get("MIMIC_ORDER_TYPE_UUID", "dbdb9a9b-56ea-11e5-a47f-08002719a237")
# Drug Order is core-seeded metadata; the default uuid ships with every OpenMRS install.
DRUG_ORDER_TYPE_UUID = os.environ.get("MIMIC_DRUG_ORDER_TYPE_UUID", "131168f4-15f5-102d-96e4-000c29c2a5d7")
RADIOLOGY_CARE_SETTING_UUID = os.environ.get("MIMIC_CARE_SETTING_UUID", "6f0c9a92-6f24-11e3-af88-005056821db0")
RADIOLOGY_ENCOUNTER_TYPE_UUID = os.environ.get("MIMIC_ENCOUNTER_TYPE_UUID", "19db8c0d-3520-48f2-babd-77f2d450e5c7")
PATIENT_ID_TYPE_UUID = os.environ.get("MIMIC_PATIENT_ID_TYPE_UUID", "8d79403a-c2cc-11de-8d13-0010c6dffd0f")  # Old Identification Number (manual) -> holds subject_id
OPENMRS_ID_TYPE_UUID = os.environ.get("MIMIC_OPENMRS_ID_TYPE_UUID", "05a29f94-c0ed-11e2-94be-8c13b969e334")  # OpenMRS ID (required)
IDGEN_SOURCE_UUID = os.environ.get("MIMIC_IDGEN_SOURCE_UUID", "8549f706-7e85-4c1d-9424-217d50a2988b")  # Generator for OpenMRS ID
LOCATION_UUID = os.environ.get("MIMIC_LOCATION_UUID", "1ce1b7d4-c865-4178-82b0-5932e51503d6")  # Community Outreach
ENCOUNTER_ROLE_UUID = os.environ.get("MIMIC_ENCOUNTER_ROLE_UUID", "13fc9b4a-49ed-429c-9dde-ca005b387a3d")  # Radiology Ordering Provider
# The order/report concept. The demo dictionary has no CXR procedure, so the ETL provisions one
# (bootstrap_radiology_concept.py) and points this at it. No default: a wrong concept 500s fhir2.
ORDER_CONCEPT_UUID = os.environ.get("MIMIC_ORDER_CONCEPT_UUID", "")

# Referring-physician seeding (#76 build item 1). The login the demo physician signs in with at the
# ack surface (#86) needs a password that clears the OpenMRS default policy (>=8, upper+lower+digit);
# this DEMO default is overridable per deployment and is never a real secret. Roles gate what that
# login can do -- "Provider" by default; override to a role that grants patient-chart view on a given
# image if the notification must be visible to the physician after login.
REFERRER_PASSWORD = os.environ.get("MIMIC_REFERRER_PASSWORD", "Referring1!")
REFERRER_ROLES = [r.strip() for r in os.environ.get("MIMIC_REFERRER_ROLES", "Provider").split(",")
                  if r.strip()]

_URGENCY = {"routine": "ROUTINE", "stat": "STAT", "urgent": "STAT", "asap": "STAT"}


@dataclass
class EtlConfig:
    fhir2_base: str = os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4").rstrip("/")
    rest_base: str = ""
    basic: tuple[str, str] = (os.environ.get("FHIR2_BASIC_USER", "admin"),
                              os.environ.get("FHIR2_BASIC_PASS", "Admin123"))
    db_host: str = os.environ.get("OMRS_DB_HOST", "mariadb")
    db_port: int = int(os.environ.get("OMRS_DB_PORT", "3306"))
    db_user: str = os.environ.get("OMRS_DB_USER", "openmrs")
    db_pass: str = os.environ.get("OMRS_DB_PASS", "openmrs")
    db_name: str = os.environ.get("OMRS_DB_NAME", "openmrs")

    def __post_init__(self):
        if not self.rest_base:
            explicit = os.environ.get("OMRS_REST_BASE_URL")
            if explicit:
                self.rest_base = explicit.rstrip("/")
            else:
                p = urlparse(self.fhir2_base)
                root = p.path.split("/ws/", 1)[0]
                self.rest_base = f"{p.scheme}://{p.netloc}{root}/ws/rest/v1"


class OmrsClient:
    def __init__(self, cfg: Optional[EtlConfig] = None, admin_provider_uuid: Optional[str] = None):
        self.cfg = cfg or EtlConfig()
        self._http = httpx.Client(auth=self.cfg.basic, timeout=60.0)
        self._conn = None
        self._provider_uuid = admin_provider_uuid
        self._referrers: dict[str, str] = {}  # username -> provider uuid (per-instance seed cache, #76)

    # --- transports ---------------------------------------------------------
    def _fget(self, path, params=None):
        r = self._http.get(f"{self.cfg.fhir2_base}/{path.lstrip('/')}", params=params); r.raise_for_status(); return r.json()

    def _fpost(self, res, body):
        r = self._http.post(f"{self.cfg.fhir2_base}/{res}", json=body); r.raise_for_status(); return r.json()

    def _fput(self, res, rid, body):
        r = self._http.put(f"{self.cfg.fhir2_base}/{res}/{rid}", json=body); r.raise_for_status(); return r.json()

    def _rpost(self, res, body):
        r = self._http.post(f"{self.cfg.rest_base}/{res}", json=body); r.raise_for_status(); return r.json()

    def _rget(self, res, params=None):
        r = self._http.get(f"{self.cfg.rest_base}/{res}", params=params); r.raise_for_status(); return r.json()

    def _db(self):
        if self._conn is None:
            import pymysql
            self._conn = pymysql.connect(host=self.cfg.db_host, port=self.cfg.db_port,
                                         user=self.cfg.db_user, password=self.cfg.db_pass,
                                         database=self.cfg.db_name, autocommit=True)
        return self._conn

    def _id_by_uuid(self, table: str, id_col: str, uuid: str) -> Optional[int]:
        with self._db().cursor() as c:
            c.execute(f"select {id_col} from {table} where uuid=%s", (uuid,))
            row = c.fetchone()
            return row[0] if row else None

    def provider_uuid(self) -> str:
        if not self._provider_uuid:
            self._provider_uuid = self._rget("provider", {"v": "custom:(uuid)", "limit": 1})["results"][0]["uuid"]
        return self._provider_uuid

    # --- referring-physician seeding (#76 build item 1) ----------------------
    def _find_provider_by_identifier(self, identifier: str) -> tuple[Optional[str], Optional[str]]:
        """(provider uuid, its person uuid) for the Provider whose identifier == `identifier`, or
        (None, None). The identifier is the stable idempotency key: get-or-create keys on it, not on
        a name, so a re-run reuses the same Provider (and thus the same fhir2 requester reference)."""
        res = self._rget("provider", {"q": identifier, "v": "custom:(uuid,identifier,person:(uuid))"})
        for p in res.get("results", []):
            if p.get("identifier") == identifier:
                return p.get("uuid"), (p.get("person") or {}).get("uuid")
        return None, None

    def _ensure_referrer_user(self, username: str, person_uuid: str, password: Optional[str]) -> None:
        """Best-effort get-or-create of the login User on the referrer's Person, so the physician can
        sign into OpenMRS and acknowledge (#86). BEST-EFFORT by design: a User-creation failure (a
        role/privilege quirk on a given image, a stricter password policy) must NOT cost the requester
        seeding -- the Provider already exists and the order will still carry a real requester, only
        the in-EHR login degrades. The failure is logged, never raised."""
        try:
            res = self._rget("user", {"q": username, "v": "custom:(uuid,username)"})
            if any(u.get("username") == username for u in res.get("results", [])):
                return
            self._rpost("user", {"username": username, "password": password or REFERRER_PASSWORD,
                                 "person": person_uuid, "roles": REFERRER_ROLES})
            _log.info("referring-physician login %s provisioned", username)
        except Exception as e:  # noqa: BLE001
            _log.warning("referring-physician login %s NOT provisioned (%s); the order still carries "
                         "a real requester, only in-EHR login/ack for this physician degrades",
                         username, e)

    def ensure_referring_provider(self, username: str, given: str, family: str,
                                  gender: str = "U", password: Optional[str] = None) -> str:
        """Get-or-create the OpenMRS Provider (and a login User) for a demo referring physician, and
        return its uuid -- the value `insert_radiology_order` stamps as the order's `orderer`, which
        fhir2 surfaces as `ServiceRequest.requester` and `resolve_ordering_provider` reads verbatim
        (#76 build item 1). Idempotent by `username` (the Provider identifier and the User's username
        are the stable keys), so a re-run of the ETL (#68) reuses the same Provider rather than
        duplicating it. Cached per client instance so a cohort's many studies seed each referrer once.

        Note on alignment (#76): the referrer is NOT written into the comms ledger. The ledger holds
        the on-call directory (docker/comms-ledger/seed_oncall.py); the ordering physician's reference
        is carried VERBATIM from fhir2 onto Communication.recipient / Task.owner, so a Provider in
        fhir2 is all resolve_ordering_provider needs -- no second write to keep in sync."""
        username = username.strip()
        if not username:
            raise ValueError("ensure_referring_provider needs a username")
        if username in self._referrers:
            return self._referrers[username]

        prov_uuid, person_uuid = self._find_provider_by_identifier(username)
        if not prov_uuid:
            # ONE atomic write: the Person is inlined on the Provider create (same pattern as
            # create_patient), so a failure here leaves NO orphan Person -- there is no window
            # between a person POST and a provider POST for a crash to strand a person on.
            created = self._rpost("provider", {
                "person": {"names": [{"givenName": given, "familyName": family}],
                           "gender": (gender or "U")[:1].upper()},
                "identifier": username,
            })
            prov_uuid = created["uuid"]
            person_uuid = (created.get("person") or {}).get("uuid")
            if not person_uuid:  # some representations omit the nested uuid; recover it for the login
                _, person_uuid = self._find_provider_by_identifier(username)
        if person_uuid:
            self._ensure_referrer_user(username, person_uuid, password)
        self._referrers[username] = prov_uuid
        return prov_uuid

    # --- creates ------------------------------------------------------------
    def _generate_openmrs_id(self) -> str:
        """OpenMRS ID is a required identifier and is normally minted by idgen; the raw patient REST
        does not auto-generate it, so we mint one here."""
        return self._rpost(f"idgen/identifiersource/{IDGEN_SOURCE_UUID}/identifier", {})["identifier"]

    def create_patient(self, subject_id: str, gender: str = "U", birthdate: str = "1970-01-01",
                        given: str = "MIMIC", family: str = "Subject") -> str:
        """Get-or-create by subject_id (the ETL must be re-runnable, #68 criterion 2). fhir2 rejects
        create without a preferred id, so this is OpenMRS REST, with two identifiers: the required
        OpenMRS ID (minted, preferred) and the MIMIC subject_id (so the EHR loader finds it again)."""
        existing = self.find_patient_by_subject_id(subject_id)
        if existing:
            return existing
        body = {
            "identifiers": [
                {"identifier": self._generate_openmrs_id(), "identifierType": OPENMRS_ID_TYPE_UUID,
                 "location": LOCATION_UUID, "preferred": True},
                {"identifier": str(subject_id), "identifierType": PATIENT_ID_TYPE_UUID,
                 "location": LOCATION_UUID, "preferred": False},
            ],
            "person": {"names": [{"givenName": given, "familyName": f"{family}{subject_id}"}],
                       "gender": (gender or "U")[:1].upper(), "birthdate": birthdate},
        }
        return self._rpost("patient", body)["uuid"]

    def find_patient_by_subject_id(self, subject_id: str) -> Optional[str]:
        res = self._rget("patient", {"q": str(subject_id), "v": "custom:(uuid,identifiers:(identifier))"})
        for p in res.get("results", []):
            if any(i.get("identifier") == str(subject_id) for i in p.get("identifiers", [])):
                return p["uuid"]
        return None

    def create_encounter(self, patient_uuid: str, when_iso: str) -> str:
        body = {"patient": patient_uuid, "encounterType": RADIOLOGY_ENCOUNTER_TYPE_UUID,
                "location": LOCATION_UUID, "encounterDatetime": when_iso,
                "encounterProviders": [{"provider": self.provider_uuid(),
                                        "encounterRole": ENCOUNTER_ROLE_UUID}]}
        return self._rpost("encounter", body)["uuid"]

    def insert_radiology_order(self, patient_uuid: str, encounter_uuid: str, accession: str,
                               concept_uuid: str, priority: str = "routine",
                               order_number: Optional[str] = None,
                               reason_concept_uuid: Optional[str] = None,
                               orderer_provider_uuid: Optional[str] = None) -> str:
        """Insert the three rows that make a RadiologyOrder (orders + test_order + radiology_order),
        the path proven in the #70 E2E. Returns the order uuid == the fhir2 ServiceRequest id the
        signed report's basedOn must point at. Idempotent by accession: returns the existing order.

        `reason_concept_uuid` (#68 gap 4) sets orders.order_reason to an ICD-10-mapped Concept
        (ensure_order_reason). The #81 resolver reads that Concept's ICD-10 mappings into
        StudyContext order.reasonCode, which fires the pneumothorax-detect reason-code slice.

        `orderer_provider_uuid` (#76 build item 1) sets orders.orderer to a specific referring
        physician's Provider; fhir2 then surfaces it as ServiceRequest.requester and the critical-
        result notification reaches that physician. Defaults to the ETL admin provider (unchanged
        pre-#76 behaviour) when None. On an EXISTING order the orderer is backfilled (see below) so a
        re-run repairs a study that was first loaded with the admin orderer -- pre-#76, seed-disabled,
        or after a swallowed best-effort seed failure -- keeping the #68 re-runnability guarantee for
        the requester, not just the fresh-insert path."""
        db = self._db()
        with db.cursor() as c:
            c.execute("select o.uuid, o.orderer, o.order_id from orders o "
                      "join radiology_order r on r.order_id=o.order_id "
                      "where o.accession_number=%s and o.voided=0 limit 1", (accession,))
            row = c.fetchone()
        if row:
            existing_uuid, existing_orderer, existing_oid = row
            # Re-run repair: if a real referring-physician orderer is now supplied and the stored
            # order still carries a different one (an admin orderer from an earlier load), UPDATE just
            # the orderer in place. Idempotency-by-accession otherwise stands -- nothing else about the
            # existing order is touched, and a matching orderer is a no-op.
            if orderer_provider_uuid:
                want = self._id_by_uuid("provider", "provider_id", orderer_provider_uuid)
                if want and want != existing_orderer:
                    with db.cursor() as u:
                        u.execute("update orders set orderer=%s where order_id=%s", (want, existing_oid))
                    _log.info("order %s orderer backfilled to provider %s on re-run",
                              existing_uuid, orderer_provider_uuid)
            return existing_uuid
        # NB: the `patient` table has no uuid column -- a Patient IS-A Person and the uuid lives on
        # `person`, where person_id == patient_id.
        pid = self._id_by_uuid("person", "person_id", patient_uuid)
        eid = self._id_by_uuid("encounter", "encounter_id", encounter_uuid)
        cid = self._id_by_uuid("concept", "concept_id", concept_uuid)
        otid = self._id_by_uuid("order_type", "order_type_id", RADIOLOGY_ORDER_TYPE_UUID)
        csid = self._id_by_uuid("care_setting", "care_setting_id", RADIOLOGY_CARE_SETTING_UUID)
        prov = self._id_by_uuid("provider", "provider_id", orderer_provider_uuid or self.provider_uuid())
        missing = [n for n, v in [("patient", pid), ("encounter", eid), ("concept", cid),
                                  ("order_type", otid), ("care_setting", csid), ("provider", prov)] if not v]
        if missing:
            raise ValueError(f"cannot insert order for accession {accession}: unresolved {missing}")
        reason_id = None
        if reason_concept_uuid:
            reason_id = self._id_by_uuid("concept", "concept_id", reason_concept_uuid)
            if not reason_id:
                raise ValueError(f"order reason concept {reason_concept_uuid} not found; "
                                 "run ensure_order_reason first")
        onum = order_number or f"MIMIC-{accession}"
        urgency = _URGENCY.get(priority.lower(), "ROUTINE")
        with db.cursor() as c:
            c.execute(
                "insert into orders (uuid, order_number, order_action, concept_id, patient_id, "
                "encounter_id, orderer, care_setting, order_type_id, urgency, accession_number, "
                "order_reason, date_activated, date_created, creator, voided) values "
                "(UUID(), %s, 'NEW', %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), 1, 0)",
                (onum, cid, pid, eid, prov, csid, otid, urgency, accession, reason_id))
            oid = c.lastrowid
            c.execute("insert into test_order (order_id) values (%s)", (oid,))
            c.execute("insert into radiology_order (order_id) values (%s)", (oid,))
            c.execute("select uuid from orders where order_id=%s", (oid,))
            return c.fetchone()[0]

    # --- dictionary provisioning at load time (#68 gaps 3+4) ------------------
    def _same_as_map_type_id(self, cur) -> int:
        cur.execute("select concept_map_type_id from concept_map_type where uuid=%s or name='SAME-AS' "
                    "limit 1", (dictionary.SAME_AS_MAP_TYPE,))
        row = cur.fetchone()
        if not row:
            raise ValueError("concept_map_type SAME-AS not found; is the OpenMRS seed loaded?")
        return row[0]

    def _icd10_source_id(self, cur) -> int:
        """The dictionary's ICD-10 source, by the SAME normalisation the #81 resolver applies
        (upper, drop dashes/spaces, prefix ICD10). Created as "ICD-10-WHO" only when absent, so
        an existing CIEL-style source is always preferred over a second parallel one."""
        cur.execute("select concept_source_id, name from concept_reference_source where retired=0")
        for sid, name in cur.fetchall():
            normalised = str(name or "").upper().replace("-", "").replace(" ", "")
            if normalised.startswith("ICD10"):
                return sid
        cur.execute(
            "insert into concept_reference_source (name, description, creator, date_created, "
            "retired, uuid) values ('ICD-10-WHO', 'ICD-10 (provisioned by the #68 MIMIC ETL; the "
            "demo dictionary had no ICD-10 source)', 1, NOW(), 0, %s)",
            (dictionary.ICD10_SOURCE_UUID,))
        return cur.lastrowid

    def _provision_concept(self, cur, concept_uuid: str, name: str, class_uuid: str) -> int:
        """Get-or-create a coded (N/A datatype) concept at a caller-fixed uuid. Mirrors
        bootstrap_radiology_concept.provision; returns concept_id."""
        cur.execute("select concept_id from concept where uuid=%s", (concept_uuid,))
        row = cur.fetchone()
        if row:
            return row[0]
        datatype_id = self._id_by_uuid("concept_datatype", "concept_datatype_id", dictionary.NA_DATATYPE)
        class_id = self._id_by_uuid("concept_class", "concept_class_id", class_uuid)
        if not datatype_id or not class_id:
            raise ValueError(f"concept datatype/class rows missing for {name}; is the seed loaded?")
        cur.execute("insert into concept (retired, datatype_id, class_id, is_set, creator, "
                    "date_created, uuid) values (0, %s, %s, 0, 1, NOW(), %s)",
                    (datatype_id, class_id, concept_uuid))
        cid = cur.lastrowid
        cur.execute("insert into concept_name (concept_id, name, locale, locale_preferred, creator, "
                    "date_created, concept_name_type, voided, uuid) "
                    "values (%s, %s, 'en', 1, 1, NOW(), 'FULLY_SPECIFIED', 0, %s)",
                    (cid, name, dictionary._u(concept_uuid + ".name.en")))
        return cid

    def ensure_order_reason(self, codes: list[str], display: str = "") -> str:
        """Get-or-create the ICD-10-mapped order-reason Concept for this code set (#68 gap 4).

        One Diagnosis-class Concept carries one SAME-AS reference-term mapping per code, because
        the #81 resolver returns ALL of a reason Concept's ICD-10 codes (deduped, in mapping
        order). Stable UUID5 on the sorted code set, so re-runs and reordered manifests reuse the
        same concept. Returns the concept uuid for insert_radiology_order."""
        codes = [c.strip() for c in codes if c and c.strip()]
        if not codes:
            raise ValueError("ensure_order_reason needs at least one ICD-10 code")
        concept_uuid = dictionary.reason_concept_uuid(codes)
        name = display or "Radiology order reason " + "+".join(sorted(codes))
        db = self._db()
        with db.cursor() as cur:
            cid = self._provision_concept(cur, concept_uuid, name, dictionary.DIAGNOSIS_CLASS)
            source_id = self._icd10_source_id(cur)
            map_type_id = self._same_as_map_type_id(cur)
            for code in codes:
                cur.execute("select concept_reference_term_id from concept_reference_term "
                            "where concept_source_id=%s and code=%s and retired=0 limit 1",
                            (source_id, code))
                row = cur.fetchone()
                term_id = row[0] if row else None
                if not term_id:
                    cur.execute("insert into concept_reference_term (concept_source_id, code, "
                                "creator, date_created, retired, uuid) values (%s, %s, 1, NOW(), 0, %s)",
                                (source_id, code, dictionary.reason_term_uuid(code)))
                    term_id = cur.lastrowid
                cur.execute("select concept_map_id from concept_reference_map "
                            "where concept_id=%s and concept_reference_term_id=%s limit 1",
                            (cid, term_id))
                if not cur.fetchone():
                    cur.execute("insert into concept_reference_map (concept_reference_term_id, "
                                "concept_map_type_id, creator, date_created, concept_id, uuid) "
                                "values (%s, %s, 1, NOW(), %s, %s)",
                                (term_id, map_type_id, cid,
                                 dictionary._u(concept_uuid + ".map." + code)))
        return concept_uuid

    def ensure_drug(self, name: str) -> str:
        """Get-or-create the Drug (a Drug-class Concept + a `drug` row) for a manifest med
        (#68 gap 3). Stable UUID5s on the normalised name. Returns the drug uuid."""
        name = (name or "").strip()
        if not name:
            raise ValueError("ensure_drug needs a drug name")
        d_uuid = dictionary.drug_uuid(name)
        db = self._db()
        with db.cursor() as cur:
            cur.execute("select uuid from drug where uuid=%s", (d_uuid,))
            if cur.fetchone():
                return d_uuid
            cid = self._provision_concept(cur, dictionary.drug_concept_uuid(name), name,
                                          dictionary.DRUG_CLASS)
            cur.execute("insert into drug (concept_id, name, combination, creator, date_created, "
                        "retired, uuid) values (%s, %s, 0, 1, NOW(), 0, %s)", (cid, name, d_uuid))
        return d_uuid

    def insert_drug_order(self, patient_uuid: str, encounter_uuid: str, drug_uuid: str) -> str:
        """Insert an active drug order (orders + drug_order rows) so fhir2 surfaces the med as a
        MedicationRequest for the EHR packet (#68 gap 3: fhir2 create 400s and the module has no
        REST create, so SQL, mirroring insert_radiology_order). Presence-only: no dose or schedule,
        which is all the anticoagulant med-flag story needs. Idempotent per (patient, drug).

        NB schema-verified against OpenMRS core 2.x; live-verify on the o3 stack before a real
        cohort load, like the #70 E2E did for the radiology-order path."""
        db = self._db()
        pid = self._id_by_uuid("person", "person_id", patient_uuid)
        eid = self._id_by_uuid("encounter", "encounter_id", encounter_uuid)
        prov = self._id_by_uuid("provider", "provider_id", self.provider_uuid())
        csid = self._id_by_uuid("care_setting", "care_setting_id", RADIOLOGY_CARE_SETTING_UUID)
        otid = self._id_by_uuid("order_type", "order_type_id", DRUG_ORDER_TYPE_UUID)
        with db.cursor() as cur:
            cur.execute("select drug_id, concept_id from drug where uuid=%s", (drug_uuid,))
            drug_row = cur.fetchone()
        missing = [n for n, v in [("patient", pid), ("encounter", eid), ("provider", prov),
                                  ("care_setting", csid), ("drug order_type", otid),
                                  ("drug", drug_row)] if not v]
        if missing:
            raise ValueError(f"cannot insert drug order: unresolved {missing}")
        drug_id, concept_id = drug_row
        with db.cursor() as cur:
            cur.execute("select o.uuid from orders o join drug_order d on d.order_id=o.order_id "
                        "where o.patient_id=%s and d.drug_inventory_id=%s and o.voided=0 limit 1",
                        (pid, drug_id))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "insert into orders (uuid, order_number, order_action, concept_id, patient_id, "
                "encounter_id, orderer, care_setting, order_type_id, urgency, date_activated, "
                "date_created, creator, voided) values "
                "(UUID(), %s, 'NEW', %s, %s, %s, %s, %s, %s, 'ROUTINE', NOW(), NOW(), 1, 0)",
                (f"MIMIC-RX-{pid}-{drug_id}", concept_id, pid, eid, prov, csid, otid))
            oid = cur.lastrowid
            cur.execute("insert into drug_order (order_id, drug_inventory_id, as_needed, "
                        "dispense_as_written) values (%s, %s, 0, 0)", (oid, drug_id))
            cur.execute("select uuid from orders where order_id=%s", (oid,))
            return cur.fetchone()[0]

    def ensure_radiology_study(self, accession: str, study_instance_uid: str,
                               performed_status: str = "COMPLETED") -> Optional[str]:
        """Upsert the radiology_study row that makes the RIS report surface exist (#76 A.2).

        The radiology module gates its Report segment (Claim Report ->
        radiologyReport.form) on the order's study having performed_status
        COMPLETED -- live-verified 2026-07-22: without the row the order page
        renders with NO report action at all. A modality would normally create
        it via MPPS; the MIMIC replay has no modality, so the ETL writes it
        after the DICOM push, carrying the REAL StudyInstanceUID from the
        shipped study (never a fabricated one: the RIS's View Study link joins
        on it).

        Direct SQL like insert_radiology_order (no REST surface for it).
        Idempotent by order: re-runs refresh the UID + status in place.
        Returns the radiology_study uuid, or None when no order exists for the
        accession (the FHIR-side load has not run) -- caller decides whether
        that is a warning or a failure.
        """
        db = self._db()
        with db.cursor() as c:
            c.execute("select o.order_id from orders o join radiology_order r "
                      "on r.order_id=o.order_id where o.accession_number=%s and o.voided=0 "
                      "limit 1", (accession,))
            row = c.fetchone()
            if not row:
                return None
            order_id = row[0]
            c.execute("select study_id, uuid from radiology_study where order_id=%s", (order_id,))
            row = c.fetchone()
            if row:
                c.execute("update radiology_study set study_instance_uid=%s, performed_status=%s, "
                          "changed_by=1, date_changed=NOW() where study_id=%s",
                          (study_instance_uid, performed_status, row[0]))
                return row[1]
            c.execute("insert into radiology_study (study_instance_uid, order_id, "
                      "performed_status, creator, date_created, uuid) values "
                      "(%s, %s, %s, 1, NOW(), UUID())",
                      (study_instance_uid, order_id, performed_status))
            c.execute("select uuid from radiology_study where order_id=%s", (order_id,))
            return c.fetchone()[0]

    def create_observation(self, patient_uuid: str, concept_uuid: str, value: float,
                           unit: str, when_iso: str) -> str:
        body = {"resourceType": "Observation", "status": "final",
                "code": {"coding": [{"code": concept_uuid}]},
                "subject": {"reference": f"Patient/{patient_uuid}"},
                "effectiveDateTime": fhir_instant(when_iso),
                "valueQuantity": {"value": value, "unit": unit}}
        return self._fpost("Observation", body)["id"]

    def create_condition(self, patient_uuid: str, concept_uuid: str, onset_iso: str) -> str:
        """OpenMRS REST /condition (fhir2 Condition create 500s here). The problem is a coded
        Concept; onset stamps it. NOTE: the coded-concept shape varies by OpenMRS build -- verify
        against the provisioned dictionary when loading real MIMIC-IV diagnoses_icd (#68)."""
        body = {"patient": patient_uuid,
                "condition": {"coded": concept_uuid},
                "clinicalStatus": "ACTIVE",
                "onsetDate": onset_iso}
        return self._rpost("condition", body)["uuid"]

    def seed_diagnostic_report(self, patient_uuid: str, service_request_uuid: str,
                               concept_uuid: str, conclusion: str, status: str = "preliminary") -> str:
        body = {"resourceType": "DiagnosticReport", "status": status,
                "code": {"coding": [{"code": concept_uuid}], "text": "Radiology report"},
                "subject": {"reference": f"Patient/{patient_uuid}"},
                "basedOn": [{"reference": f"ServiceRequest/{service_request_uuid}"}],
                "conclusion": conclusion}
        return self._fpost("DiagnosticReport", body)["id"]

    def finalize_diagnostic_report(self, report_id: str) -> str:
        """Flip a seeded preliminary report to final so the RIS poller fires report_finalized
        (the flip-to-final rehearsal cue). Re-PUTs the resource with status=final."""
        r = self._fget(f"DiagnosticReport/{report_id}")
        r["status"] = "final"
        return self._fput("DiagnosticReport", report_id, r)["id"]

    def order_for_accession(self, accession: str) -> Optional[dict]:
        """The module's accession index (what the orchestrator ingest resolver uses): the order uuid
        (== the fhir2 ServiceRequest id) and its patient uuid, or None."""
        res = self._rget("radiologyorder", {"accessionNumber": accession,
                                             "v": "custom:(uuid,patient:(uuid))"})
        for o in res.get("results", []):
            if o.get("uuid") and (o.get("patient") or {}).get("uuid"):
                return {"order_uuid": o["uuid"], "patient_uuid": o["patient"]["uuid"]}
        return None

    def find_seeded_report(self, patient_uuid: str, service_request_uuid: str) -> Optional[str]:
        """Our seeded DiagnosticReport for this order (basedOn the ServiceRequest), any status."""
        bundle = self._fget("DiagnosticReport", {"subject": f"Patient/{patient_uuid}"})
        want = f"ServiceRequest/{service_request_uuid}"
        for e in bundle.get("entry", []) or []:
            r = e.get("resource") or {}
            if any((b.get("reference") == want) for b in (r.get("basedOn") or [])):
                return r.get("id")
        return None

    def close(self):
        self._http.close()
        if self._conn:
            self._conn.close()
