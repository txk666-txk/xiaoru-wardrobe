const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
  const notices = [], revoked = [];
  const context = { window: {}, FormData, setTimeout, clearTimeout,
    URL: { createObjectURL: () => 'blob:photo', revokeObjectURL: value => revoked.push(value) } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../gui/static/js/styling.js'), 'utf8'), context);
  const mixin = context.window.FayStylingMixin;
  const ui = Object.assign(mixin.data(), mixin.methods, { base_url: 'http://localhost',
    ensureDemoStoreSession: async () => 'csrf',
    $refs: { stylePhotoInput: { click() {} } },
    $message: Object.fromEntries(['error', 'warning', 'info', 'success'].map(key => [key, message => notices.push(message)])) });
  Object.defineProperty(ui, 'styleResult', { get: () => mixin.computed.styleResult.call(ui) });
  return { ui, context, notices, revoked, mixin };
}

test('upload/drop enforces limits and busy state, and requires fresh photo consent', () => {
  const { ui, notices, revoked } = fixture();
  const first = { type: 'image/jpeg', size: 1000 };
  ui.handleStyleDrop({ dataTransfer: { files: [first] } });
  assert.equal(ui.stylePhoto, first);
  ui.styleConsent = true;
  ui.setStylePhoto({ type: 'image/png', size: 64 * 1024 * 1024 });
  assert.equal(ui.styleConsent, false);
  assert.equal(revoked.length, 1);
  ui.setStylePhoto({ type: 'image/png', size: 64 * 1024 * 1024 + 1 });
  assert.match(notices.at(-1), /64MB/);
  const previous = ui.stylePhoto;
  ui.styleBusy = true;
  ui.setStylePhoto(first);
  assert.equal(ui.stylePhoto, previous);
});

test('API requests include session credentials and CSRF without converting FormData to JSON', async () => {
  const { ui, context } = fixture();
  const calls = [];
  context.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true, data: {} }) }; };
  const body = new FormData(); body.append('mode', 'person');
  await ui.styleRequest('/api/styling/jobs', 'POST', body);
  assert.equal(calls[0].options.headers['X-Fay-Demo-CSRF'], 'csrf');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.body, body);
  assert.equal(calls[0].options.headers['Content-Type'], undefined);
});

test('refinement sends kept product IDs and displays server corrected options', async () => {
  const { ui } = fixture();
  ui.styleJob = { job_id: 'one', result: { options: { style: '通勤' }, outfits: [] } };
  ui.toggleStyleKeep('PANTS');
  let sent;
  ui.styleRequest = async (url, method, body) => { sent = body; return { job_id: 'one', result: { options: { style: '休闲' }, outfits: [] } }; };
  await ui.refineStyle('太正式了');
  assert.equal(sent.keep_product_ids[0], 'PANTS');
  assert.equal(sent.feedback, '太正式了');
  assert.equal(ui.styleOptions.style, '休闲');
  assert.equal(ui.styleBusy, false);
});

test('comparison selection is bounded, can be removed, and requires two products', async () => {
  const { ui, notices } = fixture();
  await ui.compareStyleProducts();
  assert.match(notices.at(-1), /两到三件/);
  ['A', 'B', 'C', 'D'].forEach(id => ui.toggleStyleCompare({ product_id: id }));
  assert.equal(ui.styleCompareIds.length, 3);
  ui.toggleStyleCompare({ product_id: 'B' });
  assert.equal(ui.styleCompareIds.length, 2);
  let tab;
  ui.switchStoreTab = value => { tab = value; };
  ui.styleRequest = async () => ({ summary: '比较建议', items: [] });
  await ui.compareStyleProducts();
  assert.equal(tab, 'styling');
  assert.equal(ui.styleComparison.summary, '比较建议');
});

test('person upload passes to try-on, owned clothing never replaces the person photo', async () => {
  const { ui, context, notices } = fixture();
  context.fetch = async () => ({ ok: true, json: async () => ({ ok: true, data: { product_id: 'A' } }) });
  ui.normalizeStoreProduct = p => p;
  const opened = [], photos = [];
  ui.openTryOn = p => opened.push(p);
  ui.setTryOnPhoto = p => photos.push(p);
  ui.stylePhoto = { name: 'person.jpg' };
  await ui.tryStyleProduct({ product_id: 'A', tryon_supported: true });
  assert.equal(photos[0], ui.stylePhoto);
  ui.styleMode = 'garment';
  await ui.tryStyleProduct({ product_id: 'A', tryon_supported: true });
  assert.equal(photos.length, 1);
  assert.match(notices.at(-1), /本人照片/);
  assert.equal(opened.length, 2);
});

test('sample products without authorized garment images cannot start try-on', async () => {
  const { ui, context, notices } = fixture();
  let requested = false;
  context.fetch = async () => { requested = true; };
  await ui.tryStyleProduct({ product_id: 'EC1005', tryon_supported: false });
  assert.equal(requested, false);
  assert.match(notices.at(-1), /已授权/);
});

test('clearing completed advice releases browser image and deletes server result', async () => {
  const { ui, revoked } = fixture();
  ui.stylePhoto = {}; ui.stylePreview = 'blob:old'; ui.styleJob = { job_id: 'one' };
  let method;
  ui.styleRequest = async (url, value) => { method = value; return {}; };
  await ui.clearStyleAdvice();
  assert.equal(method, 'DELETE'); assert.equal(ui.stylePhoto, null);
  assert.deepEqual(revoked, ['blob:old']); assert.equal(ui.styleJob, null);
});
