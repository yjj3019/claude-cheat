// Fake codex/grok/claude CLI for tests/test_route_cli.py. Behaviour comes from mode.txt next to this file.
const fs = require('fs');
const path = require('path');
const cp = require('child_process');

const dir = __dirname;
const argv = process.argv.slice(2);
let mode = 'ok';
try { mode = fs.readFileSync(path.join(dir, 'mode.txt'), 'utf8').trim(); } catch {}
if (argv.includes('--version') && mode !== 'version_fail') {
  console.log('fake-cli 9.9.9');
  process.exit(0);
}
const arg = (flag) => { const i = argv.indexOf(flag); return i >= 0 ? argv[i + 1] : undefined; };
let stdin = '';
try { stdin = fs.readFileSync(0, 'utf8'); } catch {}

function reply(text) {
  if (argv[0] === 'exec') { if (arg('-o')) fs.writeFileSync(arg('-o'), text); }
  else if (argv.includes('--prompt-file')) process.stdout.write('\x1b[32m' + text + '\x1b[0m\n');
  else if (argv.includes('-p')) process.stdout.write(JSON.stringify({ result: text, is_error: mode === 'claude_error' }));
  else console.log(text);
}
function grandchild(detached) {
  // Both children outlive the test deadline unless owned-tree cleanup closes them.
  const child = cp.spawn(process.execPath, ['-e', `setTimeout(() => {}, 30000)`],
    { detached, stdio: 'inherit', windowsHide: true });
  fs.writeFileSync(path.join(dir, 'gpid.txt'), String(child.pid));
  if (detached) child.unref();
}

switch (mode) {
  case 'version_fail': process.exit(1);
  case 'nonzero': console.error('boom'); process.exit(1);
  case 'nonzero_auth': console.error('Not logged in'); process.exit(1);
  case 'nonzero_flag': console.error('error: unknown option'); process.exit(2);
  case 'empty': break;
  case 'wrong_reply': reply('hello'); break;
  case 'slow': setTimeout(() => {}, 30000); break;
  case 'flood':
    for (let i = 0; i < 3; i++) fs.writeSync(1, Buffer.alloc(1024 * 1024, 'x'));
    setTimeout(() => {}, 30000);
    break;
  case 'spawn_child': grandchild(true); reply('OK'); break;
  case 'tree_slow': grandchild(false); setTimeout(() => {}, 30000); break;
  case 'env_dump':
    fs.writeFileSync(path.join(dir, 'env.txt'), Object.keys(process.env).join('\n'));
    reply('OK');
    break;
  case 'argv_dump': {
    const promptFile = arg('--prompt-file');
    fs.writeFileSync(path.join(dir, 'argv.json'), JSON.stringify({
      argv, stdin, promptFile: promptFile ? fs.readFileSync(promptFile, 'utf8') : null }));
    reply('OK');
    break;
  }
  default: reply('OK');
}
