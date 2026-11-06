# Quarantine

Scratch that should not sit on the repo root lives here.

`job_search.db` is not the canonical database. The live store is `state.db`
(or `DB_FILE`). A zero-byte `job_search.db` is scratch: move it here and do
not open it as application state. This directory is otherwise gitignored.
