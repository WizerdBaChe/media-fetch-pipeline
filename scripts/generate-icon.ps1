# Generate electron/build/icon.png -- the source electron-builder derives the
# .ico from. Committed as a SCRIPT beside the PNG rather than as a mystery
# binary: an icon nobody can regenerate is an asset the project does not
# really own.
#
# PLACEHOLDER, by ruling. It is deliberately plain: a flat --surface field
# with a single dark mark, no text (the .ico renders down to 16 px, where any
# lettering turns to mush). Rebranding is one file: replace
# electron/build/icon.png, or edit the colours below and re-run this.
#
# Palette comes from gui/src/app/theme.css, so the window's first paint, the
# taskbar icon and the UI cannot drift apart.

[CmdletBinding()]
param(
    [int]$Size = 1024
)

. "$PSScriptRoot\_common.ps1"

Add-Type -AssemblyName System.Drawing

$root       = Get-ProjectRoot
$outputDir  = Join-Path $root 'electron\build'
$outputPath = Join-Path $outputDir 'icon.png'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

# --- palette (gui/src/app/theme.css) ---------------------------------------
$surface = [System.Drawing.Color]::FromArgb(255, 0xF7, 0xF5, 0xF2)  # --surface
$ink     = [System.Drawing.Color]::FromArgb(255, 0x2B, 0x26, 0x22)  # darker --ink
$accent  = [System.Drawing.Color]::FromArgb(255, 0xD4, 0x62, 0x1A)  # --accent

$bitmap = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear($surface)

# One scale factor, so every number below reads as a fraction of the canvas
# and the shape survives a different -Size unchanged.
$u = $Size / 1024.0
function U([double]$v) { [float]($v * $u) }

# --- the mark: an arrow descending into a tray -----------------------------
# Chosen because it stays legible at 16 px: two solid shapes, one gap, no
# thin strokes and no interior detail to disappear.

$shaftWidth  = U 150
$shaftTop    = U 210
$shaftBottom = U 520
$centre      = U 512

$shaft = New-Object System.Drawing.RectangleF(
    ($centre - $shaftWidth / 2), $shaftTop, $shaftWidth, ($shaftBottom - $shaftTop))
$inkBrush = New-Object System.Drawing.SolidBrush($ink)
$graphics.FillRectangle($inkBrush, $shaft)

$head = New-Object System.Drawing.Drawing2D.GraphicsPath
$head.AddPolygon(@(
    (New-Object System.Drawing.PointF((U 300), (U 500))),
    (New-Object System.Drawing.PointF((U 724), (U 500))),
    (New-Object System.Drawing.PointF((U 512), (U 730)))
))
$graphics.FillPath($inkBrush, $head)

# The tray. Accent-coloured so the icon has one point of colour at a glance
# in the taskbar without becoming a two-object composition at 16 px.
$trayPen = New-Object System.Drawing.Pen($accent, (U 96))
$trayPen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
$trayPen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
$graphics.DrawLine($trayPen, (U 250), (U 830), (U 774), (U 830))

$trayPen.Dispose()
$head.Dispose()
$inkBrush.Dispose()

$bitmap.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()

Write-Ok "icon written to $outputPath ($Size x $Size)"
