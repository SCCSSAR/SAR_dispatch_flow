"""
gemini.py — Vertex AI Gemini call for SAR form OCR + reasoning.

PRIVACY: Image bytes are passed in-memory as raw bytes. They are never written to
disk, GCS, Firestore, or logs. The local variable holding the bytes is the only
copy; it is garbage-collected when this function returns.

The returned string is the plain-text incident summary. It is returned directly
to the browser — never persisted server-side.
"""

import datetime
import logging
import os
import time

# Issue #111 — Vertex AI SDK migration. Both call sites (extract_incident_summary
# image+text path and extract_staging_and_koester text-only PDF path) now use
# the new google-genai SDK. The legacy vertexai SDK has been fully removed.
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vertex AI configuration
# ---------------------------------------------------------------------------

# No deployment-specific default. A hardcoded project ID here means anyone
# who deploys this without setting GCP_PROJECT silently points Vertex AI at
# someone else's project and gets an opaque permission error. Validated at
# point of use in _get_client(), matching d4h.py and main.py, so importing
# this module never requires the variable.
GCP_PROJECT = os.environ.get("GCP_PROJECT", "").strip()
GCP_REGION  = os.environ.get("GCP_REGION", "us-central1")
MODEL_ID    = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_client: "genai.Client | None" = None


def _get_client() -> genai.Client:
    """Return a lazily-initialized Vertex AI client (singleton per process).

    `vertexai=True` selects Vertex AI mode (requires project + location);
    omitting it would default to the Gemini Developer API mode.

    `api_version="v1"` pins the SLA-backed stable Vertex AI endpoint
    explicitly. Per Google's 2026-04-30 announcement, the google-genai SDK
    will default-flip from v1beta1 → v1 at SDK v2.0.0 in February 2027.
    All features we use (gemini-2.5-flash, generate_content,
    GenerateContentConfig, Part.from_bytes, FinishReason.MAX_TOKENS) are
    in stable v1, so pinning now removes any risk of subtle behavioral
    drift when we eventually upgrade past google-genai 1.x.

    Issue #111 — Vertex AI SDK migration. Used by both extract_incident_summary
    (image+text path) and extract_staging_and_koester (text-only PDF path).
    """
    global _client
    if _client is None:
        if not GCP_PROJECT:
            raise ValueError(
                "GCP_PROJECT is not set. Vertex AI requires the GCP project ID "
                "of the deployment; set it in the Cloud Run service env (it is "
                "declared in terraform/environments/<env>/main.tf)."
            )
        _client = genai.Client(
            vertexai=True,
            project=GCP_PROJECT,
            location=GCP_REGION,
            http_options=types.HttpOptions(api_version="v1"),
        )
    return _client


# ---------------------------------------------------------------------------
# Canonical SAR dispatcher extraction prompt
# __INTAKE_TIMESTAMP__ is replaced at call time with the current UTC clock.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Role: You are an expert Search and Rescue Dispatcher. Your task is to extract data \
from a handwritten "Santa Clara County Search and Rescue Call-Out" form and \
initialize an incident log. Output format is plain text.

OUTPUT FORMAT RULES — MANDATORY:
- Output raw plain text ONLY. Do NOT wrap output in markdown code fences (``` or ```text).
- Do NOT use any markdown formatting. No bold, no headers with #, no bullet points with *.
- Begin your response directly with "Initial Incident Summary:" — no preamble.
- You MUST output exactly four lines containing only "---" (three hyphens) as section \
  separators between the five output sections: after Initial Incident Summary, after Event Log, \
  after LPB Questionnaire, and after Staging Area Recommendations. \
  Do NOT omit these separators. Example structure: \
  "Initial Incident Summary:\n...\n---\nEvent Log:\n...\n---\nLPB Questionnaire:\n...\n---\nStaging Area Recommendations:\n...\n---\nLPB Range Ring Analysis:\n..."

INTERNAL GEOCODING DATA — FOR YOUR REASONING ONLY, DO NOT REPRODUCE IN OUTPUT: \
The LKP address has been geocoded to real-world coordinates: __LKP_COORDS__ \
Use these coordinates as your geographic anchor when generating Staging Area \
Recommendations. Do not include this block or these coordinates anywhere in your output.

CONSTRAINTS: Use ONLY the handwritten data visible in the attached JPEG. \
Do not invent or assume any details not present on the form.

CONFIDENCE CHECK:
- If a handwritten word is smudged or illegible, mark it as [??] or [Low Confidence].
- Do not guess at unclear text.

ADDRESS VERIFICATION — MANDATORY FIRST STEP:
You MUST verify ALL handwritten location names before producing any output — this includes \
the LKP street address AND the staging area (park or other location name). \
This step is not optional — perform it even if you believe the handwritten spelling is correct.

Steps:
1. Read the full street address from the form (number, street name, city, state, zip).
   Also read the staging area name from the "Staging Area for Resources" field.
2. Look up this address in your knowledge of real street names in that city and zip code. \
   Ask yourself: does this exact street name exist in this city/zip? \
   - Example: "Traden Dr, San Jose, CA 95124" — the real street is "Tradan Dr", not Traden. \
   - Example: "Lincon Ave" — the real street is "Lincoln Ave". \
   - Example: "Almaden Expy" — correct, no change needed.
   Also verify park/location names in the Staging Area field: \
   - Example: "CAROZZA PARK MILPITAS" or "CAROCZA PARK" — the correct name is \
     "Cardoza Park, Milpitas". The 'Z' cluster near the start is consistently misread; \
     the real park is Cardoza (C-A-R-D-O-Z-A), not Carozza or Carocza. \
   - Officers write staging locations without a comma (e.g. "CARDOZA PARK MILPITAS"); \
     always add the comma in output: "Cardoza Park, Milpitas".
