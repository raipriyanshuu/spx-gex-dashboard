# SPX Gamma Exposure (GEX) Dashboard

A local Streamlit app that downloads the full SPX option chain from CBOE's free
delayed feed and shows call/put GEX by strike, the Net GEX (cumulative) and
Aggregate GEX lines, the Call Wall, Put Wall, Gamma Flip and the current gamma
regime. It runs on `http://localhost:8501`, and the only request it sends out is
the CBOE download, made only when you click **Refresh Data**.

```
spx-gex-dashboard/
├── app.py               Streamlit UI (entry point)
├── data_fetcher.py      Downloads + parses the CBOE chain
├── gex_calculator.py    Black-Scholes gamma, GEX, walls, flip, regime
├── visualizer.py        Plotly dark-theme charts
├── config.py            All constants (URLs, sign convention, colours...)
├── requirements.txt     Exact package versions
├── run.bat              Windows double-click launcher
├── .streamlit/config.toml   localhost-only, telemetry off, dark theme
└── tests/test_gex.py    Offline tests of the math and parsing
```

---

## Setup and run on Windows

### 1. Install Python (once)
Install **Python 3.11, 3.12 or 3.13** from https://www.python.org/downloads/windows/.
In the installer, tick **"Add python.exe to PATH"**.
Check it in a new terminal: `python --version`

### 2. Open the folder in VS Code
Unzip the project, then in VS Code use **File → Open Folder…** and pick
`spx-gex-dashboard`. Install the **Python** extension (Microsoft) if VS Code
suggests it.

### 3. Create a virtual environment and install packages
Open a terminal in VS Code (**Terminal → New Terminal**). It opens in the
project folder. Then run:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell says *"running scripts is disabled on this system"* when you
activate, run this once and then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

(Or switch the VS Code terminal to **Command Prompt** with the ⌄ next to the +
button, and activate with `.venv\Scripts\activate.bat`.)

If VS Code asks *"select interpreter"*, choose the one in `.venv`.

### 4. (Optional) Check that everything works
```powershell
python -m unittest discover tests -v     # offline math tests, should say OK
python data_fetcher.py                   # one live download: prints spot + contract count
```

### 5. Start the app
```powershell
streamlit run app.py
```
Your browser opens at http://localhost:8501. Click **Refresh Data**.
Stop the app with **Ctrl + C** in the terminal.

Next time, only two commands are needed:
```powershell
.venv\Scripts\activate
streamlit run app.py
```
Or just double-click **run.bat**: the first time it creates `.venv` and
installs everything, and after that it only launches the app.

---

## Data source: what I found

**CBOE's delayed-quotes page has a clean JSON endpoint behind it, so no scraping
or session workaround is needed.**

```
GET https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json
```

- The *quote-table-download* page is a JavaScript page that loads this file. A
  plain HTTP GET returns it: no login, cookies, form post or HTML parsing.
- One response has every listed SPX (AM-settled monthly) and SPXW (weekly/daily)
  contract. Each has `option` (symbol), `bid`, `ask`, `iv`, `open_interest`,
  `volume`, CBOE's own greeks, and last-trade fields. The top level holds
  `timestamp` and `data.current_price` (the SPX level).
- Symbols look like `SPXW261016P05800000` = root `SPXW`, expiry 2026-10-16, Put,
  strike 5800.000 (last 8 digits = strike × 1000).
- **Caveats:** this is an undocumented endpoint that powers CBOE's website, not a
  versioned developer API, so it can change without notice. Quotes are delayed
  ≥15 minutes. Open interest is updated once a day. Use falls under CBOE's
  website terms: this app makes one request per click for personal use. Don't
  redistribute the data or poll it in a loop.

## Implied volatility

CBOE's feed includes an `iv` field for every contract. For some illiquid or
deep in/out-of-the-money contracts it can be `0`. When that happens and the
contract has open interest, the app **back-solves IV from the bid/ask mid by
inverting Black-Scholes** (Brent's method). If there's no two-sided quote to
solve from, the contract is **excluded**. The **Data quality** tab counts both
cases and lists every affected contract, so nothing happens silently.

## Assumptions and honest limits

- **Dealer sign convention is an assumption, not observed data.** No public feed
  shows who holds each contract. The default is the widely used convention from
  SqueezeMetrics' GEX paper: end-users are net *sellers* of calls (overwriting)
  and net *buyers* of puts (hedging). Dealers take the other side, so they are
  assumed **long calls (+)** and **short puts (−)**. That is why call bars point
  right and put bars point left.
  - The original brief described "dealers short calls (−) and short puts (+)".
    Those two can't both hold: a dealer who is short puts is *short* gamma on them,
    not long. The sidebar still offers that "inverted" sign choice so you can compare.
- GEX describes **current, assumed positioning**. It is not a forecast. For SPX,
  research has documented a historical *tendency* for dealer hedging to dampen
  moves in positive-gamma regimes and amplify them in negative-gamma regimes.
  That is a tendency, not a guarantee.
- The Aggregate GEX line and gamma flip keep each contract's current IV as spot
  moves (sticky-strike simplification).
- Time to expiry uses calendar time to 09:30 ET (SPX) or 16:00 ET (SPXW), floored
  at 30 minutes so 0DTE gamma doesn't explode just before the close.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognised | Reinstall Python with "Add to PATH" ticked, or use `py` instead of `python`. |
| `streamlit` not recognised | Activate the venv first (`.venv\Scripts\activate`), or run `python -m streamlit run app.py`. |
| "Refresh failed" with 403 / timeout | Office/VPN networks sometimes block cdn.cboe.com. Try another network. Run `python data_fetcher.py` to see the raw error. |
| Port 8501 already in use | `streamlit run app.py --server.port 8502` |
| Weekend or holiday | Works fine. The data is from the last session, and expired contracts are dropped automatically. |
