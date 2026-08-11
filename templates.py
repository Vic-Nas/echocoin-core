"""UI base template and large static HTML blocks. No logic, no I/O."""

_BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PoolCoin — {title}</title>
<style>
  :root {{
    --bg:     #0d0f14;
    --panel:  #13161e;
    --border: #1e2330;
    --text:   #c8cdd8;
    --muted:  #5a6070;
    --accent: #4fa3e0;
    --green:  #3ecf8e;
    --red:    #e05c5c;
    --mono:   "JetBrains Mono", "Fira Mono", "Consolas", monospace;
    --sans:   "Inter", system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 14px; line-height: 1.6;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* Nav */
  nav {{
    display: flex; align-items: center; gap: 1.5rem;
    padding: .75rem 1.5rem; border-bottom: 1px solid var(--border);
    background: var(--panel);
  }}
  nav .brand {{ font-weight: 700; color: var(--accent); font-size: 1.1rem; letter-spacing: .05em; }}
  nav a {{ color: var(--muted); font-size: .85rem; }}
  nav a:hover {{ color: var(--text); text-decoration: none; }}

  /* Layout */
  .page {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}

  /* Cards */
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1rem;
  }}
  .card-title {{ font-size: .7rem; font-weight: 600; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted); margin-bottom: .5rem; }}

  /* Stat grid */
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .75rem; }}
  .stat {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: .75rem 1rem; }}
  .stat-label {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }}
  .stat-value {{ font-size: 1.3rem; font-weight: 700; color: var(--text); font-family: var(--mono); }}

  /* Table */
  table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
  th {{ text-align: left; padding: .5rem .75rem; color: var(--muted);
       font-weight: 600; font-size: .7rem; letter-spacing: .08em; text-transform: uppercase;
       border-bottom: 1px solid var(--border); }}
  td {{ padding: .5rem .75rem; border-bottom: 1px solid var(--border); font-family: var(--mono); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #161a24; }}

  /* Forms */
  .form-group {{ margin-bottom: .75rem; }}
  label {{ display: block; font-size: .75rem; color: var(--muted); margin-bottom: .25rem; text-transform: uppercase; letter-spacing: .06em; }}
  input, textarea {{
    width: 100%; background: #0a0c11; border: 1px solid var(--border);
    color: var(--text); padding: .5rem .75rem; border-radius: 4px;
    font-family: var(--mono); font-size: .85rem;
  }}
  input:focus, textarea:focus {{ outline: none; border-color: var(--accent); }}
  button {{
    background: var(--accent); color: #fff; border: none; border-radius: 4px;
    padding: .5rem 1.25rem; font-size: .85rem; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ opacity: .85; }}
  .btn-ghost {{ background: transparent; border: 1px solid var(--border); color: var(--muted); }}
  .btn-ghost:hover {{ border-color: var(--accent); color: var(--accent); opacity: 1; }}

  /* Alerts */
  .alert {{ padding: .6rem 1rem; border-radius: 4px; font-size: .85rem; margin-bottom: .75rem; }}
  .alert-ok  {{ background: #0d2318; border: 1px solid #1e5c3a; color: var(--green); }}
  .alert-err {{ background: #200f0f; border: 1px solid #5c1e1e; color: var(--red); }}

  /* Hash display */
  .hash {{ font-family: var(--mono); font-size: .8rem; color: var(--muted); word-break: break-all; }}
  .hash-short {{ font-family: var(--mono); font-size: .85rem; }}

  /* Section header */
  h2 {{ font-size: 1rem; font-weight: 600; color: var(--text); margin-bottom: .75rem; }}
  h3 {{ font-size: .85rem; font-weight: 600; color: var(--muted); margin-bottom: .5rem; }}

  /* Whitepaper */
  .wp {{ max-width: 740px; line-height: 1.8; }}
  .wp h1 {{ font-size: 1.6rem; font-weight: 700; color: var(--text); margin: 1.5rem 0 .5rem; }}
  .wp h2 {{ font-size: 1.1rem; font-weight: 700; color: var(--accent); margin: 1.5rem 0 .5rem; border-bottom: 1px solid var(--border); padding-bottom: .25rem; }}
  .wp h3 {{ font-size: .95rem; font-weight: 600; color: var(--text); margin: 1rem 0 .25rem; }}
  .wp p  {{ margin-bottom: .75rem; color: var(--text); }}
  .wp pre {{ background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
             padding: .75rem 1rem; font-family: var(--mono); font-size: .8rem;
             overflow-x: auto; margin: .75rem 0; }}
  .wp code {{ font-family: var(--mono); font-size: .85em; color: var(--accent); }}
  .wp pre code {{ color: var(--text); }}
  .wp ol, .wp ul {{ margin: .5rem 0 .75rem 1.25rem; }}
  .wp li {{ margin-bottom: .25rem; }}
  .wp em {{ color: var(--muted); font-style: italic; }}
  .wp strong {{ color: var(--text); font-weight: 600; }}
  .wp blockquote {{ border-left: 3px solid var(--accent); padding-left: .75rem; color: var(--muted); margin: .75rem 0; }}

  /* Send form grid */
  .output-row {{ display: grid; grid-template-columns: 1fr 140px 40px; gap: .5rem; align-items: start; margin-bottom: .35rem; }}

  /* Stats charts */
  .chart-wrap {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 1rem; margin-bottom: 1rem; }}
  .chart-wrap svg {{ display: block; width: 100%; }}
  .chart-title {{ font-size: .7rem; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin-bottom: .75rem; }}
  .legend {{ display: flex; gap: 1.5rem; margin-top: .5rem; font-size: .75rem; color: var(--muted); }}
  .legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: .35rem; }}
</style>
</head>
<body>
<nav>
  <span class="brand">POOLCOIN</span>
  <a href="/">Dashboard</a>
  <a href="/send">Send</a>
  <a href="/explorer">Explorer</a>
  <a href="/address">Address</a>
  <a href="/stats">Stats</a>
  <a href="/whitepaper">Whitepaper</a>
</nav>
<div class="page">
{body}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Stats page body (kept as a module-level raw string to avoid escape warnings
# from Python 3.12+ seeing JS template-literal ${...} and backtick sequences
# inside regular triple-quoted strings).
# ---------------------------------------------------------------------------
_STATS_BODY = r"""
        <div id="stats-loading" style="color:var(--muted);font-size:.85rem;padding:1rem 0">Loading chain data...</div>
        <div id="stats-content" style="display:none">

          <div class="stats" style="margin-bottom:1rem" id="stat-cards"></div>

          <div class="chart-wrap">
            <div class="chart-title">Circulating supply over time</div>
            <svg id="chart-supply" viewBox="0 0 800 220" xmlns="http://www.w3.org/2000/svg"></svg>
            <div class="legend">
              <span><span class="legend-dot" style="background:var(--green)"></span>Circulating</span>
              <span><span class="legend-dot" style="background:var(--accent)"></span>Cumulative minted</span>
            </div>
          </div>


        </div>

        <script>
        const SEEDS = 100_000_000; // 1 PC = 100,000,000 seeds
        const fmt = seeds => (seeds / SEEDS).toFixed(4) + ' PC';
        const fmtSeeds = n => n.toLocaleString() + ' seeds';

        function polyline(points, xKey, yKey, W, H, PAD, maxX, maxY) {
          if (!points.length) return '';
          const px = p => PAD + (p[xKey] / maxX) * (W - PAD * 2);
          const py = p => H - PAD - (p[yKey] / maxY) * (H - PAD * 2);
          return points.map((p, i) =>
            (i === 0 ? 'M' : 'L') + px(p).toFixed(1) + ',' + py(p).toFixed(1)
          ).join(' ');
        }

        function axisLabels(svg, W, H, PAD, maxX, maxY) {
          for (let i = 0; i <= 4; i++) {
            const x = PAD + (i / 4) * (W - PAD * 2);
            const val = Math.round((i / 4) * maxX);
            svg.innerHTML += `<text x="${x}" y="${H - 4}" fill="#5a6070" font-size="10" text-anchor="middle">${val}</text>`;
          }
          for (let i = 0; i <= 4; i++) {
            const y = H - PAD - (i / 4) * (H - PAD * 2);
            const val = (i / 4) * maxY / SEEDS;
            svg.innerHTML += `<text x="${PAD - 4}" y="${y + 4}" fill="#5a6070" font-size="10" text-anchor="end">${val.toFixed(1)}K</text>`;
          }
          for (let i = 1; i <= 3; i++) {
            const y = H - PAD - (i / 4) * (H - PAD * 2);
            svg.innerHTML += `<line x1="${PAD}" y1="${y}" x2="${W - PAD}" y2="${y}" stroke="#1e2330" stroke-width="1"/>`;
          }
        }

        function drawChart(svgId, points, lines, maxX, maxY) {
          const svg = document.getElementById(svgId);
          const W = 800, H = 220, PAD = 48;
          svg.innerHTML = '';
          if (!points.length) {
            svg.innerHTML = '<text x="400" y="110" fill="#5a6070" font-size="13" text-anchor="middle">No data yet — mine some blocks first.</text>';
            return;
          }
          axisLabels(svg, W, H, PAD, maxX, maxY);
          for (const {key, color, width} of lines) {
            const d = polyline(points, 'height', key, W, H, PAD, maxX, maxY);
            if (d) svg.innerHTML += `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width || 1.5}"/>`;
          }
        }

        fetch('/api/stats')
          .then(r => r.json())
          .then(data => {
            document.getElementById('stats-loading').style.display = 'none';
            document.getElementById('stats-content').style.display = '';

            const t = data.totals;
            const netLast = t.net_emission_last || 0;
            const netColor = netLast >= 0 ? 'var(--green)' : 'var(--red)';
            const netLabel = netLast >= 0 ? '+' + fmt(netLast) + '/block' : fmt(netLast) + '/block';
            const cards = [
              ['Circulating',       fmt(t.circulating)],
              ['Total minted',      fmt(t.minted)],
              ['Net emission (last block)', `<span style="color:${netColor}">${netLabel}</span>`],
            ];
            document.getElementById('stat-cards').innerHTML = cards.map(([label, val]) =>
              `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value" style="font-size:1rem">${val}</div></div>`
            ).join('');

            const pts = data.points;
            if (!pts.length) return;
            const maxX = pts[pts.length - 1].height;
            const maxY = Math.max(...pts.map(p => p.minted)) * 1.05 || 1;
            drawChart('chart-supply', pts, [
              {key: 'minted',      color: 'var(--accent)', width: 1},
              {key: 'circulating', color: 'var(--green)',  width: 2},
            ], maxX, maxY);
          })
          .catch(() => {
            document.getElementById('stats-loading').textContent = 'Failed to load stats.';
          });
        </script>
"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

