"""Explicit test-only host for retained normalized-workspace HTTP contracts.

Never import this module from an application entry point. The public app keeps
using the same repositories/services, but does not expose this retired flow.
"""
from backend.main import create_app
from backend.api import assignments, courses, grading_runs, results, submissions


def create_legacy_contract_app():
    app = create_app()
    app.router.routes = [route for route in app.routes if not getattr(route, "path", "").startswith("/courses")]
    for router in (courses.router, assignments.router, submissions.router, grading_runs.router, results.router):
        app.include_router(router)
    return app


app = create_legacy_contract_app()
