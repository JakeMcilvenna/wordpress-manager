# WordPress Manager

A project I'm working on to manage WordPress sites on Ubuntu 24.04 from one dashboard.

It's still early days. I'm building out the site setup, backup/restore and removal parts, and there's a fair bit left to test. **This isn't ready to use on a live server.** I'm doing the server-side testing on throwaway Ubuntu machines for now.

## Running it locally

There's a Docker preview if you just want to see the dashboard:

```bash
docker compose -f compose.preview.yml up --build
```

Then go to http://127.0.0.1:8090.

To stop and clean up:

```bash
docker compose -f compose.preview.yml down
```

This is just a preview of the app, not a full WordPress setup.

## Working on the app

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m wordpress_manager
.venv/bin/python -m pytest -q
```

On Windows, use `.\.venv\Scripts\python` instead of `.venv/bin/python`.

## Notes

The dashboard uses Flask. The idea is to keep sites separate from the manager itself, so removing or changing the dashboard doesn't take the sites with it.

I'm keeping the server operations restricted while I work through the safety checks. Anything involving real Ubuntu services should only be tested on disposable machines at this stage.

More detail is in [the backend notes](docs/WEBSITE_BACKENDS.md), [provisioning docs](docs/PROVISIONING.md) and [AGENTS.md](AGENTS.md).
