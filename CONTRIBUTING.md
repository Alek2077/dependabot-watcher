# Contributing

Thank you for considering a contribution to Dependabot Watcher.

## Before opening a change

- Use GitHub Issues for reproducible bugs and focused feature proposals.
- Report vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md); do not open a public security issue.
- Keep changes within Watcher's deterministic evidence, policy, authorization,
  audit, and recovery boundaries. Model output must not become action
  authority.
- The maintainer may accept, decline, or rescope a proposal to keep the project
  safe and maintainable.

## Development setup

Dependabot Watcher requires Python 3.11 or newer.

```bash
python -m pip install -e '.[dev]'
pytest
pyright src/dependabot_watcher tests
```

Add or update deterministic tests for behavior changes. Platform-specific
scheduler changes also require an appropriate installed-path or native-platform
exercise before their support status is promoted.

## Pull requests

Keep pull requests focused, explain the user-visible or safety impact, and note
the validation performed. Do not include credentials, private repository data,
local paths, approval tokens, or private runtime artifacts.

Contributions are accepted under the repository's
[Apache License 2.0](LICENSE).

## Support

Community support is provided through GitHub Issues on a best-effort basis.
There is no guaranteed response or resolution time and no commercial support
or service-level agreement.
