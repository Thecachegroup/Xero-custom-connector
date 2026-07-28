# TCG Xero Invoice Checker — MCP Connector

Replaces the monthly manual export-and-scan of `Invoice_Checker_FY26.xlsx` with a
live connector that pulls Xero line-item data, normalises it into the same `Data`
schema, and runs the reconciliation rules automatically.

---

## Why not the official Xero connector

The Xero MCP app in Claude exposes seven **report-level, read-only** tools (P&L,
balance sheet, cash position, receivables summary, top customers). It cannot see
invoice line items, bills detail, tracking categories, the item rate card, or
payroll. The entire invoice check operates at line-item level, so the official
connector is structurally unable to do this job. Hence a custom one.

---

## Setup (about 20 minutes)

### 1. Create a Xero Custom Connection

Go to <https://developer.xero.com/app/manage> -> **New app** -> **Custom Connection**.

A Custom Connection is a machine-to-machine app bound to a single Xero org. It uses
the `client_credentials` grant: no user login, no consent screen, no refresh token
to rotate or go stale. It is a paid Xero option, billed per connection per month,
charged to the card in the developer portal rather than to the Xero org's own
subscription.

- **Company URL:** `https://thecachegroup.com.au`
- **Redirect URI:** not asked for - Custom Connections have no login step

Scopes - all read, no write:

```
accounting.invoices.read
accounting.contacts.read
accounting.settings.read
accounting.payments.read
accounting.attachments.read
accounting.reports.read
payroll.payruns.read
payroll.employees.read
payroll.timesheets.read
payroll.settings.read
```

`accounting.transactions.read` does not exist - Xero split it into the granular
scopes above. Two Xero rules worth knowing before you touch scopes again:
changing them deactivates the connection until it is re-authorised, and a broad
scope removed from an existing connection cannot be re-added.

The authorising Xero user must be a **Payroll Admin** on the org, or the payroll
scopes return nothing.

### 2. Configure

```bash
cp .env.example .env      # paste in Client ID + Secret
pip install -r requirements.txt
```

There is no authorisation step. Custom Connections mint a token straight from the
client id and secret.



`config/customer_lookup.json` and `config/no_payroll_tax.json` have been seeded
from the existing workbook (22 and 54 entries respectively). These are the only
two pieces of business logic that live outside Xero. Long term they should move
into Xero as contact groups / tracking categories, and then these files go away.

### 3. Register with Claude

