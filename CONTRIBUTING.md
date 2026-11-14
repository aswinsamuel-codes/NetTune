# Contributing to NetTune

Thanks for helping. NetTune aims to stay small, honest and safe: it only claims what it can measure, and every change it makes to a system can be undone.

## Getting set up

```
git clone <your fork>
cd nettune
python -m nettune            # run the dashboard
python -m unittest discover -s tests -v   # run the tests
```

There are no dependencies to install. Please keep it that way for the engine: standard library only.

## Adding a fix

1. Create a class in `nettune/fixes/` that subclasses `Fix` (see `fixes/base.py`).
2. Implement `status`, `apply` and `revert`.
3. Call `self.remember(ctx, original)` before changing anything, so `revert` restores the exact previous value.
4. Register it in `nettune/fixes/__init__.py`.
5. Optionally add a rule in `advisor.py` that recommends it when a diagnosis shows it would help.

A fix is accepted when:

- It changes one thing, and `revert` puts that thing back, even after a restart of NetTune.
- It has a measurable or well-documented benefit. Please link a source in the pull request.
- Its `details` text states the trade-off in plain language.
- It takes no free-form input from the API. Options are chosen from values defined in code.

## Safety rules for all code

- Never build a PowerShell or cmd command from outside values (network names, adapter names, API input). Pass them with `run_ps(script, args={...})` and read them as `$env:NT_<NAME>`.
- Never insert outside text into the page with `innerHTML`. Use the `el()` helper in `web/app.js`, which sets `textContent`.
- The server must keep binding to `127.0.0.1` and keep its `Host` and token checks.
- No telemetry, and no network requests other than the tests the user starts.

## Tests

Parsers and rules are pure functions so they can be tested without a Wi-Fi card. If you fix a parsing bug, add the sample output that broke it to `tests/`.

## Reporting a problem

Open an issue with your Windows version, Wi-Fi adapter model, and what the dashboard showed. Remove network names or addresses you do not want public.
