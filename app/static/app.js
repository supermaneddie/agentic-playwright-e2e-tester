"use strict";

function showError(container, message) {
  const target = container.querySelector('.error') || document.getElementById('page-error');
  target.textContent = message;
  target.hidden = false;
}

async function api(path, body, method = 'POST') {
  const response = await fetch(path, {
    method, headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    const message = typeof payload.detail === 'string' ? payload.detail : 'Invalid input. Check required fields and JSON values.';
    throw new Error(message);
  }
  return payload;
}

const live = document.querySelector('[data-poll-url]');
let refreshing = false;
let acting = false;
let revision = 0;
let pollState = {
  versions: {}, run_id: live?.dataset.runId || null,
  after_log_id: Math.max(0, ...[...(live?.querySelectorAll('[data-log-id]') || [])].map(el => Number(el.dataset.logId))),
};
async function refresh() {
  if (!live || refreshing || acting) return;
  refreshing = true;
  const requestedRevision = revision;
  try {
    const response = await fetch(live.dataset.pollUrl, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(pollState), cache: 'no-store',
    });
    if (!response.ok) throw new Error('Could not refresh. Retrying automatically.');
    const update = await response.json();
    if (acting || requestedRevision !== revision) return;
    TestbenchPolling.applySections(live, update);
    if (update.logs) TestbenchPolling.appendLogs(live, update.logs);
    pollState = {versions: update.versions, run_id: update.run_id || null, after_log_id: update.logs?.after_log_id || 0};
  } catch (error) {
    showError(document, error.message);
  } finally { refreshing = false; }
}
if (live) window.setInterval(refresh, Number(live.dataset.pollMs));

