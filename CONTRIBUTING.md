# Contributing

Thanks for looking. Before anything else, one thing about what this repository is.

Dispatch Turbo is an **operational tool**. A change merged here can page real volunteers to
a real search for a real missing person, and it handles that person's name, date of birth
and medical information along the way. That shapes everything below: the review is slow on
purpose, the tests are unusually opinionated, and some parts of the system are deliberately
closed to change.

None of that means contributions are unwelcome. It means the most useful contribution is
often not a pull request.

---

## The most useful things you can contribute

**If you run SAR for another team:** open an issue. Questions about adapting this to your
agency, your forms, your notification system are genuinely wanted — they are the fastest
way the documentation gets better for the team after you. You do not need to write code to
be useful here.

**If you found a security issue:** do not open an issue. Follow [`SECURITY.md`](SECURITY.md).

**If the documentation is wrong:** say so, or send a pull request. Docs drift against code
silently, and an outside reader notices things a maintainer has stopped seeing.

**If you want to write code:** read the rest of this file first.

---

## Never put real data in this repository

This is the rule that has actually been broken, and the one worth stating first.

Do not include, in code, tests, issues, pull requests, screenshots or commit messages:

- A real missing person's name, date of birth, address, photograph or medical information.
- A real responder's or officer's name, phone number or email address.
- A real incident number, a real call-out form, or a live API key.

Test fixtures must be synthetic. This is not hypothetical caution: the fixtures in this
repository were once pasted verbatim from real dispatches to pin "the exact line shape",
and cleaning that up afterwards was expensive. A synthetic fixture pins a line shape just
as well.

If you need to show a real failure, describe it. Redact the specifics, or send them by
email rather than putting them in a public issue.

---

## Read the Locked Design Decisions before you change behaviour

[`CLAUDE.md`](CLAUDE.md) contains a long table of Locked Design Decisions. Each row states
a rule and what breaking it caused — most of them were written after something went wrong
on a live call-out.

They read like arbitrary constraints and they are not. A staging filter that looks
redundant is load-bearing because of how a geocoder failed in July. A retry that looks like
it should always run is gated because running it always floods a display cap. A guard that
looks like a duplicate is the belt to another guard's braces, because the field it protects
is editable by a dispatcher after the first check ran.

If your change contradicts one of these rows, that is fine — but say so in the pull
request, quote the row, and explain what changed. A pull request that silently reverts one
will be closed, and it may take a while for anyone to notice which is worse for you than a
fast no.

---

## Pull request policy

**Every pull request is reviewed and merged by the project lead. Nobody self-merges,
including the maintainer, and including automated dependency updates.** `main` is protected
and takes no direct pushes.

Practically, this means:

- Expect review to take days, not hours. This is a volunteer project run around real
  call-outs.
- A pull request that is not merged is usually not rejected. Ask.
- Large unsolicited pull requests are the ones most likely to stall. Open an issue first
  and get agreement on the approach before writing the code.

**One logical change per pull request.** If you find a second problem while fixing the
first, note it in an issue rather than bundling it in. Reviewing two changes at once
reliably produces a worse review of both.

**Keep the diff small.** No reformatting, renaming, or refactoring outside the change
itself. Match the surrounding style even where you would write it differently — a diff that
mixes a fix with a reformat hides the fix.

---

## Before you start

**To run the test suite you need Python 3.11 or later and `pytest`. That is the whole list.**

**Do not `pip install -r backend/requirements.txt` just to run the tests.** That file is the
*runtime* dependency set for the deployed container, and the suite is deliberately built to run
without it. On a clean machine `httpx`, `slack_sdk`, `google-cloud-*` and `fastapi` are all
absent and the suite still runs in a few seconds. Installing them does not get you a better run
— it starts a handful of tests that currently skip, some of which expect credentials you do not
have.

Two consequences worth knowing before a test result confuses you:

- **Most backend modules cannot be imported locally.** Six of the seven — `main`, `slack`,
  `d4h`, `caltopo`, `everbridge`, `rate_limit` — need cloud libraries that are not installed.
  Only `pdf_extract` imports cleanly. This is why so many tests read production source **as
  text** and assert on its structure: it is the only way to pin behaviour in a module the test
  cannot import. Rename a function and expect a failure on a string, not on behaviour.
- **A dozen or so tests skip, and that is a healthy run.** The causes are all "a library or a
  file is not here": `google-genai`, `httpx`, `utm`, `google.api_core`, and two dispatcher
  documents that are not shipped in this repository. A run of roughly 2,300 passing with about
  a dozen skipped is what success looks like.

### What you will not be able to run

- **A deployment.** That needs your own Google Cloud project — see
  [`docs/DEPLOYING.md`](docs/DEPLOYING.md) for the full setup, including the `gcloud`, Docker
  and Terraform prerequisites.
- **Corpus validation of an OCR change.** The corpus is real call-out forms and cannot be
  published. `backend/migration_validation/apply_helpers.py` is shipped and takes a
  `--corpus-root`, so point it at your own forms. If you are changing OCR or a text helper,
  validating against *some* real corpus is still the expectation — synthetic cases do not
  represent handwriting.
- **The spikes.** `experiments/` is intentionally never committed. Where a design note cites a
  spike, it is recording what evidence produced a number, not handing you a script.

`CLAUDE.md` opens with a fuller version of this for anyone running an AI coding agent against
the repository, including which of its instructions name tools that are not part of this
project.

## Running the tests

```bash
python3 -m pytest -q backend/
```

The full suite must pass before a pull request is opened. It also runs as a pre-flight step
in the deployment scripts, so a broken test blocks a deploy, not just a merge.

Two things about the tests that will otherwise surprise you:

**Some tests read production source as text.** Several modules cannot be imported under a
local pytest run because they need cloud credentials, so their behaviour is pinned by
reading the source file and asserting on its structure. If you rename a function or move a
constant, expect a test to fail on the string, not on the behaviour. That is deliberate —
those pins exist because hand-written test mirrors drifted from production while staying
green.

**A test that passes is not proof that it tests anything.** Pins in this repository are
expected to have been mutation-tested: reintroduce the bug, confirm the pin fails, then put
it back. Eight separate pins in this repository once asserted nothing at all and passed. If
you add a pin, prove it can fail and say so in the pull request.

---

## Commit and pull request format

Commit messages use conventional prefixes: `fix:` `feat:` `docs:` `refactor:` `perf:` `chore:`.

Write the subject in the imperative and under about 70 characters. Use the body to say
**why** — ideally naming the failure mode that prompted the change. "What" is already in
the diff.

The pull request template asks for a summary and a test plan. Fill in the test plan
honestly: "did not test X" is a useful sentence and reviewers would rather read it than
discover it.

---

## What will be declined

- Speculative features and abstractions with no current caller.
- Unrelated cleanup bundled into a functional change.
- Anything that widens what is written to logs about a person. The privacy guarantees in
  [`SECURITY.md`](SECURITY.md) are enforced by tests, and those tests do not get relaxed to
  make a change fit.
- Anything that removes a guard because it looks redundant, without evidence that the
  failure it was written for can no longer happen.

---

## Licence

Contributions are accepted under the [BSD 3-Clause licence](LICENSE) that covers this
repository. There is no separate contributor licence agreement.
