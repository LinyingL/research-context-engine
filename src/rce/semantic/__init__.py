"""Optional semantic enhancement layer (DESIGN.md section 0/7): a local,
vendor-neutral model client. Off by default and never a prerequisite -- the
deterministic layers in rce.ingest are fully usable without this package.

This package currently exposes only rce.semantic.backend's LlmBackend /
LlmError, an OpenAI-compatible chat-completions client (stdlib urllib only,
zero third-party dependency). The logic that reviews deterministic
`backed_by` candidates and proposes `supports` edges from its output lands
in a later task; per the constitution, that logic may only write via the
machine paths (`status` in {auto, pending}) -- a model's output is evidence/
annotation input to that decision, never a `confirmed`/`rejected` write.
"""