const dialog = document.getElementById('new-case-dialog');
const screenshotDialog = document.getElementById('screenshot-dialog');
const screenshotImage = document.getElementById('screenshot-image');
live?.addEventListener('click', event => {
  const button = event.target.closest('button[data-screenshot-url]');
  if (!button || !screenshotDialog) return;
  screenshotImage.src = button.dataset.screenshotUrl;
  screenshotImage.alt = `Screenshot for ${button.dataset.screenshotTitle}`;
  document.getElementById('screenshot-title').textContent = screenshotImage.alt;
  screenshotDialog.showModal();
});
document.getElementById('close-screenshot')?.addEventListener('click', () => screenshotDialog.close());
screenshotDialog?.addEventListener('close', () => screenshotImage.removeAttribute('src'));
document.getElementById('new-case-button')?.addEventListener('click', () => dialog.showModal());
document.getElementById('close-dialog')?.addEventListener('click', () => dialog.close());
const imageInput = document.getElementById('case-images');
const imageDropTarget = imageInput ? document.getElementById('description') : null;
let caseImages = JSON.parse(document.getElementById('case-image-data')?.textContent || '[]');
let imagesLoading = false;
const caseImageDialog = document.getElementById('case-image-dialog');
const caseImageFull = document.getElementById('case-image-full');
document.getElementById('close-case-image')?.addEventListener('click', () => caseImageDialog.close());
caseImageDialog?.addEventListener('close', () => caseImageFull.removeAttribute('src'));
function renderCaseImages() {
  const previews = document.getElementById('case-image-previews');
  if (!previews) return;
  previews.replaceChildren();
  caseImages.forEach((image, index) => {
    const card = document.createElement('div');
    const thumbnail = document.createElement('img');
    thumbnail.src = image.data_url;
    thumbnail.alt = image.name;
    const openImage = document.createElement('button');
    openImage.type = 'button';
    openImage.className = 'case-image-open';
    openImage.setAttribute('aria-label', `View ${image.name} at actual size`);
    openImage.setAttribute('aria-haspopup', 'dialog');
    openImage.append(thumbnail);
    openImage.addEventListener('click', () => {
      caseImageFull.src = image.data_url;
      caseImageFull.alt = image.name;
      document.getElementById('case-image-title').textContent = image.name || 'Reference image';
      caseImageDialog.showModal();
      const viewport = caseImageFull.parentElement;
      viewport.scrollTop = viewport.scrollLeft = 0;
    });
    const nameLabel = document.createElement('label');
    nameLabel.textContent = 'Image name';
    const name = document.createElement('input');
    name.type = 'text';
    name.value = image.name;
    name.required = true;
    name.maxLength = 255;
    name.disabled = imagesLoading;
    nameLabel.append(name);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.disabled = imagesLoading;
    remove.setAttribute('aria-label', `Remove ${image.name}`);
    remove.addEventListener('click', () => { caseImages.splice(index, 1); renderCaseImages(); });
    name.addEventListener('input', () => {
      image.name = name.value;
      thumbnail.alt = name.value;
      openImage.setAttribute('aria-label', `View ${name.value || 'image'} at actual size`);
      remove.setAttribute('aria-label', `Remove ${name.value || 'image'}`);
    });
    card.append(openImage, nameLabel, remove);
    previews.append(card);
  });
}
function readCaseImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({name: (file.name || 'Pasted image').slice(0, 255), data_url: reader.result});
    reader.onerror = () => reject(new Error(`Could not read ${file.name}. Please try again.`));
    reader.onabort = reader.onerror;
    reader.readAsDataURL(file);
  });
}
async function addCaseImages(files) {
  const form = imageInput.closest('form');
  if (imagesLoading) {
    showError(form, 'Please wait for images to finish loading, then drop or choose more images.');
    return;
  }
  if (!files.length) return;
  imagesLoading = true;
  imageInput.disabled = true;
  renderCaseImages();
  try {
    if (caseImages.length + files.length > 10) throw new Error('Attach at most 10 images.');
    for (const file of files) {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('Use PNG, JPEG or WebP images.');
      if (!file.size || file.size > 5 * 1024 * 1024) throw new Error('Each image must be nonempty and at most 5 MiB.');
    }
    const existingSize = caseImages.reduce((total, image) => total + image.data_url.split(',')[1].length * 3 / 4 - (image.data_url.match(/=/g) || []).length, 0);
    if (existingSize + files.reduce((total, file) => total + file.size, 0) > 20 * 1024 * 1024) throw new Error('Images must total at most 20 MiB.');
    caseImages.push(...await Promise.all(files.map(readCaseImage)));
    form.querySelector('.error').hidden = true;
  } catch (error) { showError(form, error.message); }
  finally {
    imagesLoading = false;
    imageInput.disabled = false;
    imageInput.value = '';
    renderCaseImages();
  }
}
imageInput?.addEventListener('change', () => addCaseImages([...imageInput.files]));
function isFileDrag(event) {
  return [...(event.dataTransfer?.types || [])].includes('Files');
}
for (const eventName of ['dragenter', 'dragover']) {
  imageDropTarget?.addEventListener(eventName, event => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = imagesLoading ? 'none' : 'copy';
    imageDropTarget.classList.toggle('image-drop-active', !imagesLoading);
  });
}
for (const eventName of ['dragleave', 'dragend']) {
  imageDropTarget?.addEventListener(eventName, () => imageDropTarget.classList.remove('image-drop-active'));
}
imageDropTarget?.addEventListener('drop', async event => {
  imageDropTarget.classList.remove('image-drop-active');
  if (!isFileDrag(event)) return;
  event.preventDefault();
  await addCaseImages([...event.dataTransfer.files]);
});
imageDropTarget?.addEventListener('paste', async event => {
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  let files = [...(clipboard.items || [])]
    .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
    .map(item => item.getAsFile()).filter(Boolean);
  if (!files.length) {
    files = [...(clipboard.files || [])].filter(file => file.type.startsWith('image/'));
  }
  if (!files.length) return;
  event.preventDefault();
  await addCaseImages(files);
});
renderCaseImages();
function formImages() {
  if (imagesLoading) throw new Error('Please wait for images to finish loading.');
  if (caseImages.some(image => !image.name.trim() || image.name.trim().length > 255)) {
    throw new Error('Give each image a name between 1 and 255 characters.');
  }
  return caseImages.map(image => ({...image, name: image.name.trim()}));
}

document.getElementById('submission-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type=submit]');
  button.disabled = true;
  try {
    const data = new FormData(form);
    const testData = formTestData(data);
    const result = await api('/api/testcases', {images: formImages(), project_id: data.get('project_id'), name: data.get('name'), test_type: data.get('test_type'), depends_on: data.getAll('depends_on'), description: data.get('description'), test_data: testData, prompt: data.get('prompt') || null});
    window.location.assign(`/testcases/${result.testcase_id}`);
  } catch (error) { showError(form, error.message); }
  finally { button.disabled = false; }
});

