"""Deterministic test doubles for the normalized workflow (Task 14).

The fake provider feeds the unchanged grading pipeline a fixed correction per
(q_id, student) so the adapter/run lifecycle and the E2E flow are deterministic
without external network or real API keys. It never alters prompts, skills, or
scoring code — it stands in for the LLM at the provider boundary.
"""
