const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function uploader(existing = []) {
  function element() {
    const classes = new Set();
    return {children: [], handlers: {}, dataset: {},
      classList: {toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
        remove: name => classes.delete(name), contains: name => classes.has(name)},
      addEventListener(event, handler) { this.handlers[event] = handler; },
      setAttribute() {}, append(...children) { this.children.push(...children); },
      replaceChildren() { this.children = []; }};
  }
  const input = element(), previews = element(), description = element(), error = {hidden: true};
  description.value = 'Keep the test description';
  input.closest = () => ({querySelector: () => error});
  const fullImage = element(), imageDialog = element(), imageTitle = element(), closeImage = element();
  fullImage.parentElement = {scrollTop: 100, scrollLeft: 100};
  fullImage.removeAttribute = name => { delete fullImage[name]; };
  imageDialog.showModal = () => { imageDialog.open = true; };
  imageDialog.close = () => { imageDialog.open = false; imageDialog.handlers.close(); };
  const nodes = {'case-image-dialog': imageDialog, 'case-image-full': fullImage,
    'case-image-title': imageTitle, 'close-case-image': closeImage, 'description': description, 'case-images': input, 'case-image-previews': previews,
    'case-image-data': {textContent: JSON.stringify(existing)}};
  const context = {document: {getElementById: id => nodes[id] || null,
    querySelector: () => null, createElement: element},
    FileReader: class {readAsDataURL(file) { this.result = file.url; this.onload(); }},
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../app/static/app.js'), 'utf8'), context);
  return {input, previews, description, error, fullImage, imageDialog, imageTitle, closeImage, images: () => JSON.parse(JSON.stringify(vm.runInNewContext('formImages()', context))),
    add: async files => { input.files = files; await input.handlers.change(); }};
}
const file = name => ({name, type: 'image/png', size: 8, url: 'data:image/png;base64,iVBORw0KGgo='});

test('multiple selections append images, preserve saved images and allow individual removal', async () => {
  const view = uploader([{name: 'saved.png', data_url: file('').url}]);
  await view.add([file('one.png'), file('two.png')]);
  await view.add([file('three.png')]);
  assert.deepEqual(view.images().map(image => image.name), ['saved.png', 'one.png', 'two.png', 'three.png']);
  assert.equal(view.previews.children.length, 4);
  view.previews.children[1].children[2].handlers.click();
  assert.deepEqual(view.images().map(image => image.name), ['saved.png', 'two.png', 'three.png']);
  assert.equal(view.input.disabled, false);
});

test('invalid uploads leave existing attachments intact', async () => {
  const view = uploader();
  await view.add([file('valid.png')]);
  for (const files of [[{...file('bad.svg'), type: 'image/svg+xml'}],
    [{...file('large.png'), size: 6 * 1024 * 1024}], Array.from({length: 10}, () => file('extra.png'))]) {
    await view.add(files);
    assert.equal(view.error.hidden, false);
    assert.deepEqual(view.images().map(image => image.name), ['valid.png']);
  }
});


test('dropping multiple images previews them and keeps the description intact', async () => {
  const view = uploader();
  let prevented = 0;
  const event = {dataTransfer: {types: ['Files'], files: [file('login.png'), file('home.png')]},
    preventDefault() { prevented++; }};
  view.description.handlers.dragover(event);
  assert.equal(event.dataTransfer.dropEffect, 'copy');
  assert.equal(view.description.classList.contains('image-drop-active'), true);
  await view.description.handlers.drop(event);
  assert.equal(prevented, 2);
  assert.equal(view.description.classList.contains('image-drop-active'), false);
  assert.equal(view.description.value, 'Keep the test description');
  assert.deepEqual(view.images().map(image => image.name), ['login.png', 'home.png']);
  assert.equal(view.previews.children[0].children[0].children[0].src, file('').url);
});

test('renaming saved and new images is preserved when adding more images', async () => {
  const view = uploader([{name: 'saved.png', data_url: file('').url}]);
  const rename = (index, value) => {
    const input = view.previews.children[index].children[1].children[0];
    input.value = value;
    input.handlers.input();
  };
  rename(0, 'Login screen');
  await view.add([file('second.png')]);
  rename(1, '  Expected dashboard  ');
  assert.deepEqual(view.images().map(image => image.name), ['Login screen', 'Expected dashboard']);
  assert.equal(view.previews.children[0].children[0].children[0].alt, 'Login screen');
  rename(1, '  ');
  assert.throws(() => view.images(), /Give each image a name/);
});

test('text drops keep normal behavior and unsupported file drops show an error', async () => {
  const view = uploader();
  const text = {dataTransfer: {types: ['text/plain']}, preventDefault() { assert.fail('Text drop prevented'); }};
  view.description.handlers.dragover(text);
  await view.description.handlers.drop(text);
  await view.description.handlers.drop({dataTransfer: {types: ['Files'],
    files: [{...file('notes.txt'), type: 'text/plain'}]}, preventDefault() {}});
  assert.equal(view.error.hidden, false);
  assert.match(view.error.textContent, /PNG, JPEG or WebP/);
  assert.deepEqual(view.images(), []);
});

test('pasting clipboard images appends previews once and preserves description text', async () => {
  const view = uploader([{name: 'saved.png', data_url: file('').url}]);
  const images = [file('clipboard.png'), file('')];
  let prevented = false;
  await view.description.handlers.paste({clipboardData: {
    items: images.map(image => ({kind: 'file', type: image.type, getAsFile: () => image})),
    files: images,
  }, preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.deepEqual(view.images().map(image => image.name), ['saved.png', 'clipboard.png', 'Pasted image']);
  assert.equal(view.previews.children.length, 3);
  assert.equal(view.description.value, 'Keep the test description');
});

test('text-only paste retains native behavior; clipboard files fallback supports images', async () => {
  const view = uploader();
  await view.description.handlers.paste({clipboardData: {
    items: [{kind: 'string', type: 'text/plain'}], files: [],
  }, preventDefault() { assert.fail('Text paste prevented'); }});
  assert.deepEqual(view.images(), []);
  await view.description.handlers.paste({clipboardData: {files: [file('fallback.png')]}, preventDefault() {}});
  assert.deepEqual(view.images().map(image => image.name), ['fallback.png']);
});

test('pasted images use the same format and size validation', async () => {
  const view = uploader();
  for (const image of [{...file('large.png'), size: 6 * 1024 * 1024},
    {...file('unsupported.gif'), type: 'image/gif'}]) {
    await view.description.handlers.paste({clipboardData: {files: [image]}, preventDefault() {}});
    assert.equal(view.error.hidden, false);
    assert.deepEqual(view.images(), []);
  }
});


test('preview opens original image with current name and closes without changing attachments', async () => {
  const view = uploader();
  await view.add([file('screen.png')]);
  const card = view.previews.children[0];
  const name = card.children[1].children[0];
  name.value = 'Login reference';
  name.handlers.input();
  const open = card.children[0];
  assert.equal(open.type, 'button');
  open.handlers.click();
  assert.equal(view.imageDialog.open, true);
  assert.equal(view.fullImage.src, file('').url);
  assert.equal(view.fullImage.alt, 'Login reference');
  assert.equal(view.imageTitle.textContent, 'Login reference');
  assert.equal(view.fullImage.parentElement.scrollTop, 0);
  assert.equal(view.fullImage.parentElement.scrollLeft, 0);
  view.closeImage.handlers.click();
  assert.equal(view.imageDialog.open, false);
  assert.equal(view.fullImage.src, undefined);
  assert.equal(view.images().length, 1);
});
