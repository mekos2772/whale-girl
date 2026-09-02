# dsh-computer-use UIA kernel (Windows, PowerShell 5.1)
# Design: one resident PowerShell process serves serialized JSON-line requests.
# Tool semantics align with OpenAI Codex Computer Use (macOS): element_index is first-class,
# screenshot pixel coordinates are the fallback, get_app_state returns screenshot + AX tree.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing

# Per-monitor DPI awareness (same as overlay.ps1). Without it GetWindowRect and
# CopyFromScreen return virtualized logical coordinates while UIA rectangles are
# physical, so on scaled displays (>100%) coordinates mix two spaces: element
# frames are garbage, clicks land off-target and the software cursor/ripple no
# longer lines up with the real click point.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuDpiFix {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
  [DllImport("user32.dll")] public static extern uint GetDpiForSystem();
}
"@
try { [void][CuDpiFix]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch {}  # PER_MONITOR_AWARE_V2
$script:SystemDpiScale = [Math]::Max(1.0, [CuDpiFix]::GetDpiForSystem() / 96.0)

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class CuNative {
  public sealed class VisibleWindow {
    public long Hwnd;
    public uint Pid;
    public string Title;
  }
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern int GetWindowTextLengthW(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public static IntPtr FindByTitle(string needle) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (IsWindowVisible(h) && GetWindowTextLengthW(h) > 0) {
        StringBuilder sb = new StringBuilder(GetWindowTextLengthW(h) + 1);
        GetWindowTextW(h, sb, sb.Capacity);
        if (sb.ToString().IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static IntPtr FindFirstWindowOfProcess(uint pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      uint p;
      if (GetWindowThreadProcessId(h, out p) == 0 || p != pid) return true;
      if (GetWindowTextLengthW(h) == 0) return true;
      found = h; return false;
    }, IntPtr.Zero);
    return found;
  }
  public static VisibleWindow[] ListVisibleWindows() {
    List<VisibleWindow> windows = new List<VisibleWindow>();
    EnumWindows((h, l) => {
      int length = GetWindowTextLengthW(h);
      if (!IsWindowVisible(h) || length <= 0) return true;
      StringBuilder sb = new StringBuilder(length + 1);
      GetWindowTextW(h, sb, sb.Capacity);
      uint pid;
      if (GetWindowThreadProcessId(h, out pid) != 0 && pid != 0) {
        windows.Add(new VisibleWindow { Hwnd = h.ToInt64(), Pid = pid, Title = sb.ToString() });
      }
      return true;
    }, IntPtr.Zero);
    return windows.ToArray();
  }
}
"@

# --- Input kernel -------------------------------------------------------------
# All pointer/keyboard mutations go through a single SendInput batch so no
# physical mouse motion can slip in between "move here" and "click".
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential)]
public struct CU_POINT { public int X, Y; }

public static class CuInput {
  public const int INPUT_MOUSE = 0;
  public const int INPUT_KEYBOARD = 1;

  [StructLayout(LayoutKind.Sequential)]
  public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)]
  public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Explicit)]
  public struct INPUTUNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
  [StructLayout(LayoutKind.Sequential)]
  public struct INPUT { public uint type; public INPUTUNION u; }

  [DllImport("user32.dll", SetLastError = true)]
  static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
  [DllImport("user32.dll")] static extern bool GetCursorPos(out CU_POINT p);
  [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool SetSystemCursor(IntPtr hcur, uint id);
  [DllImport("user32.dll")] public static extern bool SystemParametersInfo(uint uiAction, uint uiParam, IntPtr pvParam, uint fWinIni);
  [DllImport("user32.dll")] static extern int GetSystemMetrics(int nIndex);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern short VkKeyScan(char ch);

  public static CU_POINT GetPos() { CU_POINT p; GetCursorPos(out p); return p; }
  public static void SetPos(int x, int y) { SetCursorPos(x, y); }

  static void ToAbsolute(int x, int y, out int ax, out int ay) {
    int vx = GetSystemMetrics(76), vy = GetSystemMetrics(77);
    int vw = GetSystemMetrics(78), vh = GetSystemMetrics(79);
    if (vw <= 0) vw = GetSystemMetrics(0);
    if (vh <= 0) vh = GetSystemMetrics(1);
    double dx = ((double)(x - vx) * 65535.0) / (double)vw;
    double dy = ((double)(y - vy) * 65535.0) / (double)vh;
    if (dx < 0.0) dx = 0.0; if (dx > 65535.0) dx = 65535.0;
    if (dy < 0.0) dy = 0.0; if (dy > 65535.0) dy = 65535.0;
    ax = (int)Math.Round(dx); ay = (int)Math.Round(dy);
  }

  // flags/x/y/data are parallel arrays; one entry per synthetic event.
  // Entries flagged MOUSEEVENTF_ABSOLUTE (0x8000) take screen pixels, the rest are deltas.
  public static uint SendMouse(uint[] flags, int[] x, int[] y, int[] data) {
    int n = flags.Length;
    INPUT[] inputs = new INPUT[n];
    for (int i = 0; i < n; i++) {
      inputs[i].type = INPUT_MOUSE;
      inputs[i].u.mi.dwFlags = flags[i];
      inputs[i].u.mi.mouseData = (uint)data[i];
      inputs[i].u.mi.time = 0;
      inputs[i].u.mi.dwExtraInfo = IntPtr.Zero;
      if ((flags[i] & 0x8000) != 0) {
        int ax, ay; ToAbsolute(x[i], y[i], out ax, out ay);
        inputs[i].u.mi.dx = ax; inputs[i].u.mi.dy = ay;
      } else {
        inputs[i].u.mi.dx = x[i]; inputs[i].u.mi.dy = y[i];
      }
    }
    return SendInput((uint)n, inputs, Marshal.SizeOf(typeof(INPUT)));
  }

  // vk/down are parallel arrays: modifiers down, keys down+up, modifiers up.
  public static uint SendKey(ushort[] vk, bool[] down) {
    int n = vk.Length;
    INPUT[] inputs = new INPUT[n];
    for (int i = 0; i < n; i++) {
      inputs[i].type = INPUT_KEYBOARD;
      inputs[i].u.ki.wVk = vk[i];
      inputs[i].u.ki.wScan = 0;
      inputs[i].u.ki.dwFlags = down[i] ? 0u : 0x0002u;
      inputs[i].u.ki.time = 0;
      inputs[i].u.ki.dwExtraInfo = IntPtr.Zero;
    }
    return SendInput((uint)n, inputs, Marshal.SizeOf(typeof(INPUT)));
  }

  // KEYEVENTF_UNICODE (0x0004) down+up per character: delivers the literal
  // character regardless of the active keyboard layout or IME composition,
  // which would otherwise swallow bare letter keys (y -> 'y under Chinese IME).
  public static uint SendUnicode(ushort[] chars) {
    int n = chars.Length * 2;
    INPUT[] inputs = new INPUT[n];
    int i = 0;
    for (int c = 0; c < chars.Length; c++) {
      inputs[i].type = INPUT_KEYBOARD; inputs[i].u.ki.wVk = 0; inputs[i].u.ki.wScan = chars[c];
      inputs[i].u.ki.dwFlags = 0x0004u; inputs[i].u.ki.time = 0; inputs[i].u.ki.dwExtraInfo = IntPtr.Zero; i++;
      inputs[i].type = INPUT_KEYBOARD; inputs[i].u.ki.wVk = 0; inputs[i].u.ki.wScan = chars[c];
      inputs[i].u.ki.dwFlags = 0x0004u | 0x0002u; inputs[i].u.ki.time = 0; inputs[i].u.ki.dwExtraInfo = IntPtr.Zero; i++;
    }
    return SendInput((uint)n, inputs, Marshal.SizeOf(typeof(INPUT)));
  }

  public static ushort VkFromChar(char ch) {
    short r = VkKeyScan(ch);
    if (r == -1) return 0;
    return (ushort)(r & 0x00FF);
  }

  public static bool CharNeedsShift(char ch) {
    short r = VkKeyScan(ch);
    if (r == -1) return false;
    return (r & 0x0100) != 0;
  }
}
"@

$MOUSEEVENTF_MOVE = 0x0001
$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP = 0x0004
$MOUSEEVENTF_RIGHTDOWN = 0x0008
$MOUSEEVENTF_RIGHTUP = 0x0010
$MOUSEEVENTF_MIDDLEDOWN = 0x0020
$MOUSEEVENTF_MIDDLEUP = 0x0040
$MOUSEEVENTF_WHEEL = 0x0800
$MOUSEEVENTF_HWHEEL = 0x1000
$MOUSEEVENTF_ABSOLUTE = 0x8000
$MOUSEEVENTF_VIRTUALDESK = 0x4000
# move to an absolute screen point: MOVE | ABSOLUTE | VIRTUALDESK (multi-monitor safe)
$MOVE_ABS = $MOUSEEVENTF_MOVE -bor $MOUSEEVENTF_ABSOLUTE -bor $MOUSEEVENTF_VIRTUALDESK

