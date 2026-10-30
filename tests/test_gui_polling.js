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
    set innerHTML(value) { htmlValue = String(value); children.length = 0; },
    disabled: false,
    open: false,
    parentNode: null,
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
    appendChild(child) {
      if (child.parentNode) {
        const siblings = child.parentNode.children;
        siblings.splice(siblings.indexOf(child), 1);
      }
      children.push(child);
      child.parentNode = this;
      return child;
    },
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
        if (id === 'messages' || id === 'processMessages') {
          const append = element.appendChild;
          element.appendChild = function(node) { messageNodes.push(node); return append.call(this, node); };
        }
        elements.set(id, element);
      }
      return elements.get(id);
    },
    createElement() { return makeElement(messageHeight); },
  };
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) document.getElementById(id);
  document.getElementById('chatSlot').appendChild(document.getElementById('messages'));
  const context = vm.createContext({
    document,
    fetch(url, options) {
      let resolve, reject;
      const promise = new Promise((r, j) => { resolve = r; reject = j; });
      requests.push({ url, options, resolve: data => resolve({ json: async () => data }), reject });
      return promise;
    },
    setInterval(callback, delay) {
      assert.equal(delay, 500);
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

test('plain result card escapes model text and only renders current result links', async () => {
  const page = makePage();
  page.requests[0].resolve({...managerDone, result_delivery: {
    completion: 'candidate', headline: '<script>bad()</script>', goal: '建筑变化',
    artifact_check: '文件已检查', verification: '尚未独立验证', limitations: ['需复核'], findings: [],
    files: [{name: 'map.png', kind: 'image', url: '/api/result_file?path=task%2Fmap.png'},
      {name: 'evil', kind: 'image', url: 'javascript:alert(1)'}],
    previews: [{name: 'map.png', kind: 'image', url: '/api/result_file?path=task%2Fmap.png'},
      {name: 'evil', url: 'javascript:alert(1)'}],
    professional_report: '<img src=x onerror=bad()>',
  }});
  await flushPromises();
  const html = page.elements.get('resultSummary').innerHTML;
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('候选结果'));
  assert.ok(html.includes('/api/result_file?path=task%2Fmap.png'));
  assert.equal(page.elements.get('workspace').classList.contains('has-result'), true);
});

test('直接展示结果并把原对话节点保存在可展开的记录中', () => {
  const page = makePage();
  assert.ok(!html.includes('id="plainView"') && !html.includes('id="expertView"'));
  assert.equal(typeof page.context.setViewMode, 'undefined');
  page.context.processUpdates([{type: 'message', sender: 'Manager', content: '请确认需求', time: '10:00:00'}]);
  const messages = page.elements.get('messages');
  const originalMessage = page.messageNodes[0];
  page.context.renderDelivery({completion: 'failed', headline: '未完成', files: [], previews: []});
  assert.equal(messages.parentNode, page.elements.get('conversationRecord'));
  assert.equal(messages.children[0], originalMessage);
  const card = page.elements.get('resultSummary').innerHTML;
  assert.ok(card.includes('<details id="professionalDetails">'));
  assert.ok(!card.includes('<details open'));
  page.context.renderDelivery(null);
  assert.equal(messages.parentNode, page.elements.get('chatSlot'));
  assert.equal(messages.children[0], originalMessage);
  assert.equal(page.elements.get('resultSummary').innerHTML, '');
  assert.equal(page.elements.get('workspace').classList.contains('has-result'), false);
});

test('selected files are sent with request but approval does not replace inputs', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();
  vm.runInContext("selectedInputs.add('pair/before.tif'); sendToBackend('看看哪里变化了', false)", page.context);
  assert.deepEqual(JSON.parse(page.requests[1].options.body).selected_inputs, ['pair/before.tif']);
  page.requests[1].resolve({success: true});
  await flushPromises();
  page.requests.at(-1).resolve(managerDone);
  await flushPromises();
  page.context.sendApprove();
  assert.equal(JSON.parse(page.requests.at(-1).options.body).selected_inputs, undefined);
});

test('技术记录放入折叠过程，需求确认仍在对话中显示', () => {
  const page = makePage();
  page.context.processUpdates([{type: 'message', sender: 'Engineer', content: '完整代码', time: '10:00:00'}]);
  assert.ok(page.messageNodes[0].className.includes('technical-message'));
  assert.ok(page.messageNodes[0].innerHTML.includes('完整代码'));
  assert.equal(page.messageNodes[0].parentNode, page.elements.get('processMessages'));
  page.context.processUpdates([{type: 'message', sender: 'Manager', content: '请确认需求', time: '10:00:00'}]);
  assert.equal(page.messageNodes[1].parentNode, page.elements.get('messages'));
  assert.ok(html.includes('<details id="processDetails" class="process-details">'));
});

test('相同轮询快照保留展开状态和原对话按钮事件', () => {
  const page = makePage({messageHeight: 400});
  page.context.processUpdates([{type: 'message', sender: 'Manager', content: '确认需求'.repeat(200), time: '10:00:00'}]);
  const button = page.messageNodes[0].querySelector('.bubble').children[0];
  const result = {completion: 'candidate', headline: '候选区域', files: [], previews: []};
  page.context.renderDelivery(result);
  const panel = page.elements.get('resultSummary');
  const marker = makeElement();
  panel.appendChild(marker);
  page.elements.get('processDetails').open = true;
  page.context.renderDelivery({...result});
  assert.equal(panel.children[0], marker);
  assert.equal(page.elements.get('processDetails').open, true);
  button.onclick();
  assert.equal(button.textContent, '收起');
});

test('多图结果分别展示并转义文本，图片必须属于本次白名单', () => {
  const page = makePage();
  const file = {name: 'water.png', kind: 'image', url: '/api/result_file?path=task%2Fwater.png'};
  page.context.renderDelivery({completion: 'candidate', headline: '逐幅结果', files: [file], previews: [file],
    scene_results: [{label: 'water_01 <script>', findings: ['面积0.57；只找到13%'], preview: file},
      {label: 'water_02', findings: ['面积11.49；找到97%'], preview: {url: 'https://example.invalid/not-current.png'}}]});
  const html = page.elements.get('resultSummary').innerHTML;
  assert.ok(html.includes('water_01 &lt;script&gt;'));
  assert.ok(html.includes('只找到13%'));
  assert.ok(html.includes('water_02'));
  assert.ok(html.includes('找到97%'));
  assert.ok(!html.includes('src="https://example.invalid'));
  assert.ok(!html.includes('href="https://example.invalid'));
  assert.equal((html.match(/<img /g) || []).length, 1);
});

test('过程关闭时收到的长回复仍可完整展开', () => {
  const page = makePage({messageHeight: 0});
  const text = '完整分析方案'.repeat(300);
  page.context.processUpdates([{type: 'message', sender: 'Scientist', content: text, time: '10:00:00'}]);
  const message = page.messageNodes[0];
  const button = message.querySelector('.bubble').children[0];
  assert.ok(message.innerHTML.includes(text));
  assert.equal(button.textContent, '展开全文');
  button.onclick();
  assert.equal(button.textContent, '收起');
});

test('新建对话清除旧结果和过程并收起详情', async () => {
  const page = makePage();
  page.requests[0].resolve(managerDone);
  await flushPromises();
  page.context.processUpdates([{type: 'message', sender: 'Engineer', content: '旧代码', time: '10:00:00'}]);
  page.context.renderDelivery({completion: 'candidate', headline: '旧结果', files: [], previews: []});
  page.elements.get('processDetails').open = true;
  page.elements.get('conversationArchive').open = true;
  page.context.newChat();
  page.requests[1].resolve({success: true});
  await flushPromises();
  assert.equal(page.elements.get('processMessages').innerHTML, '');
  assert.equal(page.elements.get('processMessages').children.length, 0);
  assert.equal(page.elements.get('processDetails').open, false);
  assert.equal(page.elements.get('conversationArchive').open, false);
  assert.equal(page.elements.get('messages').parentNode, page.elements.get('chatSlot'));
  assert.equal(page.elements.get('resultSummary').innerHTML, '');
});

test('刷新顶部目录扫描保留展开状态且不触发折叠', async () => {
  const page = makePage();
  let prevented = false;
  let stopped = false;
  const panel = page.elements.get('dataPanel');
  panel.open = true;
  page.context.refreshData({preventDefault() { prevented = true; }, stopPropagation() { stopped = true; }});
  const request = page.requests.find(item => item.url === '/api/refresh_data');
  assert.equal(request.options.method, 'POST');
  request.resolve({success: true, html: '<p>本次扫描：2幅影像</p>'});
  await flushPromises();
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  assert.equal(panel.open, true);
  assert.equal(page.elements.get('dataPanelBody').innerHTML, '<p>本次扫描：2幅影像</p>');
});

test('分析过程收起时右侧管线仍接收进度监督和追溯更新', async () => {
  const page = makePage();
  page.requests[0].resolve({...managerDone, phase: 'engineer', running: true, done: false,
    waiting_confirm: false, monitor_html: '<p>正在检查本次任务</p>',
    trace_html: '<tr><td>本次执行记录</td></tr>'});
  await flushPromises();
  assert.equal(page.elements.get('processDetails').open, false);
  assert.equal(page.elements.get('step-engineer').classList.contains('active'), true);
  assert.equal(page.elements.get('step-confirm').querySelector('.step-status').textContent, '✓');
  assert.equal(page.elements.get('phaseName').textContent, 'Engineer 编码');
  assert.equal(page.elements.get('monitorBox').innerHTML, '<p>正在检查本次任务</p>');
  assert.equal(page.elements.get('traceBody').innerHTML, '<tr><td>本次执行记录</td></tr>');
});

test('温度结果直接展示摄氏度和图示，专业内容按需展开', () => {
  const page = makePage();
  page.context.renderDelivery({completion: 'candidate', headline: '已生成地表温度图',
    verification: '温度误差尚未验证', findings: [], limitations: [],
    files: [{name: 'final_lst_preview.png', url: '/api/result_file?path=current.png', kind: 'image'}], previews: [],
    scene_results: [{label: '温度影像', findings: ['平均27.5℃，相对高温不是危险等级。'],
      preview: {name: 'final_lst_preview.png', url: '/api/result_file?path=current.png', kind: 'image'}}],
    professional_report: '热常数K1/K2', professional_audit: '固定辐射传输公式'});
  const html = page.elements.get('resultSummary').innerHTML;
  assert.ok(html.includes('平均27.5℃'));
  assert.ok(html.includes('/api/result_file?path=current.png'));
  assert.ok(html.includes('<details'));
  assert.ok(!html.includes('<details open'));
  assert.ok(!html.includes('水体'));
});


test('页面刷新能恢复正在使用DeepSeek的标志与等待时间', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false,
    llm_activity: {active: true, agent: 'DeepSeek工具Agent', elapsed_seconds: 7, request_count: 1}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek正在调用');
  assert.ok(page.elements.get('deepseekStatusDetail').textContent.includes('7秒'));
  assert.equal(page.elements.get('deepseekStatus').getAttribute('aria-busy'), 'true');
  assert.equal(page.elements.get('deepseekStatus').classList.contains('busy'), true);
});

