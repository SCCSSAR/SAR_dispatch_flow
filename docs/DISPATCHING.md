# Dispatching with the Console

What a dispatcher does, start to finish, and what the app is doing underneath. This is the
generic version — your team's own guide should carry your URL, your contacts, and your
fallback procedure.

> **The app is an accelerator on top of a manual process, not a replacement for it.** Every
> dispatcher using it should already be able to run a callout by hand, because the day it
> is slow or wrong is the day someone is missing. Train the manual process first. Keep the
> fallback one click away. A dispatcher who has to think about what to do when the app
> misbehaves has already lost the time the app was meant to save.

---

## Before a callout

- Confirm you can sign in. Sign-in requires an account on the authorized dispatcher list.
- Confirm you know your team's manual fallback procedure.
- Confirm the footer shows the integration modes you expect.

The footer reports the mode of each integration. If a mode reads as its test value when
you expect live, stop and ask before you dispatch.

---

## The workflow

### Step 1 — Upload the form

1. Open the app.
2. Sign in with your authorized account.
3. Upload the intake form.

A fillable PDF is read field by field and is effectively exact. A photograph is read by
the model and is not. Upload the PDF whenever one exists.

Processing takes about a minute. A first request after an idle period takes longer,
because the service starts from cold.

### Step 2 — Review the extracted text

1. Read the extracted summary in the text area.
2. Correct any field that is wrong.
3. Read the event log at the top of the summary.

The text area is editable, and what you leave in it is what gets dispatched. Downstream
systems read the corrected text, not the original extraction.

The event log records only corrections and failures, so a short log is a good sign. Read
any warning it contains before you dispatch.

> **Check the location before anything else.** Everything downstream — the map, the staging
> recommendations, the range rings, the link responders tap on their phones — is computed
> from the last known position. If that address resolved to the wrong place, every one of
> those will be confidently, consistently wrong together, and each will look fine on its
> own. It is the one field worth reading twice.

**If a photographed form produced a date the form does not carry, delete it.** Model
extraction can supply a plausible value for a field it cannot actually read.

### Step 3 — Dispatch

Each action is independent. Run the ones your team uses.

1. Send the notification to your alerting platform.
2. Create the incident map.
3. Create the incident record.
4. Open the reference map if you want one.

Responders who accept are added to the incident channel as their replies arrive. You do
not need to keep the app open for that to happen — the polling runs server-side.

### Step 4 — After dispatching

- Monitor the responder tally.
- Post updates in the incident channel.
- Close the notification through your alerting platform when the callout ends.

---

## Staging recommendations

The app geocodes the last known position, queries nearby points of interest, and ranks
them. Parks, schools and shopping centres rank above fuel stations, because a SAR staging
area needs parking, room to brief, and somewhere to wait.

Recommendations are ordered. Number one is the app's best answer, not a promotion of
whatever the officer wrote.

### When nothing is close enough

That search runs about three quarters of a mile out. In hilly or rural areas it sometimes
finds nothing at all, and the app then searches again out to three miles. Your Event Log
says when this happened and names the wider radius.

Those recommendations are real places. They also sit farther out than what you normally
see, so weigh the travel time against the officer's own staging before you commit. If even the wider search comes up
empty, the staging message your responders see carries an `⚠️ Unverified address` warning,
and the list should be read as a starting point rather than an answer.

**To override the staging location**, enter an address, a latitude and longitude pair, or a
UTM coordinate. An override you enter is authoritative and is plotted as the command post.

### When the form gives you only coordinates

On wilderness searches, and on remote mutual aid especially, the requesting agency
frequently supplies only a coordinate — no street address, no cross street, no landmark.
Treat this as normal input. Nothing has gone wrong, and you do not need to convert
anything by hand.

Both surfaces accept a coordinate:

- Write it into the **Staging Area for Resources** field on the intake form. The app
  recognises a coordinate there before it tries to geocode the text as an address.
- Or type it into the override panel, in latitude/longitude or UTM mode.

Use the panel when the form carried no coordinate, or when the officer gives you one over
the radio after the form has arrived.

**UTM is supported.** Enter it as zone, easting, northing — `10S 590309E 4142188N`,
keeping the `E` and `N` suffixes. The app converts it and renders the staging line as
`37.42210, -121.97936 — 10S 590309E 4142188N`. Digital-map users get a coordinate their
phone can search; radio-trained responders still see the UTM they trained on. One line
serves both.

MGRS grid references are not read — the form carrying a 100 km square, such as
`10S EG 59030 42188`. Convert to full UTM first, or use latitude/longitude.

Two warnings can appear on the responder-facing staging message:

- A location conflict warning means the officer's staging location and the last known
  position disagree by an implausible distance. Confirm with the officer before responders
  roll.
- An unverified address warning means the points-of-interest lookup returned nothing, so
  the recommendation list was generated rather than measured. Treat every entry as
  unconfirmed.

Both warnings clear when you correct the underlying problem and dispatch again.

> **Staging text is what responders' phones search for.** The staging line becomes a maps
> query on the responder's device, so a friendly venue name that geocodes somewhere else
> sends the whole callout to the wrong place. A street address is always safer than a
> building's name. A decimal latitude and longitude works as a query; a raw UTM string does
> not, which is why the app writes both.

---

## Known limits

- Extraction from photographs is materially less accurate than from fillable PDFs,
  particularly for checkbox grids. Verify checkbox answers on a photographed form.
- A wrong address produces a complete, plausible, wrong set of recommendations.
- Rate limits apply per dispatcher. If you hit one during a callout, fall back to manual
  and raise it afterwards.

---

## When something goes wrong

**If the app is slow or unavailable:** fall back to your manual process immediately. Do
not troubleshoot during a callout.

**If sign-in is refused:** fall back to manual. Resolve access afterwards.

**If the extracted text is wrong:** correct it in the text area. The correction is what
gets dispatched.

**If the map or the recommendations look wrong:** check the last known position first. It
is the input everything else is derived from.

**If a downstream system received nothing:** confirm its mode in the footer before
assuming a fault.
