'use strict';
const assert = require('assert');
const { load } = require('./dom-harness');
const H = load().hooks;
H.Trio.state.channel = 'synthetic';
H.Trio.state.operator = { id: 'operator' };
const calls = [];
H.Trio.api.upload = async file => {
  calls.push(file.name);
  return { ok: true, id: calls.length, filename: file.name,
    mime: 'application/octet-stream', url: '/api/attachment/' + calls.length };
};
(async () => {
  for (const name of ['module.stl', 'assembly.f3d', 'unknown.blob']) {
    await H.Trio.actions.uploadImage({ name, type: '', size: 10 });
  }
  assert.deepStrictEqual(calls, ['module.stl', 'assembly.f3d', 'unknown.blob']);
  await assert.rejects(H.Trio.actions.uploadImage({ name: 'large.stl', type: '',
    size: 251 * 1024 * 1024 }), /larger than 250 MB/);
  assert.strictEqual(calls.length, 3);
  console.log('PASS: arbitrary file formats reach the API; size cap still applies');
})().catch(error => { console.error(error); process.exitCode = 1; });