function Read-Input {
  $raw = [Console]::In.ReadToEnd()
  if (-not $raw) { return [pscustomobject]@{} }
  return ($raw | ConvertFrom-Json)
}

function Out-Json($obj) {
  $json = $obj | ConvertTo-Json -Depth 30 -Compress
  [Console]::Out.Write($json)
}

function Fail($msg) {
  # Throwing (instead of exit) lets the serve loop answer the failing request
  # and keep the kernel alive; the one-shot dispatcher turns it into exit 1.
  throw [System.InvalidOperationException]::new($msg)
}

function Get-WindowRectArr($hwnd) {
  $r = New-Object CuNative+RECT
  if (-not [CuNative]::GetWindowRect($hwnd, [ref]$r)) { return $null }
  return @{ x = $r.Left; y = $r.Top; width = ($r.Right - $r.Left); height = ($r.Bottom - $r.Top) }
}

function Get-ProcessInfo($hwnd) {
  $procId = [uint32]0
  [void][CuNative]::GetWindowThreadProcessId($hwnd, [ref]$procId)
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  return @{
    pid = [int]$procId
    processName = if ($p) { $p.ProcessName } else { '' }
    path = if ($p -and $p.Path) { $p.Path } else { '' }
  }
}

function Resolve-Window($target) {
  if ($null -ne $target.hwnd -and $target.hwnd -ne 0) {
    $hwnd = [IntPtr]$target.hwnd
    if ([CuNative]::IsWindowVisible($hwnd)) { return $hwnd }
    return $null
  }
  if ($null -ne $target.pid -and $target.pid -ne 0) {
    $proc = Get-Process -Id $target.pid -ErrorAction SilentlyContinue
    if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) { return $proc.MainWindowHandle }
    return $null
  }
  $app = [string]$target.app
  if ([string]::IsNullOrWhiteSpace($app)) {
    $fg = [CuNative]::GetForegroundWindow()
    if ($fg -ne [IntPtr]::Zero) { return $fg }
    return $null
  }
  if ($app -match '^pid:(\d+)$') {
    $proc = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
    if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) { return $proc.MainWindowHandle }
    return $null
  }
  if ($app -match '^hwnd:(\d+)$') {
    $candidate = [IntPtr]([int64]$Matches[1])
    if ([CuNative]::IsWindowVisible($candidate)) { return $candidate }
    return $null
  }
  if ($app -match '^\d+$') {
    # Bare numbers keep the historic PID-first behaviour, then fall back to a
    # native HWND. Explicit pid:/hwnd: selectors remove any ambiguity.
    $pidValue = 0
    if ([int]::TryParse($app, [ref]$pidValue)) {
      $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
      if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) { return $proc.MainWindowHandle }
    }
    $candidate = [IntPtr]([int64]$app)
    if ([CuNative]::IsWindowVisible($candidate)) { return $candidate }
  }
  $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowHandle -ne [IntPtr]::Zero -and (
      $_.ProcessName -eq $app -or $_.ProcessName -eq ($app -replace '\.exe$','')
    )
  }
  if ($procs) { return $procs[0].MainWindowHandle }
  # Window-title match. Get-Process is tried first: EnumWindows can be blocked
  # in sandboxes/jobs, while MainWindowTitle comes from the process table.
  $byTitle = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowHandle -ne [IntPtr]::Zero -and
    $_.MainWindowTitle.IndexOf($app, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
  } | Sort-Object { $_.MainWindowTitle.Length }
  if ($byTitle) { return $byTitle[0].MainWindowHandle }
  $found = [CuNative]::FindByTitle($app)
  if ($found -ne [IntPtr]::Zero) { return $found }
  return $null
}

function Resolve-ElementByPath($root, $path) {
  $current = $root
  foreach ($seg in $path) {
    $children = $current.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    if ($children.Count -le $seg) { return $null }
    $current = $children[$seg]
  }
  return $current
}

function Get-SecondaryActions($element) {
  $names = @()
  try {
    $names = @($element.GetSupportedPatterns() | ForEach-Object {
      $_.ProgrammaticName -replace 'PatternIdentifiers\.|Pattern$',''
    } | Where-Object { $_ -in @('Invoke','Toggle','ExpandCollapse','SelectionItem','ScrollItem','Value','Text','RangeValue') })
  } catch {}
  return $names
}

function Invoke-SecondaryAction($element, $action) {
  $action = $action -replace '\.Pattern$',''
  try {
    switch ($action) {
      'Invoke' {
        $p = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $p.Invoke(); return @{ ok = $true; via = 'InvokePattern' }
      }
      'Toggle' {
        $p = $element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        $p.Toggle(); return @{ ok = $true; via = 'TogglePattern' }
      }
      'ExpandCollapse' {
        $p = $element.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($p.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded) {
          $p.Collapse()
        } else { $p.Expand() }
        return @{ ok = $true; via = 'ExpandCollapsePattern' }
      }
      'SelectionItem' {
        $p = $element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
        $p.Select(); return @{ ok = $true; via = 'SelectionItemPattern' }
      }
      'ScrollItem' {
        $p = $element.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern)
        $p.ScrollIntoView(); return @{ ok = $true; via = 'ScrollItemPattern' }
      }
      'Value' {
        return @{ ok = $false; error = 'value pattern needs a value; use set_value' }
      }
      'Text' {
        return @{ ok = $false; error = 'text pattern is not a secondary action; use select_text' }
      }
      default {
        return @{ ok = $false; error = "unsupported secondary action: $action" }
      }
    }
  } catch {
    return @{ ok = $false; error = "secondary action $action failed: $($_.Exception.Message)" }
  }
}

function Get-ButtonFlags($button) {
  if ($button -eq 'right') { return @{ down = $MOUSEEVENTF_RIGHTDOWN; up = $MOUSEEVENTF_RIGHTUP } }
  if ($button -eq 'middle') { return @{ down = $MOUSEEVENTF_MIDDLEDOWN; up = $MOUSEEVENTF_MIDDLEUP } }
  return @{ down = $MOUSEEVENTF_LEFTDOWN; up = $MOUSEEVENTF_LEFTUP }
}

# Borrow the system cursor for the duration of $body instead of taking it over:
# all cursor slots are replaced with a blank cursor (SetSystemCursor, global and
# focus-independent, unlike ShowCursor which is per input queue) and the scheme
# is reloaded from the registry afterwards. The real pointer is also put back
# exactly where the user left it, so an action never displaces the mouse.
$script:BorrowCursor = $true
# When the desktop overlay animation is running it owns cursor hiding for the
# whole motion; the kernel then only borrows the position. Otherwise the
# kernel hides the pointer itself around the real input.
$script:HideCursorInBorrow = $true
$CURSOR_SLOT_IDS = @(32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651)

function Hide-SystemCursor {
  foreach ($id in $CURSOR_SLOT_IDS) {
    $bmp = New-Object System.Drawing.Bitmap(1, 1)
    try { $hIcon = $bmp.GetHicon() } finally { $bmp.Dispose() }
    [void][CuInput]::SetSystemCursor($hIcon, [uint32]$id)   # the system consumes the handle
  }
}

function Restore-SystemCursor {
  [void][CuInput]::SystemParametersInfo(0x0057, 0, [IntPtr]::Zero, 0)   # SPI_SETCURSORS reloads the scheme
}

