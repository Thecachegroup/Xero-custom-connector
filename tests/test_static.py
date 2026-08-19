import sys, os, json, ast, re, traceback
sys.path.insert(0, '.')
os.environ.setdefault("XERO_CLIENT_ID","test"); os.environ.setdefault("XERO_CLIENT_SECRET","test")
fails, warns = [], []
def ok(m): print(f"  PASS  {m}")
def bad(m): fails.append(m); print(f"  FAIL  {m}")
def warn(m): warns.append(m); print(f"  WARN  {m}")

print("\n=== 1. Syntax and imports ===")
for f in ['src/xero_client.py','src/mappers.py','src/checks.py','src/server.py','src/writes.py','api/index.py']:
    try: ast.parse(open(f).read()); ok(f"parses {f}")
    except Exception as e: bad(f"{f}: {e}")
try:
    from src import mappers, checks, writes, xero_client
    ok("all modules import")
except Exception as e:
    bad(f"import failed: {e}"); traceback.print_exc()

print("\n=== 2. Config files ===")
for f in ['config/customer_lookup.json','config/no_payroll_tax.json','vercel.json']:
    try:
        d=json.load(open(f)); ok(f"{f} valid JSON ({len(d)} entries)")
    except Exception as e: bad(f"{f}: {e}")

print("\n=== 3. Client method contract ===")
src=open('src/xero_client.py').read()
have=set(re.findall(r'    def (\w+)\(', src)) | {'tenant_id'}
for f in ['src/server.py','src/writes.py']:
    used={x for t in re.findall(r'\bc\.(\w+)\(|\bclient\(\)\.(\w+)\(|\bclient\.(\w+)\(', open(f).read()) for x in t if x}
    miss=used-have-{'items'}
    (ok if not miss else bad)(f"{f}: client calls resolved" if not miss else f"{f}: missing {sorted(miss)}")

print("\n=== 4. Payroll endpoint versions (AU = 1.0) ===")
for f in ['src/xero_client.py','src/writes.py']:
    t=open(f).read()
    if 'payroll.xro/2.0' in t: bad(f"{f} still points at payroll 2.0 (AU is 1.0)")
    else: ok(f"{f} uses payroll 1.0")
if '/Payslip/' in src: ok("payslip endpoint singular")
else: bad("payslip endpoint not singular")

print("\n=== 5. Security guards ===")
import subprocess
r=subprocess.run([sys.executable,'-c','import importlib.util as u;s=u.spec_from_file_location("e","api/index.py");m=u.module_from_spec(s);s.loader.exec_module(m)'],
  env={**os.environ,'XERO_CLIENT_ID':'x','XERO_CLIENT_SECRET':'y','PATH':os.environ['PATH']},capture_output=True,text=True)
if r.returncode!=0 and 'MCP_SHARED_SECRET' in (r.stderr+r.stdout): ok("refuses to boot without a shared secret")
else: bad(f"booted without secret! rc={r.returncode}")
r=subprocess.run([sys.executable,'-c','''
import importlib.util as u
s=u.spec_from_file_location("e","api/index.py");m=u.module_from_spec(s);s.loader.exec_module(m)
print([getattr(x,"path","?") for x in m.app.routes])'''],
  env={**os.environ,'XERO_CLIENT_ID':'x','XERO_CLIENT_SECRET':'y','MCP_SHARED_SECRET':'a'*32,'PATH':os.environ['PATH']},capture_output=True,text=True)
if "'/mcp/"+"a"*32+"'" in r.stdout and "'/mcp'" not in r.stdout: ok("serves only the secret path")
else: bad(f"route problem: {r.stdout.strip()} {r.stderr[-200:]}")
try:
    writes._guard(); bad("writes allowed with flag off")
except writes.WritesDisabled: ok("writes blocked by default")
os.environ['TCG_WRITE_ENABLED']='true'
try: writes._guard(); ok("writes enabled when flag set")
except Exception as e: bad(f"guard stuck on: {e}")
os.environ.pop('TCG_WRITE_ENABLED')


# --- contractor ledger: item-code matching and PAYG-withholding exclusion ----

def _dat_le_frame():
    """Dat Le as the live FY27 data actually shows him: payroll name 'Dat Le',
    item/sales name 'Dat Tien Le', both on Linfox - DTL."""
    import pandas as pd
    rows = []
    for d in ("2026-07-06", "2026-07-20", "2026-08-03"):
        rows.append({"Date": d, "Source": "Sales", "Inventory code": "Linfox - DTL",
                     "Description": "Dat Tien Le - Data Analyst", "Units": 10,
                     "Rate": 1040, "Amount": 10400, "Status": "Authorised",
                     "Wages type with Super": None, "Wage Type": None,
                     "Match key": "linfox - dtl"})
        for amt, cc, wt in ((7143.05, "wages", "Wages"),
                            (857.17, "super", "Superannuation"),
                            (1670.0, "Ignore", "PAYG Withholding")):
            rows.append({"Date": d, "Source": "Payroll", "Inventory code": "Linfox - DTL",
                         "Description": "Dat Le", "Units": 0, "Rate": 0, "Amount": amt,
                         "Status": None, "Wages type with Super": cc, "Wage Type": wt,
                         "Match key": "linfox - dtl"})
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def _ledger(monkeypatch, term):
    from src import server as S
    monkeypatch.setattr(S, "_load", lambda fy: (_dat_le_frame(), None, None, None, None))
    fn = getattr(S.get_contractor_ledger, "fn", S.get_contractor_ledger)
    return fn(term)


def test_ledger_matches_on_item_code_not_name(monkeypatch):
    """Payroll name and item name differ; both must return the whole person."""
    by_item = _ledger(monkeypatch, "Dat Tien Le")
    by_payroll = _ledger(monkeypatch, "Dat Le")
    assert "Gross margin: $7,199.34" in by_item
    assert "Gross margin: $7,199.34" in by_payroll


def test_ledger_excludes_payg_withholding_from_cost(monkeypatch):
    """Withholding is carved out of gross, not added to it. 3 x 1,670 = 5,010
    must be reported but must not touch employer cost."""
    out = _ledger(monkeypatch, "Dat Le")
    assert "Employer cost: $24,000.66" in out
    assert "PAYG withheld (excluded from cost): $5,010.00" in out
