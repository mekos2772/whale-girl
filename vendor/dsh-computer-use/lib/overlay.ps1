# dsh-computer-use — desktop software-cursor overlay (Codex-style motion).
# A click-through, topmost, transparent window that animates a software cursor:
# spring motion (response=1.4s, damping=0.9, dt=1/240 semi-implicit Euler),
# grey radial fog, a short press deformation, and a restrained blue ripple.
# The old 3D LensSequence is separate and never part of the default click.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# Per-monitor DPI awareness: without it the OS bitmap-scales this window (126 -> 136 @108%).
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class DpiFix {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
"@
try { [void][DpiFix]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch {}  # PER_MONITOR_AWARE_V2

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# The software cursor replaces the real one for the whole animation: blank all
# system cursor slots (SetSystemCursor is global, unlike the per-queue
# ShowCursor) and reload the scheme from the registry on exit. If this process
# is killed before restoring, the JS side runs the same SPI reload as a failsafe.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuSysCursor {
  [DllImport("user32.dll")] public static extern bool SetSystemCursor(IntPtr hcur, uint id);
  [DllImport("user32.dll")] public static extern bool SystemParametersInfo(uint uiAction, uint uiParam, IntPtr pvParam, uint fWinIni);
  [StructLayout(LayoutKind.Sequential)] public struct POINTL { public int X, Y; }
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromPoint(POINTL pt, uint dwFlags);
  [DllImport("shcore.dll")] public static extern int GetDpiForMonitor(IntPtr hmon, int dpiType, out uint dpiX, out uint dpiY);
  // Effective DPI scale of the monitor containing (x, y); the glyph is drawn at
  // the same visual size as the user's system cursor on that monitor.
  public static double MonitorScale(int x, int y) {
    try {
      POINTL pt; pt.X = x; pt.Y = y;
      IntPtr hmon = MonitorFromPoint(pt, 2);   // MONITOR_DEFAULTTONEAREST
      if (hmon == IntPtr.Zero) return 1.0;
      uint dx, dy;
      if (GetDpiForMonitor(hmon, 0, out dx, out dy) != 0 || dx == 0) return 1.0;   // MDT_EFFECTIVE_DPI
      return dx / 96.0;
    } catch { return 1.0; }
  }
}
"@
$CURSOR_SLOT_IDS = @(32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651)

function Hide-SystemCursor {
  foreach ($id in $CURSOR_SLOT_IDS) {
    $bmp = New-Object System.Drawing.Bitmap(1, 1)
    try { $hIcon = $bmp.GetHicon() } finally { $bmp.Dispose() }
    [void][CuSysCursor]::SetSystemCursor($hIcon, [uint32]$id)   # the system consumes the handle
  }
}

function Restore-SystemCursor {
  [void][CuSysCursor]::SystemParametersInfo(0x0057, 0, [IntPtr]::Zero, 0)   # SPI_SETCURSORS reloads the scheme
}

function Read-Input {
  # Line-delimited protocol: first line starts motion, second line commits the UI action.
  $raw = [Console]::In.ReadLine()
  if (-not $raw) { return [pscustomobject]@{ points = @(); kind = 'click' } }
  return ($raw | ConvertFrom-Json)
}

function Out-Json($obj) {
  [Console]::Out.Write(($obj | ConvertTo-Json -Depth 10 -Compress))
}

# ---- official-inspired dynamics ----
$DAMPING_FRACTION = 0.9
$RESPONSE = 1.4          # spring response seconds (default officialInspired)
$DT = 1.0 / 240.0
$IDLE_VELOCITY_THRESHOLD = 28800.0
$STIFFNESS = [Math]::Min([Math]::Pow((2 * [Math]::PI) / $RESPONSE, 2), $IDLE_VELOCITY_THRESHOLD)
$DRAG = 2 * $DAMPING_FRACTION * [Math]::Sqrt($STIFFNESS)

# glyph metrics (SoftwareCursorGlyphMetrics / official reverse-engineered)
$WINDOW_SIZE = 126
$TIP_ANCHOR_X = 60.35
$TIP_ANCHOR_Y = 70.3
$POINTER_SIZE = 21
$FOG_RADIUS = 33.0   # (66 * scale) / 2

# Official cursor pointer contour (rows: y, minX, maxX in a 10..38 x 10..39 source space)
$POINTER_CONTOUR = @(
  @(39, 17, 21), @(38, 16, 22), @(37, 15, 22), @(36, 15, 23), @(35, 15, 24),
  @(34, 15, 24), @(33, 14, 25), @(32, 14, 25), @(31, 14, 26), @(30, 14, 27),
  @(29, 13, 29), @(28, 13, 31), @(27, 13, 34), @(26, 13, 36), @(25, 13, 37),
  @(24, 12, 37), @(23, 12, 37), @(22, 12, 37), @(21, 12, 37), @(20, 12, 36),
  @(19, 11, 36), @(18, 11, 34), @(17, 11, 32), @(16, 11, 30), @(15, 10, 27),
  @(14, 10, 25), @(13, 10, 23), @(12, 11, 21), @(11, 11, 19), @(10, 13, 16)
)

function Get-PointerPoints([double]$tipX, [double]$tipY, [double]$scale) {
  # map the source 10..38 x 10..39 contour so its TOP-LEFT TIP (row y=10,
  # x 13..16 — the point the eye reads as the cursor position) lands exactly on
  # (tipX, tipY); the body then hangs down-right like a normal arrow cursor.
  $size = $POINTER_SIZE * $scale
  $srcMinX = 10.0; $srcMaxX = 38.0; $srcMinY = 10.0; $srcMaxY = 39.0
  $tipSrcX = ($POINTER_CONTOUR[$POINTER_CONTOUR.Count - 1][1] + $POINTER_CONTOUR[$POINTER_CONTOUR.Count - 1][2]) / 2
  $rectLeft = $tipX - ($tipSrcX - $srcMinX) / ($srcMaxX - $srcMinX) * $size
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

$LENS_DIR = Join-Path $PSScriptRoot '..\assets\lens'

# -------- spring simulation (semi-implicit Euler at 1/240) --------
function Step-Spring([double]$pos, [double]$vel, [double]$target, [double]$dt) {
  $half = $dt / 2
  $vHalf = $vel + (-$STIFFNESS * ($pos - $target) - $DRAG * $vel) * $half
  $next = $pos + ($vHalf * $dt)
  $vNext = $vHalf + (-$STIFFNESS * ($next - $target) - $DRAG * $vHalf) * $half
  return @{ pos = $next; vel = $vNext }
}

$params = Read-Input
$points = @($params.points)
if ($points.Count -eq 0) { Out-Json @{ ok = $false; error = 'no points' }; exit 1 }
$kind = [string]$params.kind
$doLens = if ($null -eq $params.lens) { $false } else { [bool]$params.lens }
$doFog = if ($null -eq $params.fog) { $true } else { [bool]$params.fog }
$doPulse = if ($null -eq $params.pulse) { ($kind -eq 'click') } else { [bool]$params.pulse }
$interactionIndex = if ($null -eq $params.interactionIndex) { $points.Count - 1 } else { [int]$params.interactionIndex }
$interactionIndex = [Math]::Max(0, [Math]::Min($interactionIndex, $points.Count - 1))

# Resident session mode: the first motion line carries serve:true and further
# commands arrive on stdin while the overlay stays alive BETWEEN actions — the
# cursor glyph and the screen-edge glow persist until the controller ends the
# session (agent turn end / idle), matching the official turn-ended semantics.
$script:served = [bool]$params.serve
$script:cmdQueue = New-Object System.Collections.Concurrent.ConcurrentQueue[string]
$script:gateOpen = $false
$script:waitCommit = $false
$script:EOF_SENTINEL = '__dsh_overlay_eof__'

function Start-ReaderThread {
  # Blocking Console.In reads would freeze the WinForms timer, so a side
  # runspace pumps stdin into the command queue on a background thread.
  $rs = [runspacefactory]::CreateRunspace()
  $rs.Open()
  $ps = [powershell]::Create()
  $ps.Runspace = $rs
  [void]$ps.AddScript({
    param($q, $eof)
    try {
      while ($true) {
        $line = [Console]::In.ReadLine()
        if ($null -eq $line) { $q.Enqueue($eof); return }
        $q.Enqueue($line)
      }
    } catch { try { $q.Enqueue($eof) } catch {} }
  }).AddArgument($script:cmdQueue).AddArgument($script:EOF_SENTINEL)
  $script:readerPs = $ps
  $script:readerRunspace = $rs
  [void]$ps.BeginInvoke()
}

function Stop-ReaderThread {
  try { if ($script:readerPs) { $script:readerPs.Stop(); $script:readerPs.Dispose() } } catch {}
  try { if ($script:readerRunspace) { $script:readerRunspace.Dispose() } } catch {}
  $script:readerPs = $null
  $script:readerRunspace = $null
}

function Start-SessionPlay($cmd) {
  # A new motion while resident: rebind the motion parameters and start the
  # first leg from points[0] (the controller sends the previous endpoint, so
  # the glyph continuity is preserved across actions).
  $script:points = @($cmd.points)
  if ($script:points.Count -eq 0) { return }
  $script:kind = [string]$cmd.kind
  $script:doLens = if ($null -eq $cmd.lens) { $false } else { [bool]$cmd.lens }
  $script:doFog = if ($null -eq $cmd.fog) { $true } else { [bool]$cmd.fog }
  $script:doPulse = if ($null -eq $cmd.pulse) { ($script:kind -eq 'click') } else { [bool]$cmd.pulse }
  $idx = if ($null -eq $cmd.interactionIndex) { $script:points.Count - 1 } else { [int]$cmd.interactionIndex }
  $script:interactionIndex = [Math]::Max(0, [Math]::Min($idx, $script:points.Count - 1))
  $script:gateOpen = $false
  $script:waitCommit = $false
  $state.posX = [double]$script:points[0].x
  $state.posY = [double]$script:points[0].y
  $state.opacity = 255
  $state.startT = NowMs
  $state.targetIndex = $(if ($script:points.Count -gt 1) { 1 } else { 0 })
  Begin-Leg $state.targetIndex
}

if ($script:served) { Start-ReaderThread }

# Scale the glyph to the effective DPI of the monitor the motion starts on: the
# base constants are the official 100% sizes (126px window, 21px pointer).
$DPI_SCALE = [CuSysCursor]::MonitorScale([int]$points[0].x, [int]$points[0].y)
$WINDOW_SIZE = [int][Math]::Round(126 * $DPI_SCALE)
$TIP_ANCHOR_X = 60.35 * $DPI_SCALE
$TIP_ANCHOR_Y = 70.3 * $DPI_SCALE
$POINTER_SIZE = 21 * $DPI_SCALE
$FOG_RADIUS = 33.0 * $DPI_SCALE

# From here on the real pointer is visually replaced by the software cursor.
Hide-SystemCursor
# Belt and braces: on ANY terminating exit path (uncaught error, normal exit)
# .NET runs ProcessExit — restore the user's cursor scheme there too. Hard
# kills (TerminateProcess) are covered by the JS-side failsafe.
[AppDomain]::CurrentDomain.add_ProcessExit({ try { Restore-SystemCursor } catch {} })

# available lens frames
$lensFrames = @()
if ($doLens) {
  $lensFrames = @(Get-ChildItem -Path $LENS_DIR -Filter 'Lens_frame_*.png' -ErrorAction SilentlyContinue | Sort-Object Name)
}

# decode cursor image once (not required: vector-drawn pointer is the primary path)
$LENS_FRAMES_CACHE = @{}

$DEBUG_SAVE = ($env:DSH_CU_OVERLAY_DEBUG -eq '1')
$DEBUG_DIR = Join-Path $env:TEMP 'dsh-cu-overlay-debug'
if ($DEBUG_SAVE) { New-Item -ItemType Directory -Force -Path $DEBUG_DIR | Out-Null }

# paint the glyph; tip stays fixed at (TIP_ANCHOR_X, TIP_ANCHOR_Y) inside the window
$bitmap = [System.Drawing.Bitmap]::new($WINDOW_SIZE, $WINDOW_SIZE, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
function Draw-Glyph([double]$pulse, [int]$lensIndex) {
  $g = [System.Drawing.Graphics]::FromImage($bitmap)
  $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $tipX = $TIP_ANCHOR_X
  $tipY = $TIP_ANCHOR_Y

  # 1) Soft neutral-grey fog centered on the pointer tip. The pulse grows it
  # by only 1.2px, so it reads as contact instead of a white disc.
  if ($doFog) {
    $fogRadius = $FOG_RADIUS + (1.2 * $pulse)
    $fogCenterY = $tipY - (0.8 * $pulse)
    $fogPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $fogPath.AddEllipse([single]($tipX - $fogRadius), [single]($fogCenterY - $fogRadius), [single]($fogRadius * 2), [single]($fogRadius * 2))
    $fogBrush = New-Object System.Drawing.Drawing2D.PathGradientBrush($fogPath)
    $fogBrush.CenterPoint = New-Object System.Drawing.PointF([single]$tipX, [single]$fogCenterY)
    $fogBlend = New-Object System.Drawing.Drawing2D.ColorBlend(4)
    $fogBlend.Colors = [System.Drawing.Color[]]@(
      [System.Drawing.Color]::FromArgb(0, 153, 153, 153),
      [System.Drawing.Color]::FromArgb(28, 117, 112, 110),
      [System.Drawing.Color]::FromArgb([int](71 + 4 * $pulse), 110, 105, 102),
      [System.Drawing.Color]::FromArgb([int](102 + 5 * $pulse), 97, 92, 89)
    )
    # PathGradient interpolation runs from the boundary (0) to center (1).
    $fogBlend.Positions = [single[]]@(0.0, 0.18, 0.50, 1.0)
    $fogBrush.InterpolationColors = $fogBlend
    $g.FillEllipse($fogBrush, [single]($tipX - $fogRadius), [single]($fogCenterY - $fogRadius), [single]($fogRadius * 2), [single]($fogRadius * 2))
    $fogBrush.Dispose(); $fogPath.Dispose()
  }

  # 2) A flat blue contact ripple. It is deliberately thin and lives behind
  # the pointer, so the click target remains visible.
  if ($pulse -gt 0.01) {
    $rippleRadius = (6.0 + (12.0 * $pulse)) * $DPI_SCALE
    $rippleAlpha = [int](235 * $pulse)
    $ripplePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb($rippleAlpha, 28, 190, 255), [single]((1.6 + 1.4 * $pulse) * $DPI_SCALE))
    $g.DrawEllipse($ripplePen, [single]($tipX - $rippleRadius), [single]($tipY - $rippleRadius), [single]($rippleRadius * 2), [single]($rippleRadius * 2))
    $ripplePen.Dispose()
    $sparkRadius = (3.0 + (3.0 * $pulse)) * $DPI_SCALE
    $sparkPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb([int](150 * $pulse), 255, 255, 255), [single](1.0 * $DPI_SCALE))
    $g.DrawEllipse($sparkPen, [single]($tipX - $sparkRadius), [single]($tipY - $sparkRadius), [single]($sparkRadius * 2), [single]($sparkRadius * 2))
    $sparkPen.Dispose()
  }

  # 3) Optional legacy LensSequence playback. It is opt-in and is not used by
  # the normal click path.
  if ($lensIndex -ge 0 -and $lensIndex -lt $lensFrames.Count) {
    try {
      $cacheKey = $lensFrames[$lensIndex].FullName
      if (-not $LENS_FRAMES_CACHE.ContainsKey($cacheKey)) {
        $LENS_FRAMES_CACHE[$cacheKey] = [System.Drawing.Image]::FromFile($cacheKey)
      }
      $lf = $LENS_FRAMES_CACHE[$cacheKey]
      $g.DrawImage($lf, [single]($tipX - 24), [single]($tipY - 48), 48, 48)
    } catch {}
  }

  # 4) pointer arrow; a click briefly compresses, stretches and tilts it.
  $pts = Get-PointerPoints $tipX $tipY 1.0
  $fill = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(250, 97, 92, 89))
  $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(235, 230, 230, 230), [single](1.55 * $DPI_SCALE))
  $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
  $pointerState = $g.Save()
  $g.TranslateTransform([single]$tipX, [single]$tipY)
  $g.RotateTransform([single](1.72 * $pulse))
  $g.ScaleTransform([single](1.0 - 0.04 * $pulse), [single](1.0 + 0.02 * $pulse))
  $g.TranslateTransform([single](-$tipX), [single](-$tipY))
  $g.FillPolygon($fill, $pts)
  $g.DrawPolygon($pen, $pts)
  $g.Restore($pointerState)
  $fill.Dispose(); $pen.Dispose()

  $g.Dispose()
  if ($DEBUG_SAVE) {
    try {
      $name = if ($lensIndex -ge 0) {
        ('lens_{0:D2}.png' -f $lensIndex)
      } elseif ($pulse -gt 0.90) {
        'click_peak.png'
      } else {
        'idle.png'
      }
      $bitmap.Save((Join-Path $DEBUG_DIR $name), [System.Drawing.Imaging.ImageFormat]::Png)
    } catch {}
  }
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar = $false
$form.TopMost = $true
$form.Size = New-Object System.Drawing.Size($WINDOW_SIZE, $WINDOW_SIZE)
$form.BackColor = [System.Drawing.Color]::Black
# click-through + tool window
$GWL_EXSTYLE_READY = $false

