# Contributing

Create a focused branch from `main`. Preserve provider, interface, persistence, and privilege boundaries. Run `make test`, `make build`, and relevant packaging checks before opening a pull request. Never commit credentials, generated state, private infrastructure details, or release artifacts. Include migration, update, rollback, and security implications when applicable.

Keep public documentation generic and reproducible; infrastructure-specific automation belongs outside this repository.
