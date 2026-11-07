import { execFileSync } from 'node:child_process';
import { mkdir, readFile, rename, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { ROOT, HOST_NAME } from './prepare.mjs';
import { dataDirectory } from '../lib/transport.mjs';

function registryKeyFor(browser) {
  const prefix = { chrome: 'Google\\Chrome', chromium: 'Chromium', edge: 'Microsoft\\Edge' }[browser];
  if (!prefix) throw new Error('browser must be chrome, edge or chromium.');
  return `Software\\${prefix}\\NativeMessagingHosts\\${HOST_NAME}`;
}

function receiptPathFor(root, browser) {
  registryKeyFor(browser);
  return path.join(root, 'runtime', browser === 'chrome' ? 'native-registration.json' : `native-registration.${browser}.json`);
}

function powershell(script, data = {}) {
  if (process.platform !== 'win32') throw new Error('Native host registration in this experiment supports Windows only.');
  const encoded = Buffer.from(`$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); ${script}`, 'utf16le').toString('base64');
  const stdout = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], {
    windowsHide: true,
    encoding: 'utf8',
    env: { ...process.env, STILLDESK_BROWSER_REG_DATA: Buffer.from(JSON.stringify(data)).toString('base64') },
  });
  return stdout.trim() ? JSON.parse(stdout.replace(/^\uFEFF/, '')) : null;
}

const readKeyScript = registryKey => `
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('${registryKey}', $false)
if ($null -eq $key) {
  @{ keyExists = $false; defaultExists = $false; value = $null; valueKind = $null } | ConvertTo-Json -Compress
} else {
  try {
    $exists = @($key.GetValueNames()).Contains('')
    $value = $null; $kind = $null
    if ($exists) {
      $value = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
      $kind = $key.GetValueKind('').ToString()
    }
    @{ keyExists = $true; defaultExists = $exists; value = $value; valueKind = $kind } | ConvertTo-Json -Compress -Depth 8
  } finally { $key.Dispose() }
}`;

export function readRegistration({ browser = 'chrome' } = {}) {
  return powershell(readKeyScript(registryKeyFor(browser)));
}

function sameRegistration(left, right) {
  return left.keyExists === right.keyExists && left.defaultExists === right.defaultExists &&
    left.valueKind === right.valueKind && left.value === right.value;
}

