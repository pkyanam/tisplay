# Contributing

Bug reports, documentation improvements, and code contributions are welcome. For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Set up and check changes

The project supports Python 3.10 and newer. From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[linux]'
python3 -m pytest
```

The `linux` extra installs X11 support. The test suite covers stream encoding and input mapping; it does not simulate live desktop capture, platform permission prompts, or terminal rendering.

## Submit a change

Keep changes focused, describe the user-visible effect, and include or update tests when behavior changes. Run the relevant checks above, then open a pull request against `main` with a short explanation and any platform or terminal requirements for review. Please do not include credentials, personal data, or screenshots containing sensitive information.