function Invoke-WithBorrowedCursor([scriptblock]$body) {
  if (-not $script:BorrowCursor) {
    if ($env:DSH_CU_DEBUG) { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-debug.log'), "borrow=off`n") }
    return & $body
  }
  $saved = [CuInput]::GetPos()
  if ($script:HideCursorInBorrow) { Hide-SystemCursor }
  $dbg = $env:DSH_CU_DEBUG
  if ($dbg) { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-debug.log'), "saved=$($saved.X),$($saved.Y) hide=$([bool]$script:HideCursorInBorrow)`n") }
  try {
    return & $body
  } finally {
    # Synthetic input is processed asynchronously, and a press that is still
    # pending keeps the cursor captured. Drain first, then put the pointer back
    # and confirm it stuck before reloading the cursor scheme.
    Start-Sleep -Milliseconds 70
    $ok = $false
    for ($attempt = 0; $attempt -lt 4; $attempt++) {
      try { [CuInput]::SetPos($saved.X, $saved.Y) } catch {
        if ($dbg) { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-debug.log'), "restore-threw=$($_.Exception.Message)`n") }
        break
      }
      Start-Sleep -Milliseconds 30
      $now = [CuInput]::GetPos()
      if ($now.X -eq $saved.X -and $now.Y -eq $saved.Y) { $ok = $true; break }
    }
    if ($script:HideCursorInBorrow) { Restore-SystemCursor }
    if ($dbg) {
      $final = [CuInput]::GetPos()
      [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-debug.log'), "after=$($final.X),$($final.Y) restored=$ok`n")
    }
  }
}

function Invoke-ClickAt($x, $y, $button, $count) {
  $flags = Get-ButtonFlags $button
  Invoke-WithBorrowedCursor {
    for ($i = 0; $i -lt $count; $i++) {
      # move + down + up in one atomic batch: physical motion cannot land between them
      [void][CuInput]::SendMouse(
        [uint32[]]@($MOVE_ABS, $flags.down, $flags.up),
        [int[]]@($x, $x, $x),
        [int[]]@($y, $y, $y),
        [int[]]@(0, 0, 0))
      if ($i -lt ($count - 1)) { Start-Sleep -Milliseconds 60 }
    }
    Start-Sleep -Milliseconds 40
  }
}

function Invoke-Drag($fromX, $fromY, $toX, $toY, $button) {
  $flags = Get-ButtonFlags $button
  Invoke-WithBorrowedCursor {
    [void][CuInput]::SendMouse(
      [uint32[]]@($MOVE_ABS, $flags.down),
      [int[]]@($fromX, $fromX),
      [int[]]@($fromY, $fromY),
      [int[]]@(0, 0))
    Start-Sleep -Milliseconds 60
    $steps = 12
    for ($i = 1; $i -le $steps; $i++) {
      $ix = [int]($fromX + ($toX - $fromX) * $i / $steps)
      $iy = [int]($fromY + ($toY - $fromY) * $i / $steps)
      [void][CuInput]::SendMouse([uint32[]]@($MOVE_ABS), [int[]]@($ix), [int[]]@($iy), [int[]]@(0))
      Start-Sleep -Milliseconds 16
    }
    Start-Sleep -Milliseconds 40
    [void][CuInput]::SendMouse([uint32[]]@($flags.up), [int[]]@($toX), [int[]]@($toY), [int[]]@(0))
  }
}

function Invoke-Scroll($x, $y, $direction, $amount) {
  # $amount is a wheel-notch count (one notch = WHEEL_DELTA 120 = ~3 lines).
  $notches = [Math]::Max(1, [int][Math]::Round([double]$amount))
  $horizontal = ($direction -eq 'left' -or $direction -eq 'right')
  # WM_HSCROLL convention: positive wheel delta tilts right;
  # vertical: positive delta scrolls up, so down must be negative.
  $dir = 1
  if ($direction -eq 'left' -or $direction -eq 'down') { $dir = -1 }
  $wheel = if ($horizontal) { $MOUSEEVENTF_HWHEEL } else { $MOUSEEVENTF_WHEEL }
  Invoke-WithBorrowedCursor {
    # Windows clamps huge single deltas; send one event per notch instead so
    # apps accumulate them into a real page-length scroll.
    for ($i = 0; $i -lt $notches; $i++) {
      [void][CuInput]::SendMouse(
        [uint32[]]@($MOVE_ABS, $wheel),
        [int[]]@($x, $x),
        [int[]]@($y, $y),
        [int[]]@(0, [int](120 * $dir)))
      Start-Sleep -Milliseconds 12
    }
  }
}

# --- xdotool-style key names -> virtual key codes -----------------------------
# Replaced the old SendKeys string builder: it appended '' for every modifier
# and then called ''.Substring(0,1) on it, which threw for every chord.
$VK_SHIFT = 0x10
$VK_CONTROL = 0x11
$VK_MENU = 0x12

function Get-ModifierVk($name) {
  switch -Regex ($name) {
    '^(super|cmd|ctrl|control)$' { return $VK_CONTROL }
    '^(alt|option)$' { return $VK_MENU }
    '^shift$' { return $VK_SHIFT }
    '^(win|meta|superkey)$' { return 0x5B }   # VK_LWIN
    default { return 0 }
  }
}

function Get-NamedVk($name) {
  $k = $name.ToLowerInvariant() -replace '[\s_\-]', ''
  switch ($k) {
    'return' { return 0x0D }
    'enter' { return 0x0D }
    'kpenter' { return 0x0D }
    'tab' { return 0x09 }
    'escape' { return 0x1B }
    'esc' { return 0x1B }
    'space' { return 0x20 }
    'backspace' { return 0x08 }
    'back' { return 0x08 }
    'delete' { return 0x2E }
    'del' { return 0x2E }
    'insert' { return 0x2D }
    'ins' { return 0x2D }
    'up' { return 0x26 }
    'down' { return 0x28 }
    'left' { return 0x25 }
    'right' { return 0x27 }
    'home' { return 0x24 }
    'end' { return 0x23 }
    'pageup' { return 0x21 }
    'prior' { return 0x21 }
    'pagedown' { return 0x22 }
    'next' { return 0x22 }
    'printscreen' { return 0x2C }
    'print' { return 0x2C }
    'scrolllock' { return 0x91 }
    'pause' { return 0x13 }
    'numlock' { return 0x90 }
    'capslock' { return 0x14 }
    'kpadd' { return 0x6B }
    'kpsubtract' { return 0x6D }
    'kpmultiply' { return 0x6A }
    'kpdivide' { return 0x6F }
    'kpdecimal' { return 0x6E }
    'kpseparator' { return 0x6C }
    'apps' { return 0x5D }
    'menu' { return 0x5D }
    default { break }
  }
  if ($k -match '^f([1-9]|1[0-9]|2[0-4])$') { return 0x6F + [int]$Matches[1] }
  if ($k -match '^kp([0-9])$') { return 0x60 + [int]$Matches[1] }
  return 0
}

# Turn ['ctrl','shift','s'] into the vk/down arrays for one SendInput batch.
function Convert-KeyChord($keys) {
  $mods = @()
  $keysOut = @()
  foreach ($raw in $keys) {
    $token = [string]$raw
    if ([string]::IsNullOrWhiteSpace($token)) { continue }   # 'ctrl++' -> empty token
    $norm = $token.ToLowerInvariant() -replace '[\s_\-]', ''
    $mod = Get-ModifierVk $norm
    if ($mod -ne 0) { $mods += $mod; continue }

    $vk = Get-NamedVk $norm
    if ($vk -ne 0) { $keysOut += @{ vk = $vk; shift = $false }; continue }

    if ($token.Length -eq 1) {
      $ch = $token[0]
      $vk = [CuInput]::VkFromChar($ch)
      if ($vk -eq 0) { Fail "press_key: unsupported key '$token'" }
      $keysOut += @{ vk = $vk; shift = [CuInput]::CharNeedsShift($ch); ch = $token }
      continue
    }
    Fail "press_key: unknown key '$token' (use xdotool names: Return, Page_Up, KP_0, F5)"
  }
  if ($keysOut.Count -eq 0) {
    # A chord of modifiers only ("win", "ctrl") presses the modifier itself.
    if ($mods.Count -eq 0) { Fail 'press_key: no key to press' }
    $keysOut += @{ vk = $mods[$mods.Count - 1]; shift = $false }
    if ($mods.Count -gt 1) { $mods = @($mods[0..($mods.Count - 2)]) } else { $mods = @() }
  }

  $hasShift = ($mods -contains $VK_SHIFT)
  # A bare printable-char chord (no modifiers) is delivered as literal Unicode
  # characters so an active IME cannot intercept it.
  $unicodeChars = $null
  if ($mods.Count -eq 0 -and $keysOut.Count -gt 0) {
    $chars = @()
    $allChars = $true
    foreach ($k in $keysOut) {
      if ($null -ne $k.ch) { $chars += [uint16][char]($k.ch[0]) } else { $allChars = $false; break }
    }
    if ($allChars) { $unicodeChars = $chars }
  }
  $vks = @()
  $downs = @()
  foreach ($m in $mods) { $vks += [uint16]$m; $downs += $true }
  foreach ($k in $keysOut) {
    if ($k.shift -and -not $hasShift) { $vks += [uint16]$VK_SHIFT; $downs += $true }
    $vks += [uint16]$k.vk; $downs += $true
    $vks += [uint16]$k.vk; $downs += $false
    if ($k.shift -and -not $hasShift) { $vks += [uint16]$VK_SHIFT; $downs += $false }
  }
  for ($i = $mods.Count - 1; $i -ge 0; $i--) { $vks += [uint16]$mods[$i]; $downs += $false }
  return @{ vks = ([uint16[]]$vks); downs = ([bool[]]$downs); unicodeChars = $unicodeChars }
}

# Keep clipboard, paste text, restore clipboard.
function Invoke-TypeText($text) {
  $old = Get-Clipboard -Raw -ErrorAction SilentlyContinue
  $ok = $false
  # Set-Clipboard can silently lose the race against clipboard monitors/locked
  # boards; verify the board really holds our text before pasting, or the user's
  # previous clipboard content gets typed into the app instead.
  for ($attempt = 0; $attempt -lt 3 -and -not $ok; $attempt++) {
    Set-Clipboard -Value $text -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 50
    $ok = ((Get-Clipboard -Raw -ErrorAction SilentlyContinue) -eq $text)
  }
  if (-not $ok) {
    return @{ ok = $false; error = 'type_text: clipboard did not accept the text (locked or monitored); nothing was typed' }
  }
  [System.Windows.Forms.SendKeys]::SendWait('^v')
  Start-Sleep -Milliseconds 120
  try { Set-Clipboard -Value $old } catch {}
  return $null
}

function Capture-WindowPng($hwnd, $rect) {
  $bmp = New-Object System.Drawing.Bitmap($rect.width, $rect.height)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($rect.x, $rect.y, 0, 0, $bmp.Size)
  $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  return [Convert]::ToBase64String($ms.ToArray())
}

# --- Codex-style effects overlaid onto a screenshot (model-facing view) ---
# fx: { cursor:{x,y}, lens:{x,y,frame}, fog:{x,y,pulse}, trail:[{x,y}...] }, coordinates in screenshot px.
# cursor/lens/fog center on the tip point (x,y).

# Official cursor pointer contour (rows: y, minX, maxX in a 10..38 x 10..39 source space),
# reverse-engineered from the official macOS software-cursor renderer.
$POINTER_CONTOUR = @(
  @(39, 17, 21), @(38, 16, 22), @(37, 15, 22), @(36, 15, 23), @(35, 15, 24),
  @(34, 15, 24), @(33, 14, 25), @(32, 14, 25), @(31, 14, 26), @(30, 14, 27),
  @(29, 13, 29), @(28, 13, 31), @(27, 13, 34), @(26, 13, 36), @(25, 13, 37),
  @(24, 12, 37), @(23, 12, 37), @(22, 12, 37), @(21, 12, 37), @(20, 12, 36),
  @(19, 11, 36), @(18, 11, 34), @(17, 11, 32), @(16, 11, 30), @(15, 10, 27),
  @(14, 10, 25), @(13, 10, 23), @(12, 11, 21), @(11, 11, 19), @(10, 13, 16)
)

function Get-PointerPointsFx([double]$tipX, [double]$tipY, [double]$scale) {
  # Same anchoring as overlay.ps1: the contour's TOP-LEFT tip (y=10 row) sits
  # on the tip point; the body hangs down-right like a normal arrow cursor.
  $size = 21.0 * $scale
  $srcMinX = 10.0; $srcMaxX = 38.0; $srcMinY = 10.0; $srcMaxY = 39.0
  $rectLeft = $tipX - (14.5 - $srcMinX) / ($srcMaxX - $srcMinX) * $size
  $rectTop = $tipY
  $pts = New-Object System.Collections.ArrayList
  foreach ($row in $POINTER_CONTOUR) {
    $mx = ($row[1] - $srcMinX) / ($srcMaxX - $srcMinX) * $size + $rectLeft
    $my = ($row[0] - $srcMinY) / ($srcMaxY - $srcMinY) * $size + $rectTop
    [void]$pts.Add((New-Object System.Drawing.PointF([single]$mx, [single]$my)))
  }
  for ($i = $POINTER_CONTOUR.Count - 1; $i -ge 0; $i--) {
    $row = $POINTER_CONTOUR[$i]
    $mx = ($row[2] - $srcMinX) / ($srcMaxX - $srcMinX) * $size + $rectLeft
    $my = ($row[0] - $srcMinY) / ($srcMaxY - $srcMinY) * $size + $rectTop
    [void]$pts.Add((New-Object System.Drawing.PointF([single]$mx, [single]$my)))
  }
  return @($pts)
}

function Add-FxOverlay($bitmap, $fx) {
  if (-not $fx) { return $bitmap }
  $g = [System.Drawing.Graphics]::FromImage($bitmap)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias

  $assetsRoot = Join-Path $PSScriptRoot '..\assets'
  $lensDir = Join-Path $assetsRoot 'lens'

  # screenshot may be downscaled by v1 attachment normalization; scale fx coords to bitmap space
  $scaleX = 1.0; $scaleY = 1.0
  if ($fx.screenshotWidth -and $fx.screenshotHeight -and $bitmap.Width -gt 0) {
    $scaleX = [double]$bitmap.Width / [double]$fx.screenshotWidth
    $scaleY = [double]$bitmap.Height / [double]$fx.screenshotHeight
  }

  # 1) trail dots (history of past action points)
  if ($fx.trail) {
    foreach ($pt in $fx.trail) {
      $r = 4.0 * $script:SystemDpiScale
      $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(110, 90, 90, 90))
      $g.FillEllipse($brush, [single]($pt.x * $scaleX - $r), [single]($pt.y * $scaleY - $r), [single]($r * 2), [single]($r * 2))
      $brush.Dispose()
    }
  }

  # 2) fog click ring (official: radius 33 * scale, opacity 0.12 base), centered on the tip
  if ($fx.fog) {
    $pulse = [double]$fx.fog.pulse
    $fogAlpha = [int](255 * (0.12 + 0.10 * $pulse))
    if ($fogAlpha -gt 255) { $fogAlpha = 255 }
    $fogRadius = 33.0 * $script:SystemDpiScale * (1.0 + 0.20 * $pulse)
    for ($ring = 6; $ring -ge 1; $ring--) {
      $r = $fogRadius * $ring / 6
      $a = [int]($fogAlpha / $ring)
      if ($a -gt 255) { $a = 255 }
      $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb($a, 255, 255, 255))
      $g.FillEllipse($brush, [single]([double]$fx.fog.x * $scaleX - $r), [single]([double]$fx.fog.y * $scaleY - $r), [single]($r * 2), [single]($r * 2))
      $brush.Dispose()
    }
  }

  # 3) lens frame (official 48x48 glass-orb frames), bottom center sits on the tip
  if ($fx.lens -and $fx.lens.frame -ge 0) {
    $frame = Join-Path $lensDir ('Lens_frame_{0:D2}.png' -f [int]$fx.lens.frame)
    if (Test-Path $frame) {
      try {
        $lf = [System.Drawing.Image]::FromFile($frame)
        $lw = 48.0 * $scaleX * $script:SystemDpiScale; $lh = 48.0 * $scaleY * $script:SystemDpiScale
        $g.DrawImage($lf, [single]([double]$fx.lens.x * $scaleX - $lw / 2), [single]([double]$fx.lens.y * $scaleY - $lh), [single]$lw, [single]$lh)
        $lf.Dispose()
      } catch {}
    }
  }

  # 4) pointer arrow: official contour (fill 0.38/0.36/0.35 a0.98, stroke white 0.90 a0.92, w1.55)
  if ($fx.cursor) {
    $pts = Get-PointerPointsFx ([double]$fx.cursor.x * $scaleX) ([double]$fx.cursor.y * $scaleY) $script:SystemDpiScale
    $fill = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(250, 97, 92, 89))
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(235, 230, 230, 230), 1.55 * $script:SystemDpiScale)
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $g.FillPolygon($fill, $pts)
    $g.DrawPolygon($pen, $pts)
    $fill.Dispose(); $pen.Dispose()
  }

  $g.Dispose()
  return $bitmap
}

