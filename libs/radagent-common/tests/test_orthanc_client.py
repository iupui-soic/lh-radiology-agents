"""Tests for OrthancClient (issue #20).

Uses monkey-patched `_get` to intercept the HTTP call — same pattern as
test_fhir_client. Focus is on:
  * URL / params construction (single round trip via ?expand=1)
  * lean-projection shape (never raises on partial records)
  * schema-compat guarantees for the join in the Worklist API
"""
from __future__ import annotations

import asyncio

from radagent_common.orthanc_client import OrthancClient, _lean_study


def test_lean_study_projects_all_common_fields():
    raw = {
        "ID": "aorta-study-001",
        "MainDicomTags": {
            "StudyInstanceUID": "1.2.840.113619.2.55.3.111111111",
            "AccessionNumber":  "ACC-AORTA-001",
            "StudyDescription": "CT AORTA W CONTRAST",
            "StudyDate":        "20260707",
        },
        # The modality is a computed tag: it arrives in RequestedTags, never in a study's
        # MainDicomTags (the fixture mirrors a live /studies?expand&requestedTags record).
        "RequestedTags": {"ModalitiesInStudy": "CT"},
        "LastUpdate": "20260707T123005",
    }
    out = _lean_study(raw)
    assert out == {
        "orthancStudyId":    "aorta-study-001",
        "studyInstanceUID":  "1.2.840.113619.2.55.3.111111111",
        "accessionNumber":   "ACC-AORTA-001",
        "modality":          "CT",
        "studyDescription":  "CT AORTA W CONTRAST",
        "studyDate":         "20260707",
        "lastUpdate":        "20260707T123005",
    }


def test_lean_study_reads_modality_from_requested_tags():
    """The shape a real /studies?expand&requestedTags=ModalitiesInStudy record has: the
    computed tag arrives in a RequestedTags block, NEVER in the study's MainDicomTags
    (Modality is series-level; verified live on Orthanc). Pinned so the silent-empty
    modality column cannot come back."""
    raw = {"ID": "x",
           "MainDicomTags": {"StudyInstanceUID": "1.2.3"},
           "RequestedTags": {"ModalitiesInStudy": "CR"}}
    assert _lean_study(raw)["modality"] == "CR"


def test_lean_study_passes_multi_modality_value_through():
    """A multi-modality study carries the full backslash-separated DICOM value."""
    raw = {"ID": "x", "RequestedTags": {"ModalitiesInStudy": "CT\\SR"}}
    assert _lean_study(raw)["modality"] == "CT\\SR"


def test_lean_study_falls_back_to_maindicomtags_modality_keys():
    """A deployment that promotes the tag via ExtraMainDicomTags (or hands a series-shaped
    record to the projector) must still find it."""
    assert _lean_study({"ID": "x", "MainDicomTags": {"ModalitiesInStudy": "CT"}})["modality"] == "CT"
    assert _lean_study({"ID": "x", "MainDicomTags": {"Modality": "MR"}})["modality"] == "MR"


def test_lean_study_tolerates_missing_maindicomtags():
    """Defensive: a partial Orthanc record must not raise (would knock a
    study off the worklist entirely). Every string field degrades to ''."""
    out = _lean_study({"ID": "x"})
    assert out["orthancStudyId"] == "x"
    assert out["studyInstanceUID"] == ""
    assert out["modality"] == ""


def test_lean_study_omits_instance_count():
    """numberOfInstances is not projected: /studies?expand carries no Statistics
    block, so the field would always be null. It must be absent, not null."""
    out = _lean_study({"ID": "x", "MainDicomTags": {"StudyInstanceUID": "1.2.3"},
                       "Statistics": {"CountInstances": 280}})
    assert "numberOfInstances" not in out


def test_list_completed_studies_uses_expand_single_round_trip():
    client = OrthancClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return [{"ID": "s1", "MainDicomTags": {"StudyInstanceUID": "1.2.3"}},
                {"ID": "s2", "MainDicomTags": {"StudyInstanceUID": "1.2.4"}}]

    client._get = fake_get  # type: ignore[assignment]
    studies = asyncio.run(client.list_completed_studies())
    # Single round trip via ?expand=1 — critical for the join in the Worklist API
    # to stay O(1) rather than N+1 as the worklist grows. requestedTags rides the same
    # call: it is the only way the modality reaches the row (see list_completed_studies).
    assert calls == [("studies", {"expand": True, "requestedTags": "ModalitiesInStudy"})]
    assert [s["orthancStudyId"] for s in studies] == ["s1", "s2"]


def test_list_completed_studies_empty():
    """Orthanc with no studies must return [] (not None) — the caller uses
    this in a for-loop directly."""
    client = OrthancClient()

    async def fake_get(path, params=None):
        return []

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.list_completed_studies()) == []


def test_list_completed_studies_none_response_still_returns_list():
    """A malformed Orthanc reply (None instead of []) still yields [] rather
    than a TypeError in the caller."""
    client = OrthancClient()

    async def fake_get(path, params=None):
        return None

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.list_completed_studies()) == []


def test_get_study_fetches_by_orthanc_id():
    client = OrthancClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"ID": "abc", "MainDicomTags": {}}

    client._get = fake_get  # type: ignore[assignment]
    got = asyncio.run(client.get_study("abc"))
    assert calls == ["studies/abc"]
    assert got["ID"] == "abc"


