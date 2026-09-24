(() => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const panels = [...document.querySelectorAll('[data-panel]')];
  const command = document.getElementById('generated-command');
  const copyButton = document.getElementById('copy-command');
  const copyStatus = document.getElementById('copy-status');
  const hostInputs = {
    ssh: { input: document.getElementById('ssh-host'), error: document.getElementById('host-error') },
    headless: { input: document.getElementById('headless-host'), error: document.getElementById('headless-error') },
    wayland: { input: document.getElementById('wayland-host'), error: document.getElementById('wayland-error') },
  };
  let activeMode = 'local';

  // Restrict the destination to a simple OpenSSH user@host/alias token. This
  // rejects option strings and shell syntax before the value enters a command.
  function destination(value) {
    const host = value.trim();
    if (host.length > 253 || !/^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/.test(host)) return null;
    const [user, machine] = host.includes('@') ? host.split('@') : ['', host];
    if (user && (user.length > 64 || user.startsWith('-') || user.endsWith('.'))) return null;
    if (machine.startsWith('-') || machine.includes('..')) return null;
    return host;
  }

  function shellQuote(s) { return "'" + s.replace(/'/g, "'\\''") + "'"; }

  function updateCommand() {
    copyStatus.textContent = '';
    if (activeMode === 'local') {
      copyButton.disabled = false;
      command.textContent = 'tisplay';
      return;
    }
    const { input, error } = hostInputs[activeMode];
    const host = destination(input.value);
    copyButton.disabled = !host;
    error.textContent = !host ? 'Enter a host such as alice@workstation.' : '';
    if (!host) {
      command.textContent = activeMode === 'headless' ? "ssh -t HOST '~/.local/bin/tisplay --virtual'" : activeMode === 'wayland' ? "ssh -t HOST '~/.local/bin/tisplay --native-headless'" : "ssh -t HOST '~/.local/bin/tisplay'";
      return;
    }
    const executable = '~/.local/bin/tisplay';
    const remote = activeMode === 'headless' ? `${executable} --virtual` : activeMode === 'wayland' ? `${executable} --native-headless` : executable;
    // ssh passes a single remote command string to the account's shell. The
    // destination is validated above. The remote command is fixed; local
    // quoting keeps it one SSH argument, then the remote shell expands the
    // leading tilde in the installed ~/.local/bin path.
    command.textContent = `ssh -t ${shellQuote(host)} ${shellQuote(remote)}`;
  }

  function selectTab(tab, focus = false) {
    activeMode = tab.dataset.mode;
    tabs.forEach(item => {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
    });
    panels.forEach(panel => { panel.hidden = panel.dataset.panel !== activeMode; });
    if (focus) tab.focus();
    updateCommand();
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length;
      selectTab(tabs[next], true);
    });
  });
  Object.values(hostInputs).forEach(({ input }) => input.addEventListener('input', updateCommand));
  copyButton.addEventListener('click', async () => {
    const error = hostInputs[activeMode]?.error;
    if (error?.textContent) { hostInputs[activeMode].input.focus(); copyStatus.textContent = 'Fix the SSH destination before copying.'; return; }
    try {
      await navigator.clipboard.writeText(command.textContent);
      copyStatus.textContent = 'Command copied.';
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(command);
      selection.removeAllRanges();
      selection.addRange(range);
      copyStatus.textContent = 'Select the highlighted command and copy it.';
    }
  });
  updateCommand();
})();