# --- model-facing annotations: numbered crosshair grid + last-action ring -----
# The vision model cannot reliably regress raw pixel coordinates (measured on
# this machine: a 4px hit on one glyph but a 115px miss on another), while it
# answers "which marker id sits on the target?" as a multiple-choice question
# with high accuracy. Markers are drawn onto the get_app_state screenshot;
# click({marker}) maps the id back to capture pixels and snaps to the center of
# the interactive element containing it. Every annotation size is divided by
# k = min(1, displayWidth / captureWidth) so the labels stay readable after the
# attachment pipeline downscales the image to displayWidth.

function Get-ColLabel([int]$i) {
  # 0-based column index -> spreadsheet letters: A..Z, AA, AB, ...
  $s = ''
  $n = $i + 1
  while ($n -gt 0) {
    $s = [string][char](65 + (($n - 1) % 26)) + $s
    $n = [int][Math]::Floor(($n - 1) / 26)
  }
  return $s
}

function Get-GridMarkers($elements, $windowRect) {
  # Candidate interactive elements: actionable secondary pattern or settable,
  # enabled, at least 20px in both dimensions, inside the window (1px slack).
  $cands = @()
  foreach ($e in $elements) {
    if (-not $e.enabled) { continue }
    $actionable = @($e.secondaryActions | Where-Object { $_ -in @('Invoke','Toggle','ExpandCollapse','SelectionItem','ScrollItem') }).Count -gt 0
    if (-not $actionable -and -not $e.settable) { continue }
    if ($e.frame.width -lt 20 -or $e.frame.height -lt 20) { continue }
    if ($e.frame.x -lt -1 -or $e.frame.y -lt -1) { continue }
    if (($e.frame.x + $e.frame.width) -gt ($windowRect.width + 1)) { continue }
    if (($e.frame.y + $e.frame.height) -gt ($windowRect.height + 1)) { continue }
    $cands += $e
  }

  # Spacing: 0.4x the median min(w,h) of the candidates, grown stepwise until
  # the lattice stays within 100 markers. Without candidates the grid still
  # helps raw-point clicking: spread ~100 markers evenly over the window.
  if ($cands.Count -gt 0) {
    $mins = @($cands | ForEach-Object { [Math]::Min([double]$_.frame.width, [double]$_.frame.height) })
    [array]::Sort($mins)
    $mid = [int][Math]::Floor(($mins.Count - 1) / 2)
    $median = [double]$mins[$mid]
    if (($mins.Count % 2) -eq 0) { $median = ([double]$mins[$mid] + [double]$mins[$mid + 1]) / 2.0 }
    $spacing = 0.4 * $median
  } else {
    $spacing = [Math]::Sqrt(([double]$windowRect.width * [double]$windowRect.height) / 100.0)
  }
  if ($spacing -le 0.0) { $spacing = 1.0 }
  $cols = [Math]::Max(1, [int][Math]::Floor([double]$windowRect.width / $spacing) + 1)
  $rows = [Math]::Max(1, [int][Math]::Floor([double]$windowRect.height / $spacing) + 1)
  while (($cols * $rows) -gt 100) {
    $spacing = $spacing * 1.15
    $cols = [Math]::Max(1, [int][Math]::Floor([double]$windowRect.width / $spacing) + 1)
    $rows = [Math]::Max(1, [int][Math]::Floor([double]$windowRect.height / $spacing) + 1)
  }

  # Centered lattice so edge margins stay <= spacing/2.
  $markers = New-Object System.Collections.ArrayList
  $mx = [Math]::Max(0.0, ([double]$windowRect.width - ($cols - 1) * $spacing) / 2.0)
  $my = [Math]::Max(0.0, ([double]$windowRect.height - ($rows - 1) * $spacing) / 2.0)
  for ($r = 0; $r -lt $rows; $r++) {
    for ($c = 0; $c -lt $cols; $c++) {
      $x = [int][Math]::Round([Math]::Min([double]$windowRect.width, $mx + $c * $spacing))
      $y = [int][Math]::Round([Math]::Min([double]$windowRect.height, $my + $r * $spacing))
      [void]$markers.Add([pscustomobject]@{ id = (Get-ColLabel $c) + ($r + 1); x = $x; y = $y })
    }
  }

  # Every candidate element must carry at least one marker, or clicking it by
  # marker would be impossible; top uncovered elements up at their center with
  # E1, E2, ... ids (skipping any id the lattice already used).
  $next = 1
  foreach ($e in $cands) {
    $covered = $false
    foreach ($m in $markers) {
      if ($m.x -ge $e.frame.x -and $m.x -le ($e.frame.x + $e.frame.width) -and
          $m.y -ge $e.frame.y -and $m.y -le ($e.frame.y + $e.frame.height)) { $covered = $true; break }
    }
    if ($covered) { continue }
    $id = 'E' + $next
    while (@($markers | Where-Object { $_.id -eq $id }).Count -gt 0) { $next++; $id = 'E' + $next }
    [void]$markers.Add([pscustomobject]@{
      id = $id
      x = [int]($e.frame.x + $e.frame.width / 2)
      y = [int]($e.frame.y + $e.frame.height / 2)
    })
    $next++
  }
  return $markers
}

