"""The ingest resolver carries the order reason's ICD-10 codes (#81).

The interpretation registry's reason-code slice (pneumothorax-detect on J93*/J95.811) fires on
the ORDER's reasonCode; the #70 resolver dropped it because the module order reason is a
Concept, not a code. These pin the #81 shape: only the Concept's ICD-10 reference-term mappings
travel (never free text), source-name matching tolerates dictionary conventions ("ICD-10-WHO"
on the live CIEL dictionary) while excluding ICD-11, and a live dictionary's malformed mapping
never costs the patient/order join.
"""
import asyncio
from unittest import mock

import radagent_common.openmrs_rest as openmrs_rest_module
from radagent_common.openmrs_rest import OpenmrsRestClient, _icd10_reason_codes

# Representative subset of the running o3's CIEL mapping set for concept 122657 Pneumothorax
# (live sources verified: PIH, 3BT, SNOMED CT, AMPATH, IMO ProblemIT, ICD-10-WHO, CIEL, ICPC2,
# ICD-11-WHO) -- the ones that exercise the filter's accept AND reject directions.
LIVE_MAPPINGS = [
    {"conceptReferenceTerm": {"code": "2618", "conceptSource": {"name": "PIH"}}},
    {"conceptReferenceTerm": {"code": "36118008", "conceptSource": {"name": "SNOMED CT"}}},
    {"conceptReferenceTerm": {"code": "J93.9", "conceptSource": {"name": "ICD-10-WHO"}}},
    {"conceptReferenceTerm": {"code": "CB21.Z", "conceptSource": {"name": "ICD-11-WHO"}}},
    {"conceptReferenceTerm": {"code": "R99", "conceptSource": {"name": "ICPC2"}}},
]


def _order(order_reason):
    order = {"uuid": "ord-1", "urgency": "STAT", "patient": {"uuid": "pat-1"}}
    if order_reason is not None:
        order["orderReason"] = order_reason
    return order


class _BundleClient:
    """Fake httpx.AsyncClient returning one canned radiologyorder bundle."""

    bundle: dict = {"results": []}
    params_seen: dict = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _BundleClient.params_seen = params or {}

        class _R:
            def raise_for_status(self):
                return None

            def json(self):
                return _BundleClient.bundle

        return _R()


def _resolve(order):
    _BundleClient.bundle = {"results": [order]}
    with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _BundleClient):
        client = OpenmrsRestClient(base_url="http://localhost:8080/openmrs/ws/rest/v1")
        return asyncio.run(client.resolve_radiology_order_by_accession("ACC-1"))


# --- the helper: source filtering, order, dedup, junk tolerance -------------------------

def test_only_icd10_sources_survive_the_live_dictionary_mapping_set():
    """PIH/SNOMED/ICPC2 are not what triage matches on, and ICD-11 normalises to ICD11 and must
    NOT ride an ICD10 prefix match."""
    assert _icd10_reason_codes({"mappings": LIVE_MAPPINGS}) == ["J93.9"]


def test_source_name_matching_tolerates_dictionary_conventions():
    for name in ("ICD-10-WHO", "ICD-10", "icd 10", "ICD10", "ICD-10-CM"):
        got = _icd10_reason_codes(
            {"mappings": [{"conceptReferenceTerm": {"code": "J95.811",
                                                    "conceptSource": {"name": name}}}]})
        assert got == ["J95.811"], name


def test_multiple_icd10_mappings_keep_order_and_dedup():
    mappings = [
        {"conceptReferenceTerm": {"code": "J95.811", "conceptSource": {"name": "ICD-10-CM"}}},
        {"conceptReferenceTerm": {"code": "J93.9", "conceptSource": {"name": "ICD-10-WHO"}}},
        {"conceptReferenceTerm": {"code": "J95.811", "conceptSource": {"name": "ICD-10-WHO"}}},
    ]
    assert _icd10_reason_codes({"mappings": mappings}) == ["J95.811", "J93.9"]


