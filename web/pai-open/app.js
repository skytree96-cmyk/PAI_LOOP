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
// Keep both films available without JS; enhance them into accessible tabs.
const filmTabs = document.querySelector('.film-tabs');
const filmButtons = Array.from(document.querySelectorAll('.film-tab'));
const filmPanels = Array.from(document.querySelectorAll('[data-film-panel]'));
if (filmTabs && filmButtons.length && filmButtons.length === filmPanels.length) {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let activeIndex = 0;
  let resumeOnVisible = false;
  const activeVideo = () => filmPanels[activeIndex].querySelector('video');
  const pauseAll = () => filmPanels.forEach(panel => panel.querySelector('video').pause());
  const tryPlay = () => activeVideo().play().catch(() => {});
  const selectFilm = (index, focus = false) => {
    pauseAll();
    resumeOnVisible = false;
    activeIndex = index;
    filmButtons.forEach((button, i) => {
      const selected = i === index;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      filmPanels[i].hidden = !selected;
      filmPanels[i].setAttribute('role', 'tabpanel');
      filmPanels[i].setAttribute('aria-labelledby', button.id);
    });
    if (focus) filmButtons[index].focus();
    if (!reducedMotion.matches && !document.hidden) tryPlay();
  };
  filmButtons.forEach((button, index) => {
    button.addEventListener('click', () => selectFilm(index));
    button.addEventListener('keydown', event => {
      const targets = {ArrowRight: (index + 1) % filmButtons.length, ArrowLeft: (index + filmButtons.length - 1) % filmButtons.length, Home: 0, End: filmButtons.length - 1};
      if (!(event.key in targets)) return;
      event.preventDefault();
      selectFilm(targets[event.key], true);
    });
  });
  filmTabs.hidden = false;
  selectFilm(0);
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) { resumeOnVisible = false; pauseAll(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { resumeOnVisible = !activeVideo().paused; pauseAll(); }
    else if (resumeOnVisible && !reducedMotion.matches) { resumeOnVisible = false; tryPlay(); }
  });
}
