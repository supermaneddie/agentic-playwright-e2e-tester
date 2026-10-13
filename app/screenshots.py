"""Saved result images, including content clipped by application scroll containers."""

EXPANDED_PAGE_STYLE_JS = r"""() => {
  const path = el => {
    const parts = [];
    while (el && el.nodeType === 1) {
      const tag = el.tagName.toLowerCase();
      const peers = [...(el.parentElement?.children || [el])].filter(n => n.tagName === el.tagName);
      parts.unshift(tag + ':nth-of-type(' + (peers.indexOf(el) + 1) + ')');
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const rules = [], scrolls = [];
  for (const el of document.querySelectorAll('html, body, body *')) {
    if (!(el instanceof HTMLElement) || el.matches('input,textarea,select,[contenteditable]')) continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!r.width || !r.height || s.display === 'none' || s.visibility === 'hidden') continue;
    const root = el === document.documentElement || el === document.body;
    const clippedY = el.scrollHeight > el.clientHeight + 1 && (/auto|scroll|hidden|clip/.test(s.overflowY) || s.position === 'fixed');
    const clippedX = el.scrollWidth > el.clientWidth + 1 && (/auto|scroll|hidden|clip/.test(s.overflowX) || s.position === 'fixed');
    if (!root && !clippedY && !clippedX) continue;
    scrolls.push({selector: path(el), x: el.scrollLeft, y: el.scrollTop});
    // Preserve the measured content height so flex/grid sizing cannot shrink it again.
    rules.push(path(el) + ' { overflow: visible !important; contain: none !important;'
      + ((root || clippedY) ? ' height: auto !important; max-height: none !important; min-height: '
        + el.scrollHeight + 'px !important; flex-shrink: 0 !important;' : '')
      + (clippedX ? ' max-width: none !important; min-width: ' + el.scrollWidth + 'px !important;' : '')
      + ((s.position === 'fixed' && (clippedY || clippedX)) ? ' position: relative !important;' : '')
      + ' }');
  }
  return {style: rules.join('\n'), scrolls};
}"""


RESTORE_SCROLL_JS = """scrolls => {
  for (const item of scrolls) {
    const el = document.querySelector(item.selector);
    if (el) el.scrollTo({left: item.x, top: item.y, behavior: 'instant'});
  }
}"""


async def save_page_screenshot(page, path):
    state = await page.evaluate(EXPANDED_PAGE_STYLE_JS)
    # Playwright applies this CSS only for the capture and removes it even on failure.
    # Do not use these expanded-layout coordinates for browser actions or inspection.
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=5000,
            style=state["style"], mask=[page.locator("input, textarea, [contenteditable]")])
    finally:
        await page.evaluate(RESTORE_SCROLL_JS, state["scrolls"])
