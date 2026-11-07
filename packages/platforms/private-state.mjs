import { execFileSync } from 'node:child_process';
import { chmod, lstat } from 'node:fs/promises';
import { PlatformError } from './runner.mjs';

// Replace the DACL once. Removing inheritance/granting a user alone would retain
// an existing explicit Everyone ACE on a reused/shared directory.
export async function restrictPrivatePath(target, directory) {
  const info = await lstat(target);
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile()))
    throw new PlatformError('private_storage_unavailable', 'Private state must not use a linked or unexpected filesystem object');
  if (process.platform !== 'win32') {
    if (typeof process.getuid === 'function' && info.uid !== process.getuid()) throw new PlatformError('private_storage_unavailable', 'Private state is owned by a different user');
    await chmod(target, directory ? 0o700 : 0o600); return;
  }
  const script = `
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    if ($env:STILLDESK_PRIVATE_IS_DIRECTORY -eq '1') {
      $acl = New-Object System.Security.AccessControl.DirectorySecurity
      $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    } else {
      $acl = New-Object System.Security.AccessControl.FileSecurity
      $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, $system)) {
      $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', $inherit, 'None', 'Allow')
      $acl.AddAccessRule($rule)
    }
    if ($env:STILLDESK_PRIVATE_IS_DIRECTORY -eq '1') {
      [System.IO.Directory]::SetAccessControl($env:STILLDESK_PRIVATE_TARGET, $acl)
    } else {
      [System.IO.File]::SetAccessControl($env:STILLDESK_PRIVATE_TARGET, $acl)
    }
  `;
  try {
    execFileSync('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      { windowsHide: true, timeout: 15000, stdio: 'pipe', env: { ...process.env, STILLDESK_PRIVATE_TARGET: target, STILLDESK_PRIVATE_IS_DIRECTORY: directory ? '1' : '0' } });
  } catch { throw new PlatformError('private_storage_unavailable', 'Could not replace the private state permissions; the operation was not started'); }
}
