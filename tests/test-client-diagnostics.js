'use strict';
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const { load } = require('./dom-harness');
(async () => {
  const cx = load(), d = cx.hooks.Trio.diagnostics;
  for (let i = 0; i < 90; i++) d.request('GET', '/api/history?channel=SECRET&token=SECRET', 200, i);
  d.receipt('channel'); d.receipt('channel');
  d.request('POST', '/api/send', 0, 15, 'SECRET');
  const snapshot = d.snapshot();
  assert.equal(snapshot.events.length, 60);
  assert.equal(snapshot.feeds.channel.count, 2);
  assert(snapshot.feeds.channel.age_ms >= 0);
  assert(!JSON.stringify(snapshot).includes('SECRET'));
  assert.equal(snapshot.events.at(-1).error, 'Other');
  assert.equal(snapshot.events[0].path, '/api/history');
  const count = snapshot.events.length;
  d.request('POST', '/api/client-diagnostics', 200, 2);
  assert.equal(d.snapshot().events.length, count);
  // A reporting network failure is absorbed; chat callers never see rejection.
  vm.runInContext('fetch = async () => { throw new Error("SECRET"); }', cx.context);
  assert.equal(await d.report(), false);
  assert(!JSON.stringify(d.snapshot()).includes('SECRET'));
  console.log('PASS: bounded ring, query/error redaction, receipt counts, no recursion, offline reporting');
})().catch(error => { console.error(error); process.exitCode = 1; });
