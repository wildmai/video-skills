param(
  [Parameter(Mandatory=$true)][string]$Script,
  [string]$Python = "",
  [string]$OutFile = ""
)

# Run a Python script with NO visible console window on the desktop.
#
# Use this for any command issued while the screen recorder is running:
# a console that flashes onto the captured display becomes part of the video.
# The child is started with CreateNoWindow, and stdout is captured and echoed
# back so the caller still sees the output.

$py = $Python
if (-not $py) {
  $py = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $py
$psi.Arguments = "`"$Script`""
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

$p = [System.Diagnostics.Process]::Start($psi)
$stdout = $p.StandardOutput.ReadToEnd()
$stderr = $p.StandardError.ReadToEnd()
$p.WaitForExit()

if ($OutFile) {
  [System.IO.File]::WriteAllText($OutFile, $stdout + "`n--- STDERR ---`n" + $stderr,
    (New-Object System.Text.UTF8Encoding($false)))
}
if ($stdout.Trim()) { Write-Output $stdout }
if ($stderr.Trim()) { Write-Output "--- STDERR ---"; Write-Output $stderr }
Write-Output "EXIT=$($p.ExitCode)"
if ($p.ExitCode -ne 0) { exit $p.ExitCode }
