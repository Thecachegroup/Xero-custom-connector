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
