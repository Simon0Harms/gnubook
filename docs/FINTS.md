# Bank import with firefly-iii-fints-importer

[bnw/firefly-iii-fints-importer](https://github.com/bnw/firefly-iii-fints-importer) fetches transactions from
German banks via FinTS/HBCI and sends them to Firefly III's REST API. gnubook implements the part of that API
the importer uses, so the importer can send its data to gnubook unchanged:

- `GET /api/v1/accounts?type=asset`
- `GET /api/v1/accounts/{id}`
- `POST /api/v1/transactions`
- `GET /api/v1/about`

## Setup

1. Create a token in the gnubook container:

   ```bash
   gnubook gen-token
   ```

   The command prints the token (for the importer) and a `token_sha256 = "…"` line. Put that line into
   `/opt/gnubook/config.toml` under `[api]`, then run `systemctl restart gnubook`.

2. Look up the numeric **Konto-ID** of each bank account. gnubook shows it on the account page and under
   *Einstellungen*.

3. Point the importer's configuration file (`data/configurations/<name>.json`) to gnubook:

   ```json
   {
     "firefly_url": "http://192.168.1.31:8080",
     "firefly_access_token": "<token from gnubook gen-token>",
     "skip_transaction_review": "false",
     "choose_account_automation": {
       "bank_account_iban": "DE…",
       "firefly_account_id": "3",
       "from": "now - 14 days",
       "to": "now"
     }
   }
   ```

   Keep the bank settings (`bank_code`, `bank_url`, `bank_2fa`, …) as they are.

4. Run an import. The result appears on gnubook's page *Bankimport*.

## What gnubook does with each bank line

| Situation | Result |
|---|---|
| The same line was imported before | rejected as a duplicate (HTTP 422, shown by the importer), like Firefly III |
| The booking is already in the book (same account and amount, near the date, similar text, not imported by gnubook yet) | linked to the existing booking, nothing new is booked. This covers the switch from GnuCash's own online banking. |
| The counterparty IBAN belongs to one of your own accounts | transfer between the two accounts. When the other bank reports the same transfer, it is linked instead of booked again (up to 7 days apart). |
| A 0,00 line, for example a closing line with `STAND…`/`ENDSALDO…` | single split of 0,00 in the bank account, so the balance checkpoint can be evaluated |
| Anything else | new booking with a counter account (see below) |

### Booking text

The importer sends the purpose as `description` and the counterparty separately. gnubook books
`"<purpose>; <counterparty>"`, the same format GnuCash's AqBanking import uses. The bank split gets the memo
`Konto <IBAN>`.

### Counter account

gnubook tries these sources in order:

1. **Own account.** The IBAN matches one of your accounts:
   - via `[import] iban_map`;
   - via the online-banking data GnuCash stored for the account (bank code and account number);
   - via an IBAN in the account code.
2. **History.** The account that earlier bookings with the same IBAN and counterparty name used. If you
   correct a booking later, in gnubook or in GnuCash, the next import follows your correction.
3. **GnuCash's Bayesian import map** of the bank account, with the same algorithm as GnuCash: probability
   at least 90 %.
4. **Fallback account** (`[import] fallback_account`, default `Ausgleichskonto-EUR`). The dashboard then shows
   *„bitte zuordnen“*. Open the booking from *Bankimport* and choose the right account.

### Identical lines

Sometimes a bank reports two truly identical lines, for example two equal payments at the same shop on the
same day. gnubook imports both if they arrive directly one after the other within two minutes, which is
how the importer sends them. When the same lines arrive again in a later import run, they are recognised as
duplicates.

If two identical lines are *not* sent directly one after the other, the second one is treated as a duplicate
and not booked. The importer shows the message, and the next balance checkpoint would show the difference.
Enter such a booking by hand.

### Possible duplicates

If a new line resembles an existing booking but is not certainly the same, gnubook books it and marks it as
*mögliches Duplikat* on the *Bankimport* page, with a link to the similar booking. Delete one of them if they
are the same.

## Transfers between two imported bank accounts

By default a transfer from bank A to bank B becomes **one** booking with the date of the bank that is
imported first. If the banks book it on different days around a month end, one of the two balance
checkpoints shows a difference for that month.

To avoid this, book such transfers through a transit account. Each bank line is then booked on its own day:

```toml
[import]
transit_account = "Aktiva:Geldtransit"
transit_between = ["Aktiva:Barvermögen:Girokonto Musterbank", "Aktiva:Barvermögen:Girokonto Beispielbank"]
```

Create the account `Aktiva:Geldtransit` in GnuCash first. Its balance is the money that is on its way between
the banks.

## Notes

- **Keep `[api] expose_iban = false` (the default).** Then the importer always sends withdrawals and
  deposits, and gnubook recognises your own accounts itself. With `true` the importer marks transfers
  itself. gnubook still handles them, but the transit option above does not apply.
- **GnuCash Desktop open.** While it has the book open, every line is rejected with a message. Import again
  later; nothing gets lost or duplicated.
- **Which accounts the importer sees.** By default all bank, asset, cash and credit accounts. Limit them with
  `[import] accounts = ["…full account name…"]`.
