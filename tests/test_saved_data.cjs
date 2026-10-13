const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function editor(rows, removals = []) {
  let submit;
  const requests = [];
  const error = {hidden: true};
  const button = {disabled: false};
  const status = {};
  let reloaded = false;
  const form = {
    dataset: {caseId: 'case-1', revision: 'version-1'},
    addEventListener: (_, handler) => { submit = handler; },
    querySelector: selector => ({'.error': error, '[type=submit]': button, '[data-save-status]': status}[selector]),
    querySelectorAll: selector => selector === '.saved-answer' ? rows.map(row => ({
      dataset: {key: row.key, format: row.format || 'text'},
      querySelector: selector => selector === '[data-remove-answer]' ? {checked: !!row.remove} : {
        type: row.secret ? 'password' : 'textarea', value: row.value, defaultValue: row.original,
      },
    })) : removals.map(key => ({dataset: {removeInspection: key}})),
  };
  const source = fs.readFileSync(require.resolve('../app/static/app.js'), 'utf8');
  vm.runInNewContext(source.slice(source.indexOf('const savedDataForm =')), {
    document: {getElementById: () => form},
    window: {location: {reload: () => { reloaded = true; }}},
    api: async (url, body, method) => requests.push({url, body, method}),
    showError: (_, message) => { error.textContent = message; error.hidden = false; },
  });
  return {submit: () => submit({preventDefault() {}}), requests, error, button, reloaded: () => reloaded};
}

test('save only changed answers, preserve blank secrets and JSON types, remove selected entries', async () => {
  const page = editor([
    {key: 'password', secret: true, value: '', original: ''},
    {key: 'name', value: 'same', original: 'same'},
    {key: 'count', format: 'json', value: '4', original: '3'},
    {key: 'obsolete', value: 'old', original: 'old', remove: true},
    {key: '__proto__', value: 'updated', original: 'old'},
  ], ['inspection-1']);
  await page.submit();
  const request = page.requests[0];
  assert.equal(request.method, 'PATCH');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), {
    revision: 'version-1', answers: JSON.parse('{"count":4,"__proto__":"updated"}'),
    remove_answers: ['obsolete'], remove_inspections: ['inspection-1'],
  });
  assert.equal(page.reloaded(), true);
});

test('invalid JSON stays in the editor without saving', async () => {
  const page = editor([{key: 'headers', format: 'json', value: '{bad', original: '{}'}]);
  await page.submit();
  assert.equal(page.requests.length, 0);
  assert.match(page.error.textContent, /valid JSON for headers/);
  assert.equal(page.button.disabled, false);
});
