const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'gui', 'templates', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function makeElement(messageHeight = 0) {
  const children = [];
  const selectors = new Map();
  const attributes = new Map();
  const classes = new Set();
  let textValue = '';
  let htmlValue = '';
  return {
    className: '',
    get textContent() { return textValue; },
    set textContent(value) {
      textValue = String(value);
      htmlValue = textValue.replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    },
    get innerHTML() { return htmlValue; },
    set innerHTML(value) { htmlValue = String(value); },
    disabled: false,
    scrollHeight: messageHeight,
    style: {},
    children,
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      },
    },
    appendChild(child) { children.push(child); },
    querySelector(selector) {
      if (!selectors.has(selector)) selectors.set(selector, makeElement(messageHeight));
      return selectors.get(selector);
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name) ?? null; },
  };
}

function makePage({ messageHeight = 0 } = {}) {
  const requests = [];
  const elements = new Map();
  const messageNodes = [];
  let nextTimer = 0;
  const activeTimers = new Set();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        const element = makeElement(messageHeight);
        if (id === 'messages') element.appendChild = node => messageNodes.push(node);
        elements.set(id, element);
      }
      return elements.get(id);
    },
    createElement() { return makeElement(messageHeight); },
  };
  const context = vm.createContext({
    document,
    fetch(url, options) {
      let resolve;
      const promise = new Promise(r => { resolve = r; });
      requests.push({ url, options, resolve: data => resolve({ json: async () => data }) });
      return promise;
    },
    setInterval() {
      const id = ++nextTimer;
      activeTimers.add(id);
      return id;
    },
    clearInterval(id) { activeTimers.delete(id); },
    setTimeout() {},
    window: {},
  });
  vm.runInContext(script, context);
  return { context, requests, activeTimers, messageNodes, elements };
}

const managerDone = {
  updates: [],
  phase: 'confirm',
  status: '等待确认',
  running: false,
  done: true,
  cursor: 0,
  waiting_confirm: true,
  monitor_html: '',
  trace_html: '',
};

async function flushPromises() {
  await new Promise(resolve => setImmediate(resolve));
}

test('confirmation begins polling only after chat submission succeeds', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();

  vm.runInContext('sendApprove()', page.context);
  assert.deepEqual(page.requests.map(request => request.url), ['/api/updates?since=0', '/api/chat']);

  page.requests[1].resolve({ success: true, message: 'started' });
  await flushPromises();
  assert.deepEqual(page.requests.map(request => request.url), ['/api/updates?since=0', '/api/chat', '/api/updates?since=0']);
});

test('late Manager completion cannot stop polling for the confirmed run', async () => {
  const page = makePage();
  vm.runInContext('sendApprove()', page.context);
  const chat = page.requests.find(request => request.url === '/api/chat');
  chat.resolve({ success: true, message: 'started' });
  await flushPromises();

  page.requests[0].resolve(managerDone);
  await flushPromises();
  assert.equal(vm.runInContext('isRunning', page.context), true);
  assert.equal(page.activeTimers.size, 1);
});

test('rejected confirmation resynchronizes with the server terminal state', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();

  vm.runInContext('sendApprove()', page.context);
  page.requests[1].resolve({ success: false, message: 'rejected' });
  await flushPromises();

  assert.equal(page.requests[2].url, '/api/updates?since=0');
  page.requests[2].resolve({
    ...managerDone, phase: 'done', status: '已终止', waiting_confirm: false,
  });
  await flushPromises();

  assert.equal(vm.runInContext('isRunning', page.context), false);
  assert.equal(page.activeTimers.size, 0);
});

test('busy confirmation rejoins the active run instead of going idle', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();

  vm.runInContext('sendApprove()', page.context);
  page.requests[1].resolve({ success: false, busy: true, message: '系统正在运行中' });
  await flushPromises();

  assert.equal(vm.runInContext('isRunning', page.context), true);
  assert.equal(page.activeTimers.size, 1);
  assert.equal(page.requests.at(-1).url, '/api/updates?since=0');
});

test('second confirmation click while submitting does not send another POST', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();

  vm.runInContext('sendApprove(); sendApprove()', page.context);

  assert.equal(page.requests.filter(request => request.url === '/api/chat').length, 1);
});

test('a pending poll prevents another poll for the same page', () => {
  const page = makePage();
  vm.runInContext('pollUpdates()', page.context);

  assert.equal(page.requests.filter(request => request.url.startsWith('/api/updates')).length, 1);
});