function Add-AnnotateOverlay($bitmap, $annotate, $lastPoint, $elements, $windowRect) {
  # Draws in capture pixels (the bitmap is the raw window capture) and returns
  # @{ markers = [{id,x,y}...]; lastPointDrawn = bool } for tools.js/session.
  $meta = @{ markers = @(); lastPointDrawn = $false }
  if (-not $annotate) { return $meta }
  if (-not $annotate.grid -and -not $annotate.lastPoint) { return $meta }

  $k = 1.0
  $displayWidth = 1024.0
  if ($annotate.displayWidth) { $displayWidth = [double]$annotate.displayWidth }
  if ($bitmap.Width -gt 0 -and $displayWidth -gt 0) { $k = [Math]::Min(1.0, $displayWidth / [double]$bitmap.Width) }
  if ($k -le 0.0) { $k = 1.0 }

  $g = [System.Drawing.Graphics]::FromImage($bitmap)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias

  if ($annotate.grid) {
    try {
      $markers = Get-GridMarkers $elements $windowRect
      if ($markers.Count -gt 0) {
        $arm = [int][Math]::Round(5.0 / $k)
        $off = [int][Math]::Round(4.0 / $k)
        $font = New-Object System.Drawing.Font('Segoe UI', [single](14.0 / $k), [System.Drawing.GraphicsUnit]::Pixel)
        $halo = New-Object System.Drawing.Pen([System.Drawing.Color]::White, [single](3.4 / $k))
        $cross = New-Object System.Drawing.Pen([System.Drawing.Color]::Crimson, [single](1.5 / $k))
        $back = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
        $ink = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::Crimson)
        foreach ($m in $markers) {
          $x = [int]$m.x
          $y = [int]$m.y
          $g.DrawLine($halo, $x - $arm, $y, $x + $arm, $y)
          $g.DrawLine($halo, $x, $y - $arm, $x, $y + $arm)
          $g.DrawLine($cross, $x - $arm, $y, $x + $arm, $y)
          $g.DrawLine($cross, $x, $y - $arm, $x, $y + $arm)
          # label above-right of the cross on a white chip so it survives clutter
          $sz = $g.MeasureString($m.id, $font)
          $lw = [int][Math]::Ceiling($sz.Width)
          $lh = [int][Math]::Ceiling($sz.Height)
          $lx = $x + $off
          if (($lx + $lw) -gt $bitmap.Width) { $lx = $x - $off - $lw }
          $ly = $y - $off - $lh
          if ($ly -lt 0) { $ly = $y + $off }
          $g.FillRectangle($back, $lx - 1, $ly - 1, $lw + 2, $lh + 2)
          $pt = New-Object System.Drawing.PointF([single]$lx, [single]$ly)
          $g.DrawString($m.id, $font, $ink, $pt)
        }
        $font.Dispose(); $halo.Dispose(); $cross.Dispose(); $back.Dispose(); $ink.Dispose()
        $meta.markers = @($markers)
      }
    } catch {}
  }

  # Amber ring at the last action landing point (self-verification aid);
  # $lastPoint arrives in screen pixels, the rect is the window's screen rect.
  if ($annotate.lastPoint -and $lastPoint) {
    try {
      $lxp = [double]$lastPoint.x - [double]$windowRect.x
      $lyp = [double]$lastPoint.y - [double]$windowRect.y
      if ($lxp -ge 0.0 -and $lyp -ge 0.0 -and $lxp -le [double]$windowRect.width -and $lyp -le [double]$windowRect.height) {
        $rad = 12.0 / $k
        $arm2 = [single](5.0 / $k)
        $halo2 = New-Object System.Drawing.Pen([System.Drawing.Color]::White, [single](4.0 / $k))
        $ring = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 255, 140, 0), [single](2.5 / $k))
        $fx1 = [single]$lxp; $fy1 = [single]$lyp
        $g.DrawEllipse($halo2, [single]($fx1 - $rad), [single]($fy1 - $rad), [single](2.0 * $rad), [single](2.0 * $rad))
        $g.DrawEllipse($ring, [single]($fx1 - $rad), [single]($fy1 - $rad), [single](2.0 * $rad), [single](2.0 * $rad))
        $g.DrawLine($halo2, [single]($fx1 - $arm2), $fy1, [single]($fx1 + $arm2), $fy1)
        $g.DrawLine($halo2, $fx1, [single]($fy1 - $arm2), $fx1, [single]($fy1 + $arm2))
        $g.DrawLine($ring, [single]($fx1 - $arm2), $fy1, [single]($fx1 + $arm2), $fy1)
        $g.DrawLine($ring, $fx1, [single]($fy1 - $arm2), $fx1, [single]($fy1 + $arm2))
        $halo2.Dispose(); $ring.Dispose()
        $meta.lastPointDrawn = $true
      }
    } catch {}
  }

  $g.Dispose()
  return $meta
}

