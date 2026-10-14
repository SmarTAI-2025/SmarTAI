"""Explicit test-only host for retained normalized-workspace HTTP contracts.

Never import this module from an application entry point. The public app keeps
using the same repositories/services, but does not expose this retired flow.
"""
from backend.main import create_app
from backend.api import assignments, courses, grading_runs, results, submissions


def create_legacy_contract_app():
    app = create_app()
    # Newer FastAPI keeps included routers lazy; older versions flatten routes.
    # Remove the public label router in either representation before mounting
    # the legacy contract, so response-model filtering cannot mask old fields.
    app.router.routes = [
        route for route in app.routes
        if getattr(route, "original_router", None) is not courses.public_router
        and not getattr(route, "path", "").startswith("/courses")
    ]
    for router in (courses.router, assignments.router, submissions.router, grading_runs.router, results.router):
        app.include_router(router)
    return app


app = create_legacy_contract_app()