test('page refresh replays current session messages and advances the cursor', async () => {
  const page = makePage();
  page.requests[0].resolve({
    ...managerDone,
    phase: 'done', status: '分析完成', waiting_confirm: false, cursor: 2,
    updates: [
      { seq: 1, type: 'message', sender: 'User', content: '四分类', time: '01:48:00' },
      { seq: 2, type: 'message', sender: 'Manager', content: '分析完成', time: '01:49:00' },
    ],
  });
  await flushPromises();

  assert.equal(page.messageNodes.length, 2);
  assert.equal(vm.runInContext('lastEventSeq', page.context), 2);
  assert.equal(page.activeTimers.size, 0);
});

test('poll snapshot restores running controls after a refresh', async () => {
  const page = makePage();
  page.requests[0].resolve({
    ...managerDone,
    phase: 'engineer', status: '运行中...', running: true,
    done: false, waiting_confirm: false,
  });
  await flushPromises();

  assert.equal(vm.runInContext('isRunning', page.context), true);
  assert.equal(page.elements.get('stopBtn').disabled, false);
});

test('new-chat busy rejection keeps the active analysis visible', async () => {
  const page = makePage();
  page.requests[0].resolve({
    ...managerDone, phase: 'engineer', status: '运行中...', running: true,
    done: false, waiting_confirm: false,
  });
  await flushPromises();

  vm.runInContext('newChat()', page.context);
  page.requests[1].resolve({ success: false, busy: true, message: '分析正在运行' });
  await flushPromises();

  assert.equal(vm.runInContext('currentPhase', page.context), 'engineer');
  assert.equal(vm.runInContext('isRunning', page.context), true);
  assert.equal(page.activeTimers.size, 1);
});

test('new chat resets the cursor and resumes polling for the new session', async () => {
  const page = makePage();
  page.requests[0].resolve({ ...managerDone, cursor: 5 });
  await flushPromises();

  vm.runInContext('newChat()', page.context);
  page.requests[1].resolve({ success: true });
  await flushPromises();

  assert.equal(vm.runInContext('lastEventSeq', page.context), 0);
  assert.equal(page.requests.at(-1).url, '/api/updates?since=0');
  assert.equal(page.activeTimers.size, 1);
});

test('long agent reply keeps full text and toggles between collapsed and expanded', () => {
  const page = makePage({ messageHeight: 400 });
  const fullText = '完整回复-' + '遥感数据'.repeat(600);

  page.context.processUpdates([
    { type: 'message', sender: 'Engineer', content: fullText, time: '12:00:00' },
  ]);

  const message = page.messageNodes[0];
  const content = message.querySelector('.bubble-content');
  const button = message.querySelector('.bubble').children[0];
  assert.ok(message.innerHTML.includes(fullText));
  assert.equal(content.classList.contains('collapsed'), true);
  assert.equal(button.textContent, '展开全文');
  assert.equal(button.getAttribute('aria-expanded'), 'false');

  button.onclick();
  assert.equal(content.classList.contains('collapsed'), false);
  assert.equal(button.textContent, '收起');
  assert.equal(button.getAttribute('aria-expanded'), 'true');

  button.onclick();
  assert.equal(content.classList.contains('collapsed'), true);
  assert.equal(button.textContent, '展开全文');
  assert.equal(button.getAttribute('aria-expanded'), 'false');
});

test('short agent replies and user messages do not get a toggle', () => {
  const shortPage = makePage({ messageHeight: 100 });
  shortPage.context.processUpdates([
    { type: 'message', sender: 'Manager', content: '简短回复', time: '12:00:00' },
  ]);
  assert.equal(shortPage.messageNodes[0].querySelector('.bubble').children.length, 0);

  const userPage = makePage({ messageHeight: 400 });
  userPage.context.processUpdates([
    { type: 'message', sender: 'User', content: '用户输入'.repeat(600), time: '12:00:00' },
  ]);
  assert.equal(userPage.messageNodes[0].querySelector('.bubble').children.length, 0);
});

test('replayed long agent reply is collapsible after a page refresh', async () => {
  const page = makePage({ messageHeight: 400 });
  page.requests[0].resolve({
    ...managerDone,
    updates: [{ seq: 1, type: 'message', sender: 'Scientist',
      content: '科学分析'.repeat(600), time: '12:00:00' }],
    cursor: 1,
  });
  await flushPromises();

  const button = page.messageNodes[0].querySelector('.bubble').children[0];
  assert.equal(button.textContent, '展开全文');
  button.onclick();
  assert.equal(button.textContent, '收起');
});