# --- get_study_description (issue #62) --------------------------------------
# The field the interpretation tool registry selects on. The Orthanc stable event does not carry it,
# so ingress reads it back from here; without it every study falls through to the generic tool.
def _client_returning(raw):
    client = OrthancClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return raw

    client._get = fake_get  # type: ignore[assignment]
    return client, calls


def test_get_study_description_reads_the_dicom_tag():
    client, calls = _client_returning(
        {"ID": "s1", "MainDicomTags": {"StudyDescription": "CT HEAD WITHOUT CONTRAST"}})
    assert asyncio.run(client.get_study_description("s1")) == "CT HEAD WITHOUT CONTRAST"
    assert calls == ["studies/s1"]


def test_get_study_description_empty_when_the_tag_is_missing():
    # A partial Orthanc record degrades to "" rather than raising -- the study still gets a workflow,
    # it just gets the registry's generic tool. Same tolerance as _lean_study.
    client, _ = _client_returning({"ID": "s1", "MainDicomTags": {}})
    assert asyncio.run(client.get_study_description("s1")) == ""
    client, _ = _client_returning({"ID": "s1"})
    assert asyncio.run(client.get_study_description("s1")) == ""
    client, _ = _client_returning({})
    assert asyncio.run(client.get_study_description("s1")) == ""


def test_get_study_description_strips_padding():
    # DICOM string values are padded to an even length; a trailing space would defeat the registry's
    # keyword match and is not a description.
    client, _ = _client_returning({"MainDicomTags": {"StudyDescription": "  CHEST AP  "}})
    assert asyncio.run(client.get_study_description("s1")) == "CHEST AP"


# --- pixel path (#27/#71): the first real read of image DATA, not just metadata ----------------

def _fake_orthanc(client: OrthancClient, tree: dict) -> list[str]:
    """Stand in for Orthanc's study -> series -> instance walk. Records the paths fetched so a test
    can assert the client made the walk rather than guessing ids."""
    seen: list[str] = []

    async def fake_get(path, params=None):
        seen.append(path)
        return tree[path]

    client._get = fake_get  # type: ignore[assignment]
    return seen


def test_list_study_instances_orders_by_series_then_instance_number():
    """A CXR tool scores "the first instance". If the order is whatever Orthanc happened to return,
    a two-view study is a coin flip and the demo may silently score the LATERAL. Order is by
    SeriesNumber then InstanceNumber, so the pick is reproducible.

    The fixture deliberately returns everything backwards.
    """
    client = OrthancClient(base_url="http://orthanc:8042")
    seen = _fake_orthanc(client, {
        "studies/s1": {"Series": ["ser-B", "ser-A"]},                      # series out of order
        "series/ser-A": {"MainDicomTags": {"SeriesNumber": "1"}, "Instances": ["i2", "i1"]},
        "series/ser-B": {"MainDicomTags": {"SeriesNumber": "2"}, "Instances": ["i3"]},
        "instances/i1": {"MainDicomTags": {"InstanceNumber": "1"}},        # instances out of order
        "instances/i2": {"MainDicomTags": {"InstanceNumber": "2"}},
        "instances/i3": {"MainDicomTags": {"InstanceNumber": "1"}},
    })
    out = asyncio.run(client.list_study_instances("s1"))
    assert out == ["i1", "i2", "i3"], "instances must come back in (series, instance) order"
    # and the ids came from a REAL study -> series -> instance walk, not from guessing
    assert "studies/s1" in seen and {"series/ser-A", "series/ser-B"} <= set(seen)
    assert {"instances/i1", "instances/i2", "instances/i3"} <= set(seen)


def test_untagged_series_sorts_last_rather_than_winning_the_first_instance_pick():
    """An untagged SeriesNumber must NOT sort as 0 and steal the first-instance slot from a real
    series 1. Mutation: make _as_int return 0 on failure and this test fails."""
    client = OrthancClient(base_url="http://orthanc:8042")
    _fake_orthanc(client, {
        "studies/s1": {"Series": ["ser-untagged", "ser-1"]},
        "series/ser-untagged": {"MainDicomTags": {}, "Instances": ["ix"]},          # no SeriesNumber
        "series/ser-1": {"MainDicomTags": {"SeriesNumber": "1"}, "Instances": ["i1"]},
        "instances/ix": {"MainDicomTags": {}},
        "instances/i1": {"MainDicomTags": {"InstanceNumber": "1"}},
    })
    out = asyncio.run(client.list_study_instances("s1"))
    assert out[0] == "i1", "the tagged series 1 must win the first slot, not the untagged one"


def test_list_study_instances_on_a_study_with_no_series_is_empty_not_an_error():
    client = OrthancClient(base_url="http://orthanc:8042")
    _fake_orthanc(client, {"studies/empty": {}})
    assert asyncio.run(client.list_study_instances("empty")) == []


def test_get_instance_dicom_fetches_the_raw_file_not_the_preview_png():
    """/file is the Part-10 original. /preview is 8-bit and pre-windowed -- a model fed that is
    scoring a picture of the image, not the image."""
    client = OrthancClient(base_url="http://orthanc:8042")
    grabbed: list[str] = []

    async def fake_get_bytes(path):
        grabbed.append(path)
        return b"DICM-bytes"

    client._get_bytes = fake_get_bytes  # type: ignore[assignment]
    out = asyncio.run(client.get_instance_dicom("inst-9"))
    assert out == b"DICM-bytes"
    assert grabbed == ["instances/inst-9/file"]
    assert "preview" not in grabbed[0]
