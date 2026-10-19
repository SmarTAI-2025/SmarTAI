# Temporary Demo cleanup and bounded service logs

Demo identities retain the existing absolute two-hour lifetime (not an idle
timeout). The public app scans immediately on startup and every 60 seconds,
revokes expired/inactive guests and queues the existing durable cleanup worker.
Explicit exit queues the same cleanup immediately. Files, task progress/errors,
courses, usage and guest identity are removed only after producer draining and
verified storage deletion. Failures retain a retryable request. Daily global
admission/model counters survive individual session cleanup. Ordinary teacher
closure, mailbox verification, grading and source ownership are unchanged.
Ready requests are ordered by retry eligibility (creation time for untried
requests), so one failed or draining session cannot monopolize each poll.

Temporary task/source copies still use the existing teacher pipeline and are
reclaimed with the session; this change does not redesign immutable fixture
sharing or cache grading results between visitors. The published sample files
remain shared website assets. Existing database backups and cloud snapshots
have their own retention and are not rewritten by the expiry worker.

Demo task progress is already stored per task and deleted with it. Application
service logs redact guest-specific messages/arguments/tracebacks in request,
workflow, grading and storage-cleanup contexts. They keep a generic diagnostic
without the guest/task identity. Other users' logs remain unchanged. No extra
per-visitor log file is introduced. Signed Demo responses carry
`X-SmarTAI-Transient-Demo: 1` so Nginx can omit their normal access records.
Transport failures before a response, Nginx error logs and PostgreSQL's own
errors remain subject to host retention; this is not selective journal erasure
or forensic erasure of WAL, snapshots, third-party logs or backups.

## Ubuntu host configuration

Apply these separately from application deployment, after saving the current
configuration to the operator's workstation. Paths below match the audited
Lightsail host; review distribution/user differences before using elsewhere.

| Source | Destination |
| --- | --- |
| `journald.conf` | `/etc/systemd/journald.conf.d/60-smartai-retention.conf` |
| `logrotate-timer.conf` | `/etc/systemd/system/logrotate.timer.d/60-smartai-hourly.conf` |
| `logrotate-nginx` | `/etc/logrotate.d/nginx` |
| `logrotate-postgresql` | `/etc/logrotate.d/postgresql-common` |
| `nginx-demo-logging.conf` | `/etc/nginx/conf.d/smartai-demo-logging.conf` |

In Nginx's existing HTTP-level access log directive, retain the same combined
format but add the conditional flag (do not add a second duplicate log):

```nginx
access_log /var/log/nginx/access.log combined if=$smartai_log_request;
```

Check `nginx -t` and `logrotate --debug /etc/logrotate.conf` before applying a
reload. Reload Nginx, run `systemctl daemon-reload`, restart `logrotate.timer`
and `systemd-journald`, then verify service state, timer schedule, effective
journald config and both application readiness endpoints. Restore the saved
configuration and reload/restart those same services if validation fails.
No application or database restart is required. The response-based access-log
filter activates only when the corresponding application code is deployed.

Persistent journals retain up to 14 days with a 512 MiB budget, a 4 GiB
filesystem free-space requirement and daily file rotation. Runtime journals
keep their separate 100 MiB budget to avoid increasing memory-backed storage.
The 4 GiB value preserves this host's existing default free-space protection.
These are usage ceilings, not preallocated storage or guaranteed history.
Active/preallocated journal files and file-granularity deletion mean this is
not an exact byte/second boundary. Text logs rotate daily or above 10 MiB at
the next hourly check, retain at most 14 archives and discard archives over
14 days at rotation. Heavy traffic may shorten retention; hourly checks are
not a hard active-file byte cap. This is automatic rotation, not a monthly
bulk deletion job. Logs help diagnose recent faults, deployment changes,
intermittent failures and unusual access; months of history are not needed
for this small deployment's current operational use.

References: [journalctl vacuum semantics](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html),
[journald retention](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html).