# window-level click-through: set WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class OverlayWin {
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hWnd, int nIndex);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int w, int h, uint flags);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
  [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hDC);
  [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hDC);
  [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hDC, IntPtr hObject);
  [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr hObject);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool UpdateLayeredWindow(
    IntPtr hWnd, IntPtr hdcDst, ref POINT pptDst, ref SIZE psize,
    IntPtr hdcSrc, ref POINT pptSrc, uint crKey, ref BLENDFUNCTION pblend, uint flags);  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [StructLayout(LayoutKind.Sequential)] public struct SIZE { public int CX, CY; }
  [StructLayout(LayoutKind.Sequential, Pack=1)] public struct BLENDFUNCTION {
    public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat;
  }
}
"@
$GWL_EXSTYLE = -20
$WS_EX_TOOLWINDOW = 0x00000080
$WS_EX_LAYERED = 0x00080000
$WS_EX_TRANSPARENT = 0x00000020
$WS_EX_NOACTIVATE = 0x08000000
$SW_SHOWNOACTIVATE = 4

function Set-ClickThrough($hwnd) {
  $ex = [OverlayWin]::GetWindowLong($hwnd, $GWL_EXSTYLE)
  [void][OverlayWin]::SetWindowLong($hwnd, $GWL_EXSTYLE, ($ex -bor $WS_EX_TRANSPARENT -bor $WS_EX_LAYERED -bor $WS_EX_TOOLWINDOW -bor $WS_EX_NOACTIVATE))
}

