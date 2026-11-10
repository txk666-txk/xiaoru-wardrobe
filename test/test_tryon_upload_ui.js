// Run with: node --test test/test_tryon_upload_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function fixture() {
  let options;
  let created = 0;
  const revoked = [];
  const notices = [];
  const downloads = [];
  const timers = [];
  const context = {
    window: { location: { protocol: 'http:', hostname: 'localhost', port: '5000' } },
    Vue: function (config) { options = config; },
    URL: {
      createObjectURL: () => `blob:photo-${++created}`,
      revokeObjectURL: url => revoked.push(url)
    },
    document: {
      body: { appendChild() {} },
      createElement: () => ({
        click() { downloads.push({ href: this.href, filename: this.download }); },
        remove() {}
      })
    },
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); },
    console
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../gui/static/js/index.js'), 'utf8'), context);
  const input = { value: '', clicks: 0, click() { this.clicks += 1; } };
  const instance = Object.assign(options.data(), options.methods, {
    $refs: { tryOnPhotoInput: input },
    $message: {
      error: message => notices.push(message),
      warning: message => notices.push(message),
      info: message => notices.push(message),
      success: message => notices.push(message)
    }
  });
  return { instance, input, notices, revoked, context, downloads, timers };
}

const photo = (size, name = 'person.png') => ({ type: 'image/png', size, name });
const drag = files => ({ dataTransfer: { types: ['Files'], files, dropEffect: 'none' } });

test('picker and drop accept large photos, enforce 64MB, and preserve a valid preview on rejection', () => {
  const { instance: ui, notices, revoked } = fixture();
  const first = photo(9 * 1024 * 1024);
  ui.handleTryOnPhoto({ target: { files: [first] } });
  assert.equal(ui.tryOnPhoto, first);
  const boundary = photo(64 * 1024 * 1024);
  ui.handleTryOnPhotoDrop(drag([boundary]));
  assert.equal(ui.tryOnPhoto, boundary);
  assert.deepEqual(revoked, ['blob:photo-1']);
  ui.handleTryOnPhotoDrop(drag([photo(boundary.size + 1)]));
  assert.equal(ui.tryOnPhoto, boundary);
  assert.match(notices.at(-1), /64MB/);
  ui.handleTryOnPhotoDrop(drag([{ type: 'text/plain', size: 100 }]));
  assert.equal(ui.tryOnPhoto, boundary);
  assert.match(notices.at(-1), /JPEG/);
  ui.handleTryOnPhotoDrop(drag([photo(0)]));
  assert.match(notices.at(-1), /为空/);
  ui.handleTryOnPhotoDrop(drag([first, boundary]));
  assert.match(notices.at(-1), /一张/);
  ui.handleTryOnPhoto({ target: { files: [] } });
  assert.equal(ui.tryOnPhoto, boundary);
});

test('nested file drags remain highlighted until leaving and clear on drop', () => {
  const { instance: ui } = fixture();
  ui.handleTryOnPhotoDragEnter(drag([]));
  ui.handleTryOnPhotoDragEnter(drag([]));
  ui.handleTryOnPhotoDragLeave();
  assert.equal(ui.tryOnPhotoDragging, true);
  ui.handleTryOnPhotoDragLeave();
  assert.equal(ui.tryOnPhotoDragging, false);
  ui.handleTryOnPhotoDragEnter({ dataTransfer: { types: ['text/plain'] } });
  assert.equal(ui.tryOnPhotoDragging, false);
  const event = drag([photo(1000)]);
  ui.handleTryOnPhotoDragOver(event);
  assert.equal(event.dataTransfer.dropEffect, 'copy');
  assert.equal(ui.tryOnPhotoDragging, true);
  ui.handleTryOnPhotoDrop(event);
  assert.equal(ui.tryOnPhotoDragging, false);
  assert.equal(ui.tryOnPhotoDragDepth, 0);
});

test('generation locks picker and drop; clearing releases previews and drag state', () => {
  const { instance: ui, input, revoked } = fixture();
  const first = photo(1000);
  ui.setTryOnPhoto(first);
  ui.chooseTryOnPhoto();
  assert.equal(input.clicks, 1);
  ui.tryOnLoading = true;
  ui.chooseTryOnPhoto();
  ui.handleTryOnPhoto({ target: { files: [photo(2000)] } });
  ui.handleTryOnPhotoDrop(drag([photo(3000)]));
  const event = drag([]);
  ui.handleTryOnPhotoDragOver(event);
  assert.equal(event.dataTransfer.dropEffect, 'none');
  assert.equal(ui.tryOnPhoto, first);
  assert.equal(input.clicks, 1);
  ui.tryOnLoading = false;
  ui.handleTryOnPhotoDragEnter(drag([]));
  ui.clearTryOnPhoto();
  assert.equal(ui.tryOnPhoto, null);
  assert.equal(ui.tryOnPhotoPreview, '');
  assert.equal(ui.tryOnPhotoDragging, false);
  assert.equal(input.value, '');
  assert.deepEqual(revoked, ['blob:photo-1']);
});

