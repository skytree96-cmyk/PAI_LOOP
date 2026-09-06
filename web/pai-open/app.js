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
// Product film uses real PAI UI with explicitly labelled synthetic example data.
const productVideo = document.getElementById('heroVideo');
if (productVideo) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let resumeOnVisible = false;
  const tryPlay = () => productVideo.play().catch(() => {});
  if (!reducedMotion.matches && !document.hidden) tryPlay();
  reducedMotion.addEventListener('change', () => { if (reducedMotion.matches) productVideo.pause(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { resumeOnVisible = !productVideo.paused; productVideo.pause(); }
    else if (resumeOnVisible && !reducedMotion.matches) tryPlay();
  });
}
