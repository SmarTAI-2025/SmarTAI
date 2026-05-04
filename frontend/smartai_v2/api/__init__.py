"""HTTP client layer for the SmarTAI FastAPI backend.

All modules expose async functions returning plain dicts/lists. Reflex State
classes call these in @rx.event handlers and update state with the result.
"""
