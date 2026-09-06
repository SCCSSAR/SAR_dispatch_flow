# Operating the Dispatch Console

Day-to-day running: what to watch, how to read the logs, and how to tell whether a deploy
actually happened.

> **The failure mode to design against is the quiet one.** Almost nothing here is about
> the app crashing — a crash is obvious and someone fixes it. The entries below exist
> because this system has several ways to look completely healthy while doing the wrong
> thing: a deploy that reports the right version over the wrong code, a config change that
> was merged but never applied, a log filter that returns nothing and reads like proof.
> Learn those three and you have most of the operational risk covered.

---

## Reading the logs

Application logs and request logs live in different fields. This matters more than it
sounds.

Anything the code logs itself lands in `jsonPayload.message`:

```bash
gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=<service>
   AND jsonPayload.message:"<substring>"' \
  --limit=20 --project <project-id> \
  --format='value(timestamp,jsonPayload.message)'
```

The uvicorn access line lands in `textPayload`. Use that field only for request-level
questions such as authorization rejections.

**A `textPayload` filter against an application log line returns zero rows and exit code
0.** That result is indistinguishable from "the code never logged anything." Reading it as
a negative finding will send you to debug a component that was working. Match the field to
the kind of line you are looking for.

Timestamps are UTC.

---

## Verifying a deploy

Check two layers. The version string alone is not evidence.

1. Request `/version` and confirm the version and commit.
2. Confirm a marker that only the new code produces.

For a frontend change, request the served page and grep for a string the change
introduces:

```bash
curl -s https://<service-url>/ | grep -c <new-string>
```

A count of `0` means the build is mislabeled.

For a backend-only change there is no served asset to grep. Either read the module out of
the image by digest, or send an input whose output only the new code can produce.

**Why the label can lie:** the build script reads the version and commit into shell
variables near the start, and Docker snapshots the build context much later. Any change to
the working tree between those two moments produces an image with correct version
arguments and different code. For the same reason, do not run `git checkout`, `switch`,
`stash`, `pull` or `reset` in the repository while a build is running. Use a worktree
outside the repository directory instead.

---

## Configuration changes

Environment variables are managed by Terraform. Build scripts do not read Terraform
configuration.

To change an environment variable:

1. Edit the environment's `.tf` file.
2. Run `terraform apply`.
3. Confirm the new value on `/version` or in the service description.

A merged `.tf` change that has not been applied has not taken effect. The deployment looks
healthy and behaves as it did before.

---

## Rate limits

Per-user limits default to 5 per minute, 20 per hour and 50 per day, with a global cap of
200 per day. Override them with the `OCR_RATE_LIMIT_PER_MINUTE`, `OCR_RATE_LIMIT_PER_HOUR`,
`OCR_RATE_LIMIT_PER_DAY` and `OCR_DAILY_GLOBAL_CAP` environment variables.

Counters live in Firestore.

A burst of rate-limit events usually means a client is retrying, not that a dispatcher is
working quickly. Read the logs before you raise a limit.

> **The limiter is not a double-submit guard.** It catches the sixth request in a minute,
> not the second one. Two clicks a few hundred milliseconds apart both pass. Anything that
> must not run twice needs its own guard at the point of no return — this codebase uses an
> atomic document create as a tombstone before any outbound side effect.

---

## Cost

Vertex AI is the only meaningful cost at typical volume. Cloud Run, Firestore and Artifact
Registry stay near the free tier for a few activations a month.

Set a budget alert on the billing account when you deploy. Do this before you need it.

A resource inventory cannot price anything. Enable billing export if you need to attribute
cost, because every resource API returns counts and never currency.

---

## Secrets

Add every new secret version through `bin/rotate-secret.sh`. The wrapper updates the
rotation labels together with the version, and the app reads those labels to report
rotation age.

Cloud Run resolves a secret when a container starts. A new version reaches a running
container only when that container is replaced. Deploy a new revision when a rotation must
take effect immediately.

Restrict every API key to the specific APIs it needs.

---

## Monitoring

Watch these:

- Error rate and latency on the Cloud Run service.
- Rate-limit events.
- Vertex AI spend against the budget alert.
- Secret rotation age.
- Failed Cloud Tasks in the polling queues.

The task queues use small retry budgets on purpose. A default retry budget hides an outage
for hours behind automatic retries instead of surfacing it.

---

## When something goes wrong

**If the app returns 500 on upload:** read `jsonPayload.message` for the request. Confirm
the Vertex AI API is enabled and the model name is valid.

**If the extracted text is wrong:** confirm which path handled the form. Deterministic PDF
extraction and multimodal OCR fail in different ways, and the event log records which one
ran.

**If staging recommendations are missing or implausible:** confirm the address geocoded to
the expected region. A staging list is only as good as its anchor, and a wrong anchor
produces a well-formed list of the wrong places.

**If a downstream system received nothing:** confirm the integration's mode. A mode flag
set to its non-live value is the intended behavior, not a fault.

**If a change appears not to have deployed:** verify both layers before debugging anything
else. Most of the time the code is not there.

> **Prefer evidence that could have come out differently.** "The version matches" and "the
> log filter returned nothing" both feel like confirmation and neither one is. Before you
> accept a clean result, ask what it would have looked like if the thing you are checking
> were broken. If the answer is "the same," you have not tested it yet.
