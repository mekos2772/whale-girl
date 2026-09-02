// Failsafe for the "borrowed" system cursor: uia.ps1 and overlay.ps1 replace
// all cursor slots with a blank cursor (SetSystemCursor) and reload the scheme
// via SPI_SETCURSORS when they finish. If either process is killed before it
// can restore, the pointer would stay invisible — this one-shot PowerShell
// reload undoes that from the JS side. SPI_SETCURSORS is idempotent, so an
// extra call after a clean restore is harmless.
import { spawn } from 'node:child_process';

let lastRestoreAt = 0;

export function restoreSystemCursor() {
  const now = Date.now();
  if (now - lastRestoreAt < 200) return; // debounce burst kills
  lastRestoreAt = now;
  try {
    const proc = spawn('powershell.exe', [
      '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
      "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public static class CuCursorRestore{[DllImport(\"user32.dll\")]public static extern bool SystemParametersInfo(uint a,uint b,IntPtr c,uint d);}' ; [void][CuCursorRestore]::SystemParametersInfo(0x0057, 0, [IntPtr]::Zero, 0)",
    ], { stdio: 'ignore', windowsHide: true });
    proc.once('error', () => { /* best-effort failsafe */ });
  } catch { /* best-effort failsafe */ }
}
