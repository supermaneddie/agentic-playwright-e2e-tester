const assert = require('node:assert/strict');
const {test} = require('node:test');
const {patchNode, applySections, appendLogs} = require('../app/static/polling.js');

// Small DOM test double for node identity, property preservation, and keyed updates.
// No browser/network/package installation is needed for these unit tests.
const markup = new Map();
const document = {createElement: () => ({content: {}, set innerHTML(html) {
  this.content.firstElementChild = markup.get(html).cloneNode(true);
}})};
class Node {
  constructor(name, attributes = {}, children = []) {
    this.nodeName = name.toUpperCase(); this.nodeType = this.nodeName === '#TEXT' ? 3 : 1;
    this.attrs = {...attributes}; this.childNodes = []; this.parentNode = null;
    this.ownerDocument = document; this.nodeValue = attributes.text || null;
    this.value = ''; this.scrollTop = 0; this.scrollHeight = 300; this.clientHeight = 100;
    for (const child of children) this.append(typeof child === 'string' ? new Node('#text', {text: child}) : child);
  }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
  get id() { return this.attrs.id || ''; }
  get dataset() { return Object.fromEntries(Object.entries(this.attrs).filter(([k]) => k.startsWith('data-')).map(([k,v]) => [k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()),v])); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.childNodes.find(n => n.nodeType === 1) || null; }
  get nextSibling() { return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null; }
  get nextElementSibling() { let node = this.nextSibling; while (node && node.nodeType !== 1) node = node.nextSibling; return node; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  hasAttribute(name) { return name in this.attrs; }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  append(node) { this.insertBefore(node, null); }
  insertBefore(node, anchor) { node.remove(); const index = anchor ? this.childNodes.indexOf(anchor) : this.childNodes.length; this.childNodes.splice(index, 0, node); node.parentNode = this; }
  remove() { if (this.parentNode) { const children = this.parentNode.childNodes; children.splice(children.indexOf(this), 1); this.parentNode = null; } }
  replaceWith(node) { this.parentNode.insertBefore(node, this); this.remove(); }
  cloneNode(deep) { return new Node(this.nodeName, this.attrs, deep ? this.childNodes.map(n => n.cloneNode(true)) : []); }
  isEqualNode(other) { return this.nodeName === other.nodeName && this.nodeValue === other.nodeValue && JSON.stringify(this.attrs) === JSON.stringify(other.attrs) && this.childNodes.length === other.childNodes.length && this.childNodes.every((n,i) => n.isEqualNode(other.childNodes[i])); }
  querySelectorAll(selector) {
    const matches = n => selector[0] === '[' ? n.hasAttribute(selector.slice(1,-1)) : selector[0] === '#' ? n.id === selector.slice(1) : (n.attrs.class || '').split(' ').includes(selector.slice(1));
    return this.childNodes.flatMap(n => [...(n.nodeType === 1 && matches(n) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const h = (tag, attributes, ...children) => new Node(tag, attributes, children);

test('unchanged input stays attached with its typed value and container scroll', () => {
  const input = h('input', {id: 'answer'}); input.value = 'typed secret';
  const status = h('span', {}, 'running');
  const root = h('div', {}, input, status); root.scrollTop = 80;
  patchNode(root, h('div', {}, h('input', {id: 'answer'}), h('span', {}, 'waiting')));
  assert.equal(root.firstChild, input); assert.equal(input.value, 'typed secret');
  assert.equal(root.childNodes[1], status); assert.equal(status.firstChild.nodeValue, 'waiting');
  assert.equal(root.scrollTop, 80);
});

test('collapsed project remains collapsed when counts change', () => {
  const project = h('details', {'data-section': 'project:p'}, h('span', {}, '1'));
  patchNode(project, h('details', {'data-section': 'project:p', open: ''}, h('span', {}, '2')));
  assert.equal(project.hasAttribute('open'), false);
  assert.equal(project.firstChild.firstChild.nodeValue, '2');
});

test('new and deleted rows do not replace surviving keyed rows', () => {
  const row = h('tr', {'data-key': 'case:a'}, h('td', {}, 'running'));
  const deleted = h('tr', {'data-key': 'case:b'});
  const table = h('tbody', {}, row, deleted);
  patchNode(table, h('tbody', {}, h('tr', {'data-key': 'case:c'}), h('tr', {'data-key': 'case:a'}, h('td', {}, 'passed'))));
  assert.equal(table.childNodes[1], row);
  assert.equal(deleted.parentNode, null);
  assert.equal(row.firstChild.firstChild.nodeValue, 'passed');
});

test('same question form preserves typed answers and validation errors', () => {
  const input = h('input', {id: 'answer'}); input.value = 'draft';
  const form = h('form', {'data-question-signature': 'same', 'data-run-id': 'run1'}, input, h('div', {class:'error'}, 'Local error'));
  patchNode(form, h('form', {'data-question-signature':'same', 'data-run-id':'run1'}, h('input', {id:'answer'})));
  assert.equal(form.firstChild, input); assert.equal(input.value, 'draft');
  assert.equal(form.childNodes.length, 2);
});

test('a new attempt replaces its answer field instead of reusing old credentials', () => {
  const old = h('input', {id:'answer-run1'}); old.value = 'old secret';
  const form = h('form', {'data-question-signature':'same', 'data-run-id':'run1'}, old);
  patchNode(form, h('form', {'data-question-signature':'same', 'data-run-id':'run2'}, h('input', {id:'answer-run2'})));
  assert.equal(old.parentNode, null); assert.equal(form.firstChild.value, '');
});

test('section updates patch text without replacing the page or section', () => {
  const stats = h('div', {'data-section':'stats'}, h('span', {}, '1'));
  const project = h('details', {'data-section':'project:p'});
  const root = h('div', {}, stats, project);
  markup.set('stats', h('div', {'data-section':'stats'}, h('span', {}, '2')));
  applySections(root, {sections:{stats:'stats'}, order:['stats','project:p']});
  assert.equal(root.firstChild, stats); assert.equal(root.childNodes[1], project);
  assert.equal(stats.firstChild.firstChild.nodeValue, '2');
});

test('logs append only new rows while preserving the log panel and scroll', () => {
  const old = h('div', {'data-log-id':'1', 'data-kind':'progress'});
  const progress = h('div', {id:'progress-logs'}, old, h('p', {class:'log-empty'}));
  const graph = h('div', {id:'graph-logs'}, h('p', {class:'log-empty'}));
  const root = h('div', {}, progress, graph); progress.scrollTop = 20;
  markup.set('log2', h('div', {'data-log-id':'2', 'data-kind':'progress'}));
  const update = {reset:false,items:[{id:2,kind:'progress',html:'log2'}]};
  appendLogs(root, update); appendLogs(root, update);
  assert.equal(root.firstChild, progress); assert.equal(progress.firstChild, old);
  assert.equal(progress.querySelectorAll('[data-log-id]').length, 2);
  assert.equal(progress.scrollTop, 20);
});

test('expanded dependency stays open while its aggregate status changes', () => {
  const group = h('details', {'data-key': 'dependency:run:login', open: ''},
    h('summary', {}, 'Login: 1/2 steps passed'));
  const root = h('section', {}, group);
  patchNode(root, h('section', {}, h('details', {'data-key': 'dependency:run:login'},
    h('summary', {}, 'Login: 2/2 steps passed'))));
  assert.equal(root.firstChild, group);
  assert.equal(group.hasAttribute('open'), true);
  assert.equal(group.firstChild.firstChild.nodeValue, 'Login: 2/2 steps passed');
  patchNode(root, h('section', {}, h('details', {'data-key': 'dependency:retry:login'},
    h('summary', {}, 'Login: 0/2 steps passed'))));
  assert.notEqual(root.firstChild, group);
  assert.equal(root.firstChild.hasAttribute('open'), false);
});