function Activate-Window([IntPtr]$hwnd) {
  # SetForegroundWindow from a background process is silently blocked by the
  # system foreground lock; a no-op Alt tap is the standard bypass (the demo
  # scripts use the same trick). Returns whether the window ended up foreground.
  [CuNative]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)   # Alt down
  [void][CuNative]::ShowWindow($hwnd, 9)                  # SW_RESTORE
  [void][CuNative]::SetForegroundWindow($hwnd)
  [CuNative]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)   # Alt up (KEYEVENTF_KEYUP)
  Start-Sleep -Milliseconds 150
  return ([CuNative]::GetForegroundWindow() -eq $hwnd)
}

function Get-AutomationRoot([IntPtr]$hwnd) {
  # Chrome (and other Chromium apps) churn their top-level widget windows, so a
  # freshly resolved MainWindowHandle can be momentarily unusable: FromHandle
  # then throws an unrecognized-error HRESULT. Retry briefly, and if the handle
  # stays dead, fall back to any other visible window of the same process.
  $last = $null
  for ($i = 0; $i -lt 5; $i++) {
    try { return [System.Windows.Automation.AutomationElement]::FromHandle($hwnd) } catch { $last = $_ }
    Start-Sleep -Milliseconds 150
  }
  try {
    $procId = [uint32]0
    [void][CuNative]::GetWindowThreadProcessId($hwnd, [ref]$procId)
    if ($procId -ne 0) {
      $alt = [CuNative]::FindFirstWindowOfProcess($procId)
      if ($alt -ne [IntPtr]::Zero -and $alt -ne $hwnd) {
        for ($i = 0; $i -lt 3; $i++) {
          try { return [System.Windows.Automation.AutomationElement]::FromHandle($alt) } catch { $last = $_ }
          Start-Sleep -Milliseconds 150
        }
      }
    }
  } catch {}
  throw $last
}

function Invoke-GetState($params) {
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  $maxDepth = if ($null -ne $params.maxDepth) { [Math]::Max(0, [Math]::Min(50, [int]$params.maxDepth)) } else { 8 }
  $maxNodes = if ($null -ne $params.maxNodes) { [Math]::Max(1, [Math]::Min(5000, [int]$params.maxNodes)) } else { 400 }
  $includeScreenshot = if ($null -eq $params.includeScreenshot) { $true } else { [bool]$params.includeScreenshot }

  $hwnd = Resolve-Window $params
  if ($null -eq $hwnd) { Fail "target window not found: '$($params.app)'" }

  # The screenshot below is a screen capture: an occluded window would
  # photograph whatever covers it while the tree still describes the target.
  # Bring the target forward first (annotate.activate=false opts out).
  $activateOnObserve = $true
  if ($null -ne $params.annotate -and $null -ne $params.annotate.activate) {
    $activateOnObserve = [bool]$params.annotate.activate
  }
  if ($activateOnObserve -and [CuNative]::GetForegroundWindow() -ne $hwnd) {
    [void](Activate-Window $hwnd)
  }

  $rect = Get-WindowRectArr $hwnd
  if ($null -eq $rect -or $rect.width -le 0 -or $rect.height -le 0) { Fail 'target window has no capturable bounds' }
  $windowInfo = Get-ProcessInfo $hwnd
  $fg = [CuNative]::GetForegroundWindow()
  $isForeground = ($fg -eq $hwnd)

  $root = Get-AutomationRoot $hwnd
  $script:NextIndex = 0
  $elements = @()

  $queue = New-Object System.Collections.Queue
  $queue.Enqueue(@{ el = $root; path = @() })
  $depthTruncated = $false
  $nodeTruncated = $false
  $visitedCount = 0
  while ($queue.Count -gt 0 -and $visitedCount -lt $maxNodes) {
    $node = $queue.Dequeue()
    $visitedCount++
    $el = $node.el; $path = $node.path
    $index = $elements.Count

    $name = ''; $automationId = ''; $className = ''; $value = ''
    $enabled = $true; $offscreen = $false; $controlType = 'Element'
    $patterns = @()
    $elRect = @{ x = 0; y = 0; width = 0; height = 0 }
    try { $name = [string]$el.Current.Name } catch {}
    try { $automationId = [string]$el.Current.AutomationId } catch {}
    try { $className = [string]$el.Current.ClassName } catch {}
    try { $enabled = [bool]$el.Current.IsEnabled } catch {}
    try { $offscreen = [bool]$el.Current.IsOffscreen } catch {}
    try {
      $ct = $el.Current.ControlType
      if ($ct) { $controlType = $ct.ProgrammaticName -replace 'ControlType\.','' }
    } catch {}
    try { $patterns = @($el.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName -replace 'PatternIdentifiers\.|Pattern$','' }) } catch {}
    try {
      if ($patterns -contains 'Value') {
        $valuePattern = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $value = [string]$valuePattern.Current.Value
      } elseif ($patterns -contains 'Text') {
        $textPattern = $el.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
        $value = [string]$textPattern.DocumentRange.GetText(-1)
      }
    } catch {}
    try {
      $r = $el.Current.BoundingRectangle
      $elRect = @{ x = [int]($r.X - $rect.x); y = [int]($r.Y - $rect.y); width = [int]$r.Width; height = [int]$r.Height }
      if ($elRect.width -lt 0) { $elRect.width = 0 }
      if ($elRect.height -lt 0) { $elRect.height = 0 }
    } catch {}

    $secondary = @($patterns | Where-Object { $_ -in @('Invoke','Toggle','ExpandCollapse','SelectionItem','ScrollItem','Value','Text','RangeValue') })
    $settable = $patterns -contains 'Value'

    if (-not $offscreen) {
      $elements += [pscustomobject]@{
        index = $index
        path = ($path -join '.')
        role = $controlType
        name = $name
        value = $value
        className = $className
        automationId = $automationId
        enabled = $enabled
        settable = $settable
        patterns = $patterns
        secondaryActions = $secondary
        frame = $elRect
      }
    }

    # maxDepth is the deepest level that may be captured: the window root is
    # depth 0, so a node expands only while its children stay within the budget.
    if (($path.Count + 1) -gt $maxDepth) {
      $depthTruncated = $true
    } else {
      try {
        $children = $el.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
        $i = 0
        foreach ($child in $children) {
          if (($visitedCount + $queue.Count) -ge $maxNodes) { $nodeTruncated = $true; break }
          $queue.Enqueue(@{ el = $child; path = $path + $i })
          $i++
        }
      } catch {}
    }
  }
  if ($queue.Count -gt 0) { $nodeTruncated = $true }

  $screenshot = ''
  $markers = @()
  $lastPointDrawn = $false
  if ($includeScreenshot) {
    try {
      $fx = $params.fx
      if ($fx -and $fx.'disabled') { $fx = $null }
      $bmp = New-Object System.Drawing.Bitmap($rect.width, $rect.height)
      $g = [System.Drawing.Graphics]::FromImage($bmp)
      $g.CopyFromScreen($rect.x, $rect.y, 0, 0, $bmp.Size)
      $g.Dispose()
      if ($fx) { $bmp = Add-FxOverlay $bmp $fx }
      # Grid markers and the last-point ring go on top of the fx layer so the
      # selection aids are never covered by effects. Markers exist only with a
      # real capture: they are meaningless without the screenshot they were
      # drawn on.
      $annotateMeta = Add-AnnotateOverlay $bmp $params.annotate $params.lastPoint $elements $rect
      $markers = @($annotateMeta.markers)
      $lastPointDrawn = [bool]$annotateMeta.lastPointDrawn
      $ms = New-Object System.IO.MemoryStream
      $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
      $bmp.Dispose()
      $screenshot = [Convert]::ToBase64String($ms.ToArray())
    } catch { $screenshot = ''; $markers = @(); $lastPointDrawn = $false }
  }

  # tree text like Codex <app_state>
  $winTitle = try { $root.Current.Name } catch { '' }
  $lines = @()
  $lines += "App=$($windowInfo.processName) (pid $($windowInfo.pid))"
  $lines += "Window: `"$($winTitle)`", App: $($windowInfo.processName)."
  foreach ($e in $elements) {
    $indent = [string]::Empty
    $depth = ($e.path.Split('.') | Where-Object { $_ -ne '' }).Count
    if ($depth -gt 0) { $indent = ("`t" * $depth) }
    $desc = "$($e.index) $($e.role)"
    if ($e.name -and $e.name.Length -gt 0) { $desc += " $($e.name)" }
    if ($e.value -and $e.value.Length -gt 0) {
      $v = $e.value; if ($v.Length -gt 60) { $v = $v.Substring(0, 57) + '...' }
      if ($e.settable) { $desc += " (settable) $v" } else { $desc += " Value: $v" }
    }
    if ($e.automationId) { $desc += " ID: $($e.automationId)" }
    if (-not $e.enabled) { $desc += " (disabled)" }
    if ($e.secondaryActions.Count -gt 0) { $desc += " Secondary Actions: $($e.secondaryActions -join ', ')" }
    # Official <app_state> trees carry each element's window-relative frame so
    # the model can click tree hits by pixel without a second observation.
    $desc += " frame=[$($e.frame.x),$($e.frame.y),$($e.frame.width),$($e.frame.height)]"
    $lines += $indent + $desc
  }

  return @{
    ok = $true
    window = @{ hwnd = $hwnd.ToInt64(); pid = $windowInfo.pid; processName = $windowInfo.processName; title = $winTitle; bounds = $rect; foreground = $isForeground }
    elements = $elements
    elementCount = $elements.Count
    nodeCount = $visitedCount
    truncated = ($nodeTruncated -or $depthTruncated)
    truncatedByNodes = $nodeTruncated
    truncatedByDepth = $depthTruncated
    maxDepth = $maxDepth
    durationMs = [int]$timer.ElapsedMilliseconds
    screenshot = $screenshot
    screenshotWidth = $rect.width
    screenshotHeight = $rect.height
    # Annotation outputs for tools.js/session only (markers: capture px,
    # window-relative; lastPointDrawn drives the tree legend). Never surfaced
    # raw to the model output schema.
    markers = $markers
    lastPointDrawn = $lastPointDrawn
    treeText = ($lines -join "`n")
  }
}

