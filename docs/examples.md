# Loan-denial example

Run these commands from the adapter repository root.

[parabank_facade.py](../examples/parabank_facade.py) logs in as `john` / `demo` through the facade, checks Account Services, opens Request Loan, checks Apply for a Loan, enters amount `999999` and down payment `1`, submits, and checks Denied.

```powershell
python examples/parabank_facade.py
```

Add `--headed` to show the browser. Use `--url` for another starting address and `--channel` for another Playwright browser channel. Use `--output` for the parent directory containing timestamped page traces. The example prints that parent directory. Denied is the expected business result. The hosted application's data and behavior determine whether the example succeeds.

The optional [explicit-recording example](../examples/parabank_denied_loan.py) follows the same scenario using `PlaywrightEvidence` helpers. It shows the additional control available for deeper integrations.