**Local (Claude Desktop / Cowork)** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tcg-xero": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/xero-invoice-mcp",
      "env": { "XERO_CLIENT_ID": "...", "XERO_CLIENT_SECRET": "..." }
    }
  }
}
```

**Remote (claude.ai Projects + Android)** — you use claude.ai and an Android phone,
so you will want this. Set `MCP_TRANSPORT=http`, deploy to Vercel/Render/Fly behind
HTTPS, and add it as a custom connector in Claude settings. Same code, same tools.

---

## Tools

| Tool | What it does |
|---|---|
| `run_invoice_check(fy)` | The main event. Pulls the FY, runs every rule, returns a severity-ranked exception report. |
| `get_contractor_ledger(name, fy)` | Every sales/bill/pay line for one contractor with the margin. Use when one name looks wrong. |
| `get_rate_card()` | Xero item rate card — cost, sell, margin per contractor. |
| `export_workbook(fy)` | Writes drop sheets + Data + Exceptions to xlsx, matching the existing tab names. |
| `cash_and_receivables()` | Outstanding and overdue, with the biggest offenders. |

## Rules implemented

Each of these encodes a failure mode already found by hand in the FY26 workbook:

| Rule | Severity | Catches |
|---|---|---|
| `rate_mismatch` | HIGH | Invoiced/billed rate ≠ Xero rate card. Catches the unexplained rate spike. |
| `sales_without_bill` / `bill_without_sales` | HIGH | Contractor invoiced but not billed, or vice versa. Margin leakage. |
| `negative_margin` | HIGH | Cost ≥ revenue for the month. |
| `draft_sales_invoice` | HIGH | Still in Draft at period end — unbilled revenue. |
| `same_date_multiple_lines` | HIGH/MED | Same contractor, same date, different rates. Mid-period change or double-bill. |
| `credit_or_reversal` | MEDIUM | Negative lines — confirm they offset an original. |
| `unknown_item_code` | MEDIUM | Item code not in the rate card. |
| `zero_unit_line` | LOW | Zero-unit placeholders. |

**Phantom rows are gone by construction.** The `Data` frame is built from API
responses, not from formulas dragged down a column, so blank rows cannot exist.
That was the biggest structural problem in the spreadsheet and it does not survive
the move.

---

## Payroll

The connector **reads** pay runs and payslips to reconcile PAYG contractors against
sales. It does not and will not write a pay run.

Assembling a payroll run from Outlook timesheets is a separate job and is achievable
with the Microsoft 365 connector already in place: read the timesheet emails, extract
units per contractor, validate against the rate card and active-contractor list, flag
exceptions, and emit a draft for approval. What it must not do is post it. A misread
"7.5" as "75" on a scanned timesheet is real money out the door, and that gate belongs
to a human.

---

## Rate limits

Xero allows 60 calls/min, 5,000/day, 5 concurrent. The client self-throttles to
55/min and honours `Retry-After` on 429. A full FY pull is roughly 15–25 calls, so
you have enormous headroom — but the payroll payslip fetch is one call per payslip,
so a full multi-year backfill should be run once and cached, not repeated.

---

## Known limitations — read before trusting the output

Two rules are noisy by design and need tuning against your judgement:

1. **`rate_mismatch` (234 hits on FY26 data).** The Xero item rate card is
   *point-in-time* — it holds today's rate, not the rate that applied last
   September. Comparing a full year of lines against it flags every legitimate
   historical rate change. **Fix:** either restrict this rule to the current
   month (cheap), or snapshot the rate card monthly and compare each line to the
   rate in force at its date (correct, and what I'd recommend — it also gives you
   a rate-change audit trail you don't currently have).

2. **`negative_margin` (16 hits).** Monthly matching is timing-sensitive: a
   contractor bill landing in a different month to the client invoice reads as a
   loss. **Fix:** match on the *service period* (from the invoice Reference, which
   already carries e.g. "Deepti Bansal May 2026") rather than the invoice date.

Both are solvable. Neither should stop you using the connector — but treat these
two rules as "look here" rather than "this is wrong" until they're tuned.

## The real fix, upstream

The connector makes the check fast. It does not make the data right. Three things
belong in Xero, not in a spreadsheet or a JSON config:

- **Duplicate item codes** — one contractor, one code, one rate. Fix in Xero.
- **`customer_lookup.json`** — should be a Xero contact group or tracking category.
- **`no_payroll_tax.json`** — should be a contact/employee attribute in Xero.

Every one of those files is a workaround for something Xero can model natively.
Move them and the connector gets simpler, not more complex.


---

## Deployment (Render)

Render deploys from GitHub the same way Vercel does: connect the repo, set the
environment variables in the dashboard, push to deploy. Unlike Vercel it runs a
persistent process, so a full-financial-year payroll pull is not racing a
function timeout.

Use the **Starter** plan, not Free. The free plan sleeps after inactivity and
takes roughly 50 seconds to wake, which times out the first request every time.

`render.yaml` in this repo configures the service. Set these four in the Render
dashboard under **Environment** (they are deliberately marked `sync: false` so
they never live in the repo):

| Variable | Where it comes from |
|---|---|
| `XERO_CLIENT_ID` | Xero developer portal, Configuration |
| `XERO_CLIENT_SECRET` | Xero developer portal, generated once |
| `XERO_TENANT_ID` | Xero developer portal, Connection management |
| `MCP_SHARED_SECRET` | Generate a random 32+ character string |

### The endpoint is protected by the URL itself

In http mode the server listens only at `/mcp/<MCP_SHARED_SECRET>`. There is no
login page in front of it. Anyone holding the complete URL can read every
invoice, bill, contact and payslip in the TCG org.

Treat the full URL as a password: password manager only, never in a document,
email, ticket or public repo. If it is ever exposed, change `MCP_SHARED_SECRET`
in Render - the old URL dies immediately.

The server refuses to start in http mode without a secret of at least 24
characters.

---

## Writing to Xero

Off by default. `TCG_WRITE_ENABLED=false` and the connection carries read scopes
only, so nothing can change the ledger.

To turn it on you need two things:

1. **Write scopes on the Custom Connection.** Swap the read-only versions for
   `payroll.timesheets`, `accounting.settings`, `accounting.transactions`.
   Changing scopes deactivates the connection until it is re-authorised, and a
   broad scope removed from an existing connection cannot be re-added - so do
   this once, deliberately.
2. **`TCG_WRITE_ENABLED=true`** in the Render dashboard.

### Everything written is a draft

`post_draft_timesheet` creates a Draft timesheet. `create_draft_invoice` creates
a Draft invoice. Neither is approved, posted, sent or paid. You approve in Xero.

This is not a limitation to be engineered away. A timesheet where 7.5 is read as
75 is real money leaving the account, and the approval step is the only thing
between a bad number and a bank transfer.

`payroll_entry_plan` sends nothing at all - it takes 'name: days' lines and
returns the pay figure and the matching invoice figure for each person from the
current rate card, so the two can never diverge. Refuses to guess when a name
matches more than one item code.

---

## Tests

```
python3 tests/test_static.py      # syntax, contracts, endpoint versions, security guards
python3 tests/test_pipeline.py    # full pipeline against Xero-shaped fixtures
```

No credentials needed - the fixtures are Xero-shaped JSON. Run both after any
change to mappers or checks. The pipeline test deliberately includes a duplicate
item code, a z-prefixed code split across sales and cost, and a payrolling
invoice with no item code, so the rules that handle those stay honest.

---

## Known gaps

Tools for **PAYG withholding**, **payroll tax** and the **quarterly BAS** are not
built yet. The data needed for them is already being pulled; the calculations
are not written. These are tax lodgement figures, so they need to be specified
carefully and checked against a lodged return before anyone relies on them.