function closeActionDropdown(dropdown) {
  dropdown.querySelector('[data-menu-toggle]').setAttribute('aria-expanded', 'false');
  dropdown.querySelector('.action-options').hidden = true;
}

live?.addEventListener('click', event => {
  const toggle = event.target.closest('[data-menu-toggle]');
  if (!toggle || toggle.disabled || acting) return;
  const options = toggle.closest('.action-dropdown').querySelector('.action-options');
  options.hidden = !options.hidden;
  toggle.setAttribute('aria-expanded', String(!options.hidden));
});
document.addEventListener?.('click', event => {
  for (const dropdown of document.querySelectorAll('.action-dropdown')) {
    if (!dropdown.contains(event.target)) closeActionDropdown(dropdown);
  }
});
live?.addEventListener('keydown', event => {
  const dropdown = event.target.closest('.action-dropdown');
  if (event.key === 'Escape' && dropdown) {
    closeActionDropdown(dropdown);
    dropdown.querySelector('[data-menu-toggle]').focus();
  }
});

live?.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button || button.disabled || acting) return;
  const deleting = button.dataset.action === 'delete';
  if (deleting && !window.confirm('Delete this test case and all its attempts, logs, screenshots, and checkpoints? Any active run will be stopped. This cannot be undone.')) return;
  acting = true;
  revision += 1;
  button.disabled = true;
  let redirecting = false;
  const caseId = button.dataset.caseId || live.dataset.caseId;
  try {
    if (deleting) {
      await api(`/api/testcases/${caseId}`, undefined, 'DELETE');
      if (live.dataset.caseId) {
        redirecting = true;
        window.location.assign(`/projects/${live.dataset.projectId}`);
      }
    } else {
      const query = button.dataset.stepsOnly === 'true' ? '?steps_only=true' : '';
      const dropdown = button.closest('.action-dropdown');
      if (dropdown) closeActionDropdown(dropdown);
      await api(`/api/testcases/${caseId}/${button.dataset.action}${query}`);
    }
  }
  catch (error) { showError(document, error.message); }
  finally {
    if (!redirecting) { acting = false; button.disabled = false; await refresh(); }
  }
});

function clearInspection(picker) {
  picker.querySelector('[data-answer-key]').value = '';
  picker.querySelector('.inspection-image').hidden = true;
  picker.querySelector('.inspection-overlays').replaceChildren();
  picker.querySelector('.inspection-results').replaceChildren();
  picker.querySelector('[data-inspection-status]').textContent = 'Locate matches, then click a highlighted result to confirm.';
}

live?.addEventListener('input', event => {
  const picker = event.target.closest('.inspection-picker');
  if (picker && event.target.matches('[data-inspection-query], [data-inspection-mode]')) clearInspection(picker);
});
live?.addEventListener('change', event => {
  if (!event.target.matches('[data-inspection-mode]')) return;
  const picker = event.target.closest('.inspection-picker');
  clearInspection(picker);
  picker.querySelector('[data-inspection-query]').placeholder = event.target.value === 'css' ? 'e.g. button.submit' : 'Describe the element to locate';
});
live?.addEventListener('click', async event => {
  const button = event.target.closest('[data-inspection-locate]');
  if (!button || acting) return;
  const picker = button.closest('.inspection-picker');
  const form = picker.closest('form');
  const input = picker.querySelector('[data-inspection-query]');
  const mode = picker.querySelector('[data-inspection-mode]');
  const status = picker.querySelector('[data-inspection-status]');
  if (!input.value.trim()) { input.focus(); return; }
  clearInspection(picker);
  acting = true;
  revision += 1;
  button.disabled = input.disabled = mode.disabled = true;
  status.textContent = 'Locating elements…';
  try {
    const preview = await api(`/api/testcases/${live.dataset.caseId}/inspection`, {
      run_id: form.dataset.runId, key: picker.dataset.inspectionKey, mode: mode.value, query: input.value, full_page: false
    });
    const frame = picker.querySelector('.inspection-image');
    frame.querySelector('img').src = preview.screenshot;
    frame.hidden = false;
    const select = (candidate, index) => {
      picker.querySelector('[data-answer-key]').value = candidate.id;
      for (const match of picker.querySelectorAll('[data-match-id]')) {
        const selected = match.dataset.matchId === candidate.id;
        match.classList.toggle('selected', selected);
        match.setAttribute('aria-pressed', String(selected));
      }
      status.textContent = `Match ${index + 1} confirmed. Submit to resume using this selection.`;
    };
    preview.candidates.forEach((candidate, index) => {
      const overlay = document.createElement('button');
      overlay.type = 'button';
      overlay.className = 'inspection-match';
      overlay.textContent = String(index + 1);
      overlay.dataset.matchId = candidate.id;
      overlay.setAttribute('aria-label', `Confirm match ${index + 1}: ${candidate.label}`);
      overlay.setAttribute('aria-pressed', 'false');
      const box = candidate.box;
      Object.assign(overlay.style, {left: `${100 * box.x / preview.viewport.width}%`,
        top: `${100 * box.y / preview.viewport.height}%`, width: `${100 * box.width / preview.viewport.width}%`,
        height: `${100 * box.height / preview.viewport.height}%`});
      overlay.addEventListener('click', () => select(candidate, index));
      picker.querySelector('.inspection-overlays').append(overlay);
      const result = document.createElement('button');
      result.type = 'button';
      result.dataset.matchId = candidate.id;
      result.setAttribute('aria-pressed', 'false');
      result.textContent = `${index + 1}. ${candidate.label}`;
      result.addEventListener('click', () => select(candidate, index));
      picker.querySelector('.inspection-results').append(result);
    });
    status.textContent = preview.candidates.length ? `${preview.candidates.length} matches. Click a highlight or result to confirm.` : 'No visible matches. Scroll to reveal the element, then locate again.';
  } catch (error) { status.textContent = error.message; }
  finally { acting = false; button.disabled = input.disabled = mode.disabled = false; }
});

