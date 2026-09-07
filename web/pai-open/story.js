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
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const motionToggle = story.querySelector('[data-ps-motion]');
  let userMotion = null;
  let stage = null;
  let frame = 0;
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
      copy.querySelector('.ps-cta').tabIndex = selected ? 0 : -1;
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

  function paint() {
    frame = 0;
    if (!stage) return;
    const progress = Math.max(0, Math.min(1, (window.scrollY - start) / range));
    const rawPosition = progress * (steps.length - 1);
    const segment = Math.min(2, Math.floor(rawPosition));
    const fraction = rawPosition - segment;
    // Hold each step fully sharp for 70% of the scroll interval. Only the
    // middle 30% moves through the glass; the same mapping reverses on scroll up.
    const transition = Math.max(0, Math.min(1, (fraction - .35) / .30));
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
    });
    story.style.setProperty('--ps-progress', (0.1 + progress * .9).toFixed(4));
    story.style.setProperty('--ps-light-x', `${(progress * 74 - 20).toFixed(2)}px`);
    story.style.setProperty('--ps-rotate-y', `${(-12 + Math.sin(position * 1.15) * 2.2).toFixed(2)}deg`);
    story.style.setProperty('--ps-rotate-x', `${(6 + Math.sin(position * 1.4) * 1.25).toFixed(2)}deg`);
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
    paint();
  }

  function disable() {
    if (!stage) return;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    copies.forEach((copy, i) => {
      copy.inert = false;
      copy.removeAttribute('aria-hidden');
      copy.removeAttribute('style');
      copy.querySelector('.ps-cta').removeAttribute('tabindex');
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
    const wantsMotion = userMotion === null ? !reducedMotion.matches : userMotion;
    if (desktop.matches && wantsMotion) enable();
    else disable();
    if (motionToggle) {
      motionToggle.hidden = !desktop.matches;
      motionToggle.textContent = wantsMotion ? '모션 줄이기' : '스크롤 모션 켜기';
      motionToggle.setAttribute('aria-pressed', String(wantsMotion));
    }
    measure();
    schedule();
  }

  window.addEventListener('scroll', schedule, {passive: true});
  window.addEventListener('resize', syncMode, {passive: true});
  desktop.addEventListener('change', syncMode);
  reducedMotion.addEventListener('change', syncMode);
  if (motionToggle) motionToggle.addEventListener('click', () => {
    userMotion = !(userMotion === null ? !reducedMotion.matches : userMotion);
    syncMode();
  });
  window.addEventListener('load', () => {measure(); schedule();}, {once: true});
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {measure(); schedule();});
  syncMode();
})();