3. If there is ANY discrepancy between the handwritten spelling and the verified real name:
   a. Use the VERIFIED (correct map) spelling in EVERY field of your output without exception: \
      Event Name, Last Seen At, Last Known Position, Staging Area for Resources, \
      the "0." LKP reference in Staging Area Recommendations, and all Event Log entries.
   b. The handwritten (unverified) spelling must appear ONLY once, inside the correction note. \
      The correction note must use ONLY the misspelled street name (not the full address, \
      not the house number): \
      "Address corrected from [misspelled street name only] to [correct street name only]" \
      Example: "Address corrected from TRADEN to Tradan" — NOT "from 1100 TRADEN DR to 1100 Tradan Dr."
   c. Add that note to the Event Log, timestamped __INTAKE_TIMESTAMP__.
4. If the handwritten spelling exactly matches a verified real street name, proceed normally.

Note on zip codes: The zip code written on the form may be incorrect, unknown, or left \
blank — do NOT use it as a verification signal. Verify only the street name and city. \
If you believe the zip code on the form is wrong based on the verified street location, \
correct it silently (use the correct zip in your output) and note the correction in the \
Event Log along with any street name correction.

CRITICAL RULE: If you make a correction, do NOT use the handwritten misspelling anywhere \
else in your output — not in Event Name, not in Last Seen At, not anywhere. Only the \
correction note itself may contain the original handwritten spelling.

LPB QUESTIONNAIRE EXTRACTION:
FORM VERSION DETECTION — Before reading any checkboxes, determine the form version:
  - If you see "v2" anywhere in the form title or header area (e.g., "Call-Out v2" or \
a small "v2" watermark/label), this is a VERSION 2 form.
  - If no version marker is present, assume VERSION 1.

The form contains up to 12 checkbox questions in the "Missing Person (MP) Information" \
section.

VERSION 2 form checkbox layout: The YES/NO checkbox columns are on the LEFT side of the \
questionnaire section, immediately to the LEFT of the Q# number for each row. The question \
text appears to the RIGHT of the Q# number. The column headers "YES" and "NO" appear at \
the top of these two left-side columns.

VERSION 1 form checkbox layout: The checkbox grid has exactly TWO narrow columns on the \
RIGHT side of the page (right margin), with column headers "YES" and "NO" printed at the \
top of the grid.

CRITICAL LAYOUT FACT (applies to BOTH versions): The YES column is ALWAYS the LEFT of the \
two checkbox columns. The NO column is ALWAYS the RIGHT of the two checkbox columns. Each \
column contains a small square box for each question row. Only one box per row should be \
checked with an X.

For each question, determine the answer as follows:
STEP 1 — Locate the X mark for this question row. If no X is present, the answer is UNCERTAIN.
STEP 2 — Determine which of the TWO boxes in that row contains the X:
  - If the X is in the LEFT box of the pair → answer is YES (left = YES column)
  - If the X is in the RIGHT box of the pair → answer is NO (right = NO column)
  - If you cannot confidently distinguish left from right, or the X falls between boxes → UNCERTAIN
STEP 3 — Cross-check: trace a vertical line from the X upward to the column headers at \
  the top of the grid. Confirm the header matches your Step 2 determination. \
  If Step 2 and Step 3 disagree, output UNCERTAIN rather than guessing.

KNOWN HARD CASES — read these before processing Q11 and Q12:
- Q11 (Hospitals checked): On the standard SCCSSAR form, the YES box for Q11 is on the \
  LEFT side of the two-box pair. A single X in a box for Q11 is almost always YES — \
  dispatchers routinely check hospitals before calling SAR. If you see an X anywhere in \
  the Q11 row, look very carefully: is it in the LEFT box (YES) or RIGHT box (NO)? \
  Do not default to NO — the YES box being slightly closer to the question label is the \
  normal layout. Apply STEP 2 carefully: left = YES.
- Q12 (Surveillance cameras / CCTV): This is less commonly checked Yes. If you see \
  an X for Q12, apply STEP 2 carefully. If it is in the RIGHT box, the answer is NO.

- Output the answer as exactly one of: Yes / No / Unknown / UNCERTAIN
  - Use UNCERTAIN when: the mark is faint, falls between columns, left/right position \
    is genuinely ambiguous, or Step 2 and Step 3 give conflicting results.
  - UNCERTAIN answers are displayed as "NOT ANSWERED (flag for follow-up)" to the dispatcher.
  - Do NOT guess. When in doubt, output UNCERTAIN.
- If NO box is checked for a question, output UNCERTAIN.
- If a checked answer indicates an at-risk factor (e.g., dementia, alone, \
no phone, unfamiliar with area, no cold-weather gear), include that factor \
in the "at-risk" line of the Missing Person field.

EVENT LOG TIMESTAMPS:
- Entry 1: Read the "Date of Request" and "Time of Request" fields from the top \
of the form (the call-out time when the SO contacted SAR). \
You MUST reformat the date and time into ISO format: YYYY-MM-DD HH:MM — regardless \
of how it appears on the form. Example: form shows "1/7/26" and "800" → output \
"2026-01-07 08:00". Four-digit year, two-digit month, two-digit day, 24-hour HH:MM time.
- Entry 2: Use __INTAKE_TIMESTAMP__ for when this intake form was digitally processed. \
PREFIX the message with the detected form version (from FORM VERSION DETECTION in the \
LPB QUESTIONNAIRE EXTRACTION section above). Format exactly: \
"<timestamp> - v1 Intake form processed; Initial Incident Summary created" when no v2 \
marker was found, OR "<timestamp> - v2 Intake form processed; Initial Incident Summary \
created" when "v2" was visible in the form header. The version prefix is mandatory — it \
lets dispatchers and engineers confirm which checkbox layout branch was applied.
- Entry 3 (only if an address correction was made): use __INTAKE_TIMESTAMP__ again.

