# pp-core

pp-core runs [Portfolio Performance](https://github.com/portfolio-performance/portfolio)'s core without its user
interface and offers a small JSON API on `127.0.0.1`. gnubook uses it for everything that concerns the PP file:
reading it, PDF import, prices, performance figures. See [docs/PORTFOLIO-PERFORMANCE.md](../docs/PORTFOLIO-PERFORMANCE.md)
for what gnubook does with it and how it is installed (`deploy/install-pp.sh`).

## How it works

Portfolio Performance is an Eclipse RCP application: a set of OSGi bundles started by Equinox. pp-core adds one
bundle, `gnubook.ppcore`, with an Eclipse application `gnubook.ppcore.server`, and starts PP's own bundles with
that application instead of PP's workbench (`configure.sh` writes the OSGi configuration, PP's installation is not
changed). So the file format, the PDF importers, the price sources and the calculations are exactly PP's – pp-core
only calls them:

| Class | Uses from PP |
|---|---|
| `ClientHolder` | `ClientFactory` (load, save), one file per client id, backups, a lock per file |
| `Exporter` | the model (`Client`, `Security`, `Account`, `Portfolio`, transactions, units) as JSON |
| `PdfImporter` | `PDFImportAssistant`, PP's import checks (duplicates, currencies, …) and `InsertAction` |
| `QuoteUpdater` | PP's `QuoteFeed`s and update policy; ECB exchange rates (`ExchangeRateProviderFactory`) |
| `FeedFinder` | PP's search providers; Yahoo symbol by ISIN for new securities |
| `Metrics` | `PerformanceIndex` (TTWROR, drawdown, volatility), `ClientPerformanceSnapshot` (IRR, calculation), `LazySecurityPerformanceSnapshot`, `ClientSnapshot` |
| `HttpApi` | JDK `HttpServer`, token check, routing |

pp-core uses PP's internal Java API, which can change with any PP release. `build.sh` therefore compiles pp-core
against the installed PP release (an incompatible release fails there, before anything is switched), and CI builds
and starts it with the release `deploy/install-pp.sh` installs (`smoke-test.sh`). Tested with PP 0.88.0.

## Files

| File | Purpose |
|---|---|
| `src/gnubook/ppcore/*.java` | the service |
| `META-INF/MANIFEST.MF`, `plugin.xml` | OSGi bundle and Eclipse application |
| `build.sh PP_DIR OUT_JAR` | compile against a PP installation (JDK 21) |
| `configure.sh PP_DIR JAR CONFIG_DIR` | OSGi configuration: PP's bundles plus pp-core, application `gnubook.ppcore.server` |
| `ppcore-run` | start script (systemd unit `gnubook-ppcore`) |
| `smoke-test.sh PP_DIR` | build, start on a temporary port, call the API |

Manual start for development:

```bash
tar -xzf PortfolioPerformance-0.88.0-linux.gtk.x86_64.tar.gz -C /tmp/pp
ppcore/build.sh /tmp/pp/portfolio /tmp/ppcore.jar
ppcore/configure.sh /tmp/pp/portfolio /tmp/ppcore.jar /tmp/ppconf
printf 'token=%s\ndata_dir=/tmp/ppdata\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > /tmp/ppcore.properties
PPCORE_PP_DIR=/tmp/pp/portfolio PPCORE_CONF_DIR=/tmp/ppconf PPCORE_CONFIG=/tmp/ppcore.properties \
  PPCORE_WORKSPACE=/tmp/ppdata/workspace ppcore/ppcore-run
```

## Settings

`/opt/gnubook/ppcore.properties` (path: system property `ppcore.config` or environment `PPCORE_CONFIG`); every
value can be overridden with an environment variable `PPCORE_<KEY>`, e.g. `PPCORE_PORT`.

| Key | Default | Meaning |
|---|---|---|
| `token` | – | required, at least 24 characters; every request needs `Authorization: Bearer <token>` |
| `bind` | `127.0.0.1` | address to listen on |
| `port` | `8091` | port |
| `data_dir` | `/opt/gnubook/data/ppcore` | files of the clients |
| `keep_backups` | `20` | earlier versions kept per file |
| `max_upload_mb` | `64` | largest request body (file upload, PDFs) |

Data directory: `clients/<id>/portfolio.xml` (or `.portfolio`, `.zip` – the format of the uploaded file is kept),
`clients/<id>/backups/`, `clients/<id>/state.json` (original file name, import targets per bank, last price update).

