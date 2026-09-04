# OneDrive file plumbing for the TCG Xero/payroll connector

What this adds: the ability for an unattended Claude session — scheduled task,
laptop shut, no Chrome — to get **file bytes** in and out of your Microsoft
tenant. Today it cannot, and that is the single reason a consultancy brief
cannot be countersigned while you are away.

## Why it goes on this connector and not the mailer

Tested 04/09/2026:

| Route | Result |
|---|---|
| Claude M365 connector reading an attachment | text only, never bytes |
| Claude M365 connector `sharepoint_upload_file` | **403** — `Files.ReadWrite.All` not consented on that app |
| `outlook_forward_mail` on a message with an attachment | refused at the guardrail |
| **This connector's Graph app** | already holds `Files.ReadWrite.All`, already resolves your OneDrive |

`graph_diagnostics` reports "Files owner: andrew.hurnard@thecachegroup.com.au"
and finds the drive. The permission you need already exists here. Nothing new
gets consented for the OneDrive half of this.

## Design — no file content passes through the conversation

The tools return short-lived **pre-authenticated URLs**, not bytes. Claude's
sandbox fetches or PUTs directly. A 5 MB PDF costs four lines of output instead
of seven million characters of base64, and every document operation — signing,
`.docx` editing — stays in the sandbox where the toolchain is tested.

---

# Deployment

## Step 1 — add the module

Put `graph_files.py` in the same folder as `roster.py` and `writes.py` (that is
`src/`, going by the paths in your notes).

## Step 2 — register the tools

Open the file that defines `graph_diagnostics` and `find_documents`. Add the
import at the top, next to the other sibling imports:

```python
from src import graph_files
```

If the neighbouring imports are bare (`import roster`), match that instead:
`import graph_files`.

Then paste the whole contents of `tools_to_add.py` at the bottom of that file,
below the last existing `@mcp.tool()`. Five tools; nothing existing changes.

## Step 3 — check requirements

`requests` is the only dependency and the connector already uses it. Open
`requirements.txt` and confirm `requests` is listed. If it is not, add a line:

```
requests
```

## Step 4 — deploy

```powershell
git add .
git commit -m "Add OneDrive file plumbing: download/upload URLs and mail attachment capture"
git push
```

Vercel builds on push. Wait for the deployment to go green in the Vercel
dashboard before the next step.

## Step 5 — new session

The connector's tool list is cached per session. **Start a fresh Claude
conversation** or the five new tools will not be visible, however well the
deploy went. If you bump the connector URL version, do it now.

## Step 6 — prove it

In the new session, ask me to run `onedrive_selftest`. Expect:

```
token           OK
drive access    OK (nn items at root)
write           OK
read back       OK
cleanup         OK
```

Five OKs and the file half is live. It writes a probe file to
`AI Working Folder/` and deletes it again.

Anything other than five OKs: the failing line names what broke. A token
failure lists the environment variable names it looked for — if your Vercel
settings use different names, add yours to `_TENANT_KEYS` / `_CLIENT_KEYS` /
`_SECRET_KEYS` at the top of `graph_files.py`.

---

# Step 7 — mail attachments, and a decision you should make deliberately

Steps 1–6 give you OneDrive read and write. Pulling an attachment **out of your
inbox** needs one more thing, because the Exchange application access policy
currently scopes this app's mail permission to `payrollmb@` only.

There are two ways, and they are not equivalent.

### Route A — an Outlook rule (recommended, no new permissions)

Set a rule in your own Outlook that copies the documents you want handled to
`payrollmb@thecachegroup.com.au`. The app already reads that mailbox, so
nothing gets consented and nothing gets widened.

1. Outlook on the web → **Settings** (gear, top right) → **Mail** → **Rules**
2. **Add new rule**, name it `Consultancy briefs to payroll`
3. Condition: **Subject or body includes** → `consultancy brief`
4. Action: **Forward to** → `payrollmb@thecachegroup.com.au`
5. Tick **Stop processing more rules**, then **Save**

Rules forward attachments intact — that is the part the connector cannot do.

**The trade-off:** it only catches what the rule matches. A brief that arrives
titled something else is invisible. Add a second condition on the senders who
actually send them if you want it tighter, or widen to `brief OR agreement OR
contract` if you would rather over-catch.

### Route B — widen the access policy

Add your mailbox to the policy so the app reads it directly. Catches
everything, no rule to maintain.

**Understand what this does:** it gives the payroll app read access to your
entire mailbox, permanently, not just to consultancy briefs. That app already
holds `Files.ReadWrite.All` tenant-wide — the item already on your list to
narrow to `Sites.Selected`. Route B makes that app more powerful at the same
time you have an open action to make it less. I would not do both.

If you want it anyway, in Exchange Online PowerShell:

```powershell
Connect-ExchangeOnline -UserPrincipalName andrew.hurnard@thecachegroup.com.au

# See what is actually there before changing anything
Get-ApplicationAccessPolicy | Format-List Identity,AppId,ScopeName,ScopeIdentity,AccessRight
```

The existing policy scopes to a mail-enabled security group. Add yourself to
**that group** in the Microsoft 365 admin centre → **Teams & groups** → **Active
teams & groups** → the group named in `ScopeName` → **Members** → **Add
members**. Changing group membership is safer than writing a second policy, and
it propagates in a few minutes.

Do not create an overlapping policy for the same AppId — Exchange evaluates
them together and the result is rarely what you expect.

---

# How the round trip runs once this is live

A signed brief arrives while you are away:

1. `onedrive_save_mail_attachment` — the PDF lands in the contractor's folder.
   Bytes never enter the conversation.
2. `onedrive_download_url` — the sandbox pulls it and I sign it with PyMuPDF,
   using your signature from `AI Templates/andrew.jpg`.
3. `onedrive_upload_url` — the countersigned PDF goes back beside the original.
4. `send_email(attach_from_onedrive=[...])` — it goes to whoever sent it.
5. For the contractor's own agreement: `onedrive_download_url` on the right
   Employsure template, edit the real `.docx` in the sandbox — never rebuild
   it — upload, attach, send.

---

# Before you rely on it

None of this has run against your live tenant. What has been tested:

- the path handling and every guard, offline — 12 assertions, all passing
- that the sandbox can reach `graph.microsoft.com` and
  `thecachegroup-my.sharepoint.com` — both answered

What has not: a real token from your app, a real write to your drive, a real
attachment capture. `onedrive_selftest` covers the first three in about ten
seconds.

**Run one full round trip on a document that does not matter before you leave.**
Not a live brief. A dummy PDF, into `AI Working Folder`, signed, sent to
yourself. If it works end to end once with you watching, it will work while you
are away. If it does not, you will want to know that on a Tuesday at your desk
rather than from a hotel.
