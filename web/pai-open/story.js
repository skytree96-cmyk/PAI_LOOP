/* The HTML is the complete, sequential experience. Pinning progressively enhances it. */
(() => {
  'use strict';
  const runway = document.querySelector('[data-pai-story]');
  if (!runway) return;
  const story = runway.closest('.ps-story');
  const steps = [...runway.querySelectorAll('[data-ps-step]')];
  const copies = steps.map(step => step.querySelector('.ps-copy'));
  const panels = steps.map(step => step.querySelector('.ps-panel'));
  const template = runway.querySelector('[data-ps-stage-template]');
  if (steps.length !== 4 || !template || copies.some(copy => !copy) || panels.some(panel => !panel)) return;
  const desktop = window.matchMedia('(min-width: 901px) and (min-height: 650px)');
  const motionToggle = story.querySelector('[data-ps-motion]');
  // The requested product default is motion on; the visible control can turn it off.
  let userMotion = true;
  let stage = null;
  let frame = 0;
  let renderedProgress = 0;
  let previousFrameTime = 0;
  let active = -1;
  let controls = [];
  let start = 0;
  let range = 1;
  let stageSize = 1;
  let pageLabel, callout, calloutIcon, calloutSmall, calloutTitle;

  function showActive(index) {
    if (index === active) return;
    active = index;
    copies.forEach((copy, i) => {
      const selected = i === index;
      copy.inert = !selected;
      copy.setAttribute('aria-hidden', String(!selected));
      panels[i].classList.toggle('is-active', selected);
      panels[i].setAttribute('aria-hidden', String(!selected));
      panels[i].inert = !selected;
      if (selected) controls[i].setAttribute('aria-current', 'step');
      else controls[i].removeAttribute('aria-current');
    });
    const panel = panels[index];
    pageLabel.firstChild.textContent = `${String(index + 1).padStart(2, '0')} `;
    calloutIcon.textContent = panel.dataset.calloutIcon;
    calloutSmall.textContent = panel.dataset.calloutLabel;
    calloutTitle.textContent = panel.dataset.calloutTitle;
    runway.dataset.activeStep = String(index + 1);
  }

  function measure() {
    if (!stage) return;
    const stickyTop = parseFloat(getComputedStyle(stage).top) || 96;
    stageSize = stage.getBoundingClientRect().height;
    start = runway.getBoundingClientRect().top + window.scrollY - stickyTop;
    range = Math.max(1, runway.getBoundingClientRect().height - stageSize);
  }

  function scrollProgress() {
    return Math.max(0, Math.min(1, (window.scrollY - start) / range));
  }

  function render(progress) {
    const rawPosition = progress * (steps.length - 1);
    const segment = Math.min(2, Math.floor(rawPosition));
    const fraction = rawPosition - segment;
    // A 54% reading plateau leaves room for a longer, flowing transition.
    // The same continuous mapping works in reverse, without scroll snapping.
    const transition = Math.max(0, Math.min(1, (fraction - .27) / .46));
    const position = segment + transition * transition * (3 - 2 * transition);
    showActive(Math.min(3, Math.round(position)));
    copies.forEach((copy, i) => {
      const distance = i - position;
      const absolute = Math.abs(distance);
      // Copy and its CTA travel together through the glass, in either direction.
      const opacity = Math.max(0, 1 - Math.pow(absolute * 1.3, 1.2));
      const travel = Math.min(420, stageSize * .58);
      copy.style.transform = `translate3d(0, calc(-50% + ${(distance * travel).toFixed(2)}px), 0)`;
      copy.style.opacity = opacity.toFixed(3);
      copy.style.filter = `blur(${Math.max(0, Math.min(11, (absolute - .12) * 13)).toFixed(2)}px)`;
      copy.style.visibility = opacity < .01 ? 'hidden' : 'visible';
      // A newly active copy can still be entering below the masked glass.
      // Enable its link only once the copy reaches the sharp reading plateau.
      const cta = copy.querySelector('.ps-cta');
      const canUseCta = i === active && absolute < .001;
      cta.inert = !canUseCta;
      cta.tabIndex = canUseCta ? 0 : -1;
    });
    story.style.setProperty('--ps-progress', (0.1 + progress * .9).toFixed(4));
    story.style.setProperty('--ps-light-x', `${(progress * 74 - 20).toFixed(2)}px`);
    story.style.setProperty('--ps-rotate-y', `${(-12 + Math.sin(position * 1.15) * 2.2).toFixed(2)}deg`);
    story.style.setProperty('--ps-rotate-x', `${(6 + Math.sin(position * 1.4) * 1.25).toFixed(2)}deg`);
  }

  function paint(timestamp) {
    frame = 0;
    if (!stage) return;
    const target = scrollProgress();
    const elapsed = previousFrameTime ? Math.min(64, timestamp - previousFrameTime) : 1000 / 60;
    previousFrameTime = timestamp;
    // Time-based damping stays consistent across refresh rates. Only visuals
    // follow the native scroll position; wheel, touch and page scrolling stay native.
    renderedProgress += (target - renderedProgress) * (1 - Math.exp(-elapsed / 135));
    const unsettled = Math.abs(target - renderedProgress) > .00002;
    if (!unsettled) renderedProgress = target;
    render(renderedProgress);
    if (unsettled) frame = requestAnimationFrame(paint);
    else previousFrameTime = 0;
  }

  function schedule() {
    if (stage && !frame) frame = requestAnimationFrame(paint);
  }

  function goTo(index, keyboard = false) {
    if (!stage) return;
    measure();
    window.scrollTo({top: start + range * index / (steps.length - 1), behavior: 'smooth'});
    if (keyboard) controls[index].focus({preventScroll: true});
  }

  function enable() {
    if (stage) return;
    stage = template.content.firstElementChild.cloneNode(true);
    const copyWindow = stage.querySelector('.ps-copy-window');
    const screen = stage.querySelector('.ps-device-screen');
    copies.forEach(copy => copyWindow.append(copy));
    panels.forEach(panel => screen.append(panel));
    controls = [...stage.querySelectorAll('[data-ps-go]')];
    pageLabel = stage.querySelector('.ps-glass-page');
    callout = stage.querySelector('.ps-callout');
    calloutIcon = callout.querySelector('.ps-callout-icon');
    calloutSmall = callout.querySelector('small');
    calloutTitle = callout.querySelector('strong');
    controls.forEach((button, index) => {
      button.addEventListener('click', () => goTo(index));
      button.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = Math.min(3, index + 1);
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = Math.max(0, index - 1);
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = 3;
        if (next === undefined) return;
        event.preventDefault();
        goTo(next, true);
      });
    });
    runway.append(stage);
    runway.classList.add('is-enhanced');
    active = -1;
    measure();
    renderedProgress = scrollProgress();
    previousFrameTime = 0;
    render(renderedProgress);
  }

  function disable() {
    if (!stage) return;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    previousFrameTime = 0;
    copies.forEach((copy, i) => {
      copy.inert = false;
      copy.removeAttribute('aria-hidden');
      copy.removeAttribute('style');
      const cta = copy.querySelector('.ps-cta');
      cta.inert = false;
      cta.removeAttribute('tabindex');
      panels[i].inert = false;
      panels[i].removeAttribute('aria-hidden');
      panels[i].classList.remove('is-active');
      steps[i].append(copy, panels[i]);
    });
    stage.remove();
    stage = null;
    controls = [];
    active = -1;
    runway.classList.remove('is-enhanced');
    delete runway.dataset.activeStep;
  }

  function syncMode() {
    if (desktop.matches && userMotion) enable();
    else disable();
    story.dataset.motionState = stage ? 'on' : 'off';
    if (motionToggle) {
      motionToggle.hidden = !desktop.matches;
      motionToggle.textContent = userMotion ? '모션 끄기' : '스크롤 모션 켜기';
      motionToggle.setAttribute('aria-pressed', String(userMotion));
    }
    measure();
    schedule();
  }

  window.addEventListener('scroll', schedule, {passive: true});
  window.addEventListener('resize', syncMode, {passive: true});
  desktop.addEventListener('change', syncMode);
  if (motionToggle) motionToggle.addEventListener('click', () => {
    userMotion = !userMotion;
    syncMode();
  });
  window.addEventListener('load', () => {measure(); schedule();}, {once: true});
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {measure(); schedule();});
  syncMode();
})();