live?.addEventListener('click', async event => {
  const image = event.target.closest('.inspection-image img');
  if (image && !acting) {
    const picker = image.closest('.inspection-picker');
    const bounds = image.getBoundingClientRect();
    picker.dataset.scrollX = String(Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)));
    picker.dataset.scrollY = String(Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)));
    picker.querySelector('[data-inspection-status]').textContent = 'Scroll position selected. Use the arrow buttons to scroll this area.';
    return;
  }
  const button = event.target.closest('[data-inspection-view]');
  if (!button || acting) return;
  const picker = button.closest('.inspection-picker');
  const form = picker.closest('form');
  // Scrolling affects every question attached to this shared browser session.
  for (const other of form.querySelectorAll('.inspection-picker')) clearInspection(other);
  acting = true;
  revision += 1;
  const controls = [...form.querySelectorAll('.inspection-picker button, .inspection-picker input, .inspection-picker select')];
  controls.forEach(control => control.disabled = true);
  const status = picker.querySelector('[data-inspection-status]');
  status.textContent = 'Refreshing browser view…';
  try {
    const action = button.dataset.inspectionView;
    const preview = await api(`/api/testcases/${live.dataset.caseId}/inspection`, {
      run_id: form.dataset.runId, key: picker.dataset.inspectionKey,
      mode: picker.querySelector('[data-inspection-mode]').value,
      action: action === 'preview' ? 'preview' : 'scroll',
      direction: action === 'preview' ? 'down' : action,
      x: Number(picker.dataset.scrollX ?? 0.5), y: Number(picker.dataset.scrollY ?? 0.5),
      full_page: false
    });
    const frame = picker.querySelector('.inspection-image');
    frame.querySelector('img').src = preview.screenshot;
    frame.hidden = false;
    status.textContent = 'Current view refreshed. Scroll again, or locate the element in this view.';
  } catch (error) { status.textContent = error.message; }
  finally { acting = false; controls.forEach(control => control.disabled = false); }
});

live?.addEventListener('submit', async event => {
  if (event.target.id !== 'answer-form') return;
  event.preventDefault();
  if (acting) return;
  const form = event.target;
  const button = form.querySelector('[type=submit]');
  const answers = {};
  for (const input of form.querySelectorAll('[data-answer-key]')) {
    const text = input.value;
    if (input.closest('.inspection-picker') || (input.type === 'password' && input.dataset.answerFormat !== 'json')) {
      answers[input.dataset.answerKey] = text;
      continue;
    }
    try { answers[input.dataset.answerKey] = JSON.parse(text); }
    catch { answers[input.dataset.answerKey] = text; }
  }
  if ([...form.querySelectorAll('.inspection-picker [data-answer-key]')].some(input => !input.value)) {
    showError(form, 'Locate and confirm a result for each inspection question.');
    return;
  }
  acting = true;
  revision += 1;
  button.disabled = true;
  try {
    await api(`/api/testcases/${live.dataset.caseId}/answers`, {answers});
    form.reset();
  } catch (error) { showError(form, error.message); }
  finally { acting = false; button.disabled = false; await refresh(); }
});