test('replacement hides old results and late deletion cannot erase a new result', async () => {
  const { instance: ui, context } = fixture();
  let resolveDelete;
  const pending = new Promise(resolve => { resolveDelete = resolve; });
  let notifyFetch;
  const fetched = new Promise(resolve => { notifyFetch = resolve; });
  let deletionUrl;
  ui.ensureDemoStoreSession = async () => 'test-token';
  context.fetch = url => { deletionUrl = url; notifyFetch(); return pending; };
  const originalDelete = ui.deleteTryOnResult;
  let deletion;
  ui.deleteTryOnResult = function (silent) {
    deletion = originalDelete.call(this, silent);
    return deletion;
  };
  ui.tryOnResult = { job_id: 'previous-job' };
  ui.tryOnForm.consent = true;
  ui.setTryOnPhoto(photo(1000));
  assert.equal(ui.tryOnResult, null);
  assert.equal(ui.tryOnForm.consent, false);
  await fetched;
  assert.match(deletionUrl, /previous-job$/);
  ui.tryOnResult = { job_id: 'new-job' };
  resolveDelete({ ok: true, json: async () => ({ ok: true }) });
  await deletion;
  assert.equal(ui.tryOnResult.job_id, 'new-job');
});

function readyResult(ui, productId = 'DRESS-1') {
  ui.tryOnResult = { job_id: 'download-job', product_id: productId, result_url: '/api/tryon/jobs/download-job/image' };
}

function jpegResponse(size = 1000) {
  return { ok: true, status: 200, headers: { get: () => 'image/jpeg' }, blob: async () => ({ type: 'image/jpeg', size }) };
}

test('download checks the same-session JPEG and keeps its blob alive for the native save dialog', async () => {
  const { instance: ui, context, downloads, notices, revoked, timers } = fixture();
  readyResult(ui);
  let request;
  context.fetch = async (url, options) => { request = { url, options }; return jpegResponse(); };
  await ui.downloadTryOnResult();
  assert.match(request.url, /\/api\/tryon\/jobs\/download-job\/image\?download=1$/);
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.cache, 'no-store');
  assert.deepEqual(downloads, [{ href: 'blob:photo-1', filename: 'fay-tryon-DRESS-1.jpg' }]);
  assert.equal(ui.tryOnDownloading, false);
  assert.match(notices.at(-1), /已发起下载/);
  assert.doesNotMatch(notices.at(-1), /已保存/);
  assert.deepEqual(revoked, []);
  assert.equal(timers[0].delay, 60000);
  timers[0].callback();
  assert.deepEqual(revoked, ['blob:photo-1']);
});

test('expired results and invalid responses show an error without downloading a JSON or empty file', async () => {
  const cases = [
    { response: { ok: false, status: 404, json: async () => ({ error: 'tryon_result_not_found' }) }, message: /过期/, gone: true },
    { response: { ok: false, status: 500, json: async () => ({ message: '图片读取失败' }) }, message: /图片读取失败/ },
    { response: { ok: true, status: 200, headers: { get: () => 'application/json' } }, message: /JPG/ },
    { response: jpegResponse(0), message: /为空/ },
    { error: new Error('网络连接已断开'), message: /网络连接已断开/ }
  ];
  for (const scenario of cases) {
    const { instance: ui, context, downloads, notices, timers } = fixture();
    readyResult(ui);
    context.fetch = async () => {
      if (scenario.error) throw scenario.error;
      return scenario.response;
    };
    await ui.downloadTryOnResult();
    assert.equal(downloads.length, 0);
    assert.equal(timers.length, 0);
    assert.equal(ui.tryOnDownloading, false);
    assert.match(notices.at(-1), scenario.message);
    if (scenario.gone) assert.equal(ui.tryOnResult, null);
    else assert.equal(ui.tryOnResult.job_id, 'download-job');
  }
});

test('pending download blocks duplicate requests and retains the originally selected filename', async () => {
  const { instance: ui, context, downloads } = fixture();
  readyResult(ui, 'ORIGINAL');
  let resolve;
  let fetches = 0;
  context.fetch = () => { fetches += 1; return new Promise(done => { resolve = done; }); };
  const pending = ui.downloadTryOnResult();
  assert.equal(ui.tryOnDownloading, true);
  await ui.downloadTryOnResult();
  assert.equal(fetches, 1);
  readyResult(ui, 'REPLACEMENT');
  resolve(jpegResponse());
  await pending;
  assert.equal(downloads[0].filename, 'fay-tryon-ORIGINAL.jpg');
});

test('late expired download never clears a replacement result, and expired deletion allows regeneration', async () => {
  const { instance: ui, context, downloads } = fixture();
  readyResult(ui);
  let resolve;
  context.fetch = () => new Promise(done => { resolve = done; });
  const pending = ui.downloadTryOnResult();
  readyResult(ui, 'REPLACEMENT');
  const replacement = ui.tryOnResult;
  const missing = { ok: false, status: 404, json: async () => ({ error: 'tryon_result_not_found' }) };
  resolve(missing);
  await pending;
  assert.equal(ui.tryOnResult, replacement);
  assert.equal(downloads.length, 0);
  ui.ensureDemoStoreSession = async () => 'test-token';
  context.fetch = async () => missing;
  await ui.deleteTryOnResult(true);
  assert.equal(ui.tryOnResult, null);
});
