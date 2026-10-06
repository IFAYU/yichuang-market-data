# yichuang-market-data

Weekly snapshot of Taiwan listed (TWSE) and OTC (TPEx) market data used by the Yichuang Capital valuation tools, and the pipeline that produces it.

* **Data source:** TWSE / TPEx official open data (`openapi.twse.com.tw`, `www.tpex.org.tw/openapi`). Nothing else is used: no Goodinfo, Yahoo, MOPS scraping, browser automation, proxies or rotating identities.
* **Publication:** this repository's `data` branch, served by GitHub Pages. GitHub Pages is only the *publication channel*, not the data source.
* **Schedule:** every Sunday 10:00 / 14:00 / 20:00 Asia/Taipei (see `.github/workflows/weekly-publish.yml`). Once the week's publication succeeds, later runs make no requests.
* **Branches:** `main` = pipeline code, tests, docs. `data` = published files only: `manifest.json`, `health.json`, `releases/<id>/…`.

Safety rules (enforced by the code and the tests):

1. A release is immutable and is written and verified BEFORE `manifest.json` points at it; `manifest.json` is replaced last.
2. A run that fails, is refused by the gate, or does not advance the market date never touches the live manifest (Last Known Good).
3. Freshness is derived from the schedule (did a weekly publication get missed?) and the market dates inside the data, never from file build time.
4. No secrets exist in this repository or its workflows; the workflow only uses the built-in `GITHUB_TOKEN` to push to the `data` branch.

See `docs/FRESHNESS_AND_PUBLICATION.md`.
