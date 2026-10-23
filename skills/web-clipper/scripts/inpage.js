// zkfy web-clipper — in-page preprocessing run by Playwright before the DOM is
// serialized and handed to the Obsidian Web Clipper CLI.
//
// Evaluated as:  page.evaluate(SOURCE, { phase, minPx, host, alts })
//
//   phase "images"  — make lazy/responsive <img> carry the URL the browser
//                     actually loaded (currentSrc) in their src attribute.
//   phase "prepare" — tag every large <canvas>/<svg> with data-zkclip="graph-NN"
//                     and return its metadata (+ serialized SVG text with the
//                     computed presentation styles inlined).
//   phase "replace" — swap tagged elements for sized <img> placeholders that
//                     point at the synthetic host; clip.py substitutes the
//                     captured bytes when it localizes assets.
//
// The file must stay a single function expression (Playwright evaluates it
// and calls it with the argument object).
(args) => {
  const phase = args.phase;
  const minPx = args.minPx || 48;
  const host = args.host || 'https://clip-assets.invalid/';
  // Graphs inside site chrome (navigation, sidebars, Obsidian Publish's graph view) are not content.
  const SKIP = 'a,button,nav,header,footer,aside,[role="navigation"],[role="button"],[role="complementary"],'
    + '[class*="sidebar"],[id*="sidebar"],[class*="side-column"],[class*="right-column"],[class*="left-column"],'
    + '[class*="graph-view"]';
  const SVG_STYLE_PROPS = [
    'fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-dasharray',
    'stroke-linecap', 'stroke-linejoin', 'stroke-opacity', 'opacity',
    'font-family', 'font-size', 'font-weight', 'font-style', 'text-anchor',
    'dominant-baseline', 'visibility', 'color',
  ];
  const MAX_STYLED_NODES = 4000;

  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  };
  const big = (el) => {
    const b = box(el);
    return b.w >= minPx && b.h >= minPx;
  };
  const skip = (el) => !!(el.parentElement && el.parentElement.closest(SKIP));
  const label = (el) => {
    let titleText = '';
    try {
      const t = el.querySelector(':scope > title');
      titleText = t ? t.textContent : '';
    } catch (e) { /* ignore */ }
    return (el.getAttribute('aria-label') || el.getAttribute('title') || titleText || '')
      .replace(/\s+/g, ' ').trim();
  };

  const serializeSvg = (svg) => {
    const clone = svg.cloneNode(true);
    clone.removeAttribute('data-zkclip');
    if (!clone.getAttribute('xmlns')) clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    try {
      if (clone.querySelector('[*|href]') && !clone.getAttribute('xmlns:xlink')) {
        clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
      }
    } catch (e) { /* ignore */ }
    const b = box(svg);
    if (!clone.getAttribute('width')) clone.setAttribute('width', String(b.w));
    if (!clone.getAttribute('height')) clone.setAttribute('height', String(b.h));
    if (!clone.getAttribute('viewBox') && b.w && b.h) clone.setAttribute('viewBox', `0 0 ${b.w} ${b.h}`);
    // Inline computed presentation styles so the file renders outside the page's CSS.
    const src = [svg, ...svg.querySelectorAll('*')];
    const dst = [clone, ...clone.querySelectorAll('*')];
    // The root carries every value; descendants only what differs from their parent.
    if (src.length === dst.length && src.length <= MAX_STYLED_NODES) {
      for (let i = 0; i < src.length; i++) {
        let cs, parentCs = null;
        try {
          cs = getComputedStyle(src[i]);
          if (i > 0 && src[i].parentElement) parentCs = getComputedStyle(src[i].parentElement);
        } catch (e) { continue; }
        const parts = [];
        for (const p of SVG_STYLE_PROPS) {
          const v = cs.getPropertyValue(p);
          if (v && (!parentCs || parentCs.getPropertyValue(p) !== v)) parts.push(p + ':' + v);
        }
        if (parts.length) dst[i].setAttribute('style', parts.join(';'));
      }
    }
    return new XMLSerializer().serializeToString(clone);
  };

  if (phase === 'images') {
    let fixed = 0;
    for (const img of Array.from(document.images)) {
      const cur = img.currentSrc || '';
      const srcAttr = img.getAttribute('src') || '';
      if (cur && !cur.startsWith('data:') && cur !== img.src) {
        img.setAttribute('src', cur);
        fixed++;
        continue;
      }
      if (!srcAttr || srcAttr.startsWith('data:')) {
        const lazy = img.getAttribute('data-src') || img.getAttribute('data-lazy-src')
          || img.getAttribute('data-original') || '';
        if (lazy) {
          img.setAttribute('src', lazy);
          fixed++;
        }
      }
    }
    return { fixed };
  }

  if (phase === 'prepare') {
    const items = [];
    let n = 0;
    const nextId = () => 'graph-' + String(++n).padStart(2, '0');

    for (const c of Array.from(document.querySelectorAll('canvas'))) {
      if (skip(c) || !big(c)) continue;
      const id = nextId();
      c.setAttribute('data-zkclip', id);
      const b = box(c);
      items.push({ id, kind: 'canvas', ext: 'png', width: b.w, height: b.h, alt: label(c) });
    }

    for (const s of Array.from(document.querySelectorAll('svg'))) {
      if (s.parentElement && s.parentElement.closest('svg')) continue; // nested svg
      if (skip(s) || !big(s)) continue;
      // Sprite references (<use> only) are icons, not graphs.
      if (s.querySelector('use') &&
          !s.querySelector('path,rect,circle,ellipse,line,polyline,polygon,text,image,foreignObject')) continue;
      const id = nextId();
      s.setAttribute('data-zkclip', id);
      const b = box(s);
      let data = '';
      try { data = serializeSvg(s); } catch (e) { data = ''; }
      items.push({ id, kind: 'svg', ext: 'svg', width: b.w, height: b.h, alt: label(s), data });
    }
    return items;
  }

  if (phase === 'replace') {
    let replaced = 0;
    for (const el of Array.from(document.querySelectorAll('[data-zkclip]'))) {
      const id = el.getAttribute('data-zkclip');
      const ext = el.tagName.toLowerCase() === 'canvas' ? 'png' : 'svg';
      const b = box(el);
      const img = document.createElement('img');
      img.setAttribute('src', host + id + '.' + ext);
      img.setAttribute('alt', (args.alts && args.alts[id]) || '');
      img.setAttribute('width', String(Math.max(b.w, 33)));
      img.setAttribute('height', String(Math.max(b.h, 33)));
      el.replaceWith(img);
      replaced++;
    }
    return { replaced };
  }

  return null;
}
