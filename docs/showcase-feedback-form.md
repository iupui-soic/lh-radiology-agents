# Showcase feedback form (#76)

The evaluation instrument for the MIMIC-CXR showcase. One participant, one form, filled at the
end of a session while the arcs are fresh. `scripts/mimic/showcase_metrics.py` measures what the
pipeline did; this measures what the participant thought of it.

**How it is recorded.** The operator transcribes each form into one JSON file per participant:

```bash
cd scripts/mimic
python showcase_feedback.py template > ~/feedback/P01.json    # blank, every key present
$EDITOR ~/feedback/P01.json                                    # fill from the paper form
python showcase_feedback.py ~/feedback/                        # the tally
```

**Identity.** Participants are pseudonymous here: `P01`, `P02`, assigned by the operator. There is
no name field and none should be added. Who took part is recorded once, in the run-book's
credentialing record (prerequisite 4), and that is the only place it belongs. Do not write patient
identifiers, accession numbers or report text into the free-text boxes.

**Scale.** Every scored item is 1 to 5:

| 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| not at all | a little | neutral | quite | very much |

Leave an item **blank** if you did not see that part of the demo. Blank is a real answer here: it
is dropped from the averages rather than counted as a low score.

---

## A. About you

- Participant ID (assigned by the operator): `________`
- Role: radiologist / referring physician / trainee / other: `________`
- Years in practice since qualification: `________`
- PhysioNet credentialed: yes / no
- Session date: `____-__-__`
- Arcs seen: 1 (routine) / 2 (pneumothorax) / 3 (sloppy dictation) / 4 (pre-read EHR)

## B. Each stage

For each stage below, score the same three questions:

- **Usefulness**: did this help you do the read?
- **Trust**: would you rely on it as shown, today?
- **Workflow fit**: does it fit how you actually work?

The three come apart on purpose. A stage can be useful and still not trusted, and that gap is the
result worth having.

| # | Stage | Usefulness | Trust | Workflow fit | Comment |
|---|-------|-----------|-------|--------------|---------|
| 1 | **Reading worklist**: the priority order, and what sits at the top | | | | |
| 2 | **Viewer**: hanging protocol, the AI finding banner, the CAD evidence overlay | | | | |
| 3 | **Pre-sign AI draft impression** waiting in the RIS before you read | | | | |
| 4 | **Post-sign verification** verdict and the sign-off gate | | | | |
| 5 | **Critical-result page** and the acknowledgement loop | | | | |
| 6 | **Pre-read EHR context**: labs, medications, problems | | | | |

## C. Overall

1. Would you use this in practice as it stands? yes / no / undecided
2. Biggest value:
3. Biggest concern:
4. Any safety concern you would want addressed before this touched a real patient:
5. Anything missing that you expected to see:

---

## Notes for the operator

- Hand the form out **after** the arcs, not between them, so an early stage is not scored against
  a promise the later arcs keep.
- Score every stage the participant saw, including ones they disliked. A refused stage with a
  comment is more useful than a blank.
- Section C question 4 is the one to read out loud and wait on. It is the question this whole
  showcase exists to surface, and people volunteer it least.
- Transcribe the same day. Then run the tally and keep the summary next to the session's metrics
  output in the demo diary.
