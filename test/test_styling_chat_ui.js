const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  let options;
  const notices = [], timers = [], revoked = [];
  const context = { window: {location: {protocol: 'http:', hostname: 'localhost', port: '5000'}},
    Vue: function(config) {options = config;}, FormData, Audio: function () {},
    document: {querySelector: () => ({scrollHeight: 10, scrollTop: 0}), addEventListener() {}},
    URL: {createObjectURL: () => 'blob:photo', revokeObjectURL: url => revoked.push(url)},
    setTimeout: fn => {timers.push(fn); return timers.length;}, clearTimeout() {}, console };
  vm.runInNewContext(fs.readFileSync('gui/static/js/styling.js', 'utf8'), context);
  vm.runInNewContext(fs.readFileSync('gui/static/js/index.js', 'utf8') + '\nglobalThis.Chat = FayInterface;', context);
  const mixin = context.window.FayStylingMixin;
  const ui = Object.assign(options.data(), mixin.data(), mixin.methods, options.methods, {
    selectedUser: [1, 'User'], liveState: 1, base_url: '',
    userList: [[1, 'User']], messages: [],
    fayService: {unlockAudio() {}, registerHumanOutput() {}},
    $set: (obj, key, value) => { obj[key] = value; },
    $nextTick() {},
    $message: Object.fromEntries(['error','warning','info','success'].map(key => [key, value => notices.push(value)])),
    ensureDemoStoreSession: async () => 'token'
  });
  return {ui, context, notices, revoked, timers};
}

test('chat photo validates size, resets consent, and stages only after consent', async () => {
  const {ui, notices, revoked} = fixture();
  ui.setChatStylePhoto({type:'image/png', size:100});
  await assert.rejects(() => ui.prepareChatStyleAttachment(), /同意/);
  ui.chatStyleConsent = true;
  ui.setChatStylePhoto({type:'image/jpeg', size:500});
  assert.equal(ui.chatStyleConsent, false);
  assert.equal(revoked.length, 1);
  ui.setChatStylePhoto({type:'image/png', size:65*1024*1024});
  assert.match(notices.at(-1), /64MB/);
  ui.chatStylePhoto = new Blob(['data'], {type:'image/jpeg'}); ui.chatStyleConsent = true;
  let staged;
  ui.styleRequest = async (path, method, body) => { staged = {path, method, body}; return {job_id:'photo-one'}; };
  assert.equal(await ui.prepareChatStyleAttachment(), 'photo-one');
  assert.equal(staged.path, '/api/styling/attachments');
  assert.equal(staged.body.get('consent'), 'true');
  ui.chatStylePhotoSent = true;
  assert.equal(await ui.prepareChatStyleAttachment(), null);
});

test('failed chat requests preserve input and attachment for retry; success invokes Fay', async () => {
  const {ui, context, notices} = fixture();
  ui.newMessage = '通勤搭配，预算300';
  ui.prepareChatStyleAttachment = async () => 'photo-one';
  ui.styleRequest = async () => null;
  context.fetch = async () => ({ok:false, json:async () => ({result:'error', message:'失败'})});
  await ui.sendMessage();
  assert.equal(ui.newMessage, '通勤搭配，预算300');
  assert.equal(ui.chatStyleSending, false);
  assert.equal(ui.chatStylePhotoSent, false);
  assert.match(notices.at(-1), /失败/);
  let submitted;
  context.fetch = async (url, options) => {submitted = options.body; return {ok:true, json:async () => ({result:'successful'})};};
  await ui.sendMessage();
  const payload = JSON.parse(new URLSearchParams(submitted).get('data'));
  assert.equal(payload.styling_attachment_id, 'photo-one');
  assert.equal(payload.username, 'User');
  assert.equal(ui.newMessage, '');
  assert.equal(ui.chatStylePhotoSent, true);
});

