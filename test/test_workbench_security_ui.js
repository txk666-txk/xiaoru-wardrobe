const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function securityFixture() {
  const calls = [];
  const origin = 'http://127.0.0.1:5000';
  class BrowserRequest extends Request {
    constructor(input, options) {
      super(typeof input === 'string' ? new URL(input, origin).href : input, options);
    }
  }
  class XHR {
    constructor() { this.headers = {}; this.readyState = 0; }
    open(method, url) { this.method = method; this.url = url; this.readyState = 1; }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.sent = body; }
    dispatchEvent() {}
  }
  const window = {location: {origin, href: origin + '/'}, fetch: async (input, options) => {
    calls.push({input, options});
    return {ok: true, json: async () => String(input.url || input).includes('ws-ticket') ? {ticket: 'one-use'} : {csrf: 'csrf-test'}};
  }};
  vm.runInNewContext(fs.readFileSync('gui/static/js/security.js', 'utf8'), {
    window, Request: BrowserRequest, Headers, URL, XMLHttpRequest: XHR, Event, console
  });
  return {window, calls, XHR};
}

test('same-origin fetch adds CSRF, preserves multipart uploads, and never leaks token cross-origin', async () => {
  const {window, calls} = securityFixture();
  const form = new FormData(); form.append('photo', new Blob(['image']), 'photo.jpg');
  await window.fetch('/api/tryon/jobs', {method: 'POST', body: form, headers: {'X-Fay-Demo-CSRF': 'commerce-token'}});
  const upload = calls.at(-1).input;
  assert.equal(upload.headers.get('X-Fay-CSRF'), 'csrf-test');
  assert.equal(upload.headers.get('X-Fay-Demo-CSRF'), 'commerce-token');
  assert.match(upload.headers.get('Content-Type'), /^multipart\/form-data; boundary=/);
  assert.equal((await upload.formData()).get('photo').name, 'photo.jpg');
  await window.fetch('https://external.test/upload', {method: 'POST', body: 'data'});
  assert.equal(calls.at(-1).input.headers.get('X-Fay-CSRF'), null);
  await window.fetch('/api/get-data');
  assert.equal(calls.at(-1).input.headers.get('X-Fay-CSRF'), null);
});

test('legacy XHR and WebSocket ticket requests use the same initialized browser session', async () => {
  const {window, calls, XHR} = securityFixture();
  const xhr = new XHR();
  xhr.open('POST', '/api/get-msg');
  xhr.send('data=%7B%7D');
  await window.faySecurity.ready;
  await Promise.resolve();
  assert.equal(xhr.headers['X-Fay-CSRF'], 'csrf-test');
  assert.equal(xhr.sent, 'data=%7B%7D');
  assert.equal(await window.faySecurity.ticket('User', 'human'), 'one-use');
  const request = calls.at(-1).input;
  assert.deepEqual(await request.json(), {username: 'User', audience: 'human'});
  assert.equal(request.headers.get('X-Fay-CSRF'), 'csrf-test');
  assert.equal(request.credentials, 'same-origin');
});

function chatFixture() {
  const tickets = [], sockets = [], notices = [];
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
    send(message) { this.sent.push(JSON.parse(message)); }
    close() { this.closed = true; this.readyState = 3; }
  }
  const window = {location: {protocol: 'http:', hostname: '127.0.0.1', port: '5000'},
    faySecurity: {ticket: async (username, audience) => { tickets.push({username, audience}); return `${username}-${audience}`; }}};
  const context = {window, Vue: function () {}, Audio: function () {
    this.pause = () => {}; this.removeAttribute = () => {}; this.load = () => {};
  },
    WebSocket: Socket, document: {addEventListener() {}, removeEventListener() {}},
    setTimeout, clearTimeout, console};
  vm.runInNewContext(fs.readFileSync('gui/static/js/index.js', 'utf8') + '\nglobalThis.Chat = FayInterface;', context);
  const ui = {selectedUser: [1, 'Alice'], $message: {error: value => notices.push(value)}};
  const chat = new context.Chat('ws://127.0.0.1:10003', '', ui);
  return {chat, ui, window, tickets, sockets, notices};
}

test('chat and avatar obtain scoped tickets and user switching closes old sockets and audio', async () => {
  const {chat, ui, tickets, sockets} = chatFixture();
  await chat.connectWebSocket(); await chat.connectHumanWebSocket();
  assert.equal(tickets.length, 2);
  assert.match(sockets[0].url, /ticket=Alice-web$/);
  assert.match(sockets[1].url, /ticket=Alice-human$/);
  chat.registerHumanOutput();
  assert.equal(sockets[1].sent[0].Username, 'Alice');
  chat.audioQueue.push({url: 'old-audio'});
  chat.audioPlaying = true;
  ui.selectedUser = [2, 'Bob'];
  chat.registerHumanOutput('Bob');
  await new Promise(setImmediate);
  assert.equal(sockets[0].closed, true);
  assert.equal(sockets[1].closed, true);
  assert.equal(chat.audioQueue.length, 0);
  assert.equal(chat.audioPlaying, false);
  assert.match(chat.websocket.url, /ticket=Bob-web$/);
  assert.match(chat.humanWebsocket.url, /ticket=Bob-human$/);
  chat.destroy();
});

test('late ticket responses cannot reconnect a previous user', async () => {
  const {chat, ui, window, sockets} = chatFixture();
  let release;
  window.faySecurity.ticket = () => new Promise(resolve => {release = resolve;});
  const old = chat.connectWebSocket();
  ui.selectedUser = [2, 'Bob'];
  window.faySecurity.ticket = async () => 'bob-ticket';
  await chat.connectWebSocket();
  release('alice-ticket'); await old;
  assert.equal(sockets.length, 1);
  assert.match(sockets[0].url, /bob-ticket$/);
  chat.destroy();
});

test('customer avatar stays consistent when legacy robot state messages arrive', () => {
  for (const script of ['gui/static/js/index.js', 'gui/static/js/setting.js', 'faymcp/static/js/setting.js']) {
    for (const portrait of [undefined, '/static/images/customer-avatar-local.png']) {
      let vueOptions;
      const window = {location: {protocol: 'http:', hostname: '127.0.0.1', port: '5000'},
        FAY_CUSTOMER_AVATAR_URL: portrait};
      const context = {window, Vue: function (options) {vueOptions = options;}, Audio: function () {},
        document: {addEventListener() {}}, console, setTimeout, clearTimeout};
      vm.runInNewContext(fs.readFileSync(script, 'utf8') + '\nglobalThis.Chat = FayInterface;', context);
      const expected = portrait || '/static/images/xiaoru-avatar.svg';
      assert.equal(vueOptions.data().robot, expected, script);
      const ui = {$set: (target, key, value) => {target[key] = value;}};
      new context.Chat('', '', ui).handleIncomingMessage({robot: '/robot/Speaking.jpg'});
      assert.equal(ui.robot, expected, script);
    }
  }
});