def test_malformed_mappings_contribute_nothing_and_never_raise():
    """A live dictionary can hand back junk; the resolver is best-effort end to end and a broken
    mapping must not cost the patient/order join."""
    junk = {"mappings": [
        "not-a-dict",
        {"conceptReferenceTerm": "not-a-dict"},
        {"conceptReferenceTerm": {"code": "J93.9", "conceptSource": "not-a-dict"}},
        {"conceptReferenceTerm": {"code": None, "conceptSource": {"name": "ICD-10-WHO"}}},
        {"conceptReferenceTerm": {"code": "   ", "conceptSource": {"name": "ICD-10-WHO"}}},
        {"conceptReferenceTerm": {"code": "J12.9", "conceptSource": {"name": None}}},
        {"conceptReferenceTerm": {"code": " J93.0 ", "conceptSource": {"name": "ICD-10-WHO"}}},
    ]}
    assert _icd10_reason_codes(junk) == ["J93.0"]   # the one valid entry, stripped
    assert _icd10_reason_codes(None) == []
    assert _icd10_reason_codes({}) == []
    assert _icd10_reason_codes({"mappings": "not-a-list-shaped"}) in ([],)


# --- through the resolver: the resolved dict shape ---------------------------------------

def test_a_rule_out_pneumothorax_order_resolves_with_its_reason_codes():
    out = _resolve(_order({"mappings": LIVE_MAPPINGS}))
    assert out == {
        "fhirPatientId": "Patient/pat-1",
        "fhirServiceRequestId": "ServiceRequest/ord-1",
        "priority": "stat",
        "reasonCode": ["J93.9"],       # array of strings, the StudyContext order.reasonCode shape
    }


def test_an_order_without_a_reason_keeps_the_pre_81_shape():
    out = _resolve(_order(None))
    assert out == {"fhirPatientId": "Patient/pat-1",
                   "fhirServiceRequestId": "ServiceRequest/ord-1", "priority": "stat"}


def test_an_order_whose_reason_has_no_icd10_mapping_omits_the_key():
    out = _resolve(_order({"mappings": [
        {"conceptReferenceTerm": {"code": "36118008", "conceptSource": {"name": "SNOMED CT"}}}]}))
    assert "reasonCode" not in out


class _RepRejectingClient(_BundleClient):
    """400s the orderReason rep (a deployment whose converter rejects it), 200s the base rep."""

    calls: list = []

    async def get(self, url, params=None):
        _RepRejectingClient.calls.append(params.get("v", ""))
        if "orderReason" in params.get("v", ""):
            import httpx as _hx
            req = _hx.Request("GET", url)
            raise _hx.HTTPStatusError(
                "400", request=req, response=_hx.Response(400, request=req))
        # The rep controls the response shape: a base-rep answer carries no orderReason, so the
        # fake must not hand back fields the request never asked for.
        _BundleClient.bundle = {"results": [
            {k: v for k, v in r.items() if k != "orderReason"}
            for r in _BundleClient.bundle.get("results", [])]}
        return await super().get(url, params=params)


def test_a_rep_rejecting_deployment_still_resolves_the_join_without_reason_codes():
    """The reason rep must never COST the join: if the module 400s the nested rep, the resolve
    retries with the pre-#81 rep and returns patient/order/priority -- losing only reasonCode.
    Without the fallback, a converter 400 would bubble into ingress' best-effort swallow and
    degrade EVERY study to Patient/UNRESOLVED (found by hostile re-verification)."""
    _RepRejectingClient.calls = []
    _BundleClient.bundle = {"results": [_order({"mappings": LIVE_MAPPINGS})]}
    with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _RepRejectingClient):
        client = OpenmrsRestClient(base_url="http://localhost:8080/openmrs/ws/rest/v1")
        out = asyncio.run(client.resolve_radiology_order_by_accession("ACC-1"))
    assert out == {"fhirPatientId": "Patient/pat-1",
                   "fhirServiceRequestId": "ServiceRequest/ord-1", "priority": "stat"}
    assert len(_RepRejectingClient.calls) == 2
    assert "orderReason" in _RepRejectingClient.calls[0]      # tried the full rep first
    assert "orderReason" not in _RepRejectingClient.calls[1]  # fell back to the join-only rep


def test_a_non_400_failure_keeps_its_outage_semantics():
    """Only a rep REJECTION falls back; an outage-shaped error (500, connect) must still bubble
    to the caller's best-effort swallow exactly as pre-#81."""
    class _FailingClient(_BundleClient):
        async def get(self, url, params=None):
            import httpx as _hx
            req = _hx.Request("GET", url)
            raise _hx.HTTPStatusError(
                "500", request=req, response=_hx.Response(500, request=req))

    import pytest
    with mock.patch.object(openmrs_rest_module.httpx, "AsyncClient", _FailingClient):
        client = OpenmrsRestClient(base_url="http://localhost:8080/openmrs/ws/rest/v1")
        with pytest.raises(Exception):
            asyncio.run(client.resolve_radiology_order_by_accession("ACC-1"))


