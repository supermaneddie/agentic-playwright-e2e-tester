const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function editor(confirmed, testData = '{}', failure = false) {
  const handlers = {};
  const button = {disabled: true};
  const loading = {hidden: false};
  const error = {hidden: true};
  const form = {
    dataset: {caseId: 'case-1'},
    addEventListener: (event, handler) => { handlers[event] = handler; },
    querySelector: selector => selector === '.error' ? error : button,
  };
  const requests = [], prompts = [], redirects = [];
  const context = {
    document: {
      querySelector: () => null,
      getElementById: id => id === 'edit-case-form' ? form : id === 'edit-loading' ? loading : null,
    },
    window: {
      confirm: message => { prompts.push(message); return confirmed; },
      location: {assign: url => redirects.push(url)},
    },
    FormData: class {
      getAll(key) { return key === "depends_on" ? ["login", "setup"] : []; }
      get(key) { return {name: 'Updated name', description: 'Updated', test_type: 'http', depends_on: ['login', 'setup'], test_data: testData, prompt: ''}[key]; }
    },
    fetch: async (url, options) => {
      requests.push({url, ...options});
      if (failure) return {ok: false, status: 503, json: async () => ({detail: 'Could not save changes.'})};
      return {ok: true, status: 200, json: async () => ({testcase_id: 'case-1'})};
    },
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/app.js'), 'utf8'), context);
  return {requests, prompts, redirects, error, button, loading,
    submit: () => handlers.submit({preventDefault() {}, currentTarget: form})};
}

test('save is enabled only after the edit handler is registered', () => {
  const view = editor(true);
  assert.equal(view.button.disabled, false);
  assert.equal(view.loading.hidden, true);
});

test('failed save displays an error without navigating away', async () => {
  const view = editor(true, '{}', true);
  await view.submit();
  assert.equal(view.error.hidden, false);
  assert.equal(view.error.textContent, 'Could not save changes.');
  assert.equal(view.redirects.length, 0);
  assert.equal(view.button.disabled, false);
});

test('canceling edit confirmation sends no update', async () => {
  const view = editor(false);
  await view.submit();
  assert.match(view.prompts[0], /All previous results.*permanently cleared/);
  assert.equal(view.requests.length, 0);
  assert.equal(view.redirects.length, 0);
  assert.equal(view.button.disabled, false);
});

test('confirming edit sends validated inputs and explicit confirmation', async () => {
  const view = editor(true, '{"key":"value"}');
  await view.submit();
  assert.equal(view.requests.length, 1);
  assert.equal(view.requests[0].url, '/api/testcases/case-1');
  assert.equal(view.requests[0].method, 'PUT');
  assert.deepEqual(JSON.parse(view.requests[0].body), {
    name: 'Updated name', description: 'Updated', test_type: 'http', depends_on: ['login', 'setup'], test_data: {key: 'value'},
    images: [], prompt: null, confirm_clear_results: true,
  });
  assert.deepEqual(view.redirects, ['/testcases/case-1']);
});

test('invalid JSON is rejected before confirmation or update', async () => {
  const view = editor(true, '{invalid');
  await view.submit();
  assert.equal(view.prompts.length, 0);
  assert.equal(view.requests.length, 0);
  assert.equal(view.error.hidden, false);
});
