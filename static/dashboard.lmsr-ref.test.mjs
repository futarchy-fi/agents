import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';

const dashboardHtml = fs.readFileSync(
  path.join(import.meta.dirname, 'dashboard.html'),
  'utf8',
);

function marketDetailFixture() {
  return {
    market_id: 7,
    question: 'Will PR #7 merge?',
    category: 'pr_merge',
    category_id: 'snapshot-labs/sx-monorepo#7@2026-03-17',
    status: 'open',
    outcomes: ['yes', 'no'],
    prices: { yes: '0.5', no: '0.5' },
    b: '57.70780163555854',
    liquidity: '40',
    num_trades: 0,
    resolution: null,
    created_at: '2026-03-17T00:00:00Z',
    deadline: '2026-03-18T00:00:00Z',
    resolved_at: null,
    amm_account_id: 1,
    q: { yes: '0', no: '0' },
    volume: '0',
    metadata: {
      repo: 'snapshot-labs/sx-monorepo',
      market_type: 'conditional',
      pr_url: 'https://github.com/snapshot-labs/sx-monorepo/pull/7',
    },
  };
}

function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    headers: { 'Content-Type': 'application/json' },
  });
}

async function flushUi(window) {
  await new Promise(resolve => window.setTimeout(resolve, 0));
  await Promise.resolve();
  await new Promise(resolve => window.setTimeout(resolve, 0));
}

test('LMSR reference stays expanded across scheduled market detail refreshes', async () => {
  const intervalCallbacks = [];
  const market = marketDetailFixture();

  const dom = new JSDOM(dashboardHtml, {
    runScripts: 'dangerously',
    url: 'https://example.test/dashboard#/market/7',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async (url) => {
        const pathname = new URL(String(url), 'https://example.test').pathname;
        if (pathname === '/v1/markets/7') return jsonResponse(market);
        if (pathname === '/v1/markets/7/trades') return jsonResponse([]);
        if (pathname === '/v1/markets/7/positions') return jsonResponse([]);
        if (pathname === '/v1/markets/7/depth') return jsonResponse({ rows: [] });
        if (pathname === '/v1/markets') return jsonResponse([market]);
        throw new Error(`Unhandled fetch path: ${pathname}`);
      };

      window.setInterval = (fn) => {
        intervalCallbacks.push(fn);
        return intervalCallbacks.length;
      };
      window.clearInterval = () => {};
    },
  });

  await flushUi(dom.window);

  const details = dom.window.document.querySelector('details.lmsr-ref');
  assert.ok(details, 'expected LMSR reference details element to render');
  assert.equal(intervalCallbacks.length, 1, 'expected detail refresh interval to be registered');

  details.open = true;
  details.dispatchEvent(new dom.window.Event('toggle'));
  assert.equal(details.open, true, 'expected LMSR reference to be open before refresh');

  await intervalCallbacks[0]();
  await flushUi(dom.window);

  const refreshedDetails = dom.window.document.querySelector('details.lmsr-ref');
  assert.ok(refreshedDetails, 'expected LMSR reference to render after refresh');
  assert.equal(
    refreshedDetails.open,
    true,
    'expected LMSR reference to remain open after scheduled refresh',
  );
});
