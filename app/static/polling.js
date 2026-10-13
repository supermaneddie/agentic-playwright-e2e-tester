/* Keep existing nodes, inputs and scroll containers alive while applying AJAX updates. */
"use strict";

(function (root) {
  const key = node => node.nodeType === 1 ? node.getAttribute('data-key') || node.id || '' : '';
  const sameKind = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName;

  function patchNode(current, incoming) {
    if (!sameKind(current, incoming)) {
      const replacement = incoming.cloneNode(true);
      current.replaceWith(replacement);
      return replacement;
    }
    if (current.nodeName === 'FORM' && current.dataset.questionSignature
        && current.dataset.questionSignature === incoming.dataset.questionSignature
        && current.dataset.runId === incoming.dataset.runId) return current;
    if (current.isEqualNode(incoming)) return current;
    if (current.nodeType !== 1) {
      if (current.nodeValue !== incoming.nodeValue) current.nodeValue = incoming.nodeValue;
      return current;
    }
    for (const attribute of [...current.attributes]) {
      if (current.nodeName === 'DETAILS' && attribute.name === 'open') continue;
      if (!incoming.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of [...incoming.attributes]) {
      if (current.nodeName === 'DETAILS' && attribute.name === 'open') continue;
      if (current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
    }
    // User-entered values are live properties, not server-owned content.
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(current.nodeName)) return current;
    const oldChildren = [...current.childNodes];
    const keyed = new Map(oldChildren.filter(node => key(node)).map(node => [key(node), node]));
    const used = new Set();
    let anchor = current.firstChild;
    for (const desired of [...incoming.childNodes]) {
      const id = key(desired);
      let node = id ? keyed.get(id) : anchor && !key(anchor) && sameKind(anchor, desired) ? anchor : null;
      if (!node) {
        node = desired.cloneNode(true);
        current.insertBefore(node, anchor);
      } else {
        used.add(node);
        if (node !== anchor) current.insertBefore(node, anchor);
        node = patchNode(node, desired);
      }
      anchor = node.nextSibling;
    }
    for (const node of oldChildren) {
      if (!used.has(node) && node.parentNode === current) node.remove();
    }
    return current;
  }

  function parseElement(html, document) {
    const template = document.createElement('template');
    template.innerHTML = html;
    return template.content.firstElementChild;
  }

  function applySections(container, update) {
    const sections = new Map([...container.querySelectorAll('[data-section]')].map(el => [el.dataset.section, el]));
    for (const [name, html] of Object.entries(update.sections)) {
      const incoming = parseElement(html, container.ownerDocument);
      if (!incoming || incoming.dataset.section !== name) throw new Error('Invalid section update.');
      const existing = sections.get(name);
      if (existing) sections.set(name, patchNode(existing, incoming));
      else { container.append(incoming); sections.set(name, incoming); }
    }
    if (update.order) {
      const keep = new Set(update.order);
      for (const [name, section] of sections) if (!keep.has(name)) section.remove();
      let anchor = container.firstElementChild;
      for (const name of update.order) {
        const section = sections.get(name);
        if (section !== anchor) container.insertBefore(section, anchor);
        anchor = section.nextElementSibling;
      }
    }
  }

  function appendLogs(container, logs) {
    for (const kind of ['progress', 'graph']) {
      const panel = container.querySelector(`#${kind}-logs`);
      const atBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 24;
      if (logs.reset) for (const line of panel.querySelectorAll('[data-log-id]')) line.remove();
      const known = new Set([...panel.querySelectorAll('[data-log-id]')].map(el => Number(el.dataset.logId)));
      for (const log of logs.items) {
        if (log.kind === kind && !known.has(log.id)) {
          panel.append(parseElement(log.html, container.ownerDocument));
          known.add(log.id);
        }
      }
      const lines = [...panel.querySelectorAll('[data-log-id]')];
      for (const line of lines.slice(0, Math.max(0, lines.length - 1000))) line.remove();
      panel.querySelector('.log-empty').hidden = lines.length > 0;
      if (atBottom) panel.scrollTop = panel.scrollHeight;
    }
  }

  const api = {patchNode, applySections, appendLogs};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TestbenchPolling = api;
})(typeof window !== 'undefined' ? window : globalThis);
