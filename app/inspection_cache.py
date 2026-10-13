"""Persist observed DOM paths, with cheap validation before reuse on a new page."""

import hashlib
import json

from app.inspection import ELEMENT_LABEL_JS, VisualTarget, VisualTargets


DESCRIBE_JS = """el => {
  const path = node => {
    const parts = [];
    while (node?.nodeType === 1) {
      const tag = node.tagName.toLowerCase();
      const siblings = [...(node.parentElement?.children || [node])].filter(n => n.tagName === node.tagName);
      parts.unshift(tag + ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')');
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const r = el.getBoundingClientRect();
  return {path: path(el), identity: {tag: el.tagName, type: el.getAttribute('type'),
    role: el.getAttribute('role'), name: el.getAttribute('name'), id: el.id,
    placeholder: el.getAttribute('placeholder'), autocomplete: el.getAttribute('autocomplete'),
    context: [...function* () {let parent = el.parentElement; while (parent) {
      yield [parent.tagName, parent.id, parent.getAttribute('role'), parent.getAttribute('aria-label')];
      parent = parent.parentElement;
    }}()],
    label: (""" + ELEMENT_LABEL_JS + """)(el), disabled: !!el.disabled},
    box: {x: r.x, y: r.y, width: r.width, height: r.height},
    viewport: {width: innerWidth, height: innerHeight}, scroll: {x: scrollX, y: scrollY}};
}"""


def cache_key(page, target, operation, title, position, vision):
    payload = [1, page.url, target, operation, title, position, vision]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


async def capture(page, result):
    # Multiple visual candidates have not yet been disambiguated by execution.
    if isinstance(result, VisualTargets):
        return None
    visual = isinstance(result, VisualTarget)
    locator = page.locator(result.selector if visual else result)
    if await locator.count() != 1 or not await locator.is_visible():
        return None
    entry = await locator.evaluate(DESCRIBE_JS)
    # Store identity as a digest to avoid persisting visible labels or field names.
    entry['identity'] = fingerprint(entry['identity'])
    if visual:
        entry['point'] = {'x': result.x, 'y': result.y}
    return entry


def fingerprint(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


async def restore(page, entry, exact_label):
    if entry.get("page_digest") and entry["page_digest"] != hashlib.sha256(page.url.encode()).hexdigest():
        return None
    locator = page.locator(entry['path'])
    if await locator.count() != 1 or not await locator.is_visible():
        return None
    current = await locator.evaluate(DESCRIBE_JS)
    if current['identity']['disabled'] or fingerprint(current['identity']) != entry['identity']:
        return None
    if entry.get('coordinate_space') == 'document':
        def document_box(state):
            return {**state['box'], 'x': state['box']['x'] + state['scroll']['x'],
                    'y': state['box']['y'] + state['scroll']['y']}

        if current['viewport'] != entry['viewport'] or document_box(current) != entry['box']:
            return None
        await locator.scroll_into_view_if_needed(timeout=5000)
        current = await locator.evaluate(DESCRIBE_JS)
        if (fingerprint(current['identity']) != entry['identity']
                or current['viewport'] != entry['viewport']
                or document_box(current) != entry['box']):
            return None
        box = current['box']
        point = {'x': box['x'] + box['width'] / 2, 'y': box['y'] + box['height'] / 2}
        if not await locator.evaluate(
                '(el, p) => {const hit = document.elementFromPoint(p.x, p.y); return hit === el || el.contains(hit);}',
                point):
            return None
        return VisualTarget(entry['path'], **point, exact_label=exact_label)
    if 'point' in entry:
        if any(current[key] != entry[key] for key in ('box', 'viewport', 'scroll')):
            return None
        if not await locator.evaluate(
                '(el, p) => {const hit = document.elementFromPoint(p.x, p.y); return hit === el || el.contains(hit);}',
                entry['point']):
            return None
        return VisualTarget(entry['path'], **entry['point'], exact_label=exact_label)
    return entry['path']
