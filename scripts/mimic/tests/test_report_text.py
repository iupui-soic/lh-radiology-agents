"""strip_html runs to a fixpoint: the TinyMCE double-encode class.

The RIS report form (TinyMCE) re-encodes markup handed to it as text, so a pasted body reaches
the radiology_report row double-encoded. The old single strip-then-unescape pass stripped the
outer tags and then unescaped the inner ones back into LITERAL tags -- exactly the conclusion
fhir2 stored for s56689183 (2026-08-09), which the verification parser could not read, so the
signed study parked at the sign-off gate. These pin the loop, the letter-after-`<` guard that
keeps dictated comparisons out of the tag pattern, and the invariants the rest of the repo
leans on: idempotence, and whitespace runs never collapsed (the cohort forensics fingerprint
stored-vs-manifest bodies on that invariant).
"""
import html
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from report_text import strip_html  # noqa: E402

# The live shape: TinyMCE wrapped an already-encoded body in a fresh <p>.
DOUBLE = ("<p>&lt;p&gt;FINDINGS: Right apical pneumothorax. No effusion. / "
          "IMPRESSION: Pneumothorax, communicated.&lt;/p&gt;</p>")


def test_single_encoded_tinymce_body_is_unchanged_behaviour():
    assert strip_html("<p>FINDINGS: Clear lungs.</p>") == "FINDINGS: Clear lungs."


def test_double_encoded_body_reaches_plain_text():
    out = strip_html(DOUBLE)
    assert out.startswith("FINDINGS:") and "IMPRESSION:" in out
    assert "<" not in out and "&lt;" not in out


def test_triple_encoded_body_converges_too():
    out = strip_html("<p>" + html.escape(DOUBLE) + "</p>")
    assert "IMPRESSION:" in out
    assert "<" not in out and "&lt;" not in out and "&amp;" not in out


def test_strip_html_is_idempotent():
    once = strip_html(DOUBLE)
    assert strip_html(once) == once


def test_dictated_comparisons_are_not_tags():
    # The old any-char pattern ate "< 3 cm and improving >" whole. A tag must start with a
    # letter (or / or !) -- in the raw text AND after any unescape round mints new `<`.
    s = "Effusion < 3 cm and improving > baseline."
    assert strip_html(s) == s
    assert strip_html("<p>pH &lt; 7.2, then &gt; 7.3 on repeat</p>") == \
        "pH < 7.2, then > 7.3 on repeat"


def test_whitespace_runs_are_never_collapsed():
    assert strip_html("FINDINGS:  two  spaces\n\nand a blank line") == \
        "FINDINGS:  two  spaces\n\nand a blank line"


def test_empty_and_none_are_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""
