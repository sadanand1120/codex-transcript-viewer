// Full-transcript filtering and navigation.

const allNodes = document.querySelectorAll('.tree-node');
let activeFilter = 'default';

function setFilter(filter, btn) {
  activeFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}

function filterTree(search) {
  applyFilters(search);
}

function roleVisible(classes, text) {
  if (activeFilter === 'no-tools') {
    return !classes.includes('tree-role-tool') && !classes.includes('tree-role-system');
  }
  if (activeFilter === 'user-only') return classes.includes('tree-role-user');
  if (activeFilter === 'answers') {
    return classes.includes('tree-role-user') ||
      (classes.includes('tree-role-assistant') && text.includes('\u2705'));
  }
  if (activeFilter === 'default') {
    return !classes.includes('tree-role-system') && !classes.includes('tree-role-thinking');
  }
  return true;
}

function applyFilters(search) {
  search = (search || document.getElementById('tree-search').value).toLowerCase();
  allNodes.forEach(node => {
    const id = node.getAttribute('href')?.slice(1);
    const target = id ? document.getElementById(id) : null;
    const searchable = `${node.textContent} ${target?.textContent || ''}`.toLowerCase();
    const visible = roleVisible(node.className, node.textContent) &&
      (!search || searchable.includes(search));
    node.style.display = visible ? '' : 'none';
    if (target) target.style.display = visible ? '' : 'none';
  });
}

allNodes.forEach(node => {
  node.addEventListener('click', function(e) {
    e.preventDefault();
    const id = this.getAttribute('href')?.slice(1);
    const target = id ? document.getElementById(id) : null;
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.style.outline = '2px solid var(--accent)';
      setTimeout(() => target.style.outline = '', 2000);
    }
    allNodes.forEach(n => n.classList.remove('active'));
    this.classList.add('active');
  });
});

applyFilters();
