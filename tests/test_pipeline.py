import sys, os
sys.path.insert(0,'.')
os.environ.setdefault("XERO_CLIENT_ID","t"); os.environ.setdefault("XERO_CLIENT_SECRET","t")
from datetime import date
from src import mappers, checks
import pandas as pd

def xd(iso):  # Xero /Date(ms+0000)/ format
    from datetime import datetime, timezone
    return "/Date(%d+0000)/" % int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()*1000)

# --- Xero-shaped fixtures modelled on real TCG patterns ---
items = [
 {"Code":"Linfox - DTL","Name":"Dat Le","IsSold":True,"IsPurchased":True,
  "PurchaseDetails":{"UnitPrice":758.93},"SalesDetails":{"UnitPrice":1040.00}},
 {"Code":"Linfox - JJ","Name":"Jay Jhala","IsSold":True,"IsPurchased":True,
  "PurchaseDetails":{"UnitPrice":1100.00},"SalesDetails":{"UnitPrice":1266.00}},
 {"Code":"zLinfox - SJ","Name":"Simon Jones","IsSold":True,"IsPurchased":True,
  "PurchaseDetails":{"UnitPrice":900.00},"SalesDetails":{"UnitPrice":1137.00}},
 {"Code":"Linfox - DUP","Name":"Dup Person","IsSold":True,"IsPurchased":True,
  "PurchaseDetails":{"UnitPrice":800.00},"SalesDetails":{"UnitPrice":1000.00}},
 {"Code":"Linfox - DUP","Name":"Dup Person","IsSold":True,"IsPurchased":True,
  "PurchaseDetails":{"UnitPrice":850.00},"SalesDetails":{"UnitPrice":1000.00}},
]
sales_inv = [{"InvoiceID":"i1","InvoiceNumber":"TCG-21001","Type":"ACCREC","Status":"AUTHORISED",
  "Date":xd("2025-08-15"),"DueDate":xd("2025-09-14"),"AmountDue":0,"Total":10400,
  "Contact":{"Name":"Linfox IT"},"Reference":"Dat Le Aug 2025",
  "LineItems":[{"ItemCode":"Linfox - DTL","Description":"Dat Le","Quantity":10,"UnitAmount":1040.0,"LineAmount":10400.0}]},
 {"InvoiceID":"i2","InvoiceNumber":"TCG-21002","Type":"ACCREC","Status":"AUTHORISED",
  "Date":xd("2025-08-15"),"DueDate":xd("2025-09-14"),"AmountDue":13926,"Total":13926,
  "Contact":{"Name":"Linfox IT"},"Reference":"Jay Jhala Aug 2025",
  "LineItems":[{"ItemCode":"Linfox - JJ","Description":"Jay Jhala","Quantity":11,"UnitAmount":1266.0,"LineAmount":13926.0}]},
 {"InvoiceID":"i3","InvoiceNumber":"TCG-21003","Type":"ACCREC","Status":"AUTHORISED",
  "Date":xd("2025-08-20"),"DueDate":xd("2025-09-19"),"AmountDue":9391,"Total":9391,
  "Contact":{"Name":"Xenon Media"},"Reference":"payrolling",
  "LineItems":[{"Description":"Shane Bell Base Salary","Quantity":1,"UnitAmount":9391.0,"LineAmount":9391.0},
               {"Description":"Superannuation","Quantity":1,"UnitAmount":1100.93,"LineAmount":1100.93}]},
 {"InvoiceID":"i4","InvoiceNumber":"TCG-21004","Type":"ACCREC","Status":"AUTHORISED",
  "Date":xd("2025-08-25"),"DueDate":xd("2025-09-24"),"AmountDue":0,"Total":11370,
  "Contact":{"Name":"Linfox IT"},"Reference":"Simon Jones Aug 2025",
  "LineItems":[{"ItemCode":"zLinfox - SJ","Description":"Simon Jones","Quantity":10,"UnitAmount":1137.0,"LineAmount":11370.0}]}]