test('执行工具时不把一般运行状态当成DeepSeek正在调用', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false, llm_activity: {active: false}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek待命');
  assert.equal(page.elements.get('deepseekStatus').classList.contains('busy'), false);
  assert.equal(page.elements.get('stopBtn').disabled, false);
});

test('请求结束或任务终止会撤下等待动画', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false, llm_activity: {active: true}});
  await flushPromises();
  vm.runInContext('pollUpdates()', page.context);
  page.requests[1].resolve({updates: [], running: false, done: true, status: '已终止', llm_activity: {active: false}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatus').classList.contains('busy'), false);
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek待命');
});

test('新会话清除旧模型请求标志', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false, llm_activity: {active: true}});
  await flushPromises();
  vm.runInContext('newChat()', page.context);
  page.requests.find(x => x.url === '/api/new_chat').resolve({success: true});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatus').getAttribute('aria-busy'), 'false');
  assert.equal(page.elements.get('deepseekStatusDetail').textContent, '');
});

test('轮询断开时明确状态未知并能在恢复后更新', async () => {
  const page = makePage();
  page.requests[0].reject(new Error('模拟断开'));
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek状态待确认');
  vm.runInContext('pollUpdates()', page.context);
  page.requests[1].resolve({updates: [], running: true, done: false, llm_activity: {active: true, agent: 'Manager', elapsed_seconds: 2}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek正在调用');
  assert.equal(page.elements.get('deepseekStatus').classList.contains('unknown'), false);
});

test('状态文字不注入模型文本且兼容没有新字段的响应', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false,
    llm_activity: {active: true, agent: '<script>bad()</script>', elapsed_seconds: -3}});
  await flushPromises();
  assert.ok(!page.elements.get('deepseekStatusDetail').innerHTML.includes('<script>'));
  vm.runInContext('pollUpdates()', page.context);
  page.requests[1].resolve({updates: [], running: true, done: false});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek待命');
});