test('visual cards attach only to matching Fay conversation, including late-arriving stream chunks', async () => {
  const {ui} = fixture();
  const presentation = {revision:'r1', conversation_id:'conv1', username:'User', kind:'styling', plan:{outfits:[]}};
  ui.styleRequest = async () => presentation;
  ui.messages = [{type:'fay', conversation_id:'different'}, {type:'member', conversation_id:'conv1'}, {type:'fay', conversation_id:'conv1', username:'User'}];
  await ui.syncChatStylePresentation('r1');
  assert.equal(ui.messages[0].presentation, undefined);
  assert.equal(ui.messages[1].presentation, undefined);
  assert.equal(ui.messages[2].presentation, presentation);
  const late = {type:'fay', username:'User', conversation_id:'conv1'};
  ui.attachChatStylePresentation(late);
  assert.equal(late.presentation, presentation);
});

test('order choices attach only to the matching chat and select the displayed order ID', async () => {
  const {ui} = fixture();
  const order = {order_id:'FX-20261125-1234ABCD', first_item_name:'宽松衬衫'};
  const presentation = {revision:'order-revision', conversation_id:'order-chat', username:'User',
    kind:'recent_orders', orders:[order]};
  ui.styleRequest = async path => {
    assert.equal(path, '/api/styling/conversation');
    return presentation;
  };
  ui.messages = [{type:'fay', conversation_id:'older-chat', username:'User'},
    {type:'fay', conversation_id:'order-chat', username:'Other'},
    {type:'fay', conversation_id:'order-chat', username:'User'}];
  await ui.syncChatStylePresentation('stale-revision');
  assert.equal(ui.messages[2].presentation, undefined);
  await ui.syncChatStylePresentation('order-revision');
  assert.equal(ui.messages[0].presentation, undefined);
  assert.equal(ui.messages[1].presentation, undefined);
  assert.equal(ui.messages[2].presentation, presentation);
  let prompt;
  ui.sendCommercePrompt = text => {prompt = text;};
  assert.match(fs.readFileSync('gui/templates/index.html', 'utf8'), /item.presentation.orders[^\n]+askOrder\(order\)/);
  ui.askOrder(ui.messages[2].presentation.orders[0]);
  assert.equal(prompt, '我的订单 FX-20261125-1234ABCD 现在是什么状态？');
});

test('a fast WebSocket reply before HTTP acknowledgement cannot re-enable a stale tool spinner', async () => {
  const {ui, context} = fixture();
  ui.newMessage = '这款商品 YZREF-ONE 身高170cm体重65kg，喜欢宽松';
  ui.styleRequest = async () => ({revision:'old'});
  context.fetch = async () => {
    assert.equal(ui.chatStylePending, true);
    ui.chatStylePending = false; ui.chatStyleProgress = '';
    return {ok:true, json:async () => ({result:'successful'})};
  };
  await ui.sendMessage();
  assert.equal(ui.chatStylePending, false);
  assert.equal(ui.chatStyleProgress, '');
});

test('natural outfit requests synchronize tool cards without requiring a budget keyword', async () => {
  for (const phrase of ['搭一套', '搭一身', '配一套', '配一身', '推荐一套衣服，上衣要棕色的外套，内搭要白色，裤子颜色随意']) {
    const {ui, context} = fixture();
    ui.newMessage = '帮我' + phrase;
    ui.styleRequest = async () => ({revision:'before-request'});
    let syncedRevision;
    ui.pollChatStylePresentation = revision => {syncedRevision = revision;};
    context.fetch = async () => {
      assert.equal(ui.chatStylePending, true, phrase);
      return {ok:true, json:async () => ({result:'successful'})};
    };
    await ui.sendMessage();
    assert.equal(syncedRevision, 'before-request', phrase);
    assert.equal(ui.chatStylePending, true, phrase);
    assert.equal(ui.newMessage, '', phrase);
  }
});

test('budget-only answer to the current styling question starts card polling', async () => {
  const {ui, context} = fixture();
  ui.newMessage = '200元';
  ui.styleRequest = async () => ({revision: 'question', username: 'User', kind: 'styling', phase: 'collecting'});
  let baseline;
  ui.pollChatStylePresentation = revision => {baseline = revision;};
  context.fetch = async () => ({ok:true, json:async () => ({result:'successful'})});
  await ui.sendMessage();
  assert.equal(ui.chatStylePending, true);
  assert.equal(baseline, 'question');
});