# ---- screen-edge glow: the conspicuous "computer use is acting" indicator ----
# Four thin layered strips along the monitor edges of the interaction point,
# pre-rendered blue gradients, pulsing via constant alpha (cheap ULW re-present).
$GLOW_DEPTH = [int](44 * $DPI_SCALE)
$GLOW_COLOR = [System.Drawing.Color]::FromArgb(210, 0, 150, 255)
$script:GlowStrips = @()
$script:GlowTick = 0

function New-GlowStrip([int]$w, [int]$h, [int]$dir) {
  # dir: 0 top (fade down), 1 bottom (fade up), 2 left (fade right), 3 right (fade left)
  $bmp = New-Object System.Drawing.Bitmap($w, $h)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
  $angle = switch ($dir) { 0 { 90 } 1 { 270 } 2 { 0 } default { 180 } }
  $transparent = [System.Drawing.Color]::FromArgb(0, $GLOW_COLOR.R, $GLOW_COLOR.G, $GLOW_COLOR.B)
  $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $GLOW_COLOR, $transparent, [single]$angle)
  $g.FillRectangle($brush, $rect)
  $brush.Dispose()
  $g.Dispose()
  return $bmp
}

function Present-Strip($strip, [byte]$alpha) {
  $screenDc = [OverlayWin]::GetDC([IntPtr]::Zero)
  $memoryDc = [OverlayWin]::CreateCompatibleDC($screenDc)
  $hBitmap = $strip.bitmap.GetHbitmap([System.Drawing.Color]::FromArgb(0, 0, 0, 0))
  $oldBitmap = [OverlayWin]::SelectObject($memoryDc, $hBitmap)
  try {
    $dst = New-Object 'OverlayWin+POINT'
    $dst.X = $strip.x; $dst.Y = $strip.y
    $src = New-Object 'OverlayWin+POINT'
    $src.X = 0; $src.Y = 0
    $size = New-Object 'OverlayWin+SIZE'
    $size.CX = $strip.w; $size.CY = $strip.h
    $blend = New-Object 'OverlayWin+BLENDFUNCTION'
    $blend.BlendOp = 0
    $blend.BlendFlags = 0
    $blend.SourceConstantAlpha = $alpha
    $blend.AlphaFormat = 1
    [void][OverlayWin]::UpdateLayeredWindow($strip.form.Handle, $screenDc, [ref]$dst, [ref]$size, $memoryDc, [ref]$src, 0, [ref]$blend, 2)
  } finally {
    [void][OverlayWin]::SelectObject($memoryDc, $oldBitmap)
    [void][OverlayWin]::DeleteObject($hBitmap)
    [void][OverlayWin]::DeleteDC($memoryDc)
    [void][OverlayWin]::ReleaseDC([IntPtr]::Zero, $screenDc)
  }
}

