# Local Invite Registration Design

## Goal

Restore invite-only registration in the local environment, initialize the empty
local database with an administrator, and create a small batch of teacher invite
codes for local use.

## Configuration

- Set `SMARTAI_REGISTRATION_CLOSED=true` in the Git-ignored root `.env` file.
- Do not change `.env.example` or production deployment configuration.
- Restart the backend after the change so the process reloads the setting.

## Data Initialization

- Create the local administrator with username `admin` and the password supplied
  by the user at execution time.
- Generate five teacher invite codes through the existing authentication
  repository, attributed to that administrator.
- Leave invite email and course binding empty so each code can be used by any
  teacher registrant.
- Set each invite to expire seven days after creation.

## Safety

- Do not commit or print the administrator password in repository files.
- Do not commit invite codes; return them only in the task result.
- Do not reset or replace the database.
- Stop if an existing `admin` account has conflicting state instead of
  overwriting it.

## Verification

1. Confirm the parsed registration setting is closed.
2. Confirm an active administrator named `admin` exists.
3. Confirm five unused, unexpired teacher invites were created by that
   administrator.
4. Report the backend restart requirement and the generated codes.