test('completed text retries the exact card after an early fetch failure or delayed notification', async () => {
  const {ui, context, timers} = fixture();
  const old = {revision:'old', conversation_id:'question', username:'User', kind:'styling', phase:'collecting'};
  const pants = {revision:'new', conversation_id:'answer', username:'User', kind:'styling', phase:'recommended', plan:{outfits:[{items:[{group:'bottoms'}]}]}};
  const replies = [old, old, pants];
  ui.styleRequest = async () => replies.shift();
  ui.chatStylePending = true;
  ui.pollChatStylePresentation('old');
  const chat = new context.Chat('', '', ui);

  // The initial notification fetch sees stale data, so no valid card is cached.
  assert.equal(await ui.syncChatStylePresentation('new'), false);
  chat.handleIncomingMessage({panelReply:{type:'fay', username:'User', id:'answer-id', conversation_id:'answer', content:'已推荐', is_end:true}});
  assert.equal(ui.chatStylePending, false);
  assert.equal(ui.messages[0].presentation, null);

  // The final reply has a known conversation ID. A stale response must not
  // attach to this reply or stop the polling retry.
  await timers.at(-1)();
  assert.equal(ui.messages[0].presentation, null);
  await timers.at(-1)();
  assert.equal(ui.messages[0].presentation, pants);
  assert.equal(ui.chatStyleTimer, null);
});

test('a late polling response cannot attach or stop polling after a newer request starts', async () => {
  const {ui, timers} = fixture();
  let complete;
  ui.styleRequest = () => new Promise(resolve => {complete = resolve;});
  ui.chatStylePending = true;
  ui.pollChatStylePresentation('old');
  const pending = timers.at(-1)();
  ui.pollChatStylePresentation('new');
  const timer = ui.chatStyleTimer;
  complete({revision:'answer', conversation_id:'previous', username:'User', kind:'styling'});
  await pending;
  assert.equal(ui.chatStylePresentations.previous, undefined);
  assert.equal(ui.chatStyleTimer, timer);
  assert.equal(ui.chatStylePending, true);
});

test('a duplicate final event for the preceding turn cannot cancel the current polling request', async () => {
  const {ui} = fixture();
  const previous = {revision:'previous', conversation_id:'previous-turn', username:'User', kind:'styling'};
  ui.styleRequest = async () => previous;
  await ui.syncChatStylePresentation(previous.revision);
  ui.chatStylePending = true;
  ui.pollChatStylePresentation(previous.revision);
  const timer = ui.chatStyleTimer;

  ui.finishChatStyleReply(previous.conversation_id);
  assert.equal(ui.chatStyleTimer, timer);
  assert.equal(ui.chatStylePending, true);
});

test('size chip submits an Agent question with user supplied values; try-on retains separate photo consent', async () => {
  const {ui} = fixture();
  let prompt;
  ui.sendCommercePrompt = value => {prompt = value;};
  ui.chatStyleSize({product_id:'YZREF-ONE'}, {options:{height_cm:170, weight_kg:65, fit:'宽松'}});
  assert.match(prompt, /YZREF-ONE/); assert.match(prompt, /170cm/); assert.match(prompt, /65kg/);
  const owned = {name:'owned.png'}, person = {name:'person.png'};
  ui.stylePhoto = owned; ui.styleMode = 'garment'; ui.chatStyleMode = 'person'; ui.chatStylePhoto = person;
  ui.tryOnForm = {consent:false};
  ui.tryStyleProduct = async () => {assert.equal(ui.stylePhoto, person); assert.equal(ui.styleMode, 'person');};
  await ui.tryChatStyleProduct({product_id:'YZREF-ONE'});
  assert.equal(ui.stylePhoto, owned); assert.equal(ui.styleMode, 'garment'); assert.equal(ui.tryOnForm.consent, false);
});
