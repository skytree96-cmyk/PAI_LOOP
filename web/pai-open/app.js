const nav = document.getElementById('siteNav');
const menu = document.getElementById('menuToggle');
function closeMenu() { nav.classList.remove('is-open'); menu.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-label', '메뉴 열기'); }
menu.addEventListener('click', () => {
  const open = menu.getAttribute('aria-expanded') !== 'true';
  nav.classList.toggle('is-open', open);
  menu.setAttribute('aria-expanded', String(open));
  menu.setAttribute('aria-label', open ? '메뉴 닫기' : '메뉴 열기');
});
nav.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') { closeMenu(); menu.focus(); } });
const tabs = [...document.querySelectorAll('[data-preview]')];
function selectTab(tab, focus = false) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    document.getElementById(item.getAttribute('aria-controls')).hidden = !selected;
  });
  if (focus) tab.focus();
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) { event.preventDefault(); selectTab(tabs[next], true); }
  });
});
// Public introduction stays static: no app API requests, analysis or data writes.
const backgroundVideo = document.getElementById('heroVideo');
const motionToggle = document.getElementById('motionToggle');
if (backgroundVideo && motionToggle) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let pausedByUser = false;
  function applyMotion() {
    const paused = pausedByUser || reducedMotion.matches || document.hidden;
    motionToggle.hidden = reducedMotion.matches;
    motionToggle.setAttribute('aria-pressed', String(pausedByUser));
    motionToggle.setAttribute('aria-label', pausedByUser ? '배경 영상 재생' : '배경 영상 일시정지');
    motionToggle.textContent = pausedByUser ? '▶' : 'Ⅱ';
    if (paused) backgroundVideo.pause(); else backgroundVideo.play().catch(() => { motionToggle.hidden = true; });
  }
  motionToggle.addEventListener('click', () => { pausedByUser = !pausedByUser; applyMotion(); });
  reducedMotion.addEventListener('change', applyMotion);
  document.addEventListener('visibilitychange', applyMotion);
  backgroundVideo.addEventListener('error', () => { backgroundVideo.hidden = true; motionToggle.hidden = true; });
  applyMotion();
}
