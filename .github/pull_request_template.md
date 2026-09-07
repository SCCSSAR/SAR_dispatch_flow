## Summary

<!-- What changed, and why. Name the failure mode this fixes where there is one. -->

-

## Test plan

<!-- What you actually ran or checked. "Did not test X" is a useful line — write it. -->

- [ ] `python3 -m pytest -q backend/` passes

## Checklist

- [ ] One logical change. No unrelated cleanup or reformatting.
- [ ] No real subject, responder or officer data, and no live keys, in the code, the
      tests, or this description.
- [ ] Does not silently contradict a Locked Design Decision in `CLAUDE.md` — or quotes
      the row and explains the change.
- [ ] Any new test pin was mutation-tested: the bug was reintroduced and the pin failed.