function Invoke-ListApps($params) {
  $fg = [CuNative]::GetForegroundWindow()
  $apps = New-Object System.Collections.ArrayList
  $windows = @([CuNative]::ListVisibleWindows() | Sort-Object Title)
  foreach ($w in $windows) {
    $p = Get-Process -Id ([int]$w.Pid) -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    [void]$apps.Add([pscustomobject]@{
      hwnd = [int64]$w.Hwnd
      pid = [int]$w.Pid
      app = $p.ProcessName
      title = [string]$w.Title
      foreground = ($fg.ToInt64() -eq [int64]$w.Hwnd)
    })
  }
  return @{ ok = $true; apps = @($apps); count = $apps.Count }
}

function Invoke-Action($params) {
  $action = [string]$params.action   # click | type_text | press_key | scroll | drag | set_value | select_text | perform_secondary_action
  $hwnd = Resolve-Window $params
  if ($null -eq $hwnd) { Fail "target window not found: '$($params.app)'" }

  # Snapshot alignment: element paths, markers and coordinates all live in the
  # latest get_app_state snapshot's window space. If the app param resolves to a
  # DIFFERENT window (multi-window apps like Chrome, or a switched app), act on
  # the snapshot window when it still exists and belongs to the same process;
  # otherwise refuse loudly instead of clicking elsewhere with stale geometry.
  $snapshotHwnd = if ($null -ne $params.snapshot_hwnd) { [int64]$params.snapshot_hwnd } else { 0 }
  if ($snapshotHwnd -ne 0 -and $snapshotHwnd -ne $hwnd.ToInt64()) {
    $snapProc = [uint32]0; [void][CuNative]::GetWindowThreadProcessId([IntPtr]$snapshotHwnd, [ref]$snapProc)
    $curProc = [uint32]0; [void][CuNative]::GetWindowThreadProcessId($hwnd, [ref]$curProc)
    $snapUsable = ($snapProc -ne 0 -and $snapProc -eq $curProc -and [CuNative]::IsWindowVisible([IntPtr]$snapshotHwnd))
    if ($snapUsable) {
      $hwnd = [IntPtr]$snapshotHwnd
    } else {
      Fail "action targets window $($hwnd.ToInt64()) but the get_app_state snapshot was taken for window $snapshotHwnd; call get_app_state for the current app first"
    }
  }

  # Resolve the UIA tree root BEFORE activating: Chrome's accessibility
  # provider intermittently fails FromHandle with an unrecognized-error
  # HRESULT while the window is mid-activation (Alt bypass + SetForeground).
  # Tree walking does not need the foreground; only the real input below does.
  $root = Get-AutomationRoot $hwnd

  $rect = Get-WindowRectArr $hwnd
  if ($null -eq $rect -or $rect.width -le 0 -or $rect.height -le 0) { Fail 'target window has no actionable bounds' }
  $fg = [CuNative]::GetForegroundWindow()

  # Bring target forward: official Codex Windows operates the foreground app.
  $activate = if ($null -eq $params.activate) { $true } else { [bool]$params.activate }
  if ($activate -and $fg -ne $hwnd) {
    [void](Activate-Window $hwnd)
  }

  # screen->screenshot coordinate: params.x/y are screenshot pixels
  $toScreen = {
    param($x, $y)
    return @{ x = [int]($rect.x + $x); y = [int]($rect.y + $y) }
  }

  $element = $null
  # An explicit path (even the empty one) means "address by element". The empty
  # path is the window root, i.e. element_index 0 -- it used to be dropped by the
  # IsNullOrWhiteSpace guard and silently degraded to a click at window (0,0).
  $pathValue = $null
  if ($null -ne $params.path) { $pathValue = [string]$params.path }
  elseif ($null -ne $params.element_path) { $pathValue = [string]$params.element_path }
  if ($null -ne $pathValue) {
    $segments = @()
    if (-not [string]::IsNullOrWhiteSpace($pathValue)) {
      $segments = @($pathValue -split '\.' | Where-Object { $_ -ne '' } | ForEach-Object { [int]$_ })
    }
    $element = Resolve-ElementByPath $root $segments
    if ($null -eq $element) { Fail "element path '$pathValue' no longer resolves; call get_app_state again" }
  }

  $script:BorrowCursor = if ($null -eq $params.borrowCursor) { $true } else { [bool]$params.borrowCursor }
  $script:HideCursorInBorrow = if ($null -ne $params.hide_cursor) { [bool]$params.hide_cursor } else { $true }

  switch ($action) {
    'click' {
      $button = if ($params.mouse_button) { [string]$params.mouse_button } else { 'left' }
      $count = if ($null -ne $params.click_count) { [int]$params.click_count } else { 1 }
      if ($count -lt 1 -or $count -gt 3) { Fail 'click_count must be between 1 and 3' }
      if ($element) {
        $invoke = $null
        $r = $element.Current.BoundingRectangle
        $elCenter = @{ x = [int]($r.X - $rect.x) + [int]($r.Width / 2); y = [int]($r.Y - $rect.y) + [int]($r.Height / 2) }
        try {
          $pp = $element.GetSupportedPatterns()
          if ($pp -contains [System.Windows.Automation.InvokePattern]::Pattern) {
            $ip = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $ip.Invoke()
            $invoke = 'InvokePattern'
          }
        } catch {}
        if ($invoke) {
          $ptS = & $toScreen $elCenter.x $elCenter.y
          return @{ ok = $true; via = 'InvokePattern'; note = 'clicked element via AX Invoke pattern'; x = $ptS.x; y = $ptS.y }
        }
        $pt = & $toScreen $elCenter.x $elCenter.y
        Invoke-ClickAt $pt.x $pt.y $button $count
        return @{ ok = $true; via = 'coordinate'; x = $pt.x; y = $pt.y }
      }
      $x = [double]$params.x; $y = [double]$params.y
      if ($x -lt 0 -or $y -lt 0 -or $x -ge $rect.width -or $y -ge $rect.height) {
        Fail "click coordinate ($x, $y) is outside the latest window bounds $($rect.width)x$($rect.height)"
      }
      $pt = & $toScreen $x $y
      Invoke-ClickAt $pt.x $pt.y $button $count
      return @{ ok = $true; via = 'coordinate'; x = $pt.x; y = $pt.y }
    }

    'perform_secondary_action' {
      if (-not $element) { Fail 'perform_secondary_action requires element_index' }
      $result = Invoke-SecondaryAction $element ([string]$params.secondary_action)
      return $result
    }

    'set_value' {
      if (-not $element) { Fail 'set_value requires element_index' }
      try {
        $vp = $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $vp.SetValue([string]$params.value)
        return @{ ok = $true; via = 'ValuePattern.SetValue' }
      } catch {
        # fallback: focus + select-all + paste
        try { $element.SetFocus() } catch {}
        Start-Sleep -Milliseconds 60
        [System.Windows.Forms.SendKeys]::SendWait('^a')
        Start-Sleep -Milliseconds 40
        Invoke-TypeText ([string]$params.value)
        return @{ ok = $true; via = 'focus+clipboard fallback' }
      }
    }

    'select_text' {
      if (-not $element) { Fail 'select_text requires element_index' }
      $needle = [string]$params.text
      if ([string]::IsNullOrWhiteSpace($needle)) { Fail 'select_text requires text' }
      $prefix = if ($null -ne $params.prefix) { [string]$params.prefix } else { '' }
      $suffix = if ($null -ne $params.suffix) { [string]$params.suffix } else { '' }
      $selection = if ($params.selection) { [string]$params.selection } else { 'text' }

      $tp = $null
      try { $tp = $element.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern) } catch { $tp = $null }
      if ($null -eq $tp) {
        # Edit controls often expose TextPattern on an inner document element.
        try {
          $cond = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::IsTextPatternAvailableProperty, $true)
          $doc = $element.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
          if ($doc) { $tp = $doc.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern) }
        } catch { $tp = $null }
      }
      if ($null -eq $tp) {
        Fail 'select_text: element does not expose a UIA TextPattern, so text inside it cannot be addressed'
      }

      $UNIT = [System.Windows.Automation.Text.TextUnit]::Character
      $docRange = $tp.DocumentRange
      $full = $docRange.GetText(-1)
      if ([string]::IsNullOrEmpty($full)) { Fail 'select_text: element is empty' }

      # Locate the match: prefix/suffix disambiguate repeated occurrences.
      $context = $prefix + $needle + $suffix
      $offset = $full.IndexOf($context, [System.StringComparison]::Ordinal)
      if ($offset -lt 0) { $offset = $full.IndexOf($context, [System.StringComparison]::OrdinalIgnoreCase) }
      if ($offset -lt 0) { Fail "select_text: '$context' not found in element text" }
      $textStart = $offset + $prefix.Length

      $Start2 = [System.Windows.Automation.Text.TextPatternRangeEndpoint]::Start
      $End2 = [System.Windows.Automation.Text.TextPatternRangeEndpoint]::End
      $range = $docRange.Clone()
      [void]$range.MoveEndpointByRange($End2, $range, $Start2)            # collapse at doc start
      $moved = $range.MoveEndpointByUnit($End2, $UNIT, $textStart)
      if ($moved -ne $textStart) { Fail "select_text: cannot reach offset $textStart (stopped at $moved)" }
      [void]$range.MoveEndpointByRange($Start2, $range, $End2)
      $moved = $range.MoveEndpointByUnit($End2, $UNIT, $needle.Length)
      if ($moved -ne $needle.Length) { Fail "select_text: text ends early (selected $moved of $($needle.Length) characters)" }

      if ($selection -eq 'cursor_before') {
        [void]$range.MoveEndpointByRange($End2, $range, $Start2)
      } elseif ($selection -eq 'cursor_after') {
        [void]$range.MoveEndpointByRange($Start2, $range, $End2)
      }

      try { $element.SetFocus() } catch {}
      Start-Sleep -Milliseconds 40
      $range.Select()
      return @{
        ok = $true
        via = 'TextPattern'
        selection = $selection
        note = "$selection on '$needle' at offset $textStart (len $($needle.Length))"
      }
    }

    'scroll' {
      $direction = [string]$params.direction
      $pages = if ($null -ne $params.pages) { [double]$params.pages } else { 1.0 }
      if ($pages -le 0 -or $pages -gt 10 -or [double]::IsNaN($pages) -or [double]::IsInfinity($pages)) {
        Fail 'scroll pages must be greater than 0 and at most 10'
      }
      # One page ≈ 12 notches (12 × WHEEL_DELTA ≈ two thirds of a viewport at
      # default Chrome line height); fractional pages scale the notch count.
      $notches = [Math]::Max(1, [int][Math]::Round(12 * $pages))
      if ($element) {
        $r = $element.Current.BoundingRectangle
        $pt = & $toScreen ([int]($r.X - $rect.x) + [int]($r.Width / 2)) ([int]($r.Y - $rect.y) + [int]($r.Height / 2))
        Invoke-Scroll $pt.x $pt.y $direction $notches
        return @{ ok = $true; via = 'mouse wheel'; direction = $direction; pages = $pages; x = $pt.x; y = $pt.y }
      } else {
        if ($null -ne $params.x -and $null -ne $params.y) {
          if ([double]$params.x -lt 0 -or [double]$params.y -lt 0 -or [double]$params.x -ge $rect.width -or [double]$params.y -ge $rect.height) {
            Fail "scroll coordinate ($($params.x), $($params.y)) is outside the latest window bounds $($rect.width)x$($rect.height)"
          }
          $pt = & $toScreen ([double]$params.x) ([double]$params.y)
        } else {
          $pt = & $toScreen ([double]$rect.width / 2) ([double]$rect.height / 2)
        }
        Invoke-Scroll $pt.x $pt.y $direction $notches
        return @{ ok = $true; via = 'mouse wheel'; direction = $direction; pages = $pages; x = $pt.x; y = $pt.y }
      }
    }

    'drag' {
      $fromX = [double]$params.from_x; $fromY = [double]$params.from_y
      $toX = [double]$params.to_x; $toY = [double]$params.to_y
      $button = if ($params.mouse_button) { [string]$params.mouse_button } else { 'left' }
      $pt1 = & $toScreen $fromX $fromY
      $pt2 = & $toScreen $toX $toY
      Invoke-Drag $pt1.x $pt1.y $pt2.x $pt2.y $button
      return @{ ok = $true; via = 'SendInput drag'; x = $pt2.x; y = $pt2.y }
    }

    'press_key' {
      $keys = @([string]$params.key -split '\+')
      $chord = Convert-KeyChord $keys
      if ($element) { try { $element.SetFocus() } catch {} }
      Start-Sleep -Milliseconds 60
      $expected = 0
      if ($chord.unicodeChars) {
        $sent = [CuInput]::SendUnicode([uint16[]]$chord.unicodeChars)
        $expected = 2 * $chord.unicodeChars.Count
      } else {
        $sent = [CuInput]::SendKey($chord.vks, $chord.downs)
        $expected = $chord.vks.Length
      }
      Start-Sleep -Milliseconds 40
      if ($sent -ne $expected) {
        return @{ ok = $false; error = "press_key: SendInput accepted $sent of $expected events" }
      }
      return @{ ok = $true; via = 'SendInput'; key = [string]$params.key; events = [int]$sent }
    }

    'type_text' {
      if ($element) { try { $element.SetFocus() } catch {} }
      Start-Sleep -Milliseconds 60
      $paste = Invoke-TypeText ([string]$params.text)
      if ($paste) { return $paste }
      return @{ ok = $true; via = 'clipboard+ctrl-v' }
    }

    default { Fail "unknown action: $action" }
  }
}