bills = [{"InvoiceID":"b1","InvoiceNumber":"BILL-1","Type":"ACCPAY","Status":"AUTHORISED",
  "Date":xd("2025-08-15"),"DueDate":xd("2025-09-14"),"AmountDue":0,"Total":12100,
  "Contact":{"Name":"Jay Jhala"},"Reference":"Jay Jhala Aug 2025",
  "LineItems":[{"ItemCode":"Linfox - JJ","Description":"Jay Jhala","Quantity":11,"UnitAmount":1100.0,"LineAmount":12100.0}]},
 {"InvoiceID":"b2","InvoiceNumber":"BILL-2","Type":"ACCPAY","Status":"AUTHORISED",
  "Date":xd("2025-08-25"),"DueDate":xd("2025-09-24"),"AmountDue":0,"Total":9000,
  "Contact":{"Name":"Simon Jones"},"Reference":"Simon Jones Aug 2025",
  "LineItems":[{"ItemCode":"Linfox - SJ","Description":"Simon Jones","Quantity":10,"UnitAmount":900.0,"LineAmount":9000.0}]}]
pay_runs = [{"PayRunID":"p1","PaymentDate":xd("2025-08-18"),"PayRunPeriodEndDate":xd("2025-08-17"),
  "PayRunStatus":"POSTED","Payslips":[
    {"EmployeeID":"e1","PayslipID":"s1","FirstName":"Dat","LastName":"Le",
     "Wages":7589.30,"Tax":2100.00,"Super":910.72,"NetPay":5489.30,"Deductions":0}]}]

print("=== A. Mappers ===")
it = mappers.items_to_rows(items);            print(f"  items      -> {len(it)} rows")
sa = mappers.invoices_to_rows(sales_inv);     print(f"  sales      -> {len(sa)} rows")
bi = mappers.invoices_to_rows(bills);         print(f"  bills      -> {len(bi)} rows")
pr = mappers.payrun_summaries_to_rows(pay_runs); print(f"  payroll    -> {len(pr)} rows")
print(pr[["Employee","Pay Item","Date","Amount","Cost Class"]].to_string(index=False))

print("\n=== B. Unified frame ===")
data = mappers.build_data_frame(sa, bi, pr, it,
        customer_lookup={"Linfox IT":"Linfox IT","Xenon Media":"Xenon Media"},
        no_payroll_tax=set(), current_fy_start=date(2025,7,1))
print(f"  {len(data)} rows, columns: {len(data.columns)}")
print(data[["Source","Description","Inventory code","Date","Units","Amount","Wage Type","Wages type with Super"]].to_string(index=False))

print("\n=== C. Cost maths ===")
cost_rows = data[(data["Source"].isin(["Bills","Payroll"])) &
                 (data["Wages type with Super"].astype(str).str.lower()!="ignore")]
print(f"  employer cost = ${cost_rows['Amount'].sum():,.2f}  (expect 20,600.02 = bill 12,100 + wages 7,589.30 + super 910.72)")
paygw = data[data["Wages type with Super"].astype(str).str.lower()=="ignore"]
print(f"  PAYG withheld, excluded from cost = ${paygw['Amount'].sum():,.2f}  (expect 2,100.00)")
print(f"  sales = ${data[data['Source']=='Sales']['Amount'].sum():,.2f}  (expect 33,717.00)")

print("\n=== D. Transaction date used for payroll ===")
pdates = sorted(data[data["Source"]=="Payroll"]["Date"].dt.date.unique())
print(f"  payroll dates {pdates}  (payment date 2025-08-18, NOT period end 08-17)")
assert pdates==[date(2025,8,18)], "payroll not using payment date"
print("  PASS payment date used")

print("\n=== E. Checks ===")
ex = checks.run_all(data, it)
print(f"  {len(ex)} exceptions")
if not ex.empty:
    print(ex[["contractor","severity","rule","detail"]].to_string(index=False))