def test_the_custom_rep_asks_for_the_reason_mappings():
    """The rep is the wire contract with the module (verified against the live o3): if it stops
    naming orderReason mappings, every reason silently vanishes and these tests still pass on
    canned bundles -- so pin the request itself."""
    _resolve(_order(None))
    rep = _BundleClient.params_seen.get("v", "")
    assert "orderReason:(mappings:(display," in rep    # the shape a live o3 actually fills
    assert "conceptReferenceTerm:(code,conceptSource:(name))" in rep


# --- the shape a LIVE o3 returns ---------------------------------------------------------
#
# The REST layer does not materialise the nested `conceptReferenceTerm` inside a custom rep: it
# answers `{"resourceVersion": "1.9"}` per mapping and puts the two facts in `display` instead.
# Every fixture above uses the structured shape, which is why the codes could silently stop
# travelling while the whole suite stayed green (found by rehearsing #76 against the demo host).
LIVE_DISPLAY_MAPPINGS = [
    {"display": "PIH: 2618", "resourceVersion": "1.9"},
    {"display": "SNOMED CT: 36118008", "resourceVersion": "1.9"},
    {"display": "ICD-10: J95.811", "resourceVersion": "1.9"},
    {"display": "ICD-11-WHO: CB21.Z", "resourceVersion": "1.9"},
]


def test_the_live_display_only_mapping_shape_still_yields_its_icd10_code():
    assert _icd10_reason_codes({"mappings": LIVE_DISPLAY_MAPPINGS}) == ["J95.811"]


def test_the_display_fallback_applies_the_same_source_filter():
    """ICD-11 must not ride the ICD10 prefix, and a non-ICD source contributes nothing -- the
    display path cannot be laxer than the structured one."""
    for name in ("ICD-10-WHO", "ICD-10", "icd 10", "ICD10", "ICD-10-CM"):
        assert _icd10_reason_codes({"mappings": [{"display": f"{name}: J95.811"}]}) == ["J95.811"]
    for display in ("ICD-11-WHO: CB21.Z", "SNOMED CT: 36118008", "CIEL: 122657"):
        assert _icd10_reason_codes({"mappings": [{"display": display}]}) == []


def test_a_structured_term_still_wins_where_a_deployment_materialises_it():
    """Both shapes on one mapping: the structured term is the more precise source of truth."""
    mapping = {"display": "ICD-10: J93.9",
               "conceptReferenceTerm": {"code": "J95.811", "conceptSource": {"name": "ICD-10-WHO"}}}
    assert _icd10_reason_codes({"mappings": [mapping]}) == ["J95.811"]


def test_a_non_icd10_structured_term_falls_through_to_the_display():
    """A mapping can carry a materialised non-ICD term AND an ICD-10 display; dropping the whole
    mapping on the first miss would lose the code the order actually needs."""
    mapping = {"display": "ICD-10: J93.9",
               "conceptReferenceTerm": {"code": "36118008", "conceptSource": {"name": "SNOMED CT"}}}
    assert _icd10_reason_codes({"mappings": [mapping]}) == ["J93.9"]


def test_malformed_displays_contribute_nothing_and_never_raise():
    junk = {"mappings": [
        {"display": None},
        {"display": "no-colon-at-all"},
        {"display": "ICD-10:"},
        {"display": "ICD-10:    "},
        {"display": ": J93.9"},
        {"display": "ICD-10:  J93.0  "},
    ]}
    assert _icd10_reason_codes(junk) == ["J93.0"]


def test_display_mappings_dedup_and_keep_order():
    mappings = [{"display": "ICD-10-CM: J95.811"}, {"display": "ICD-10-WHO: J93.9"},
                {"display": "ICD-10: J95.811"}]
    assert _icd10_reason_codes({"mappings": mappings}) == ["J95.811", "J93.9"]


def test_a_live_shaped_order_resolves_with_its_reason_codes_end_to_end():
    """The regression that mattered: ten STAT pneumothorax studies resolved WITHOUT reasonCode on
    the demo host, so triage never applied the +25 promotion and the cohort topped out at URGENT."""
    out = _resolve(_order({"mappings": LIVE_DISPLAY_MAPPINGS}))
    assert out["reasonCode"] == ["J95.811"]
    assert out["priority"] == "stat"