INSTRUCTIONS:
FIRST: Produce the "Initial Incident Summary".
SECOND: Produce the "Event Log".
THIRD: Produce the "LPB Questionnaire" section.
FOURTH: Produce the "Staging Area Recommendations" section.
FIFTH: Produce the "LPB Range Ring Analysis" section using Robert Koester's methodology.

---

Initial Incident Summary:

Event Name: YYYY-MM-DD AGENCY STREET
Event #: [from form]
Agency: [from form]
Contact: [officer name and phone from form]
Missing Person: [full name]; at-risk: [all risk factors from form + LPB questionnaire. \
If "Mental health component" is Yes and its detail names a condition already listed as a primary \
at-risk factor (e.g., Dementia), include "Mental health component" in the list WITHOUT repeating \
the detail — e.g., "Dementia, Weather, Alone, Mental health component" NOT "Mental health component: Dementia"]
DOB: [DOB from form] ([calculated age] years old)
Last Seen At: [date/time only from form — e.g., "1/6/26 2300". Do NOT include address here; that goes in Last Known Position below]
Last Known Position: [full address including street, city, and state — e.g., "1100 Tradan Dr., San Jose, CA"]
Residence Address: [subject's home/residence address from form including city and state; \
if same as Last Known Position, repeat it; if not on form, output "Not recorded"]
Last Seen Wearing: [if present on form, else "Not recorded"]
Staging Area for Resources: [Copy the staging area written on the form by the officer, \
if present, with verified spelling. This is the officer's designated RP (rendezvous point) \
where the officer will be waiting — it takes absolute priority over app recommendations. \
If the officer did not write a staging area, leave this blank. \
Apply name verification to the staging location exactly as you do to street addresses: \
look up the park or location name, correct any misspelling, and add city if written without one. \
The officer writes staging locations without a comma between name and city \
(e.g. "CARDOZA PARK MILPITAS") — output with correct punctuation: "Cardoza Park, Milpitas". \
Specific known location: "CARDOZA PARK" or any spelling variant (CAROZZA, CAROCZA, CAROOZA) \
refers to Cardoza Park, Milpitas, CA — always output as "Cardoza Park, Milpitas". \
Log any name correction in the Event Log.]
CalTopo Map ID:
Dispatcher:

---

Event Log:
[Date of Request from form] [Time of Request from form] - Request received from [agency/officer from form]
__INTAKE_TIMESTAMP__ - Intake form processed; Initial Incident Summary created
[Only if address corrected]: __INTAKE_TIMESTAMP__ - Address corrected from [handwritten text] to [verified address]

---

LPB Questionnaire:
Q1 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Familiar with area
Q2 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Has phone — number: [if noted on form]
Q3 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Uses public transit (VTA)
Q4 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Entered into MUPS — date: [if noted]
Q5 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Alone
Q6 - [Yes / No / NOT ANSWERED (flag for follow-up)] - At-risk — reason: [if noted]
Q7 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Proper equipment (hiking gear, cold-weather gear, etc.)
Q8 - [Yes / No / Unknown / NOT ANSWERED (flag for follow-up)] - Speaks English
Q9 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Mental health component — detail: [if noted]
Q10 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Prior missing
Q11 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Hospitals checked
Q12 - [Yes / No / NOT ANSWERED (flag for follow-up)] - Surveillance cameras / CCTV

Format each line as: Q# - ANSWER - Question text
DESIGN DECISION (do not revert without team discussion): Answer appears BEFORE question \
text so dispatchers can scan the answer column at a glance. Internal UNCERTAIN label \
must NOT appear in output — use "NOT ANSWERED (flag for follow-up)" instead.

---

INTERNAL STAGING DATA — FOR YOUR REASONING ONLY, DO NOT REPRODUCE IN OUTPUT:
__STAGING_CANDIDATES__

---

Staging Area Recommendations:

DESIGN DECISION: The dispatcher — not the AI — selects the final staging location. \
Rank entries purely by operational quality using the pre-fetched candidate list. \
Work through the list TOP TO BOTTOM in the order provided — the list is already sorted \
by priority and distance — do NOT reorder it. \
The total list is capped at 7 entries. \
Skip a candidate ONLY if it is explicitly excluded by a time-of-day rule below. \
Do NOT skip candidates because of perceived parking or lighting concerns — include them \
and let the dispatcher decide.

Do NOT include the LKP or Residence address as a staging entry — they are already shown \
as separate markers on the CalTopo map. Do NOT stage directly at the LKP/residence address.

OFFICER-DESIGNATED STAGING — LABELING RULES:
Check the "Staging Area for Resources" field in the incident data. If it is not blank:
  - If the officer's location MATCHES one of the pre-fetched candidates (by name or address): \
    include that candidate in its normal ranked position and append the exact phrase \
    " — Officer-designated staging location" at the END of the body after all other text. \
    Example: "2. Cardoza Park, Milpitas — City park. 0.45 mi from LKP; parking ~15–20 vehicles; \
restrooms likely; lighting unverified. — Officer-designated staging location."
  - If the officer's location does NOT match any pre-fetched candidate: append it as the \
    LAST numbered entry: "[N]. [officer staging text] — Officer-designated staging location \
(not among top recommendations — dispatcher discretion)."
  - If "Staging Area for Resources" is blank: omit any officer entry.

OFFICER STAGING LABEL RULE — MANDATORY: Any officer-designated entry MUST contain the EXACT \
phrase "Officer-designated staging location" verbatim. Do NOT paraphrase. Server-side code \
uses this exact phrase to place the Command Post marker on the CalTopo map — any variation \
silently drops the marker.

All entries use plain sequential numbering (1, 2, 3…) — no special O./0. prefixes.

If no pre-fetched candidates were provided, use your own knowledge to find suitable \
locations within 0.75 miles of the LKP coordinates — prioritize fast food, pharmacies, \
and hotels first; gas stations last (poor parking availability).

GEOGRAPHIC DIVERSITY RULE: Do NOT select more than 2 candidates from the same street. \
If 3 or more candidates share the same street name, include only the closest 2 from that \
street and skip the rest — choose the next-closest candidates from different streets instead. \
This ensures the dispatcher has genuinely different route options, not 4 locations on one block.

All staging locations should have direct road access for emergency vehicles.

Time-of-day and day-of-week rules for exclusions (current time: __CURRENT_DAYTIME__):
- Schools and colleges (any location labeled "School" in the candidate list, \
  including high schools and colleges tagged as such in OSM): exclude during \
  school hours (7am–3:30pm weekdays). Available and preferred in evenings, \
  weekends, and holidays.
- Churches / places of worship: good staging Mon–Sat. Less preferred Sunday morning \
  (services typically 8am–12pm) — exclude or note limited access if Sunday AM. \
  Do NOT apply Sunday restrictions on any other day of the week.

PARK FORMAT RULE — MANDATORY:
DESIGN DECISION (do not revert): Parks are navigated by name, not street address. \
Dispatchers and field teams look up parks by name. A street address for a park entrance \
is often wrong or misleading (multiple entrances, wrong gate, etc.). \
For ANY location that is a park, open space, recreation area, or sports/community field: \
  - Format as: "[N]. [Park Name, City] — [park type description]. [details sentence]" \
  - Example: "3. Cardoza Park, Milpitas — City park. 0.45 mi from LKP; parking ~15–20 vehicles; restrooms likely; well-lit." \
  - Do NOT include a street address for parks. \
  - This applies to ALL park entries including any officer-designated park entry.

Format each non-park entry as:
[N]. [Full street address, City] — [Business or location name]. [One sentence covering, in order: \
(1) approximate distance from LKP (e.g. "0.22 mi from LKP"); \
(2) parking capacity as a numeric range estimate (e.g. "parking ~10–15 vehicles") \
    — ALWAYS use a range with an en-dash (e.g. "~5–10", "~10–15", "~15–20"); \
    do NOT use a single number (e.g. never "~10 vehicles"); \
    do NOT use "ample" or "limited"; \
(3) restroom access — pre-fetched candidates do NOT include verified restroom data; \
    infer by type: "restrooms likely" for gas stations, fast food, pharmacies, large grocery, parks with facilities; \
    "restrooms unlikely" for churches, small cafes/donut shops, small retail, storage; \
(4) lighting — pre-fetched candidates do NOT include verified lighting data; \
    infer by type: "well-lit" for gas stations, fast food, 24-hr pharmacies; \
    "lighting unverified" for churches, small shops, parks, residential areas; \
(5) any time-of-day note if applicable.]
IMPORTANT: Always include the city name in the street address for non-park entries. \
Correct: "1898 North Capitol Ave, Milpitas — Valero" \
Wrong: "1898 North Capitol Ave — Valero"

STAGING FORMAT RULE — MANDATORY (non-park entries): The street address MUST come first, then the business name. \
Correct: "1898 North Capitol Ave, Milpitas — Valero" \
Wrong: "Valero — 1898 North Capitol Ave, Milpitas" \
Never swap the order. Address first, dash, then name. (Park entries use park name first — see PARK FORMAT RULE.)

ADDRESS REQUIRED RULE — MANDATORY: Only include a staging candidate if it has a known street \
address OR is a park/open space. If a candidate in the pre-fetched list has no street address \
and is not a park (e.g. only a name like "Silicon Valley University"), SKIP it entirely — \
a dispatcher cannot route emergency vehicles to an unverifiable name alone.

After the numbered list, if any pre-fetched candidates were excluded due to time-of-day \
or day-of-week rules, append a brief note (one line per excluded category). Examples:
  Note: 2 schools excluded — daytime weekday (available after 3:30pm).
  Note: 1 church excluded — Sunday morning service hours (available after ~12pm).
If nothing was excluded, omit this section entirely.

---

LPB Range Ring Analysis (Robert Koester — "Lost Person Behavior"):

1. Subject Category: Identify the single best-fit Koester category based on the subject \
profile, at-risk factors, and LPB questionnaire answers. \
Output this line in EXACTLY this format (no other wording):
"1. Subject Category: [Category]. Key factors: [Factor1], [Factor2]."
Example: "1. Subject Category: Dementia. Key factors: Dementia, Alone."
Use exactly 2-3 factors that directly determined the Koester category classification. \
Valid key factors are subject characteristics such as: the diagnosis (e.g. Dementia), \
behavioral state (e.g. Alone, Despondent), or functional status (e.g. No phone). \
Do NOT use situational/environmental conditions as key factors (e.g. Weather, Cold, Night) \
— those belong in Local Modifiers, not Key factors. \
Do NOT use "At Risk" as a factor (it is a consequence, not a driver). \
Do NOT use "Mental health component" as a factor (it is a category label, not a driver — \
use the specific diagnosis instead, e.g. "Dementia"). \
Example of correct key factors for a Dementia subject who is alone and has no phone: \
"Key factors: Dementia, Alone, No phone."

CHILD AGE BRACKET SELECTION: The Koester child categories are defined by these exact age ranges: \
Child 1-3 (age 1, 2, or 3), Child 4-6 (age 4, 5, or 6), Child 7-9 (age 7, 8, or 9), \
Child 10-12 (age 10, 11, or 12). \
If the subject's age is 13 or older, they do NOT fall into any Child category. \
For ages 13-17 with no other diagnosis, use "Hiker" as the closest Koester category \
and note the age in Key factors. \
If the subject has a mental health diagnosis, use that diagnosis category instead.

2. Koester Statistics for this category:
   DESIGN DECISION (do not revert): miles appear FIRST — field teams think in miles, not km.
   Format each distance line as: "- X.X miles (X.X km) — [percentile label]"
   Example:
   - 0.2 miles (0.3 km) — 25th percentile distance
   - 0.3 miles (0.5 km) — 50th percentile distance (median)
   - 0.6 miles (1.0 km) — 75th percentile distance
   Use ONLY the following published Koester values — do NOT recall values from training data \
   (values listed as mi / km for each percentile: 25th / 50th / 75th):
     Dementia:               0.2 mi (0.3 km) / 0.3 mi (0.5 km) / 0.6 mi (1.0 km)
     Mentally Ill:           0.7 mi (1.2 km) / 1.7 mi (2.8 km) / 4.0 mi (6.5 km)
     Despondent:             0.4 mi (0.7 km) / 1.6 mi (2.5 km) / 3.6 mi (5.8 km)
     Substance Intoxication: 0.2 mi (0.4 km) / 0.7 mi (1.2 km) / 2.4 mi (3.8 km)
     Child 1-3:              0.1 mi (0.2 km) / 0.2 mi (0.4 km) / 0.6 mi (0.9 km)
     Child 4-6:              0.2 mi (0.4 km) / 0.6 mi (0.9 km) / 1.4 mi (2.3 km)
     Child 7-9:              0.3 mi (0.5 km) / 0.9 mi (1.5 km) / 3.3 mi (5.3 km)
     Child 10-12:            0.3 mi (0.5 km) / 1.0 mi (1.6 km) / 3.2 mi (5.2 km)
     Hiker:                  0.8 mi (1.3 km) / 3.0 mi (4.9 km) / 8.6 mi (13.8 km)
     Climber:                0.8 mi (1.3 km) / 2.4 mi (3.9 km) / 5.5 mi (8.8 km)
   Use exactly one decimal place for both mi and km — e.g., "0.3 mi (0.5 km)", \
   never "0.31 mi" or "0.12 mi".

3. Local Modifiers: In exactly one sentence, describe the terrain and environmental \
factors at the LKP using specific terms from this list where applicable: \
"urban grid" (city street network), "major roads" or "freeway barriers" \
(roads that channel or limit travel), "park boundaries", "river/creek barriers", \
"hills" or "canyons". State which factors expand the radius and which constrain it. \
Do NOT add free adjectives like "dense" or adverbs like "nearby" — use the exact terms above. \
Example: "Urban grid and major roads constrain travel to street corridors."

"""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def extract_incident_summary(
    image_bytes: bytes,
    lkp_coords: str = "",
    staging_candidates: list | None = None,
) -> str:
    """
    Send image bytes to Gemini and return the structured incident summary.

    Args:
        image_bytes:        Raw JPEG bytes (already validated by image_validation.py).
                            This is the ONLY copy — never stored, logged, or written anywhere.
        lkp_coords:         Optional geocoded coordinates string to inject into the staging
                            section, e.g. "37.40087,-121.88387 (Berryessa, San Jose, CA)".
                            Leave empty on first pass; populate for second pass with real coords.
        staging_candidates: Optional list of pre-fetched OSM POI dicts from _query_overpass_staging().
                            Each dict has keys: name, amenity, addr, dist_m.
                            When provided, Gemini formats these real candidates instead of
                            inventing locations from training data.

    Returns:
        Plain-text incident summary string as produced by the model.

    Raises:
        RuntimeError: if the Vertex AI call fails.
    """
    # Inject current server time for the intake log entry.
    # Format: "2026-02-19 00:44" (Pacific local time — no timezone label, matches form convention)
    import zoneinfo
    _PT = zoneinfo.ZoneInfo("America/Los_Angeles")
    _now_pt = datetime.datetime.now(_PT)
    intake_timestamp = _now_pt.strftime("%Y-%m-%d %H:%M")
    prompt = SYSTEM_PROMPT.replace("__INTAKE_TIMESTAMP__", intake_timestamp)

    # Inject explicit day-of-week + time for staging exclusion rules.
    # Providing the day name directly prevents Gemini from having to calculate it from the date,
    # which causes non-deterministic errors (e.g., calling Thursday "Sunday").
    current_daytime = _now_pt.strftime("%A, %H:%M PT")  # e.g. "Thursday, 14:12 PT"
    prompt = prompt.replace("__CURRENT_DAYTIME__", current_daytime)

    # Inject geocoded LKP coordinates for staging anchor.
    # If lkp_coords is empty (first pass), use a fallback note.
    coords_value = lkp_coords if lkp_coords else \
        "(coordinates not yet available — use the verified street address above as anchor)"
    prompt = prompt.replace("__LKP_COORDS__", coords_value)

    # Inject pre-fetched OSM staging candidates (or fallback instruction if none available).
    # Gemini's job is to FORMAT this real list — not to invent locations from training data.
    if staging_candidates:
        type_labels = {
            "park": "City park", "fast_food": "Fast food", "fuel": "Gas station",
            "pharmacy": "Pharmacy", "hotel": "Hotel", "motel": "Motel",
            "school": "School", "college": "School",  # treat colleges same as schools for exclusion
            "convenience": "Convenience store",
            "supermarket": "Grocery store", "grocery": "Grocery store",
            "chemist": "Pharmacy", "place_of_worship": "Church/Place of Worship",
            "mall": "Shopping center",  # issue #669
        }
        lines = [
            "STAGING CANDIDATES — pre-fetched from OpenStreetMap (real nearby locations). "
            "Use ONLY these candidates; do NOT substitute other locations from your training data:"
        ]
        for i, c in enumerate(staging_candidates, 1):
            label = type_labels.get(c["amenity"], c["amenity"].replace("_", " ").title())
            dist_mi = c["dist_m"] / 1609.34
            addr = c["addr"] if c["addr"] != "(address not in OSM)" else ""
            addr_part = f" — {addr}" if addr else ""
            lines.append(
                f"  {i}. {c['name']}{addr_part} [{label}, {dist_mi:.2f} mi from LKP]"
            )
        candidates_block = "\n".join(lines)
    else:
        candidates_block = (
            "(No pre-fetched candidates available — use your knowledge to find suitable "
            "locations within 0.75 miles of the LKP coordinates above. Prioritize fast food, "
            "pharmacies, and hotels first; gas stations last (poor parking availability).)"
        )
    prompt = prompt.replace("__STAGING_CANDIDATES__", candidates_block)

    start = time.monotonic()

    try:
        client = _get_client()

        # Pass image inline as raw bytes — no GCS upload, no temp file
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        )

        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[prompt, image_part],
            config=types.GenerateContentConfig(
                temperature=0.1,         # Low temperature — factual extraction, not creative
                # 32768 tokens — raised from 16384 after a confirmed MAX_TOKENS truncation in
                # production (finish_reason=MAX_TOKENS on Torres form, 2026-03-08;
                # latency_ms=67418). Previously raised from 8192→16384 for same reason
                # (finish_reason=MAX_TOKENS Q5). Gemini 2.5 Flash supports up to 65536 output
                # tokens; 32768 gives 2× headroom.
                max_output_tokens=32768,
            ),
        )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        finish_reason = response.candidates[0].finish_reason if response.candidates else None

        # Log operational metadata only — NO PII, NO image data, NO extracted text
        logger.info(
            "Vertex AI call complete | model=%s latency_ms=%d finish_reason=%s",
            MODEL_ID,
            elapsed_ms,
            finish_reason,
        )

        # finish_reason=MAX_TOKENS — output was cut off before completion.
        # Raise immediately so the dispatcher sees an error and retries rather than
        # silently receiving a truncated form that looks complete.
        if finish_reason == types.FinishReason.MAX_TOKENS:
            raise RuntimeError(
                f"Gemini output truncated (MAX_TOKENS) after {elapsed_ms}ms — "
                "retry or check output token budget"
            )

        return response.text

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "Vertex AI call failed | model=%s latency_ms=%d error=%s",
            MODEL_ID,
            elapsed_ms,
            type(exc).__name__,   # log error type only, not message (may contain input data)
        )
        raise RuntimeError("OCR service error") from exc

    finally:
        # Explicitly clear the reference — image_bytes goes out of scope here
        del image_bytes


# ---------------------------------------------------------------------------
# PDF path: text-only staging + Koester prompt
# ---------------------------------------------------------------------------

# DESIGN DECISION (do not revert without team discussion): The PDF ingest path
# extracts form fields directly via AcroForm (100% deterministic for checkboxes).
# Gemini is still needed for staging recommendations and Koester LPB analysis,
# which require geographic reasoning. This prompt handles ONLY those two sections.
# The Initial Incident Summary, Event Log, and LPB Questionnaire are built
# server-side in pdf_extract.py and injected here as __SYNTHETIC_SUMMARY__.
#
# This is a text-only call — no image Part. The Vertex AI call uses
# client.models.generate_content(contents=[text_string]) rather than
# contents=[prompt, image_part] used by the JPEG path.
#
# Placeholders replaced at call time:
#   __SYNTHETIC_SUMMARY__    — pre-built 3-section text from pdf_extract.build_synthetic_summary()
#   __LKP_COORDS__           — geocoded lat/lng string from Nominatim
#   __STAGING_CANDIDATES__   — pre-fetched OSM POI list from Overpass
#   __CURRENT_DAYTIME__      — day-of-week + time PT (for school/church exclusion rules)

STAGING_KOESTER_PROMPT = """\
Role: You are an expert Search and Rescue Dispatcher. A v2 SAR call-out form has been \
submitted as a typed PDF. Form fields have been extracted directly — checkbox answers \
and subject details below are 100% accurate. Your task is to produce ONLY two output \
sections: Staging Area Recommendations and LPB Range Ring Analysis.

OUTPUT FORMAT RULES — MANDATORY:
- Output raw plain text ONLY. Do NOT wrap output in markdown code fences.
- Begin your response directly with "Staging Area Recommendations:" — no preamble.
- You MUST output exactly one line containing only "---" (three hyphens) as a section \
  separator between Staging Area Recommendations and LPB Range Ring Analysis.
- Do NOT reproduce or summarize the pre-extracted incident data below.

PRE-EXTRACTED INCIDENT DATA — use as context for Koester category and staging decisions. \
Do NOT reproduce this in your output:
__SYNTHETIC_SUMMARY__

INTERNAL GEOCODING DATA — FOR YOUR REASONING ONLY, DO NOT REPRODUCE IN OUTPUT:
The LKP address has been geocoded to real-world coordinates: __LKP_COORDS__
Use these coordinates as your geographic anchor when generating Staging Area \
Recommendations. Do not include this block or these coordinates anywhere in your output.

INTERNAL STAGING DATA — FOR YOUR REASONING ONLY, DO NOT REPRODUCE IN OUTPUT:
__STAGING_CANDIDATES__

---

Staging Area Recommendations:

DESIGN DECISION: The dispatcher — not the AI — selects the final staging location. \
Rank entries purely by operational quality using the pre-fetched candidate list. \
Work through the list TOP TO BOTTOM in the order provided — the list is already sorted \
by priority and distance — do NOT reorder it. \
The total list is capped at 7 entries. \
Skip a candidate ONLY if it is explicitly excluded by a time-of-day rule below. \
Do NOT skip candidates because of perceived parking or lighting concerns.

Do NOT include the LKP or Residence address as a staging entry — they are already shown \
as separate markers on the CalTopo map. Do NOT stage directly at the LKP/residence address.

OFFICER-DESIGNATED STAGING — LABELING RULES:
Check the "Staging Area for Resources" field in the pre-extracted data. If it is not blank:
  - If the officer's location MATCHES one of the pre-fetched candidates (by name or address): \
    include that candidate in its normal ranked position and append the exact phrase \
    " — Officer-designated staging location" at the END of the body after all other text. \
    Example: "2. Cardoza Park, Milpitas — City park. 0.45 mi from LKP; parking ~15–20 vehicles; \
restrooms likely; lighting unverified. — Officer-designated staging location."
  - If the officer's location does NOT match any pre-fetched candidate: append it as the \
    LAST numbered entry: "[N]. [officer staging text] — Officer-designated staging location \
(not among top recommendations — dispatcher discretion)."
  - If "Staging Area for Resources" is blank: omit any officer entry.

OFFICER STAGING LABEL RULE — MANDATORY: Any officer-designated entry MUST contain the EXACT \
phrase "Officer-designated staging location" verbatim. Server-side code uses this exact phrase \
to place the Command Post marker on the CalTopo map — any variation silently drops the marker.

All entries use plain sequential numbering (1, 2, 3…) — no special O./0. prefixes.

If no pre-fetched candidates were provided, use your own knowledge to find suitable \
locations within 0.75 miles of the LKP coordinates — prioritize fast food, pharmacies, \
and hotels first; gas stations last (poor parking availability).

GEOGRAPHIC DIVERSITY RULE: Do NOT select more than 2 candidates from the same street. \
If 3 or more candidates share the same street name, include only the closest 2 and skip the rest.

All staging locations should have direct road access for emergency vehicles.

Time-of-day and day-of-week rules for exclusions (current time: __CURRENT_DAYTIME__):
- Schools and colleges (any location labeled "School" in the candidate list, \
  including high schools and colleges tagged as such in OSM): exclude during \
  school hours (7am–3:30pm weekdays). Available and preferred in evenings, \
  weekends, and holidays.
- Churches / places of worship: good staging Mon–Sat. Less preferred Sunday morning \
  (services typically 8am–12pm) — exclude or note limited access if Sunday AM. \
  Do NOT apply Sunday restrictions on any other day of the week.

PARK FORMAT RULE — MANDATORY:
DESIGN DECISION (do not revert): Parks are navigated by name, not street address. \
For ANY location that is a park, open space, recreation area, or sports/community field: \
  - Format as: "[N]. [Park Name, City] — [park type description]. [details sentence]" \
  - Example: "3. Cardoza Park, Milpitas — City park. 0.45 mi from LKP; parking ~15–20 vehicles; restrooms likely; well-lit." \
  - Do NOT include a street address for parks. \
  - This applies to ALL park entries including any officer-designated park entry.

Format each non-park entry as:
[N]. [Full street address, City] — [Business or location name]. [One sentence covering, in order: \
(1) approximate distance from LKP (e.g. "0.22 mi from LKP"); \
(2) parking capacity as a numeric range estimate (e.g. "parking ~10–15 vehicles") \
    — ALWAYS use a range with an en-dash (e.g. "~5–10", "~10–15", "~15–20"); \
    do NOT use a single number; do NOT use "ample" or "limited"; \
(3) restroom access — infer by type: "restrooms likely" for gas stations, fast food, pharmacies, \
    large grocery, parks with facilities; "restrooms unlikely" for churches, small cafes, small retail; \
(4) lighting — infer by type: "well-lit" for gas stations, fast food, 24-hr pharmacies; \
    "lighting unverified" for churches, small shops, parks, residential areas; \
(5) any time-of-day note if applicable.]
IMPORTANT: Always include the city name in the street address for non-park entries. \
Correct: "1898 North Capitol Ave, Milpitas — Valero" \
Wrong: "1898 North Capitol Ave — Valero"

STAGING FORMAT RULE — MANDATORY (non-park entries): The street address MUST come first, then \
the business name. Correct: "1898 North Capitol Ave, Milpitas — Valero". Address first, dash, then name.

ADDRESS REQUIRED RULE — MANDATORY: Only include a staging candidate if it has a known street \
address OR is a park/open space. If a candidate has no street address and is not a park, SKIP it.

After the numbered list, if any pre-fetched candidates were excluded due to time-of-day \
or day-of-week rules, append a brief note (one line per excluded category):
  Note: 2 schools excluded — daytime weekday (available after 3:30pm).
If nothing was excluded, omit this note entirely.

---

LPB Range Ring Analysis (Robert Koester — "Lost Person Behavior"):

Use the pre-extracted subject profile above (DOB/age, at-risk factors, Q9 mental health \
detail, Q6 at-risk reason) to identify the Koester category. The Q answers are 100% \
accurate — no need to re-interpret them.

1. Subject Category: Identify the single best-fit Koester category based on the subject \
profile and pre-extracted Q answers. \
Output this line in EXACTLY this format:
"1. Subject Category: [Category]. Key factors: [Factor1], [Factor2]."
Example: "1. Subject Category: Dementia. Key factors: Dementia, Alone."
Use exactly 2-3 factors. Valid key factors: diagnosis, behavioral state (e.g. Alone, Despondent), \
or functional status (e.g. No phone). Do NOT use situational/environmental conditions as key \
factors. Do NOT use "Mental health component" as a factor — use the specific diagnosis instead.

CHILD AGE BRACKET SELECTION: Child categories: 1-3, 4-6, 7-9, 10-12 (exact age ranges). \
Age 13+: do NOT use any Child category. Ages 13-17 without diagnosis → use "Hiker". \
If subject has a mental health diagnosis, use that category instead.

2. Koester Statistics for this category:
   DESIGN DECISION (do not revert): miles appear FIRST — field teams think in miles, not km.
   Format each distance line as: "- X.X miles (X.X km) — [percentile label]"
   Use ONLY these published Koester values (mi / km for 25th / 50th / 75th percentile):
     Dementia:               0.2 mi (0.3 km) / 0.3 mi (0.5 km) / 0.6 mi (1.0 km)
     Mentally Ill:           0.7 mi (1.2 km) / 1.7 mi (2.8 km) / 4.0 mi (6.5 km)
     Despondent:             0.4 mi (0.7 km) / 1.6 mi (2.5 km) / 3.6 mi (5.8 km)
     Substance Intoxication: 0.2 mi (0.4 km) / 0.7 mi (1.2 km) / 2.4 mi (3.8 km)
     Child 1-3:              0.1 mi (0.2 km) / 0.2 mi (0.4 km) / 0.6 mi (0.9 km)
     Child 4-6:              0.2 mi (0.4 km) / 0.6 mi (0.9 km) / 1.4 mi (2.3 km)
     Child 7-9:              0.3 mi (0.5 km) / 0.9 mi (1.5 km) / 3.3 mi (5.3 km)
     Child 10-12:            0.3 mi (0.5 km) / 1.0 mi (1.6 km) / 3.2 mi (5.2 km)
     Hiker:                  0.8 mi (1.3 km) / 3.0 mi (4.9 km) / 8.6 mi (13.8 km)
     Climber:                0.8 mi (1.3 km) / 2.4 mi (3.9 km) / 5.5 mi (8.8 km)
   Use exactly one decimal place for both mi and km — never "0.31 mi" or "0.12 mi".

3. Local Modifiers: In exactly one sentence, describe the terrain and environmental \
factors at the LKP using specific terms where applicable: \
"urban grid", "major roads" or "freeway barriers", "park boundaries", "river/creek barriers", \
"hills" or "canyons". State which factors expand the radius and which constrain it. \
Do NOT add free adjectives like "dense" or adverbs like "nearby". \
Example: "Urban grid and major roads constrain travel to street corridors."
"""


async def extract_staging_and_koester(
    structured_context: str,
    lkp_coords: str = "",
    staging_candidates: list | None = None,
) -> str:
    """
    Text-only Gemini call for the PDF ingest path.

    Produces ONLY Staging Area Recommendations and LPB Range Ring Analysis.
    The Initial Incident Summary, Event Log, and LPB Questionnaire sections
    are built server-side from AcroForm fields and passed in as structured_context.

    Args:
        structured_context:  Output of pdf_extract.build_synthetic_summary() —
                             the 3-section pre-built text block.
        lkp_coords:          Geocoded lat/lng string from Nominatim.
        staging_candidates:  Pre-fetched OSM POI list from Overpass.

    Returns:
        Plain-text string starting with "Staging Area Recommendations:" and
        containing a single "---" separator before "LPB Range Ring Analysis:".

    Raises:
        RuntimeError: if the Vertex AI call fails.
    """
    import zoneinfo
    _PT = zoneinfo.ZoneInfo("America/Los_Angeles")
    _now_pt = datetime.datetime.now(_PT)

    current_daytime = _now_pt.strftime("%A, %H:%M PT")
    prompt = STAGING_KOESTER_PROMPT.replace("__CURRENT_DAYTIME__", current_daytime)
    prompt = prompt.replace("__SYNTHETIC_SUMMARY__", structured_context)

    coords_value = lkp_coords if lkp_coords else \
        "(coordinates not yet available — use the verified street address in the summary above as anchor)"
    prompt = prompt.replace("__LKP_COORDS__", coords_value)

    # Reuse the same candidate block builder as extract_incident_summary
    if staging_candidates:
        type_labels = {
            "park": "City park", "fast_food": "Fast food", "fuel": "Gas station",
            "pharmacy": "Pharmacy", "hotel": "Hotel", "motel": "Motel",
            "school": "School", "college": "School",  # treat colleges same as schools for exclusion
            "convenience": "Convenience store",
            "supermarket": "Grocery store", "grocery": "Grocery store",
            "chemist": "Pharmacy", "place_of_worship": "Church/Place of Worship",
            "mall": "Shopping center",  # issue #669
        }
        lines = [
            "STAGING CANDIDATES — pre-fetched from OpenStreetMap (real nearby locations). "
            "Use ONLY these candidates; do NOT substitute other locations from your training data:"
        ]
        for i, c in enumerate(staging_candidates, 1):
            label = type_labels.get(c["amenity"], c["amenity"].replace("_", " ").title())
            dist_mi = c["dist_m"] / 1609.34
            addr = c["addr"] if c["addr"] != "(address not in OSM)" else ""
            addr_part = f" — {addr}" if addr else ""
            lines.append(
                f"  {i}. {c['name']}{addr_part} [{label}, {dist_mi:.2f} mi from LKP]"
            )
        candidates_block = "\n".join(lines)
    else:
        candidates_block = (
            "(No pre-fetched candidates available — use your knowledge to find suitable "
            "locations within 0.75 miles of the LKP coordinates above. Prioritize fast food, "
            "pharmacies, and hotels first; gas stations last (poor parking availability).)"
        )
    prompt = prompt.replace("__STAGING_CANDIDATES__", candidates_block)

    start = time.monotonic()

    try:
        client = _get_client()

        # Text-only call — no image Part
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0.1,
                # 16384 tokens — raised from 8192 after a confirmed MAX_TOKENS truncation
                # in live testing (finish_reason=MAX_TOKENS on staging+Koester pass for PDF path,
                # 2026-03-04). Matches the JPEG-path token budget. 8192 was insufficient
                # when staging candidates are verbose and Koester analysis is long.
                max_output_tokens=16384,
            ),
        )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        finish_reason = response.candidates[0].finish_reason if response.candidates else None

        logger.info(
            "Vertex AI staging+Koester call complete (PDF path) | model=%s latency_ms=%d finish_reason=%s",
            MODEL_ID,
            elapsed_ms,
            finish_reason,
        )

        if finish_reason == types.FinishReason.MAX_TOKENS:
            raise RuntimeError(
                f"Gemini staging output truncated (MAX_TOKENS) after {elapsed_ms}ms — retry"
            )

        return response.text

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "Vertex AI staging call failed (PDF path) | model=%s latency_ms=%d error=%s",
            MODEL_ID,
            elapsed_ms,
            type(exc).__name__,
        )
        raise RuntimeError("Staging service error") from exc
