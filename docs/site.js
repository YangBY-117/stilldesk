const samples = {
  cli: 'git clone https://github.com/YangBY-117/stilldesk.git\ncd stilldesk\nnode bin/stilldesk.mjs doctor\nnode bin/stilldesk.mjs browser list_pages',
  mcp: '{\n  "mcpServers": {\n    "stilldesk": {\n      "command": "node",\n      "args": ["/path/to/stilldesk/bin/stilldesk.mjs", "mcp"]\n    }\n  }\n}',
  config: 'node bin/stilldesk.mjs config codex\nnode bin/stilldesk.mjs config gemini\nnode bin/stilldesk.mjs config kimi\n# Exports a configuration. Does not launch your agent.',
};
let selected = 'cli';
const code = document.querySelector('#install-code code');
const tabs = [...document.querySelectorAll('[data-code]')];
function select(tab) {
  selected = tab.dataset.code;
  tabs.forEach(t => { t.classList.toggle('active', t === tab); t.setAttribute('aria-selected', String(t === tab)); t.tabIndex = t === tab ? 0 : -1; });
  if (code) code.textContent = samples[selected];
  document.querySelector('#install-code')?.setAttribute('aria-labelledby', tab.id);
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => select(tab));
  tab.addEventListener('keydown', event => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault(); const t = tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]; select(t); t.focus();
    }
  });
});
if (tabs[0]) select(tabs[0]);
document.querySelector('#copy-code')?.addEventListener('click', async event => {
  try { await navigator.clipboard.writeText(samples[selected]); event.currentTarget.textContent = 'Copied'; }
  catch { event.currentTarget.textContent = 'Select code to copy'; }
});
const steps = [
  ['browser_take_snapshot', 'page and file input found', 'done'],
  ['browser_fill_form', 'report details prepared', 'done'],
  ['browser_upload_file', 'bytes transferred through extension', 'done'],
  ['browser_read_text', 'page confirmation checked', 'done'],
];
let timer;
function replay() {
  clearTimeout(timer);
  const lines = document.querySelector('#demo-lines'); const outcome = document.querySelector('#demo-outcome');
  if (!lines || !outcome) return;
  lines.replaceChildren(); outcome.textContent = '';
  let index = 0;
  function next() {
    if (index === steps.length) { outcome.textContent = '✓ Task complete. Your cursor never moved.'; return; }
    const [method, label, time] = steps[index++];
    const row = document.createElement('li');
    const check = document.createElement('span'); check.className = 'check'; check.textContent = '✓';
    const content = document.createElement('span');
    const methodNode = document.createElement('span'); methodNode.className = 'method'; methodNode.textContent = method;
    content.append(methodNode, document.createElement('br'), document.createTextNode(label));
    const ms = document.createElement('small'); ms.textContent = time;
    row.append(check, content, ms); lines.append(row);
    timer = setTimeout(next, matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 700);
  }
  next();
}
document.querySelector('#replay')?.addEventListener('click', replay);
replay();
