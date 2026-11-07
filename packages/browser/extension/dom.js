(() => {
  if (globalThis.__stilldeskBrowser) return;
  // getRandomValues remains available on ordinary HTTP pages as well.
  const newEpoch = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  let epoch = newEpoch();
  let nodes = new Map();
  const staging = new Map();
  setInterval(() => {
    for (const [id, stage] of staging) if (Date.now() - stage.updated > 120000) staging.delete(id);
  }, 30000);
  const cap = (value, max) => String(value || '').slice(0, max);
  function rootOf(element) {
    try { return element.shadowRoot || chrome.dom?.openOrClosedShadowRoot(element); } catch { return element.shadowRoot; }
  }
  function* walk(root) {
    for (const element of root.children || []) {
      yield element;
      const shadow = rootOf(element);
      if (shadow) yield* walk(shadow);
      yield* walk(element);
    }
  }
  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
  }
  function get(ref) {
    const element = nodes.get(ref);
    if (!element || !element.isConnected || element.ownerDocument !== document || !ref.startsWith(epoch + ':')) throw new Error('Stale uid; take a new snapshot');
    return element;
  }
  function name(element) {
    const aria = element.getAttribute('aria-label');
    const labels = element.labels ? Array.from(element.labels).map(l => l.textContent).join(' ') : '';
    return cap(aria || labels || element.getAttribute('alt') || element.getAttribute('title') ||
      element.getAttribute('placeholder') || element.innerText || element.textContent || element.id, 250).trim();
  }
  function role(element) {
    return element.getAttribute('role') || ({ BUTTON: 'button', A: 'link', TEXTAREA: 'textbox',
      SELECT: 'combobox', IFRAME: 'iframe', H1: 'heading', H2: 'heading', H3: 'heading' })[element.tagName] ||
      (element.tagName === 'INPUT' ? ({ checkbox: 'checkbox', radio: 'radio', file: 'file',
        button: 'button', submit: 'button' })[element.type] || 'textbox' : 'element');
  }
  function text(maxText) {
    let result = document.body?.innerText || '';
    for (const element of walk(document)) {
      const shadow = rootOf(element);
      if (shadow && visible(element)) result += '\n' + Array.from(shadow.children).map(e => e.innerText || '').join('\n');
    }
    const max = Math.max(1, Math.min(Number(maxText) || 20000, 60000));
    return { text: result.slice(0, max), truncated: result.length > max };
  }
  function snapshot(args) {
    epoch = newEpoch();
    nodes = new Map();
    const result = [];
    const max = Math.max(1, Math.min(Number(args.maxNodes) || 600, 1500));
    let omitted = 0;
    for (const element of walk(document)) {
      const isFile = element.tagName === 'INPUT' && element.type === 'file';
      if (!isFile && (!visible(element) || !element.matches('button,a,input,textarea,select,[role],[contenteditable],[draggable],[tabindex],h1,h2,h3,iframe'))) continue;
      if (result.length >= max) { omitted++; continue; }
      const ref = `${epoch}:${result.length}`;
      nodes.set(ref, element);
      const box = element.getBoundingClientRect();
      const row = { ref, role: role(element), name: name(element), tag: element.tagName.toLowerCase(),
        id: element.id || undefined, visible: visible(element), disabled: !!element.disabled,
        rect: { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) } };
      if (element.tagName === 'INPUT') row.type = element.type;
      if (isFile) Object.assign(row, { multiple: element.multiple, accept: element.accept,
        files: Array.from(element.files || []).map(f => ({ name: f.name, size: f.size })) });
      else if ('value' in element && element.type !== 'password') row.value = cap(element.value, 500);
      if (element.type === 'checkbox' || element.type === 'radio') row.checked = element.checked;
      result.push(row);
    }
    return { url: location.href, title: document.title, nodes: result, omittedNodes: omitted,
      viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
      layout: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }, ...text(args.maxText) };
  }
  function event(element, type) { element.dispatchEvent(new Event(type, { bubbles: true, composed: true })); }
  function fill(element, value) {
    if (element.disabled || element.readOnly) throw new Error('Element is disabled or readonly');
    if (element.type === 'file') throw new Error('Use upload_file for file inputs');
    if (element.type === 'checkbox' || element.type === 'radio') {
      if (!['true', 'false'].includes(value)) throw new Error('Checkbox values must be true or false strings');
      if (element.type === 'radio' && value !== 'true') throw new Error('Radio buttons can only be selected');
      if (element.checked !== (value === 'true')) element.click();
    } else if (element.isContentEditable) {
      element.focus();
      element.textContent = String(value);
      event(element, 'input'); event(element, 'change');
    } else {
      const proto = element.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype :
        element.tagName === 'SELECT' ? HTMLSelectElement.prototype :
          element.tagName === 'INPUT' ? HTMLInputElement.prototype : undefined;
      if (!proto) throw new Error('Element is not a supported form control');
      if (element.tagName === 'SELECT' && !Array.from(element.options).some(o => o.value === String(value))) {
        throw new Error('Select option does not exist');
      }
      element.focus();
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, String(value));
      event(element, 'input'); event(element, 'change');
    }
    return { filled: true, eventIsTrusted: false };
  }
  function getStage(uploadId) {
    const stage = staging.get(uploadId);
    if (!stage || Date.now() - stage.updated > 120000 || !stage.element.isConnected || stage.element.ownerDocument !== document) {
      staging.delete(uploadId);
      throw new Error('Upload stage expired or target was removed');
    }
    stage.updated = Date.now();
    return stage;
  }
  async function run(action, args) {
    try {
      let value;
      if (action === 'snapshot') value = snapshot(args);
      else if (action === 'read_text') value = { url: location.href, ...text(args.maxText) };
      else if (action === 'validate') { get(args.ref); value = { valid: true }; }
      else if (action === 'click') {
        const element = get(args.ref);
        if (!visible(element) || element.disabled) throw new Error('Element is hidden or disabled');
        if (element.type === 'file') throw new Error('Use upload_file; opening the native file chooser is unsupported');
        const anchor = element.closest('a[href]');
        if (anchor && anchor.target && !['_self', '_parent', '_top'].includes(anchor.target.toLowerCase())) {
          value = { openBackgroundUrl: anchor.href };
        } else {
          element.scrollIntoView({ block: 'center', inline: 'center' });
          element.click();
          value = { dispatched: true, eventIsTrusted: false, outcomeRequiresInspection: true };
        }
      } else if (action === 'fill') value = fill(get(args.ref), args.value);
      else if (action === 'select') {
        const element = get(args.ref);
        if (element.tagName !== 'SELECT') throw new Error('select requires a select element');
        value = fill(element, args.value);
      } else if (action === 'check') {
        const element = get(args.ref);
        if (!['checkbox', 'radio'].includes(element.type)) throw new Error('check requires a checkbox or radio');
        value = fill(element, args.checked ? 'true' : 'false');
      } else if (action === 'scroll') {
        const element = args.ref ? get(args.ref) : document.scrollingElement;
        const x = Number(args.x || 0), y = Number(args.y || 0);
        if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('Scroll offsets must be finite');
        element.scrollBy({ left: x, top: y, behavior: 'instant' });
        value = { scrolled: true, x: element.scrollLeft, y: element.scrollTop, systemPointerMoved: false };
      } else if (action === 'drag') {
        const source = get(args.from), target = get(args.to);
        if (!visible(source) || !visible(target)) throw new Error('Drag targets must be visible');
        const dataTransfer = new DataTransfer();
        for (const [element, type] of [[source,'dragstart'],[target,'dragenter'],[target,'dragover'],[target,'drop'],[source,'dragend']]) {
          const box = element.getBoundingClientRect();
          element.dispatchEvent(new DragEvent(type, { dataTransfer, bubbles: true, composed: true,
            cancelable: true, clientX: box.x + box.width/2, clientY: box.y + box.height/2 }));
        }
        value = { dispatched: true, eventIsTrusted: false, systemPointerMoved: false, outcomeRequiresInspection: true };
      }
      else if (action === 'hover') {
        const element = get(args.ref);
        element.scrollIntoView({ block: 'center' });
        const box = element.getBoundingClientRect();
        const coordinates = { clientX: box.x + box.width / 2, clientY: box.y + box.height / 2 };
        for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter']) {
          const Constructor = type.startsWith('pointer') ? PointerEvent : MouseEvent;
          element.dispatchEvent(new Constructor(type, { ...coordinates, bubbles: type.endsWith('over'),
            composed: true, pointerType: 'mouse', pointerId: 1 }));
        }
        value = { dispatched: true, eventIsTrusted: false, cssHoverGuaranteed: false };
      } else if (action === 'press_key') {
        const element = get(args.ref);
        const parts = String(args.key).split('+');
        const key = parts.pop();
        const modifiers = { ctrlKey: false, altKey: false, shiftKey: false, metaKey: false };
        for (const modifier of parts) {
          const field = ({ Control: 'ctrlKey', Alt: 'altKey', Shift: 'shiftKey', Meta: 'metaKey' })[modifier];
          if (!field) throw new Error('Unsupported key modifier');
          modifiers[field] = true;
        }
        if (!key) throw new Error('A final key is required');
        element.focus();
        for (const type of ['keydown', 'keyup']) element.dispatchEvent(new KeyboardEvent(type, {
          key, ...modifiers, bubbles: true, composed: true, cancelable: true }));
        value = { dispatched: true, eventIsTrusted: false, nativeDefaultAction: false };
      } else if (action === 'upload_begin') {
        const element = get(args.ref);
        if (element.tagName !== 'INPUT' || element.type !== 'file') throw new Error('Target must be an input[type=file] from the snapshot, including hidden inputs');
        if (element.disabled || element.webkitdirectory) throw new Error('Disabled and directory inputs are unsupported');
        if (!element.multiple && args.files.length > 1) throw new Error('Target does not support multiple files');
        staging.set(args.uploadId, { element, files: args.files, chunks: args.files.map(() => []),
          received: args.files.map(() => 0), updated: Date.now() });
        value = { staged: true };
      } else if (action === 'upload_chunk') {
        const stage = getStage(args.uploadId);
        if (!stage.files[args.fileIndex] || stage.received[args.fileIndex] !== args.offset) throw new Error('Out-of-order chunk');
        const bytes = Uint8Array.from(atob(args.base64), c => c.charCodeAt(0));
        if (bytes.length > 192 * 1024 || stage.received[args.fileIndex] + bytes.length > stage.files[args.fileIndex].size) throw new Error('Chunk exceeds declared size');
        stage.chunks[args.fileIndex].push(bytes);
        stage.received[args.fileIndex] += bytes.length;
        value = { received: stage.received[args.fileIndex] };
      } else if (action === 'upload_commit') {
        const stage = getStage(args.uploadId);
        const element = stage.element;
        if (element.tagName !== 'INPUT' || element.type !== 'file' || element.disabled || element.webkitdirectory || (!element.multiple && stage.files.length > 1)) {
          staging.delete(args.uploadId);
          throw new Error('File input properties changed during upload; inspect again');
        }
        const transfer = new DataTransfer();
        stage.files.forEach((file, index) => {
          if (stage.received[index] !== file.size) throw new Error('Incomplete file transfer');
          transfer.items.add(new File(stage.chunks[index], file.name, { type: file.type || '', lastModified: file.lastModified }));
        });
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set.call(stage.element, transfer.files);
        // Record the selected files before site handlers may replace/reset the control.
        const files = Array.from(stage.element.files).map(f => ({ name: f.name, size: f.size, type: f.type }));
        staging.delete(args.uploadId);
        event(stage.element, 'input'); event(stage.element, 'change');
        value = { assigned: true, files, eventIsTrusted: false, serverReceiptVerified: false };
      } else if (action === 'upload_abort') {
        staging.delete(args.uploadId); value = { aborted: true };
      } else throw new Error('Unsupported DOM operation');
      return { ok: true, value };
    } catch (error) { return { ok: false, error: String(error.message || error) }; }
  }
  Object.defineProperty(globalThis, '__stilldeskBrowser', { value: { run }, configurable: false });
})();
