# Single-use registration invitations

New teacher registrations require an invitation **and** the existing permitted
school email verification. Existing accounts, login, password reset, sessions
and anonymous Demo admission do not check invitations. Invitations never choose
roles, enroll students or grant model credits; the retired invite API stays closed.

Generate the initial batch of 50 outside the repository:

```sh
python tools/generate_registration_invites.py /private/path/new-batch
```

The destination must not already exist. It contains `invitation-codes.txt` for
private distribution, `registration-invites.env` with only SHA-256 digests, and
a checksum manifest. The directory is mode 700 and the files mode 600. Each code
has 120 random bits, is case-insensitive and accepts omission of printed hyphens.
Never commit either the plaintext list or deployment configuration.

Before deploying this version, copy the generated digest setting
`SMARTAI_REGISTRATION_INVITE_DIGESTS` into the backend's private environment file.
Keep the full list when updating it; replacing it revokes other unspent codes.
An empty configuration admits no new registrations. Apply Alembic revision
`0031_registration_invites` before starting the new backend. No extra dependencies,
public administration endpoints, deployment hooks or administrator UI are needed.

The current AWS deploy process loads `/etc/smartai/smartai.env`, backs up the
database, runs `alembic upgrade head`, and then starts the new release. Preloading
the digest setting does not change the old backend's behavior. Publish the code
through a reviewed PR; a successful main CI triggers the existing AWS deployment.
Verify both frontend and backend deployments before distributing invitations.

Sending or resending an email does not reserve or consume a code. The first
successful confirmation inserts its digest into `registration_invites` in the
same transaction that creates the teacher account. The primary key makes two
simultaneous confirmations compete in the database. A failed account insert rolls
back the consumption. Confirming the same successful email link is idempotent.
Deleting an account or restarting the server does not free the code.

Old **unconfirmed** email links without an invitation must restart registration
with a valid code; accounts already created remain unchanged. Used or revoked
invitations are rejected on request, resend and confirmation. Do not delete the
consumption ledger or downgrade the invitation migration on a used database:
that would lose the single-use history. Code rollback can leave this additive
schema in place; a backup restore also requires reconciling consumption history.

Validation uses synthetic codes and fake email delivery, including independent
SQLite/PostgreSQL connections racing for the same invitation. Never spend any
of the real 50 codes as a smoke test.