const editCaseForm = document.getElementById('edit-case-form');
editCaseForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type=submit]');
  if (button.disabled) return;
  try {
    const data = new FormData(form);
    const testData = formTestData(data);
    const images = formImages();
    if (!window.confirm('Save changes? All previous results for this test case, including attempts, logs, screenshots and checkpoints, will be permanently cleared. Any active run will be stopped. This cannot be undone.')) return;
    button.disabled = true;
    const result = await api(`/api/testcases/${form.dataset.caseId}`, {
      name: data.get('name'), test_type: data.get('test_type'), depends_on: data.getAll('depends_on'), description: data.get('description'),
      images, test_data: testData, prompt: data.get('prompt') || null, confirm_clear_results: true,
    }, 'PUT');
    window.location.assign(`/testcases/${result.testcase_id}`);
  } catch (error) { showError(form, error.message); }
  finally { button.disabled = false; }
});
if (editCaseForm) {
  editCaseForm.querySelector('[type=submit]').disabled = false;
  document.getElementById('edit-loading').hidden = true;
}

function formTestData(data) {
  let value;
  try { value = JSON.parse(data.get('test_data') || '{}'); }
  catch { throw new Error('Test data must be valid JSON.'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Test data must be a JSON object.');
  return value;
}

document.getElementById('project-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[type=submit]');
  button.disabled = true;
  try {
    const data = new FormData(form);
    const id = form.dataset.projectId;
    const result = await api(id ? `/api/projects/${id}` : '/api/projects', {
      name: data.get('name'), description: data.get('description'),
      test_data: formTestData(data), prompt: data.get('prompt') || null,
    }, id ? 'PUT' : 'POST');
    window.location.assign(`/projects/${result.project_id}`);
  } catch (error) { showError(form, error.message); }
  finally { button.disabled = false; }
});

document.getElementById('delete-project')?.addEventListener('click', async event => {
  if (!window.confirm('Delete this project and all its test cases, attempts, logs, screenshots and checkpoints? Active runs will be stopped. This cannot be undone.')) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api(`/api/projects/${button.dataset.projectId}`, undefined, 'DELETE');
    window.location.assign('/');
  } catch (error) { showError(document, error.message); button.disabled = false; }
});

const savedDataForm = document.getElementById('saved-data-form');
savedDataForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = savedDataForm.querySelector('[type=submit]');
  button.disabled = true;
  savedDataForm.querySelector('.error').hidden = true;
  try {
    const answers = Object.create(null);
    const removeAnswers = [];
    for (const row of savedDataForm.querySelectorAll('.saved-answer')) {
      if (row.querySelector('[data-remove-answer]').checked) {
        removeAnswers.push(row.dataset.key);
        continue;
      }
      const input = row.querySelector('[data-saved-value]');
      if (!input || input.value === input.defaultValue) continue;
      if (input.type === 'password' && !input.value) continue;
      if (!input.value.trim()) throw new Error(`Enter a value for ${row.dataset.key}, or select Remove.`);
      if (row.dataset.format === 'json') {
        try { answers[row.dataset.key] = JSON.parse(input.value); }
        catch { throw new Error(`Enter valid JSON for ${row.dataset.key}.`); }
      } else answers[row.dataset.key] = input.value;
    }
    await api(`/api/testcases/${savedDataForm.dataset.caseId}/saved-data`, {
      revision: savedDataForm.dataset.revision, answers, remove_answers: removeAnswers,
      remove_inspections: [...savedDataForm.querySelectorAll('[data-remove-inspection]:checked')].map(input => input.dataset.removeInspection),
    }, 'PATCH');
    savedDataForm.querySelector('[data-save-status]').textContent = 'Saved. Reloading…';
    window.location.reload();
  } catch (error) {
    showError(savedDataForm, error.message);
    button.disabled = false;
  }
});