test('首次轮询前短请求已结束仍能看到本轮次数', async () => {
  const page = makePage();
  page.requests[0].resolve({updates: [], running: true, done: false,
    llm_activity: {active: false, total_requests: 1, finished_requests: 1}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek本轮已调用 1 次');
  assert.equal(page.elements.get('deepseekStatus').classList.contains('busy'), false);
  assert.equal(page.elements.get('deepseekStatus').classList.contains('called'), true);
});

test('任务结束及刷新保留累计次数但不继续转圈', async () => {
  const page = makePage();
  page.requests[0].resolve({...managerDone, waiting_confirm: false,
    llm_activity: {active: true, total_requests: 7, finished_requests: 7}});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek本轮已调用 7 次');
  assert.equal(page.elements.get('deepseekStatus').getAttribute('aria-busy'), 'false');
  assert.equal(page.elements.get('deepseekStatusDetail').textContent, '当前未等待模型响应');
  assert.equal(page.activeTimers.size, 0);
});

test('累计次数随真实快照更新并在新会话清零', async () => {
  const page = makePage();
  page.requests[0].resolve({...managerDone, llm_activity: {active: false, total_requests: 2}});
  await flushPromises();
  vm.runInContext('newChat()', page.context);
  page.requests.find(x => x.url === '/api/new_chat').resolve({success: true});
  await flushPromises();
  assert.equal(page.elements.get('deepseekStatusText').textContent, 'DeepSeek待命');
  assert.equal(page.elements.get('deepseekStatus').classList.contains('called'), false);
  page.requests.at(-1).resolve({updates: [], running: true, done: false,
    llm_activity: {active: true, total_requests: 1, agent: 'Manager'}});
  await flushPromises();
  assert.ok(page.elements.get('deepseekStatusDetail').textContent.includes('本轮第 1 次请求'));
  assert.equal(page.elements.get('deepseekStatus').classList.contains('busy'), true);
});


test('自主解释回答不显示不存在的下载结果', () => {
  const page = makePage();
  page.context.renderDelivery({completion: 'answered', headline: '已回答你的问题。', goal: 'NDVI是什么', findings: ['NDVI是植被指数'], files: [], previews: [], limitations: []});
  const result = page.elements.get('resultSummary').innerHTML;
  assert.ok(result.includes('说明回答'));
  assert.ok(!result.includes('下载本次结果'));
});

test('自主结果逐项显示用户要求并转义描述', () => {
  const page = makePage();
  page.context.renderDelivery({completion: 'candidate', headline: '部分完成', files: [], previews: [], requirements: [
    {met: true, description: '分布图'}, {met: false, description: '<script>面积</script>'}]});
  const result = page.elements.get('resultSummary').innerHTML;
  assert.ok(result.includes('已完成：分布图'));
  assert.ok(result.includes('未完成：&lt;script&gt;面积&lt;/script&gt;'));
  assert.ok(!result.includes('<script>面积'));
});
