/* Progressive enhancement for the project documentation. No network or account access. */
(() => {
  const toggle = document.querySelector('.ps-nav-toggle');
  const navigation = document.querySelector('.ps-nav-links');
  toggle?.addEventListener('click', () => {
    const expanded = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(expanded));
    navigation.classList.toggle('is-open', expanded);
  });
  navigation?.addEventListener('click', event => {
    if (event.target.closest('a')) {
      navigation.classList.remove('is-open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const text = document.getElementById(button.dataset.copy).textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = 'Copied';
      } catch {
        const range = document.createRange();
        range.selectNodeContents(document.getElementById(button.dataset.copy));
        const selection = window.getSelection();
        selection.removeAllRanges(); selection.addRange(range);
        button.textContent = 'Selected';
      }
      setTimeout(() => { button.textContent = 'Copy'; }, 1800);
    });
  });
  document.querySelectorAll('[data-show]').forEach(button => {
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-show]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      document.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== button.dataset.show; });
    });
  });
  const formation = document.getElementById('formation');
  if (formation) {
    const data = JSON.parse(document.getElementById('formation-data').textContent);
    formation.addEventListener('change', () => {
      const selected = data.find(item => item.id === formation.value);
      document.getElementById('formation-title').textContent = selected.name;
      document.getElementById('formation-purpose').textContent = selected.purpose;
      document.getElementById('formation-members').textContent = selected.members;
      document.getElementById('formation-command').textContent = selected.command;
    });
  }
  const theme = document.getElementById('theme-picker');
  if (theme) {
    const context = document.getElementById('context-picker');
    const update = () => {
      const img = document.getElementById('theme-preview');
      img.src = `assets/${theme.value}-${context.value}.svg`;
      img.alt = `${theme.value} status line rendered at ${context.value} percent context using sample session data`;
      img.closest('figure').querySelector('figcaption a').href = img.src;
      document.getElementById('theme-command').textContent = `bash theme.sh ${theme.value}`;
    };
    theme.addEventListener('change', update); context.addEventListener('change', update);
  }
  const domain = document.getElementById('whole-domain');
  if (domain) {
    const choices = [...document.querySelectorAll('.entity-choice')];
    const update = () => {
      const selected = choices.filter(item => domain.checked || item.checked).map(item => item.value);
      const list = document.getElementById('entity-result');
      list.replaceChildren(...selected.map(value => { const li = document.createElement('li'); li.textContent = value; return li; }));
      document.getElementById('entity-count').textContent = `${selected.length} of 3 sample switches${selected.length ? '' : ' explicitly selected'}`;
      document.getElementById('filter-json').textContent = selected.length ? JSON.stringify(domain.checked ? {include_domains: ['switch']} : {include_entities: selected}, null, 2) : 'No explicit include filter selected.';
      document.getElementById('filter-explanation').textContent = domain.checked ? 'The entire domain includes every supported switch, including ones added later.' : selected.length ? 'Only these named switches are explicitly included. The broad switch-domain include has been removed.' : 'An empty include filter does not guarantee an empty bridge. Inspect the full bridge configuration before applying changes.';
    };
    domain.addEventListener('change', () => { choices.forEach(item => { item.checked = false; }); update(); });
    choices.forEach(item => item.addEventListener('change', () => { domain.checked = false; update(); }));
    update();
  }
})();