## API

All paths below `/api/v1`, JSON in and out, errors as `{"error": code, "message": text}` with status 400, 401, 404,
409 (file busy or already there) or 500. Client ids are `[a-z0-9][a-z0-9_-]*` (gnubook uses `book-<id>`).

| Method and path | Purpose |
|---|---|
| `GET /health` | `{"status", "ppVersion", "service", "clients"}` |
| `GET /feeds` | PP's price sources `[{"id", "name"}]` |
| `GET /clients/{id}` | summary: `exists`, file, size, modified, `revision` (SHA-256 of the file), base currency, counts, `firstDate`, portfolios, accounts, original name, `lastPriceUpdate`, `importTargets` |
| `GET /clients/{id}/file` | the file (header `X-Revision`) |
| `PUT /clients/{id}/file` | replace the file (body = file, header `X-Filename`); the previous one goes to the backups; encrypted files are refused |
| `POST /clients/{id}/create` | new empty file `{"currency", "portfolio", "account"}` → 201 |
| `POST /clients/{id}/demo` | fictional demo file `{"start", "months", "seed"}` (class `Demo`) → 201; replaces only an earlier demo file, never an uploaded one (409) |
| `GET /clients/{id}/export?prices=all\|none\|YYYY-MM-DD` | everything gnubook books: securities (with prices, also in the base currency), accounts, portfolios, transactions with units and cross entries |
| `POST /clients/{id}/import` | PDF import `{"files": [{"name", "data" (base64)}], "portfolio", "account", "apply", "autoFeed"}`: extracts with PP's importers, checks, imports what is OK; result with session, items (status OK/WARNING/ERROR and messages), file errors, targets |
| `GET /clients/{id}/import/{session}` | the result again (sessions live 6 hours) |
| `POST /clients/{id}/import/{session}/apply` | import items with warnings after all `{"force": [index], "portfolio", "account", "extractor", "autoFeed"}` |
| `GET /clients/{id}/quotes` | state of the last price update |
| `POST /clients/{id}/quotes?wait=N` | update prices `{"securities": [uuid]}` (empty = all); 202 while running, waits up to N seconds |
| `GET /clients/{id}/performance?from&to&portfolio` | TTWROR, IRR, drawdown, volatility, calculation table, daily series |
| `GET /clients/{id}/holdings?date` | positions with FIFO cost, gains, dividends, IRR/TTWROR per security; cash accounts; totals |
| `GET /clients/{id}/search?q` | search price sources (Yahoo and PP's other search providers) |
| `PATCH /clients/{id}/securities/{uuid}` | change `feed`, `ticker`, `feedUrl`, `latestFeed`, `latestFeedUrl`, `name`, `isin`, `wkn`, `retired` |
| `POST /clients/{id}/transactions` | manual inbound/outbound delivery (class `ManualTransaction`) `{"type": "DELIVERY_INBOUND"\|"DELIVERY_OUTBOUND", "portfolio", "security"` or `"isin"` (new security on inbound: plus `"name"`, `"currency"`)`, "date", "shares", "amount", "fees", "taxes", "note", "force"}` → 201; `amount` empty = shares × price of that day; foreign-currency securities get a gross value unit with the rate of that day; 409 `not_enough_shares` when more shares go out than are held, unless `force` |
| `DELETE /clients/{id}/transactions/{uuid}` | delete a transaction (with its cross entry) |

Every change is saved at once (atomically, with a backup of the previous version).

## License

pp-core is part of gnubook and licensed under the GNU General Public License, version 3 or later (see
[LICENSE](../LICENSE)), with the following additional permission:

> Additional permission under GNU GPL version 3 section 7
>
> If you modify this Program, or any covered work, by linking or combining it with Portfolio Performance (or a
> modified version of that library), containing parts covered by the terms of the Eclipse Public License 1.0, the
> licensors of this Program grant you additional permission to convey the resulting work. Corresponding Source for
> a non-source form of such a combination shall include the source code for the parts of Portfolio Performance used
> as well as that of the covered work.

Portfolio Performance (by Andreas Buchen and contributors) is licensed under the Eclipse Public License 1.0, as
stated in its bundles (`about.html`). It is not part of gnubook: `deploy/install-pp.sh` downloads the official
release.