# ---- dispatch ----
# SendKeys needs WinForms
Add-Type -AssemblyName System.Windows.Forms

function Invoke-Command($params) {
  # reset per-call script state (the kernel process may serve many requests)
  $script:BorrowCursor = $true
  $script:HideCursorInBorrow = $true
  $action = [string]$params.action   # click | type_text | press_key | scroll | drag | set_value | select_text | perform_secondary_action
  $result = switch ($action) {
    'get_state' { Invoke-GetState $params }
    'list_apps' { Invoke-ListApps $params }
    'click' { Invoke-Action $params }
    'perform_secondary_action' { Invoke-Action $params }
    'set_value' { Invoke-Action $params }
    'select_text' { Invoke-Action $params }
    'scroll' { Invoke-Action $params }
    'drag' { Invoke-Action $params }
    'press_key' { Invoke-Action $params }
    'type_text' { Invoke-Action $params }
    default { Fail "unknown action: $action" }
  }
  return $result
}

function Out-JsonLine($obj) {
  [Console]::Out.Write(($obj | ConvertTo-Json -Depth 30 -Compress))
  [Console]::Out.Write("`n")
  [Console]::Out.Flush()
}

if ($args -contains '--serve') {
  # Resident kernel: one JSON request per stdin line, one JSON response line per
  # request. Saves the PowerShell/UIA Add-Type startup on every action; the JS
  # side recycles the process after a crash, a timeout or the request budget.
  $script:ServedRequests = 0
  while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try { $params = $line | ConvertFrom-Json } catch {
      Out-JsonLine @{ ok = $false; error = 'bad request json' }
      continue
    }
    try {
      $result = Invoke-Command $params
      Out-JsonLine $result
    } catch {
      Out-JsonLine @{ ok = $false; error = $_.Exception.Message }
    }
    $script:ServedRequests += 1
    if ($script:ServedRequests -ge 200) { break }   # recycle: next call respawns fresh
  }
  exit 0
}

# one-shot mode (legacy direct callers): single request on stdin, then exit
try {
  $params = Read-Input
  $result = Invoke-Command $params
  Out-Json $result
} catch {
  Out-Json @{ ok = $false; error = $_.Exception.Message }
  exit 1
}
