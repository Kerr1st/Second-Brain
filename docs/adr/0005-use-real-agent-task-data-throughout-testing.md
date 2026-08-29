---
status: accepted
---

# Use real Agent Task data throughout testing

Real Codex history may be used at every testing and evaluation stage, and raw real-data fixtures may
be committed to Git. There is no requirement to synthesize or redact fixture content. This accepts
that committed history remains in Git and relies on repository access controls; an isolated test
database remains required only to prevent test mutations from damaging the live memory store.