async function readReceipt(receiptPath) {
  try { return JSON.parse(await readFile(receiptPath, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

async function writeReceipt(receiptPath, receipt) {
  await mkdir(path.dirname(receiptPath), { recursive: true });
  const temporary = `${receiptPath}.${process.pid}.tmp`;
  await writeFile(temporary, JSON.stringify(receipt, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 });
  await rename(temporary, receiptPath);
}

export async function registrationPlan({ root = ROOT, state = dataDirectory(), browser = 'chrome' } = {}) {
  if (process.platform !== 'win32') return fileRegistrationPlan({ state, browser });
  const registryKey = registryKeyFor(browser);
  const hostManifestPath = path.resolve(state, 'native', 'host-manifest.json');
  const host = JSON.parse(await readFile(hostManifestPath, 'utf8'));
  if (host.name !== HOST_NAME || !Array.isArray(host.allowed_origins) || host.allowed_origins.length !== 1 ||
      !/^chrome-extension:\/\/[a-p]{32}\/$/.test(host.allowed_origins[0])) {
    throw new Error('Native manifest must name this experimental host and exactly one valid extension origin.');
  }
  return { browser, hostName: HOST_NAME, registryKey: `HKEY_CURRENT_USER\\${registryKey}`, hostManifestPath,
    current: readRegistration({ browser }), receiptPath: receiptPathFor(state, browser) };
}

export async function applyRegistration(options = {}) {
  const plan = await registrationPlan(options);
  if (plan.targetPath) return applyFileRegistration(plan);
  const previous = await readReceipt(plan.receiptPath);
  if (previous?.active) {
    if (plan.current.defaultExists && plan.current.valueKind === 'String' && plan.current.value === previous.installedValue &&
        previous.installedValue === plan.hostManifestPath) return { ...plan, applied: true, alreadyApplied: true };
    throw new Error('An active registration receipt exists but the current registry value changed. Refusing to overwrite it.');
  }
  if (plan.current.defaultExists && !['String', 'ExpandString'].includes(plan.current.valueKind)) {
    throw new Error(`Existing native host default has unsupported type ${plan.current.valueKind}; no changes were made.`);
  }
  const receipt = { schemaVersion: 1, active: true, state: 'prepared', browser: plan.browser, hostName: HOST_NAME,
    registryKey: plan.registryKey, installedValue: plan.hostManifestPath, original: plan.current,
    appliedAt: new Date().toISOString() };
  // Persist recovery information before changing the registry.
  await writeReceipt(plan.receiptPath, receipt);
  powershell(`
$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:STILLDESK_BROWSER_REG_DATA)) | ConvertFrom-Json
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('${registryKeyFor(plan.browser)}')
try {
  $hasDefault = @($key.GetValueNames()).Contains('')
  $value = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  $kind = $null
  if ($hasDefault) { $kind = $key.GetValueKind('').ToString() }
  if ($hasDefault -ne $data.original.defaultExists -or ($hasDefault -and ($value -cne $data.original.value -or $kind -cne $data.original.valueKind))) {
    throw 'Registry value changed concurrently; registration was not applied.'
  }
  $key.SetValue('', [string]$data.path, [Microsoft.Win32.RegistryValueKind]::String)
} finally { $key.Dispose() }
@{ written = $true } | ConvertTo-Json -Compress`, { original: plan.current, path: plan.hostManifestPath });
  receipt.state = 'applied';
  await writeReceipt(plan.receiptPath, receipt);
  const actual = readRegistration({ browser: plan.browser });
  if (!actual.defaultExists || actual.valueKind !== 'String' || actual.value !== plan.hostManifestPath) throw new Error('Native host registry readback did not match.');
  return { ...plan, applied: true, alreadyApplied: false };
}

export async function rollbackRegistration({ root = ROOT, state = dataDirectory(), browser = 'chrome' } = {}) {
  if (process.platform !== 'win32') return rollbackFileRegistration({ state, browser });
  const registryKey = registryKeyFor(browser);
  const printedKey = `HKEY_CURRENT_USER\\${registryKey}`;
  const receiptPath = receiptPathFor(state, browser);
  const receipt = await readReceipt(receiptPath);
  if (!receipt?.active) return { rolledBack: false, reason: 'No active experimental registration receipt.' };
  if (receipt.hostName !== HOST_NAME || receipt.registryKey !== printedKey) throw new Error('Unexpected native host registration receipt.');
  const current = readRegistration({ browser });
  if (!current.defaultExists || current.valueKind !== 'String' || current.value !== receipt.installedValue) {
    if (receipt.state === 'prepared' && sameRegistration(current, receipt.original)) {
      receipt.active = false;
      receipt.state = 'unchanged';
      await writeReceipt(receiptPath, receipt);
      return { rolledBack: false, reason: 'Registration never changed the original value.' };
    }
    throw new Error('The native host registry value changed after installation; rollback preserved that later change.');
  }
  powershell(`
$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:STILLDESK_BROWSER_REG_DATA)) | ConvertFrom-Json
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('${registryKey}', $true)
if ($null -eq $key) { throw 'Native host registry key disappeared.' }
try {
  $value = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  if ($value -cne $data.installedValue -or $key.GetValueKind('').ToString() -cne 'String') { throw 'Native host value changed concurrently; rollback was not applied.' }
  if ($data.original.defaultExists) {
    $kind = [Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$data.original.valueKind)
    $key.SetValue('', [string]$data.original.value, $kind)
  } else {
    $key.DeleteValue('', $false)
  }
} finally { $key.Dispose() }
if (-not $data.original.keyExists) {
  $check = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('${registryKey}', $false)
  $empty = $false
  if ($null -ne $check) {
    try { $empty = ($check.ValueCount -eq 0 -and $check.SubKeyCount -eq 0) } finally { $check.Dispose() }
  }
  if ($empty) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey('${registryKey}', $false) }
}
@{ restored = $true } | ConvertTo-Json -Compress`, receipt);
  receipt.active = false;
  receipt.state = 'rolled-back';
  receipt.rolledBackAt = new Date().toISOString();
  await writeReceipt(receiptPath, receipt);
  return { rolledBack: true, browser, registryKey: printedKey, restored: readRegistration({ browser }) };
}

export function nativeHostDirectory(browser, platform = process.platform, userDirectory = os.homedir()) {
  registryKeyFor(browser);
  if (platform === 'darwin') return path.join(userDirectory, 'Library', 'Application Support',
    { chrome: 'Google/Chrome', edge: 'Microsoft Edge', chromium: 'Chromium' }[browser], 'NativeMessagingHosts');
  if (platform === 'linux') return path.join(userDirectory, '.config',
    { chrome: 'google-chrome', edge: 'microsoft-edge', chromium: 'chromium' }[browser], 'NativeMessagingHosts');
  throw new Error('File-based native registration supports macOS and Linux; Windows uses the current-user registry');
}
async function fileRegistrationPlan({ state, browser }) {
  const hostManifestPath = path.resolve(state, 'native', 'host-manifest.json');
  const text = await readFile(hostManifestPath, 'utf8');
  const host = JSON.parse(text);
  if (host.name !== HOST_NAME || host.allowed_origins?.length !== 1 || !/^chrome-extension:\/\/[a-p]{32}\/$/.test(host.allowed_origins[0])) throw new Error('Invalid native host manifest');
  const targetPath = path.join(nativeHostDirectory(browser), HOST_NAME + '.json');
  let original = null;
  try { original = (await readFile(targetPath)).toString('base64'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  return { browser, hostName: HOST_NAME, hostManifestPath, targetPath, text, original,
    receiptPath: receiptPathFor(state, browser), platform: process.platform };
}
async function applyFileRegistration(plan) {
  const previous = await readReceipt(plan.receiptPath);
  if (previous?.active) {
    if (plan.original === Buffer.from(plan.text).toString('base64') && previous.installed === plan.original) return { ...plan, applied: true, alreadyApplied: true };
    throw new Error('Active installation changed; refusing to overwrite it');
  }
  const receipt = { active: true, targetPath: plan.targetPath, original: plan.original, installed: Buffer.from(plan.text).toString('base64'), browser: plan.browser, hostName: HOST_NAME };
  await writeReceipt(plan.receiptPath, receipt);
  await mkdir(path.dirname(plan.targetPath), { recursive: true, mode: 0o700 });
  await writeFile(plan.targetPath, plan.text, { mode: 0o600 });
  return { ...plan, applied: true, alreadyApplied: false };
}
async function rollbackFileRegistration({ state, browser }) {
  const receiptPath = receiptPathFor(state, browser);
  const receipt = await readReceipt(receiptPath);
  if (!receipt?.active) return { rolledBack: false, reason: 'No active registration receipt' };
  const target = path.join(nativeHostDirectory(browser), HOST_NAME + '.json');
  if (receipt.targetPath !== target || receipt.hostName !== HOST_NAME) throw new Error('Invalid registration receipt');
  const current = (await readFile(target)).toString('base64');
  if (current !== receipt.installed) throw new Error('Native host changed after installation; preserved later change');
  if (receipt.original === null) await rm(target); else await writeFile(target, Buffer.from(receipt.original, 'base64'), { mode: 0o600 });
  receipt.active = false;
  await writeReceipt(receiptPath, receipt);
  return { rolledBack: true, browser };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const flags = process.argv.slice(2);
  const modes = flags.filter(flag => ['--apply', '--rollback'].includes(flag));
  const browsers = flags.filter(flag => flag.startsWith('--browser='));
  if (flags.some(flag => !['--apply', '--rollback', '--browser=chrome', '--browser=chromium', '--browser=edge'].includes(flag)) || modes.length > 1 || browsers.length > 1) {
    process.stderr.write('Usage: node scripts/register-native-host.mjs [--apply | --rollback] [--browser=chrome|edge|chromium]\n');
    process.exitCode = 1;
  } else {
    const operation = flags.includes('--rollback') ? rollbackRegistration : flags.includes('--apply') ? applyRegistration : registrationPlan;
    operation({ browser: browsers[0]?.split('=')[1] ?? 'chrome' }).then(result => process.stdout.write(JSON.stringify(result, null, 2) + '\n')).catch(error => {
      process.stderr.write(`Registration failed: ${error.message}\n`);
      process.exitCode = 1;
    });
  }
}
