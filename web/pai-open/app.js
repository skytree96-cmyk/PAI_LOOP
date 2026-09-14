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
// Original 3D film uses real PAI UI with explicitly labelled synthetic examples.
const productVideo = document.getElementById('heroVideo');
if (productVideo) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let resumeOnVisible = false;
  let inViewport = true;
  let autoPaused = false;
  const tryPlay = () => productVideo.play().catch(() => {});
  if (!reducedMotion.matches && !document.hidden) tryPlay();
  reducedMotion.addEventListener('change', () => { if (reducedMotion.matches) { productVideo.pause(); resumeOnVisible = false; autoPaused = false; } });
  new IntersectionObserver(entries => {
    inViewport = entries[0].isIntersecting;
    if (!inViewport && !productVideo.paused) { autoPaused = true; productVideo.pause(); }
    else if (inViewport && autoPaused && !document.hidden && !reducedMotion.matches) { autoPaused = false; tryPlay(); }
  }, { threshold: 0.15 }).observe(productVideo);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { resumeOnVisible = !productVideo.paused; productVideo.pause(); }
    else if (resumeOnVisible && inViewport && !reducedMotion.matches) tryPlay();
  });
}
