# Changelog

## 0.3.0

- Replace personal phone numbers with fictional examples.
- Discover account numbers from billing tables, including numbers without usage.
- Keep dynamic creation of six sensors for newly discovered numbers.
- Remove sensitive login, cookie, HTML and subscriber-statistics logging.
- Restrict form actions and export links to the HTTPS customer portal.
- Reject unsuccessful HTTP login responses and persistent expired sessions.
- Close sessions when initial setup fails.
- Update Beautiful Soup and pin current workflow actions.
- Add synthetic regression tests, privacy checks and dependency auditing.

Actual account login must be verified in the user's Home Assistant.