function Start-GlowStrips {
  try {
    $ip = $points[$interactionIndex]
    $mon = [System.Windows.Forms.Screen]::GetBounds((New-Object System.Drawing.Point([int]$ip.x, [int]$ip.y)))
    $defs = @(
      @{ x = $mon.X; y = $mon.Y; w = $mon.Width; h = $GLOW_DEPTH; dir = 0 },
      @{ x = $mon.X; y = $mon.Y + $mon.Height - $GLOW_DEPTH; w = $mon.Width; h = $GLOW_DEPTH; dir = 1 },
      @{ x = $mon.X; y = $mon.Y; w = $GLOW_DEPTH; h = $mon.Height; dir = 2 },
      @{ x = $mon.X + $mon.Width - $GLOW_DEPTH; y = $mon.Y; w = $GLOW_DEPTH; h = $mon.Height; dir = 3 }
    )
    foreach ($def in $defs) {
      $form = New-Object System.Windows.Forms.Form
      $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
      $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
      $form.ShowInTaskbar = $false
      $form.TopMost = $true
      $form.Size = New-Object System.Drawing.Size($def.w, $def.h)
      $strip = @{ form = $form; bitmap = (New-GlowStrip $def.w $def.h $def.dir); x = $def.x; y = $def.y; w = $def.w; h = $def.h }
      $script:GlowStrips += $strip
      $hand = $form.Handle
      Set-ClickThrough $hand
      [void][OverlayWin]::ShowWindow($hand, $SW_SHOWNOACTIVATE)
      # The FIRST ShowWindow call after spawn can be suppressed by inherited
      # STARTF_USESHOWWINDOW (node spawns with windowsHide:true), and ULW
      # content only becomes visible once the window is shown. A second show
      # is always honored — the glyph form escapes the same trap because
      # Application::Run shows it downstream.
      [void][OverlayWin]::ShowWindow($hand, $SW_SHOWNOACTIVATE)
      Present-Strip $strip 160
    }
  } catch {
    try { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'), "[glow-start] $($_.Exception.Message)`n") } catch {}
  }
}

function Stop-GlowStrips {
  foreach ($strip in $script:GlowStrips) {
    try { $strip.form.Close() } catch {}
    try { $strip.bitmap.Dispose() } catch {}
  }
  $script:GlowStrips = @()
}

# Pulse 0.55..1.0 of the base alpha; $fade scales it down during the fade-out.
function Update-GlowStrips([double]$fade) {
  if ($script:GlowStrips.Count -eq 0) { return }
  $script:GlowTick += 1
  if (($script:GlowTick % 2) -ne 0) { return }   # ~30ms cadence is plenty
  $pulse = 0.55 + 0.45 * [Math]::Abs([Math]::Sin((NowMs) / 480.0))
  $alpha = [int][Math]::Min(255, 210 * $pulse * $fade)
  if ($alpha -le 0) {
    if ($fade -le 0) { Stop-GlowStrips }
    return
  }
  foreach ($strip in $script:GlowStrips) {
    try { Present-Strip $strip $alpha } catch {}
  }
}

function Present-Glyph([byte]$opacity = 255) {
  $screenDc = [OverlayWin]::GetDC([IntPtr]::Zero)
  $memoryDc = [OverlayWin]::CreateCompatibleDC($screenDc)
  $hBitmap = $bitmap.GetHbitmap([System.Drawing.Color]::FromArgb(0, 0, 0, 0))
  $oldBitmap = [OverlayWin]::SelectObject($memoryDc, $hBitmap)
  try {
    $dst = New-Object 'OverlayWin+POINT'
    $dst.X = [int]($state.posX - $TIP_ANCHOR_X)
    $dst.Y = [int]($state.posY - $TIP_ANCHOR_Y)
    $src = New-Object 'OverlayWin+POINT'
    $src.X = 0; $src.Y = 0
    $size = New-Object 'OverlayWin+SIZE'
    $size.CX = $WINDOW_SIZE; $size.CY = $WINDOW_SIZE
    $blend = New-Object 'OverlayWin+BLENDFUNCTION'
    $blend.BlendOp = 0       # AC_SRC_OVER
    $blend.BlendFlags = 0
    $blend.SourceConstantAlpha = $opacity
    $blend.AlphaFormat = 1   # AC_SRC_ALPHA
    $ulw = [OverlayWin]::UpdateLayeredWindow(
      $form.Handle, $screenDc, [ref]$dst, [ref]$size,
      $memoryDc, [ref]$src, 0, [ref]$blend, 2)
    if (-not $ulw) {
      # Silent failure here means an invisible overlay: always leave a trace.
      $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
      [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'),
        "[ulw] ok=False err=$err dst=$($dst.X),$($dst.Y) size=$($size.CX)x$($size.CY) opacity=$opacity`n")
    }
  } finally {
    [void][OverlayWin]::SelectObject($memoryDc, $oldBitmap)
    [void][OverlayWin]::DeleteObject($hBitmap)
    [void][OverlayWin]::DeleteDC($memoryDc)
    [void][OverlayWin]::ReleaseDC([IntPtr]::Zero, $screenDc)
  }
}

# ---- animation state ----
function NowMs { return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }

function Sample-Cubic([double]$p0, [double]$p1, [double]$p2, [double]$p3, [double]$t) {
  $u = 1.0 - $t
  return ($u * $u * $u * $p0) + (3 * $u * $u * $t * $p1) + (3 * $u * $t * $t * $p2) + ($t * $t * $t * $p3)
}

$state = @{
  targetIndex = $(if ($points.Count -gt 1) { 1 } else { 0 })
  posX = 0.0; posY = 0.0
  progress = 0.0; progressVelocity = 0.0
  startX = 0.0; startY = 0.0
  control1X = 0.0; control1Y = 0.0
  control2X = 0.0; control2Y = 0.0
  pulse = 0.0
  lensPhase = -1      # -1: no lens yet; 0..N: frame index
  lensTimerMs = 0
  phase = 'move'      # move -> commit gate -> move/pulse/lens -> fade -> done
  phaseStart = 0
  fadeStart = 0
  arrivedAt = 0
  startT = NowMs
  lastTick = NowMs
  opacity = 255
}

$first = $points[0]

function Begin-Leg([int]$targetIndex) {
  $target = $points[$targetIndex]
  $state.targetIndex = $targetIndex
  $state.startX = [double]$state.posX
  $state.startY = [double]$state.posY
  $state.progress = 0.0
  $state.progressVelocity = 0.0
  $dx = [double]$target.x - $state.startX
  $dy = [double]$target.y - $state.startY
  $distance = [Math]::Max([Math]::Sqrt($dx * $dx + $dy * $dy), 0.001)
  $ux = $dx / $distance; $uy = $dy / $distance
  $nx = -$uy; $ny = $ux
  $side = if (($targetIndex % 2) -eq 0) { -1.0 } else { 1.0 }
  $arc = [Math]::Min(120.0, $distance * 0.12) * $side
  $state.control1X = $state.startX + ($dx * 0.34) + ($nx * $arc)
  $state.control1Y = $state.startY + ($dy * 0.34) + ($ny * $arc)
  $state.control2X = [double]$target.x - ($dx * 0.24) + ($nx * $arc * 0.55)
  $state.control2Y = [double]$target.y - ($dy * 0.24) + ($ny * $arc * 0.55)
  $state.lastTick = NowMs
  $state.phase = 'move'
}

function Start-PostEffect([long]$now) {
  if ($doPulse) {
    $state.phase = 'pulse'; $state.phaseStart = $now
  } elseif ($doLens -and $lensFrames.Count -gt 0) {
    $state.phase = 'lens'; $state.lensPhase = 0; $state.lensTimerMs = 0; $state.phaseStart = $now
  } elseif ($script:served) {
    $state.phase = 'hold'   # resident: cursor and glow stay for the next action
  } else {
    $state.phase = 'fade'; $state.fadeStart = $now
  }
}

function Signal-Arrived {
  [Console]::Out.WriteLine('{"event":"arrived"}')
  [Console]::Out.Flush()
}

$form.Add_Shown({
  try {
    $h = $form.Handle
    Set-ClickThrough $h
    if ($points.Count -gt 0) {
      $state.posX = [double]$points[0].x
      $state.posY = [double]$points[0].y
    }
    Begin-Leg $state.targetIndex
    # window tracks the tip anchor, so the pointer tip stays at the target point
    $loc = New-Object System.Drawing.Point([int]($state.posX - $TIP_ANCHOR_X), [int]($state.posY - $TIP_ANCHOR_Y))
    $form.Location = $loc
    Draw-Glyph 0.0 -1
    Present-Glyph 255
  } catch {
    try { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'), "[shown] $($_.Exception.ToString())`n") } catch {}
    try { $form.Close() } catch {}
  }
})

$MAX_ANIMATION_MS = 9000
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 16
$timer.Add_Tick({
  try {
    $now = NowMs
    $tickDeltaMs = [Math]::Max(1.0, [Math]::Min(100.0, $now - $state.lastTick))
    $state.lastTick = $now
    $elapsed = $now - $state.startT

    # Drain controller commands (serve mode). Single-shot mode never enqueues.
    $cmdLine = $null
    while ($script:cmdQueue.TryDequeue([ref]$cmdLine)) {
      try {
        if ($cmdLine -eq $script:EOF_SENTINEL) {
          # Controller died: fade out and restore the cursor (failsafe).
          if ($state.phase -ne 'done' -and $state.phase -ne 'fade') {
            $state.phase = 'fade'; $state.fadeStart = $now; $state.phaseStart = $now
          }
          continue
        }
        $c = $cmdLine | ConvertFrom-Json
        switch ([string]$c.op) {
          'commit' { $script:gateOpen = $true }
          'fade' {
            if ($state.phase -ne 'done' -and $state.phase -ne 'fade') {
              $state.phase = 'fade'; $state.fadeStart = $now; $state.phaseStart = $now
            }
          }
          'exit' {
            $state.phase = 'done'
            Stop-GlowStrips
            $timer.Stop()
            try { $form.Close() } catch {}
          }
          'play' {
            if ($state.phase -ne 'done') { Start-SessionPlay $c }
          }
          'probe' {
            # Diagnostics: dump strip window state to %TEMP%\dsh-cu-overlay-diag.txt.
            try {
              $lines = @("phase=$($state.phase) strips=$($script:GlowStrips.Count) tick=$($script:GlowTick) depth=$GLOW_DEPTH dpi=$DPI_SCALE")
              foreach ($strip in $script:GlowStrips) {
                $lines += "strip h=$($strip.form.Handle) visible=$($strip.form.Visible) bounds=$($strip.form.Bounds) topmost=$($strip.form.TopMost)"
              }
              # Also dump the first strip's bitmap for inspection.
              if ($script:GlowStrips.Count -gt 0) {
                $script:GlowStrips[0].bitmap.Save((Join-Path $env:TEMP 'dsh-cu-strip-bitmap.png'), [System.Drawing.Imaging.ImageFormat]::Png)
              }
              [System.IO.File]::WriteAllLines((Join-Path $env:TEMP 'dsh-cu-overlay-diag.txt'), $lines)
            } catch {}
          }
        }
      } catch {}
    }

    if ($elapsed -gt $MAX_ANIMATION_MS -and $state.phase -ne 'done' -and $state.phase -ne 'hold' -and $state.phase -ne 'fade') {
      $state.phase = 'fade'
      $state.fadeStart = $now
      $state.phaseStart = $now
    }

    switch ($state.phase) {
    'move' {
      if ($script:waitCommit) {
        # Resident commit gate: the controller writes {op:commit} after starting
        # the real UI action. Poll the queue instead of blocking ReadLine so the
        # glow keeps breathing during the mutation.
        if ($script:gateOpen -or (($now - $state.arrivedAt) -gt 9000)) {
          $script:waitCommit = $false
          $script:gateOpen = $false
          $now = NowMs
          if ($state.targetIndex -lt $points.Count - 1) { Begin-Leg ($state.targetIndex + 1) }
          else { Start-PostEffect $now }
        } else {
          Present-Glyph ([byte]$state.opacity)
          Update-GlowStrips 1.0
        }
      } else {
        $target = $points[$state.targetIndex]
        # The confirmed simulation step is 1/240s. Advance enough fixed substeps
        # to match wall time even when layered-window presentation costs a frame.
        $substeps = [Math]::Max(1, [Math]::Min(24, [int][Math]::Round($tickDeltaMs / ($DT * 1000.0))))
        for ($step = 0; $step -lt $substeps; $step++) {
          $spring = Step-Spring $state.progress $state.progressVelocity 1.0 $DT
          $state.progress = $spring.pos
          $state.progressVelocity = $spring.vel
        }
        $visibleProgress = [Math]::Max(0.0, [Math]::Min(1.0, $state.progress))
        $state.posX = Sample-Cubic $state.startX $state.control1X $state.control2X ([double]$target.x) $visibleProgress
        $state.posY = Sample-Cubic $state.startY $state.control1Y $state.control2Y ([double]$target.y) $visibleProgress
        $loc = New-Object System.Drawing.Point([int]($state.posX - $TIP_ANCHOR_X), [int]($state.posY - $TIP_ANCHOR_Y))
        $form.Location = $loc
        Draw-Glyph 0.0 -1
        Present-Glyph ([byte]$state.opacity)
        Update-GlowStrips 1.0
        if ($state.progress -ge 1.0) {
          $state.posX = [double]$target.x; $state.posY = [double]$target.y
          if ($state.targetIndex -eq $interactionIndex) {
            Signal-Arrived
            if ($script:served) {
              $script:waitCommit = $true
              $state.arrivedAt = $now
            } else {
              # The controller writes one commit line after starting the real UI action.
              # This short gate keeps the visual press/drag aligned with the mutation.
              [void][Console]::In.ReadLine()
              # The UI action may block this timer callback for hundreds of
              # milliseconds. Start post-effects from the commit completion time,
              # not from the stale pre-action tick timestamp.
              $now = NowMs
              if ($state.targetIndex -lt $points.Count - 1) {
                Begin-Leg ($state.targetIndex + 1)
              } else {
                Start-PostEffect $now
              }
            }
          } elseif ($state.targetIndex -lt $points.Count - 1) {
            Begin-Leg ($state.targetIndex + 1)
          } else {
            Start-PostEffect $now
          }
        }
      }
    }
    'pulse' {
      $t = ($now - $state.phaseStart) / 260.0   # short, clearly legible press/release
      if ($t -lt 1.0) {
        $state.pulse = [Math]::Sin([Math]::PI * $t)
        Draw-Glyph $state.pulse -1
        Present-Glyph ([byte]$state.opacity)
        Update-GlowStrips 1.0
      } else {
        $state.pulse = 0
        if ($doLens -and $lensFrames.Count -gt 0) {
          $state.phase = 'lens'
          $state.lensPhase = 0
          $state.lensTimerMs = 0
          $state.phaseStart = $now
        } elseif ($script:served) {
          $state.phase = 'hold'
        } else { $state.phase = 'fade'; $state.fadeStart = $now }
      }
    }
    'lens' {
      $fp = 33.0   # frame playback ms
      $state.lensTimerMs = $now - $state.phaseStart
      $idx = [Math]::Min([int]($state.lensTimerMs / $fp), $lensFrames.Count - 1)
      Draw-Glyph 0.0 $idx
      Present-Glyph ([byte]$state.opacity)
      if ($idx -ge $lensFrames.Count - 1) {
        if ($script:served) { $state.phase = 'hold' }
        else { $state.phase = 'fade'; $state.fadeStart = $now }
      }
    }
    'hold' {
      # Resident idle: the software cursor stays parked at the last endpoint and
      # the screen-edge glow keeps breathing until the controller ends the
      # session (agent/status idle or the JS idle timeout) or the next action
      # arrives as a {op:play} command.
      Draw-Glyph 0.0 -1
      Present-Glyph ([byte]$state.opacity)
      Update-GlowStrips 1.0
    }
    'fade' {
      $t = ($now - $state.fadeStart) / 250.0
      if ($t -lt 1.0) {
        $state.opacity = [int](255 * (1.0 - $t))
        Present-Glyph ([byte]$state.opacity)
        Update-GlowStrips (1.0 - $t)
      } else {
        $state.phase = 'done'
      }
    }
    'done' {
      Stop-GlowStrips
      $timer.Stop()
      $form.Close()
    }
    }
  } catch {
    try { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'), "[$(NowMs)] $($_.Exception.ToString())`n") } catch {}
    try { $form.Close() } catch {}
  }
})

$timer.Start()
# Show WITHOUT activating (SW_SHOWNOACTIVATE): the overlay must never steal focus.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuFgProbe {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
# remember the foreground owner and restore it after showing the overlay
$savedForeground = [CuFgProbe]::GetForegroundWindow()
$form.CreateControl()
$hand = $form.Handle
Set-ClickThrough $hand
[void][OverlayWin]::ShowWindow($hand, $SW_SHOWNOACTIVATE)
Start-Sleep -Milliseconds 120
if ($savedForeground -ne [IntPtr]::Zero) {
  try { [void][CuFgProbe]::SetForegroundWindow($savedForeground) } catch {}
}
# Remember where the user's hand was (OverlayWin is available from here on):
# SendInput clicks move the (hidden) physical cursor to the automation targets,
# and on exit it must be returned there — otherwise the pointer "restores" at
# the last clicked spot, far from the user's hand.
$script:savedCursorPos = New-Object 'OverlayWin+POINT'
[void][OverlayWin]::GetCursorPos([ref]$script:savedCursorPos)
Start-GlowStrips
try {
  [System.Windows.Forms.Application]::Run($form)
} catch {}
Stop-ReaderThread
foreach ($key in @($LENS_FRAMES_CACHE.Keys)) {
  try { $LENS_FRAMES_CACHE[$key].Dispose() } catch {}
}
$bitmap.Dispose()
# Return the physical pointer to where the user's hand was, then reload the
# cursor scheme so it reappears there (two attempts: the first SetCursorPos
# can race a still-draining synthetic input batch).
try {
  [void][OverlayWin]::SetCursorPos($script:savedCursorPos.X, $script:savedCursorPos.Y)
  Start-Sleep -Milliseconds 70
  [void][OverlayWin]::SetCursorPos($script:savedCursorPos.X, $script:savedCursorPos.Y)
  [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'), "[exit] pointer returned to $($script:savedCursorPos.X),$($script:savedCursorPos.Y)`n")
} catch {
  try { [System.IO.File]::AppendAllText((Join-Path $env:TEMP 'dsh-cu-overlay-err.log'), "[exit] SetCursorPos failed: $($_.Exception.Message)`n") } catch {}
}
Restore-SystemCursor
Out-Json @{ ok = $true; kind = $kind; points = $points.Count }
