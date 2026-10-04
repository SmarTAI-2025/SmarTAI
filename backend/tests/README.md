# Backend test execution

Run the full suite locally with `python -m pytest backend/tests -q --durations=30`.
Without `--test-shard`, no tests are deselected by the CI sharding hook.

SQLite tests receive a fresh file-backed database before every test. The empty
ORM schema is created once after collection and copied into the disposable test
database; rows, schema changes and SQLite sidecars never carry over. Foreign
keys remain enabled by the application's database engine. PostgreSQL fixtures
retain their existing schema and migration lifecycle. Tests that exercise DDL
or migration changes should create their own database, as the migration suites
already do; do not mutate the shared schema template or ORM metadata.

GitHub runs four independent SQLite jobs with `--test-shard=1/4` through `4/4`.
The deterministic partition balances collected case counts and keeps each test
module together, preserving its fixture and test order. Every shard collects
the same suite. Run shards on separate runners; concurrent local processes
must have separate database URLs, storage roots and temporary/lock directories.

`Backend (SQLite)` retains the existing required check name and succeeds only
when all four jobs succeed. Each job records slow setup/call/teardown timings
and uploads a JUnit report retained for seven days. Old runs for the same PR
are cancelled when a newer commit arrives; main/manual runs are independent,
and the existing successful-main CI requirement for AWS deployment is unchanged.
