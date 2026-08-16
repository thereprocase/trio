const fs = require('fs');
const vm = require('vm');
const path = require('path');
// Resolved against THIS FILE, not the working directory: tests/run-all.sh
// runs from tests/, and a cwd-relative path made these pass standalone
// from the repo root while failing under the runner.
const WEB_JS = n => path.resolve(__dirname, '..', 'server', 'web', 'js', n);

const context = {
  window: {},
  location: { search: '', pathname: '/', href: 'http://localhost/' },
  URLSearchParams,
  document: {
    getElementById: () => null,
    querySelectorAll: () => [],
    body: { classList: { toggle() {} } },
    documentElement: { dataset: {} },
  },
  localStorage: { getItem: () => null, setItem() {} },
  fetch: () => Promise.resolve({ ok: false, status: 404 }),
  EventTarget: class { addEventListener() {} dispatchEvent() {} },
  CustomEvent: class {},
  console,
  setInterval() {},
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
for (const name of ['01-store.js', '02-api.js', '05-loader.js', '04-events.js',
                    '06-core.js', '09-ui.js', '20-workspace.js']) {
  vm.runInContext(fs.readFileSync(WEB_JS(name), 'utf8'), context);
}

let failures = 0;
function check(name, condition) {
  console.log((condition ? 'PASS: ' : 'FAIL: ') + name);
  if (!condition) failures += 1;
}

const w = context.Trio.workspace;
const trend = w.trendChip(2.25, 'h24');
check('trend uses percentage-point units and names its measurement window',
      trend.includes('+2.3 pp/hr') && trend.includes('last 24h'));

const daily = w.dailyChangeLine({ percentage_points: 5.5, elapsed_hours: 24 });
check('daily change reports the actual quota movement over 24 hours',
      daily.includes('+5.5 pp') && daily.includes('last 24h'));

const forecast = w.projectionLine({
  exhausted: false, will_exhaust: true, before_reset: false,
  exhaust_at: Date.now() / 1000 + 30 * 3600,
  rate_per_hr: 2, window: 'h24', projected_at_reset: 60,
  hours_to_reset: 10,
}, false, 'Weekly quota');
check('forecast states expected usage at reset',
      forecast.includes('60% expected at reset'));
check('forecast exposes the exact current + rate × time calculation',
      forecast.includes('40% now + 2.0 pp/hr × 10h')
      && forecast.includes('24h trend'));
check('forecast states whether reset arrives before exhaustion',
      forecast.includes('Reset arrives before 100%'));

const pending = w.projectionLine({ exhausted: false, rate_per_hr: null }, false, 'Quota');
check('forecast explains when the burn baseline is not ready',
      pending.includes('Forecast pending') && pending.includes('at least 1 minute'));

console.log(`\n${failures ? 'FAILED' : 'OK'} — ${failures} failure(s)`);
process.exitCode = failures ? 1 : 0;
