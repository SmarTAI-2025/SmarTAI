# SmarTAI frontend

The supported frontend is the React application in [app/](app/). Its public
build serves the landing page, live demo and teacher task workflow; the
administrator console uses a separate private build.

See [app/README.md](app/README.md) for development and deployment configuration.
Current hosting is Cloudflare Pages for the public frontend and AWS for the API.

The former Reflex application has been retired. This directory no longer
contains its executable configuration, pages or state modules. Remaining Reflex
design notes are historical references, not a runnable fallback or deployment
instructions. Do not restore an old frontend route or legacy grading API to
work around a problem in the current task workflow.

Retained normalized teacher/student source is not imported by the public
application. Its old HTTP workflow is unmounted from the standard backend app;
isolated contract tests exercise the preserved services without exposing those
URLs. Legacy task bookmarks that still exist only redirect to the current
workflow. The live demo and signed-in teachers use that same task implementation.
