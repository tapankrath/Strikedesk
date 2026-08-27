# StrikeDesk

Ready-made options trades, screened by outlook, risk, reward, and probability — built on
end-of-day options chain data. Static site, no backend required (yet).

## 1. Create the repo

1. On GitHub, click **New repository**. Name it whatever you like (e.g. `strikedesk`).
   Keep it **Public** — GitHub Pages on a free account only serves public repos.
2. Don't initialize with a README (you already have one here).

## 2. Upload the files

Keep this exact folder structure when you upload:

```
your-repo/
├── index.html
├── manifest.json
├── service-worker.js
├── .nojekyll
├── README.md
└── icons/
    ├── icon-192.png
    ├── icon-512.png
    ├── apple-touch-icon.png
    └── favicon.png
```

Easiest path: on the repo's GitHub page, click **Add file → Upload files**, drag in
`index.html`, `manifest.json`, `service-worker.js`, `.nojekyll`, and `README.md`, then
create an `icons` folder by dragging the four icon files in together (typing
`icons/icon-192.png` etc. as the file name when you drop a single file also works, if
GitHub's uploader doesn't preserve the subfolder automatically).

## 3. Turn on GitHub Pages

1. In the repo, go to **Settings → Pages**.
2. Under **Build and deployment**, set **Source** to `Deploy from a branch`.
3. Branch: `main`, folder: `/ (root)`. Save.
4. GitHub gives you a URL like `https://yourusername.github.io/strikedesk/` — it takes
   a minute or two to go live after the first deploy.

## 4. Add it to your iPhone home screen

1. Open the GitHub Pages URL in **Safari** on your iPhone (must be Safari, not Chrome —
   iOS only allows installing home-screen apps from Safari).
2. Tap the **Share** icon (square with an arrow) in the toolbar.
3. Scroll down and tap **Add to Home Screen**.
4. Confirm the name (defaults to "StrikeDesk") and tap **Add**.

It'll now launch full-screen from your home screen, no Safari address bar, with the
bar-chart icon you generated.

## Updating the site later

Any time you push new commits to `main` (or re-upload changed files through the GitHub
UI), GitHub Pages redeploys automatically within a minute or two. If you change
`index.html`, `manifest.json`, or any icon, bump `CACHE_NAME` in `service-worker.js`
(e.g. `strikedesk-v1` → `strikedesk-v2`) so phones that already installed the app pick
up the change instead of serving a stale cached copy.

## Current state

Right now all trade data is mocked directly in `index.html` (see the `trades` array in
the `<script>` block) — there's no live data source wired up yet. Filtering, the trade
detail modal, and the mobile drawer are all fully functional against that mock data.

## Where this goes next

- **Real EOD data**: swap the mock `trades` array for a fetch against a real end-of-day
  options provider (ORATS, EODHD, Finnhub, etc.).
- **Free nightly refresh without a backend**: a scheduled GitHub Actions workflow can
  pull EOD data after market close, write it to a `data.json` file in this repo, and
  commit it automatically. `index.html` then just fetches `data.json` instead of using
  the hardcoded array — zero server cost, updates once a day.
- **AI Trade Analysis button**: currently just a UI shell — wire "Copy Prompt" to build
  a real prompt string from the active filters + visible rows.
