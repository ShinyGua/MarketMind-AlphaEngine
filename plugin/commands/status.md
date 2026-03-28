# /mm:status — Show all workspace statuses

## Description

Display the current pipeline status for all company workspaces.

## Steps

### 1. Find All Workspaces

```bash
ls -d workspaces/*/status.json 2>/dev/null
```

### 2. Display Status Table

For each workspace that has a `status.json`, read it and display:

```
MarketMind Workspaces
=====================

  Ticker   Stage              Completed    Mode
  ------   -----              ---------    ----
  NVDA     discuss_synthesis   8/14        daily
  AAPL     completed          14/14        daily
  MSFT     collect             2/14        weekly

To run a pipeline:  /mm:run workspaces/{TICKER}
To create new:      /mm:init
```

### 3. Empty State

If no workspaces found:

```
No workspaces found. Run /mm:init to create one.
```
